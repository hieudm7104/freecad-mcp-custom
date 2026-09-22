"""Viewport endpoints backing the Blender tab of the live preview page.

There's no HTML here: the page itself is served by the FreeCAD MCP server
(`src/freecad_mcp/preview.py`), which proxies these routes so the browser
only ever talks to one origin and this server's API key never reaches it.
These are the Blender equivalents of that module's own `/preview*` routes,
named to match 1:1 so the proxy is a straight pass-through.

Gated by the same `?key=` query parameter as the FreeCAD side (an `<img>`
tag can't set an auth header), so `http_auth.py` exempts `/preview*` from
its header check and each handler verifies the key itself.
"""

import base64
import json
import os
import uuid
from pathlib import Path

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# 1x1 transparent PNG, shown while there's nothing real to display yet.
_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

SHADING_MODES = ("WIREFRAME", "SOLID", "MATERIAL", "RENDERED")

# Finds the 3D viewport in every snippet below. Blender in this container
# runs a normal GUI (under Xvfb), so there is exactly one.
_FIND_VIEW = (
    "import bpy\n"
    "area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)\n"
    "if area is None:\n"
    "    raise RuntimeError('no 3D viewport')\n"
    "space = area.spaces.active\n"
    "r3d = space.region_3d\n"
)

# A scene built for rendering usually has the viewport locked to the scene
# camera, and in that mode `view_rotation`/`view_distance` are simply not
# what's on screen — orbiting appeared to do nothing at all (the controls
# reported success and the screenshot came back byte-identical). Blender's
# own viewport leaves camera view as soon as you orbit; do the same, seeding
# the free view from the camera's transform so the image doesn't jump.
#
# The orbit pivot has to be derived from the geometry, not from the existing
# view_distance: in camera view that field keeps whatever the last free view
# left there (Blender's 14.71-unit startup default), which in a 7 cm scene
# put the pivot ~15 m away and left the viewport staring at empty space.
_LEAVE_CAMERA = (
    "if r3d.view_perspective == 'CAMERA':\n"
    "    from mathutils import Vector\n"
    "    cam = bpy.context.scene.camera\n"
    "    r3d.view_perspective = 'PERSP'\n"
    "    if cam is not None:\n"
    "        corners = [o.matrix_world @ Vector(c)\n"
    "                   for o in bpy.context.scene.objects if o.type == 'MESH'\n"
    "                   for c in o.bound_box]\n"
    "        if corners:\n"
    "            lo = Vector((min(c[i] for c in corners) for i in range(3)))\n"
    "            hi = Vector((max(c[i] for c in corners) for i in range(3)))\n"
    "            centre = (lo + hi) / 2\n"
    "        else:\n"
    "            centre = Vector((0.0, 0.0, 0.0))\n"
    "        r3d.view_rotation = cam.matrix_world.to_quaternion()\n"
    "        r3d.view_location = centre\n"
    "        r3d.view_distance = max((cam.matrix_world.translation - centre).length, 1e-3)\n"
)


def preview_enabled() -> bool:
    """Whether to serve the viewport routes at all — off unless asked for.

    Mirrors ``preview_enabled()`` in src/freecad_mcp/preview.py; see there for
    why this is opt-in. Note both halves have to be enabled for the Blender
    tab to work: this server serves the viewport routes, the FreeCAD one
    serves the page and proxies to them.
    """
    return os.environ.get("BLENDER_MCP_PREVIEW", "").strip().lower() in {"1", "true", "yes", "on"}


def _run_in_blender(code: str) -> str:
    from blender_mcp.server import get_blender_connection

    result = get_blender_connection().send_command("execute_code", {"code": code})
    return (result or {}).get("result", "").strip()


def _screenshot(max_size: int) -> bytes:
    from blender_mcp.server import get_blender_connection

    # Written by the addon in the `blender_cli` container and read back here —
    # the two only agree on this path because both mount the same volume at
    # the same mount point (see docker-compose.yml's blender_tmp).
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"preview_{uuid.uuid4().hex}.png"
    try:
        result = get_blender_connection().send_command(
            "get_viewport_screenshot",
            {"max_size": max_size, "filepath": str(tmp), "format": "png"},
        )
        if not isinstance(result, dict) or result.get("error") or not tmp.is_file():
            return _BLANK_PNG
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def _status() -> dict:
    raw = _run_in_blender(
        "import bpy, json\n"
        "area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)\n"
        "print(json.dumps({\n"
        "    'scene': bpy.context.scene.name,\n"
        "    'objects': len(bpy.data.objects),\n"
        "    'blend': bpy.path.basename(bpy.data.filepath) or '(unsaved)',\n"
        "    'shading': area.spaces.active.shading.type if area else None,\n"
        "    'view': area.spaces.active.region_3d.view_perspective if area else None,\n"
        "}))\n"
    )
    return json.loads(raw)


def _orbit(delta_azimuth: float, delta_elevation: float) -> None:
    _run_in_blender(
        _FIND_VIEW
        + _LEAVE_CAMERA
        + "import math\n"
        "from mathutils import Quaternion, Vector\n"
        "rot = r3d.view_rotation\n"
        # Turntable, matching the FreeCAD preview's feel: yaw always around
        # the world's vertical axis, pitch around the view's current right
        # axis, so dragging sideways never rolls the horizon.
        "right = rot @ Vector((1.0, 0.0, 0.0))\n"
        f"yaw = Quaternion(Vector((0.0, 0.0, 1.0)), math.radians({delta_azimuth}))\n"
        f"pitch = Quaternion(right, math.radians({delta_elevation}))\n"
        "r3d.view_rotation = yaw @ pitch @ rot\n"
        "r3d.update()\n"
        "print('ok')\n"
    )


