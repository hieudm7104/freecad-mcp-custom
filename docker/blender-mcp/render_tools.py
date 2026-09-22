"""One MCP tool that renders the *live* Blender scene with Cycles.

Replaces the old one-shot `render` container (docker/render/): that one booted
its own `blender -b`, imported a file and rendered it, blind to the materials,
lights and camera the agent had just built in the live instance — which is the
scene a user actually wants a picture of. Importing a mesh is already
reachable through execute_blender_code, so all that survives from that script
is the render itself plus its two pieces of hard-won knowledge: Blender's
file-format enum overrides, and the bounding-box camera framing that fixed
renders coming out solid black.

Lives here rather than in the blender image on purpose: editing anything baked
into blender_cli means a rebuild and restart, which throws away the in-memory
scene — the whole reason the two containers are split.
"""

import json
from pathlib import Path

# Blender's image_settings.file_format enum doesn't always match a file
# extension's own name — plain `.suffix.upper()` gives "JPG" (invalid; must be
# "JPEG"), "TIF" (must be "TIFF"), "EXR" (must be "OPEN_EXR"), each of which
# raises TypeError from Blender rather than silently doing the wrong thing.
# Only extensions that differ from their upper-cased form are listed.
_FILE_FORMAT_OVERRIDES = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".exr": "OPEN_EXR",
    ".tga": "TARGA",
}

# `bpy.ops.object.camera_add`/`light_add` (what the batch script used) drop a
# fresh Camera.001/Sun.001 into the scene on every call and steal
# scene.camera; in a long-running instance that litters the user's scene. Make
# the objects directly, name them, and reuse them. Framing prefers the
# selection when there is one: a studio scene's backdrop plane is orders of
# magnitude bigger than the subject, and framing everything leaves the model a
# speck (the same trap the preview's Reset view hit).
#
# This used to be gated on `scene.camera is None`, which meant it never ran:
# blender_cli boots Blender's factory startup file, which always has a Camera
# framed on the default 2 m cube, so every render was aimed wherever that
# camera happened to point. Nothing in the scene distinguishes that untouched
# factory camera from one an agent placed on purpose — both are just a camera
# object — so the caller says which it is: `frame=False` keeps the scene's own
# camera and lighting, and the default frames on the subject.
_FRAME = """
meshes = [o for o in scene.objects if o.type == 'MESH' and o.visible_get()]
framed = [o for o in meshes if o.select_get()] or meshes
if not framed:
    raise RuntimeError('scene has nothing to render')
corners = [o.matrix_world @ mathutils.Vector(c) for o in framed for c in o.bound_box]
lo = mathutils.Vector(min(c[i] for c in corners) for i in range(3))
hi = mathutils.Vector(max(c[i] for c in corners) for i in range(3))
centre = (lo + hi) / 2
distance = max((hi - lo).length / 2, 1e-3) * 3.5
loc = centre + mathutils.Vector((1.0, -1.0, 0.6)).normalized() * distance
cam = bpy.data.objects.get('MCPRenderCam')
if cam is None:
    cam = bpy.data.objects.new('MCPRenderCam', bpy.data.cameras.new('MCPRenderCam'))
    scene.collection.objects.link(cam)
cam.location = loc
cam.rotation_euler = (centre - loc).to_track_quat('-Z', 'Y').to_euler()
scene.camera = cam
sun = bpy.data.objects.get('MCPRenderSun')
if sun is None:
    sun = bpy.data.objects.new('MCPRenderSun', bpy.data.lights.new('MCPRenderSun', type='SUN'))
    scene.collection.objects.link(sun)
sun.location = centre + mathutils.Vector((0.0, 0.0, distance))
sun.data.energy = 3.0
sun.rotation_euler = mathutils.Vector((0.3, -0.3, -1.0)).to_track_quat('-Z', 'Y').to_euler()
"""


def _render_code(
    out: str, fmt: str, samples: int, resolution_x: int, resolution_y: int, frame: bool
) -> str:
    """The script sent to Blender. Kept separate so it can be checked below."""
    return (
        "import bpy, json, mathutils\n"
        "scene = bpy.context.scene\n"
        + (_FRAME if frame else "")
        + "scene.render.engine = 'CYCLES'\n"
        f"scene.cycles.samples = {int(samples)}\n"
        f"scene.render.resolution_x = {int(resolution_x)}\n"
        f"scene.render.resolution_y = {int(resolution_y)}\n"
        f"scene.render.filepath = {out!r}\n"
        f"scene.render.image_settings.file_format = {fmt!r}\n"
        "bpy.ops.render.render(write_still=True)\n"
        "prefs = bpy.context.preferences.addons['cycles'].preferences\n"
        f"print(json.dumps({{'path': {out!r}, 'device': scene.cycles.device,\n"
        "                   'backend': prefs.compute_device_type,\n"
        "                   'camera': scene.camera.name if scene.camera else None,\n"
        f"                   'samples': {int(samples)},\n"
        f"                   'resolution': [{int(resolution_x)}, {int(resolution_y)}]}}))\n"
    )


