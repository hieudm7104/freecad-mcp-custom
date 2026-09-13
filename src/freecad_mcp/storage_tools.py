"""MCP tools that persist FreeCAD work to MinIO object storage.

Registered only when object storage is configured (see ``storage.py``) and
only for the streamable-http/Docker deployment, so the stdio path and any
non-MinIO deployment keep exactly the tool set they had before.

Two hops are involved and they are easy to confuse: FreeCAD itself runs in
the `freecad` container, while these tools run in the `mcp` container. The
`/data` volume is mounted in both, so "save" means *ask FreeCAD (over RPC)
to write the file into /data*, then upload that file from /data here; a
load is the same in reverse. Nothing is sent through the RPC socket itself.
"""

from pathlib import Path

from . import storage
from .responses import ToolResponse, json_response, text_response
from .storage import data_path as _data_path, normalise_key as _normalise_key

_FREECAD_PREFIX = "freecad/"


def _run_in_freecad(freecad, code: str) -> str:
    """Run *code* in FreeCAD over RPC, returning its stdout."""
    res = freecad.execute_code(code)
    if not res.get("success"):
        raise RuntimeError(res.get("error") or res.get("message") or "FreeCAD rejected the code")
    message = res.get("message", "")
    return message.split("Output: ", 1)[1].strip() if "Output: " in message else ""


def register_storage_tools(mcp, get_connection) -> None:
    @mcp.tool(structured_output=False)
    def save_document_to_storage(doc_name: str, object_key: str = "") -> ToolResponse:
        """Save an open FreeCAD document to object storage so it survives restarts.

        A FreeCAD document only exists in the running FreeCAD process's memory
        until it is saved — restarting or rebuilding the container loses it.
        This writes it to the shared /data volume and uploads it to the
        storage bucket. The document's own file path is set to the /data copy,
        so later plain saves in FreeCAD update it too.

        Args:
            doc_name: Name of the open document (see list_documents).
            object_key: Storage key to write. Defaults to "freecad/<doc_name>.FCStd".

        Returns:
            The storage key, bucket and size written.
        """
        try:
            key = _normalise_key(object_key or f"{_FREECAD_PREFIX}{doc_name}.FCStd")
            local_path = _data_path(Path(key).name)
            _run_in_freecad(
                get_connection(),
                "import FreeCAD\n"
                f"doc = FreeCAD.getDocument({doc_name!r})\n"
                f"doc.saveAs({str(local_path)!r})\n"
                "print(doc.FileName)\n",
            )
            return json_response(storage.upload(local_path, key))
        except Exception as e:
            return text_response(f"Failed to save '{doc_name}' to storage: {e}")

    @mcp.tool(structured_output=False)
    def load_document_from_storage(object_key: str) -> ToolResponse:
        """Download a saved document from object storage and open it in FreeCAD.

        Args:
            object_key: Storage key, e.g. "freecad/MyProject.FCStd"
                (see list_storage_files).

        Returns:
            The name of the opened document.
        """
        try:
            key = _normalise_key(object_key)
            local_path = _data_path(Path(key).name)
            storage.download(key, local_path)
            doc_name = _run_in_freecad(
                get_connection(),
                "import FreeCAD\n"
                f"doc = FreeCAD.openDocument({str(local_path)!r})\n"
                "print(doc.Name)\n",
            )
            return json_response({"key": key, "document": doc_name, "path": str(local_path)})
        except Exception as e:
            return text_response(f"Failed to load '{object_key}' from storage: {e}")

    @mcp.tool(structured_output=False)
    def list_storage_files(prefix: str = "") -> ToolResponse:
        """List files kept in object storage (saved FreeCAD documents, exports, renders).

        Args:
            prefix: Optional key prefix to filter by, e.g. "freecad/".

        Returns:
            Each stored object's key, size and last-modified time.
        """
        try:
            return json_response(
                {"bucket": storage.bucket_name(), "objects": storage.list_objects(prefix)}
            )
        except Exception as e:
            return text_response(f"Failed to list storage: {e}")

    @mcp.tool(structured_output=False)
    def upload_file_to_storage(data_path: str, object_key: str = "") -> ToolResponse:
        """Upload a file that already exists in /data (an export, a render, ...).

        Args:
            data_path: File under /data, e.g. "part.step" or "/data/part.png".
            object_key: Storage key to write. Defaults to "freecad/<filename>".

        Returns:
            The storage key, bucket and size written.
        """
        try:
            local_path = _data_path(data_path)
            key = _normalise_key(object_key or f"{_FREECAD_PREFIX}{local_path.name}")
            return json_response(storage.upload(local_path, key))
        except Exception as e:
            return text_response(f"Failed to upload '{data_path}': {e}")

    @mcp.tool(structured_output=False)
    def download_file_from_storage(object_key: str, data_path: str = "") -> ToolResponse:
        """Download a stored file into /data, where FreeCAD and the renderer can read it.

        Args:
            object_key: Storage key to fetch (see list_storage_files).
            data_path: Destination under /data. Defaults to the key's filename.

        Returns:
            The local path written and its size.
        """
        try:
            key = _normalise_key(object_key)
            local_path = _data_path(data_path or Path(key).name)
            return json_response(storage.download(key, local_path))
        except Exception as e:
            return text_response(f"Failed to download '{object_key}': {e}")