def _zoom(factor: float) -> None:
    _run_in_blender(
        _FIND_VIEW
        + _LEAVE_CAMERA
        + f"r3d.view_distance = max(0.01, r3d.view_distance * {factor})\n"
        "r3d.update()\n"
        "print(r3d.view_distance)\n"
    )


def _camera_view() -> None:
    """Snap back to what the scene camera sees — i.e. what a render will look like."""
    _run_in_blender(
        _FIND_VIEW
        + "if bpy.context.scene.camera is None:\n"
        "    raise RuntimeError('scene has no camera')\n"
        "r3d.view_perspective = 'CAMERA'\n"
        "r3d.update()\n"
        "print('ok')\n"
    )


def _reset() -> None:
    _run_in_blender(
        _FIND_VIEW
        + "from mathutils import Vector\n"
        "region = next((r for r in area.regions if r.type == 'WINDOW'), None)\n"
        "r3d.view_perspective = 'PERSP'\n"
        "r3d.view_rotation = Vector((1.0, -1.0, 0.7)).normalized().to_track_quat('Z', 'Y')\n"
        "r3d.update()\n"
        # Frames the scene; both ops keep the rotation just set and only
        # move/zoom. Needs an area+region override because nothing is "the
        # active area" outside a real UI event.
        #
        # Selected-first, like Blender's own numpad-. : a studio scene has a
        # backdrop plane orders of magnitude bigger than the subject (2 m vs
        # a 7 cm gear here), and plain view_all frames the backdrop and
        # leaves the actual model a speck in the middle.
        "if region is not None and len(bpy.data.objects):\n"
        "    with bpy.context.temp_override(area=area, region=region, space_data=space):\n"
        "        if bpy.context.selected_objects:\n"
        "            bpy.ops.view3d.view_selected()\n"
        "        else:\n"
        "            bpy.ops.view3d.view_all()\n"
        "print('ok')\n"
    )


def _set_shading(mode: str) -> None:
    # Viewport RENDERED shading runs Cycles but is the one Cycles path that
    # does NOT fire `render_pre`, which is where docker/blender/startup.py
    # enables the GPU — so without this the Rendered view silently drops to
    # CPU and looks like a hang. Call the registered handler directly; a
    # GPU-less host simply has none registered and behaves as before.
    gpu = (
        "for _h in bpy.app.handlers.render_pre:\n"
        "    _h(bpy.context.scene)\n"
    ) if mode == "RENDERED" else ""
    _run_in_blender(
        _FIND_VIEW + gpu + f"space.shading.type = {mode!r}\nprint(space.shading.type)\n"
    )


def register_preview_routes(app: Starlette, api_key: str) -> None:
    def _authorised(request: Request) -> bool:
        import hmac

        provided = request.query_params.get("key", "")
        return bool(provided) and hmac.compare_digest(provided, api_key)

    async def preview_png(request: Request):
        if not _authorised(request):
            return Response(status_code=401)
        try:
            max_size = int(request.query_params.get("w") or 1000)
        except ValueError:
            max_size = 1000
        # Off the event loop: the round trip renders a frame in Blender and
        # writes it to disk, and this page polls it every couple of seconds.
        png = await anyio.to_thread.run_sync(_screenshot, max_size)
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def preview_status(request: Request):
        if not _authorised(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            status = await anyio.to_thread.run_sync(_status)
        except Exception as e:
            return JSONResponse({"connected": False, "error": str(e)})
        return JSONResponse({"connected": True, **status})

    async def preview_orbit(request: Request):
        if not _authorised(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            dx = float(request.query_params.get("dx", "0"))
            dy = float(request.query_params.get("dy", "0"))
        except ValueError:
            return JSONResponse({"success": False, "error": "dx/dy must be numbers"}, status_code=400)
        try:
            await anyio.to_thread.run_sync(_orbit, dx, dy)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})
        return JSONResponse({"success": True})

    async def preview_zoom(request: Request):
        if not _authorised(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            factor = float(request.query_params.get("factor", "1"))
        except ValueError:
            return JSONResponse({"success": False, "error": "factor must be a number"}, status_code=400)
        try:
            await anyio.to_thread.run_sync(_zoom, factor)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})
        return JSONResponse({"success": True})

    async def preview_reset(request: Request):
        if not _authorised(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            await anyio.to_thread.run_sync(_reset)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})
        return JSONResponse({"success": True})

    async def preview_camera(request: Request):
        if not _authorised(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            await anyio.to_thread.run_sync(_camera_view)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})
        return JSONResponse({"success": True})

    async def preview_shading(request: Request):
        if not _authorised(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        mode = (request.query_params.get("mode") or "").upper()
        if mode not in SHADING_MODES:
            return JSONResponse(
                {"success": False, "error": f"mode must be one of {', '.join(SHADING_MODES)}"},
                status_code=400,
            )
        try:
            await anyio.to_thread.run_sync(_set_shading, mode)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})
        return JSONResponse({"success": True, "shading": mode})

    app.add_route("/preview.png", preview_png, methods=["GET"])
    app.add_route("/preview/status", preview_status, methods=["GET"])
    app.add_route("/preview/orbit", preview_orbit, methods=["POST"])
    app.add_route("/preview/zoom", preview_zoom, methods=["POST"])
    app.add_route("/preview/reset", preview_reset, methods=["POST"])
    app.add_route("/preview/camera", preview_camera, methods=["POST"])
    app.add_route("/preview/shading", preview_shading, methods=["POST"])