def _file_format(output_path: str) -> str:
    suffix = Path(output_path).suffix.lower()
    return _FILE_FORMAT_OVERRIDES.get(suffix, suffix.lstrip(".").upper() or "PNG")


def register_render_tools(mcp) -> None:
    @mcp.tool()
    def render_image(
        output: str = "render.png",
        samples: int = 128,
        resolution_x: int = 1920,
        resolution_y: int = 1080,
        frame: bool = True,
    ) -> str:
        """Render the current live Blender scene with Cycles to a file in /data.

        Use this rather than hand-writing bpy.ops.render.render() through
        execute_blender_code: the GPU is enabled from a render_pre handler, so
        this path gets OptiX/CUDA (~3 s) and a hand-rolled one gets whatever
        the scene happens to carry.

        Parameters:
        - output: Image path under /data. The extension picks the format
          (.png/.jpg/.tif/.exr/.webp/...).
        - samples: Cycles samples (default 128).
        - resolution_x / resolution_y: Output size in pixels.
        - frame: Point a camera (MCPRenderCam) and a sun at the scene's
          bounding box, or at the selection if anything is selected. On by
          default, because the scene Blender starts with already has a camera
          aimed at nothing in particular. Pass False to render through the
          camera and lighting you placed yourself.

        To render a CAD part, export it from FreeCAD to /data and import it
        with execute_blender_code first. Upload the result with
        upload_file_to_storage to keep it.

        The return value reports that a file was written and how big it is.
        That is not a promise the picture shows what you wanted — look at it
        (get_viewport_screenshot renders the viewport, not this file).
        """
        # ponytail: the render runs inline on the addon's single command
        # queue, so it blocks preview polling and every other tool call while
        # it runs, and both socket ends time out at 180 s. Fine at the
        # measured 2-4 s for 128 samples; if renders get heavy, move this to a
        # `blender -b` subprocess driven from the sent code.
        import storage
        from blender_mcp.server import get_blender_connection

        try:
            out = str(storage.data_path(output))
            written = Path(out)
            # A file left by an earlier render makes the is_file() check below
            # pass for a render that produced nothing, so require the mtime to
            # move as well.
            before = written.stat().st_mtime_ns if written.is_file() else None
            code = _render_code(
                out, _file_format(out), samples, resolution_x, resolution_y, frame
            )
            result = get_blender_connection().send_command("execute_code", {"code": code})
            # Cycles' own progress goes to the process's real stdout, not the
            # addon's Python-level capture, but take the last line anyway so a
            # stray print can't break the JSON.
            lines = (result or {}).get("result", "").strip().splitlines()
            # bpy.ops.render.render() returns {'CANCELLED'} without raising
            # (out of VRAM, a broken node tree, a render aborted by another
            # queued command) and the print below it still runs, so that JSON
            # is not evidence of anything on its own. /data is the shared
            # volume, so stat the file from this side.
            if not written.is_file():
                return f"Render reported success but wrote no file at {out}"
            if before is not None and written.stat().st_mtime_ns == before:
                return (
                    f"Render reported success but did not rewrite {out} — that "
                    "file is the one an earlier render left there."
                )
            try:
                detail = dict(json.loads(lines[-1]) if lines else {})
            except Exception:
                detail = {}
            detail.update(
                path=out,
                bytes=written.stat().st_size,
                verified="file written; pixels not checked",
            )
            return json.dumps(detail)
        except Exception as e:
            return f"Render failed: {e}"


if __name__ == "__main__":
    # The sent script is assembled from f-strings and only ever runs inside
    # Blender, where a syntax error surfaces as an opaque socket reply. Parse
    # it here instead.
    import ast

    for name in ("/data/a.png", "/data/o'dd \"q\".exr"):
        for frame in (True, False):
            ast.parse(_render_code(name, _file_format(name), 16, 64, 64, frame))
    # The framing used to be gated on a condition that was never true; keep it
    # gated on nothing but the argument.
    assert "MCPRenderCam" in _render_code("/data/a.png", "PNG", 16, 64, 64, True)
    assert "MCPRenderCam" not in _render_code("/data/a.png", "PNG", 16, 64, 64, False)
    assert _file_format("/data/x.jpg") == "JPEG"
    assert _file_format("/data/x.tif") == "TIFF"
    assert _file_format("/data/x.exr") == "OPEN_EXR"
    assert _file_format("/data/x.webp") == "WEBP"
    assert _file_format("/data/x") == "PNG"
    print("render_tools self-check ok")
