"""MCP tools that persist Blender work to MinIO object storage.

The Blender counterpart of `src/freecad_mcp/storage_tools.py`, registered on
upstream blender-mcp's own `FastMCP` instance from `run_http.py` (upstream
has no storage tools of its own). `storage.py` next to this file is the same
module the FreeCAD server uses — COPY'd in by the Dockerfile so key layout,
bucket and path rules can't drift between the two servers.

Same two-container shape as the FreeCAD side: Blender runs in the
`blender_cli` container, this runs in `mcp_blender`, and `/data` is mounted in
both — so saving means *asking Blender to write the .blend into /data*, then
uploading that file from here.
"""

import json
from pathlib import Path

import storage
from blender_mcp.server import get_blender_connection

_BLENDER_PREFIX = "blender/"


def _run_in_blender(code: str) -> str:
    """Run *code* in Blender over the addon socket, returning its stdout."""
    result = get_blender_connection().send_command("execute_code", {"code": code})
    return (result or {}).get("result", "").strip()


def _current_blend_name() -> str:
    """Filename of the .blend currently open, or a default for an unsaved scene."""
    try:
        current = _run_in_blender("import bpy\nprint(bpy.data.filepath)")
    except Exception:
        current = ""
    return Path(current).name if current else "scene.blend"


def register_storage_tools(mcp) -> None:
    @mcp.tool()
    def save_blend_to_storage(name: str = "", object_key: str = "") -> str:
        """Save the current Blender scene to object storage so it survives restarts.

        A Blender scene only exists in the running Blender process's memory
        until it is saved — restarting or rebuilding the container loses it.
        This writes a .blend into the shared /data volume and uploads it to
        the storage bucket.

        Parameters:
        - name: Base name for the file, e.g. "chair" -> chair.blend. Defaults
          to the currently open .blend's name, or scene.blend if unsaved.
        - object_key: Storage key to write. Defaults to "blender/<filename>".
        """
        try:
            if object_key:
                filename = Path(storage.normalise_key(object_key)).name
            elif name:
                filename = name if name.endswith(".blend") else f"{name}.blend"
            else:
                filename = _current_blend_name()

            local_path = storage.data_path(filename)
            key = storage.normalise_key(object_key or f"{_BLENDER_PREFIX}{filename}")

            _run_in_blender(
                "import bpy\n"
                f"bpy.ops.wm.save_as_mainfile(filepath={str(local_path)!r})\n"
                "print(bpy.data.filepath)\n"
            )
            return json.dumps(storage.upload(local_path, key), default=str)
        except Exception as e:
            return f"Failed to save scene to storage: {e}"

    @mcp.tool()
    def load_blend_from_storage(object_key: str) -> str:
        """Download a saved .blend from object storage and open it in Blender.

        Replaces whatever is currently open — save the current scene first if
        it matters.

        Parameters:
        - object_key: Storage key, e.g. "blender/chair.blend" (see
          list_storage_files).
        """
        try:
            key = storage.normalise_key(object_key)
            local_path = storage.data_path(Path(key).name)
            storage.download(key, local_path)
            _run_in_blender(
                "import bpy\n"
                f"bpy.ops.wm.open_mainfile(filepath={str(local_path)!r})\n"
                "print(bpy.data.filepath)\n"
            )
            return json.dumps({"key": key, "path": str(local_path), "opened": True})
        except Exception as e:
            return f"Failed to load '{object_key}' from storage: {e}"

    @mcp.tool()
    def list_storage_files(prefix: str = "") -> str:
        """List files kept in object storage (saved scenes, meshes, renders).

        Shares one bucket with the FreeCAD MCP server, so FreeCAD exports
        ("freecad/...") show up here too and can be downloaded and imported.

        Parameters:
        - prefix: Optional key prefix to filter by, e.g. "blender/".
        """
        try:
            return json.dumps(
                {"bucket": storage.bucket_name(), "objects": storage.list_objects(prefix)},
                default=str,
            )
        except Exception as e:
            return f"Failed to list storage: {e}"

    @mcp.tool()
    def upload_file_to_storage(data_path: str, object_key: str = "") -> str:
        """Upload a file that already exists in /data (an export, a render, ...).

        Parameters:
        - data_path: File under /data, e.g. "chair.glb" or "/data/render.png".
        - object_key: Storage key to write. Defaults to "blender/<filename>".
        """
        try:
            local_path = storage.data_path(data_path)
            key = storage.normalise_key(object_key or f"{_BLENDER_PREFIX}{local_path.name}")
            return json.dumps(storage.upload(local_path, key), default=str)
        except Exception as e:
            return f"Failed to upload '{data_path}': {e}"

    @mcp.tool()
    def download_file_from_storage(object_key: str, data_path: str = "") -> str:
        """Download a stored file into /data, where Blender can import it.

        Parameters:
        - object_key: Storage key to fetch (see list_storage_files).
        - data_path: Destination under /data. Defaults to the key's filename.
        """
        try:
            key = storage.normalise_key(object_key)
            local_path = storage.data_path(data_path or Path(key).name)
            return json.dumps(storage.download(key, local_path), default=str)
        except Exception as e:
            return f"Failed to download '{object_key}': {e}"
