"""Active-view orientation, sizing, and screenshot capture."""

import re
from typing import Any

import FreeCAD
import FreeCADGui

from rpc_server.gui_dispatch import _flush_gui_events


_VIEW_DISPATCH = {
    "Isometric": "viewIsometric",
    "Front": "viewFront",
    "Top": "viewTop",
    "Right": "viewRight",
    "Back": "viewBack",
    "Left": "viewLeft",
    "Bottom": "viewBottom",
    "Dimetric": "viewDimetric",
    "Trimetric": "viewTrimetric",
}


def _get_view_size(view: Any) -> tuple[int, int]:
    try:
        size = view.getSize()
        if isinstance(size, (list, tuple)) and len(size) >= 2:
            return max(1, int(size[0])), max(1, int(size[1]))
        return max(1, int(size.width())), max(1, int(size.height()))
    except Exception:
        return 1024, 768


# Longest edge used when the caller does not ask for a specific size. The
# screenshot's cost to an LLM client scales with its pixel count, and hosts
# commonly downscale anything larger than ~1.5k px before the model ever sees
# it, so rendering at the full window size just inflates the payload. An
# explicit width/height is always honoured as given.
MAX_AUTO_SCREENSHOT_EDGE = 1024


def _scale_to_max_edge(width: int, height: int, max_edge: int) -> tuple[int, int]:
    longest = max(width, height)
    if longest <= max_edge:
        return width, height
    scale = max_edge / longest
    return max(1, int(width * scale)), max(1, int(height * scale))


def _resolve_screenshot_size(
    view: Any,
    width: int | None,
    height: int | None,
) -> tuple[int, int]:
    view_width, view_height = _get_view_size(view)
    if width is None and height is None:
        return _scale_to_max_edge(view_width, view_height, MAX_AUTO_SCREENSHOT_EDGE)
    resolved_width = view_width if width is None else max(1, int(width))
    resolved_height = view_height if height is None else max(1, int(height))
    return resolved_width, resolved_height


_STD_COMMAND_DISPATCH = {
    "Isometric": "Std_ViewIsometric",
    "Front": "Std_ViewFront",
    "Top": "Std_ViewTop",
    "Right": "Std_ViewRight",
    "Back": "Std_ViewRear",
    "Left": "Std_ViewLeft",
    "Bottom": "Std_ViewBottom",
    "Dimetric": "Std_ViewDimetric",
    "Trimetric": "Std_ViewTrimetric",
}


def apply_view_orientation(view: Any, view_name: str) -> None:
    method_name = _VIEW_DISPATCH.get(view_name)
    if method_name is None:
        raise ValueError(f"Invalid view name: {view_name}")
    if hasattr(view, method_name):
        getattr(view, method_name)()
    else:
        # Fallback for views that lack the direct Python method
        # (e.g. some FreeCAD versions / view types)
        cmd = _STD_COMMAND_DISPATCH.get(view_name)
        if cmd:
            FreeCADGui.runCommand(cmd)
        else:
            FreeCAD.Console.PrintWarning(
                f"apply_view_orientation: no method or command for '{view_name}'\n"
            )


