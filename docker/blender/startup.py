"""Passed to `blender --python` at container startup: enables the vendored
MCP-for-Blender addon (blender_mcp.py, copied into the addons folder by
entrypoint.sh). The addon's own register() then auto-starts its socket
server on 0.0.0.0:9876 (blendermcp_auto_start_server defaults to True, and
all external asset integrations — Poly Haven/Sketchfab/Hyper3D/Hunyuan3D/Poly
Pizza — default to False), so no further setup is needed here.

It also points Cycles at the GPU, but *lazily* — from a `render_pre` handler
instead of at startup. Blender ships with `compute_device_type` set to NONE,
i.e. CPU rendering, and nothing in an MCP session changes that unless the
client explicitly asks, so every render an agent drove through this server
ran on CPU (~7x slower, zero VRAM, no error) despite the container having the
GPU reserved. Hanging it off render_pre means one copy of the logic covers
every render — the `render_image` tool (docker/blender-mcp/render_tools.py),
a hand-written bpy.ops.render.render() through execute_blender_code, or F12 —
and the CUDA/OptiX driver is only opened once something actually renders.

Measured, so nobody re-litigates it: the enable itself costs 0 MiB of VRAM
whenever it runs (assigning `compute_device_type` is free; `get_devices()` is
what dlopens libnvoptix and opens /dev/nvidia-uvm, and even that reserves
nothing). Cycles allocates ~2.5 GB for the duration of a render and frees it
by itself ~0.5 s after the render ends. The ~180 MiB this process holds while
idle is the *viewport's GL context*, which the live preview needs. So this is
about not opening the driver at boot — there was never idle VRAM to reclaim.

The one Cycles path that does NOT fire render_pre is viewport RENDERED
shading; docker/blender-mcp/preview_api.py's shading route calls this handler
itself so the preview panel's Rendered view doesn't quietly drop to CPU.
"""

import bpy

bpy.ops.preferences.addon_enable(module="blender_mcp")
print("[blender] blender_mcp addon enabled")


@bpy.app.handlers.persistent
def _gpu_on_render(*_args) -> None:
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences

        # "Try it and see": Blender's device enum doesn't reliably list what
        # it really supports until you assign to it, so assign and catch
        # TypeError rather than trusting a pre-check.
        backend = None
        for candidate in ("OPTIX", "CUDA", "HIP", "ONEAPI"):
            try:
                prefs.compute_device_type = candidate
            except TypeError:
                continue
            backend = candidate
            break

        if backend is None:
            print("[blender] no GPU backend available — Cycles stays on CPU")
            return

        prefs.get_devices()
        devices = [d for d in prefs.devices if d.type == backend]
        for device in prefs.devices:
            device.use = device.type == backend

        if not devices:
            print(f"[blender] {backend} selected but no devices found — Cycles stays on CPU")
            return

        # Every call, not just the first: the backend and which devices are
        # enabled live in preferences, but `scene.cycles.device` is stored *in
        # the .blend*, so a file loaded from storage that was saved on CPU
        # carries CPU back in with it. Re-running get_devices() costs ~0.03 s.
        for scene in bpy.data.scenes:
            scene.cycles.device = "GPU"

        print(f"[blender] Cycles on {backend}: {', '.join(d.name for d in devices)}")
    except Exception as e:  # never let GPU setup abort the render itself
        print(f"[blender] GPU setup failed ({e}) — Cycles stays on CPU")


bpy.app.handlers.render_pre.append(_gpu_on_render)
# Also on load, because viewport RENDERED shading never fires render_pre and a
# .blend restored from storage carries its own shading *and* its own
# `scene.cycles.device`: open one that was saved on CPU in RENDERED view and
# the preview panel quietly renders on the CPU with nothing to trigger a fix.
# Costs ~0.03 s and, measured, 0 MiB — `get_devices()` opens the driver but
# reserves no VRAM.
bpy.app.handlers.load_post.append(_gpu_on_render)
