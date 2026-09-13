"""Passed to `blender --python` at container startup: enables the vendored
MCP-for-Blender addon (blender_mcp.py, copied into the addons folder by
entrypoint.sh). The addon's own register() then auto-starts its socket
server on 0.0.0.0:9876 (blendermcp_auto_start_server defaults to True, and
all external asset integrations — Poly Haven/Sketchfab/Hyper3D/Hunyuan3D/Poly
Pizza — default to False), so no further setup is needed here.

It also points Cycles at the GPU. Blender ships with `compute_device_type`
set to NONE, i.e. CPU rendering, and nothing in an MCP session changes that
unless the client explicitly asks — so every render an agent drives through
this server ran on CPU, using zero VRAM, despite the container having the
GPU reserved in docker-compose.yml. Setting it here makes the GPU the
default for the life of the container instead of something each client has
to remember. (`docker/render/render.py`, the one-shot batch renderer, does
its own equivalent setup — this is the live-instance counterpart.)
"""

import bpy

bpy.ops.preferences.addon_enable(module="blender_mcp")
print("[blender] blender_mcp addon enabled")


def _enable_gpu() -> None:
    prefs = bpy.context.preferences.addons["cycles"].preferences

    # Same "try it and see" approach as render.py: Blender's device enum
    # doesn't reliably list what it really supports until you assign to it,
    # so assign and catch TypeError rather than trusting a pre-check.
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

    for scene in bpy.data.scenes:
        scene.cycles.device = "GPU"

    print(f"[blender] Cycles on {backend}: {', '.join(d.name for d in devices)}")


@bpy.app.handlers.persistent
def _gpu_on_load(*_args) -> None:
    # The backend choice and which devices are enabled live in preferences,
    # which survive a file load — but `scene.cycles.device` is stored *in the
    # .blend*, so opening a file that was saved on CPU (every .blend written
    # here before this change) silently puts renders back on the CPU.
    for scene in bpy.data.scenes:
        scene.cycles.device = "GPU"


try:
    _enable_gpu()
    bpy.app.handlers.load_post.append(_gpu_on_load)
except Exception as e:  # never let this keep the MCP socket from starting
    print(f"[blender] GPU setup failed ({e}) — Cycles stays on CPU")