def save_active_screenshot(
    save_path: str,
    view_name: str | None = "Isometric",
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
):
    """Save a PNG of the active view to ``save_path``.

    ``view_name`` may be ``None``/``""`` to capture whatever the camera's
    current orientation/zoom already is, without forcing a canned view or
    re-fitting — used by the live-preview page (``preview.py``) so a caller
    driving the camera directly via ``orbit_camera``/``zoom_camera`` doesn't
    get overridden on the next poll.

    Returns ``True`` on success, or an error string on failure (preserves the
    legacy GUI-handler return contract).
    """
    try:
        view = FreeCADGui.ActiveDocument.ActiveView
        if not hasattr(view, "saveImage"):
            return "Current view does not support screenshots"

        if view_name:
            apply_view_orientation(view, view_name)

        focused_selection = False
        # The resolved object we frame on (when focus_object is given), kept so
        # the framing can be re-applied synchronously right before saveImage().
        focus_target = None

        if focus_object:
            doc = FreeCAD.ActiveDocument
            obj = doc.getObject(focus_object) if doc else None
            if obj:
                FreeCADGui.Selection.clearSelection()
                FreeCADGui.Selection.addSelection(obj)
                FreeCADGui.SendMsgToActiveView("ViewSelection")
                focused_selection = True
                focus_target = obj
                _flush_gui_events()
                FreeCADGui.Selection.clearSelection()
            else:
                view.fitAll()
        elif view_name:
            # Only refit when a canned orientation was just forced above —
            # otherwise this would silently undo a caller's custom
            # orbit/zoom on every poll.
            view.fitAll()

        _flush_gui_events()
        # On macOS, when the FreeCAD window is not exposed (fully occluded or
        # minimized), saveImage() right after pumping the event loop grabs a blank
        # frame. Re-issuing the framing synchronously forces a redraw first. The
        # flush above is kept intentionally — Linux needs it for the stale-frame
        # fix (#51/#53).
        if focused_selection and focus_target is not None:
            FreeCADGui.Selection.addSelection(focus_target)
            FreeCADGui.SendMsgToActiveView("ViewSelection")
            FreeCADGui.Selection.clearSelection()
        elif view_name:
            view.fitAll()
        else:
            # Same redraw-forcing need as the fitAll() above, but without
            # touching framing/zoom: re-apply the camera's own current
            # orientation as a no-op that still nudges Coin3D to redraw.
            view.setCameraOrientation(view.getCameraOrientation())
        resolved_width, resolved_height = _resolve_screenshot_size(view, width, height)
        # On Wayland the offscreen GL contexts used by the default saveImage()
        # method render solid black; "Framebuffer" reads back the on-screen GL
        # context and captures correctly (and also works on X11/Windows/macOS).
        # FreeCAD < 1.0 lacks the method argument — fall back to the legacy call.
        try:
            view.saveImage(save_path, resolved_width, resolved_height, "Current", "Framebuffer")
        except TypeError:
            view.saveImage(save_path, resolved_width, resolved_height, "Current")

        if focused_selection:
            FreeCADGui.Selection.clearSelection()
            _flush_gui_events(delay_ms=0)
        return True
    except Exception as e:
        return str(e)


def _camera_size_field(cam_str: str) -> tuple[str, float] | None:
    """Return (field_name, value) for the camera's zoom-controlling field.

    Orthographic cameras (FreeCAD's default) use ``height``; perspective
    cameras use ``heightAngle``. Returns None if neither is found.
    """
    m = re.search(r"\b(height|heightAngle)\s+([-\d.eE]+)", cam_str)
    if not m:
        return None
    return m.group(1), float(m.group(2))


def _set_camera_size(view: Any, field: str, value: float) -> None:
    new_cam = re.sub(
        rf"\b{field}(\s+)[-\d.eE]+",
        lambda m: f"{field}{m.group(1)}{value}",
        view.getCamera(),
        count=1,
    )
    view.setCamera(new_cam)


def orbit_camera(delta_azimuth_deg: float, delta_elevation_deg: float) -> None:
    """Incrementally rotate the active view's camera (mouse-drag orbit).

    ``delta_azimuth_deg`` rotates around the world's vertical (Z) axis;
    ``delta_elevation_deg`` tilts around the camera's current right axis —
    a "turntable"-style orbit, not a full trackball. Re-fits the view
    afterward so the model stays framed as the camera moves around it, but
    restores the caller's previous zoom level first (``fitAll`` resets zoom
    to whatever frames the whole scene), so repeated small calls (as driven
    by mouse-drag input on the live preview page) orbit around the model
    instead of it drifting/rescaling on every step.
    """
    view = FreeCADGui.ActiveDocument.ActiveView
    size = _camera_size_field(view.getCamera())

    rot = view.getCameraOrientation()
    right = rot.multVec(FreeCAD.Vector(1, 0, 0))
    yaw = FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), delta_azimuth_deg)
    pitch = FreeCAD.Rotation(right, delta_elevation_deg)
    view.setCameraOrientation(yaw.multiply(pitch.multiply(rot)))
    view.fitAll()

    if size is not None:
        _set_camera_size(view, *size)


def zoom_camera(factor: float) -> None:
    """Scale the active view's zoom level by *factor* (<1 zooms in, >1 out)."""
    view = FreeCADGui.ActiveDocument.ActiveView
    size = _camera_size_field(view.getCamera())
    if size is None:
        raise ValueError("Active camera has no height/heightAngle field to zoom")
    field, value = size
    _set_camera_size(view, field, value * factor)
