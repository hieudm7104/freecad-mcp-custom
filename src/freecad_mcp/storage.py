"""S3/MinIO object storage helpers.

Documents in FreeCAD and scenes in Blender live only in the running
process's memory — a container rebuild or restart loses anything not
explicitly written to disk, and the `/data` volume that *is* persistent is
still only reachable from inside these containers. This module backs both
MCP servers' "save/load project" tools with a MinIO bucket, so work
survives container churn and can be fetched from outside Docker.

Deliberately free of any FreeCAD or Blender import: the same file is used
by the `mcp_freecad` container (as `freecad_mcp.storage`) and COPY'd
standalone into the `mcp_blender` image (as `storage`), so they can't drift
apart on bucket naming, key layout or error handling. See
`docker/blender-mcp/Dockerfile`.

Configured entirely by environment (all set in docker-compose.yml):
``MINIO_ENDPOINT`` (host:port), ``MINIO_ACCESS_KEY``, ``MINIO_SECRET_KEY``,
``MINIO_BUCKET`` (default ``cad``) and ``MINIO_SECURE`` (default off —
traffic stays on the compose network). With ``MINIO_ENDPOINT`` unset the
storage tools simply aren't registered, so the stdio/local path and any
deployment without MinIO behave exactly as before.
"""

import os
from pathlib import Path
from typing import Any

DEFAULT_BUCKET = "cad"

# The volume both MCP servers share with their CAD/3D process — the only
# place these tools are allowed to read from or write to.
DATA_DIR = Path(os.environ.get("MCP_DATA_DIR", "/data"))

_client = None


def normalise_key(object_key: str) -> str:
    key = object_key.strip().lstrip("/")
    if not key or ".." in key.split("/"):
        raise ValueError(f"Invalid object key: {object_key!r}")
    return key


def data_path(path: str) -> Path:
    """Resolve *path* inside the shared data volume, refusing to escape it.

    Object storage shouldn't double as a way to pull arbitrary files out of
    (or drop them into) the container, so a tool argument naming a path
    outside the volume is rejected rather than silently honoured.
    """
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else DATA_DIR / candidate).resolve()
    root = DATA_DIR.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Path must be inside {root}: {path!r}")
    return resolved


def is_configured() -> bool:
    return bool(
        os.environ.get("MINIO_ENDPOINT")
        and os.environ.get("MINIO_ACCESS_KEY")
        and os.environ.get("MINIO_SECRET_KEY")
    )


def bucket_name() -> str:
    return os.environ.get("MINIO_BUCKET") or DEFAULT_BUCKET


def endpoint() -> str:
    return os.environ.get("MINIO_ENDPOINT", "")


def client():
    """Return a cached MinIO client, creating the bucket on first use."""
    global _client
    if _client is not None:
        return _client

    if not is_configured():
        raise RuntimeError(
            "Object storage is not configured (set MINIO_ENDPOINT, "
            "MINIO_ACCESS_KEY and MINIO_SECRET_KEY)"
        )

    # Imported lazily so this module stays importable (and the tools stay
    # merely unavailable rather than crashing the server) wherever the
    # `minio` package isn't installed — e.g. a plain `pip install .` of this
    # project outside the Docker images.
    from minio import Minio

    secure = os.environ.get("MINIO_SECURE", "").strip().lower() in ("1", "true", "yes", "on")
    _client = Minio(
        endpoint(),
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=secure,
    )

    bucket = bucket_name()
    if not _client.bucket_exists(bucket):
        _client.make_bucket(bucket)

    return _client


def upload(local_path: str | Path, object_key: str) -> dict[str, Any]:
    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(f"Nothing to upload at {path}")
    client().fput_object(bucket_name(), object_key, str(path))
    return {"key": object_key, "bucket": bucket_name(), "size": path.stat().st_size}


def download(object_key: str, local_path: str | Path) -> dict[str, Any]:
    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    client().fget_object(bucket_name(), object_key, str(path))
    return {"key": object_key, "path": str(path), "size": path.stat().st_size}


def list_objects(prefix: str = "") -> list[dict[str, Any]]:
    objects = client().list_objects(bucket_name(), prefix=prefix or None, recursive=True)
    return [
        {
            "key": obj.object_name,
            "size": obj.size,
            "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
        }
        for obj in objects
    ]


def exists(object_key: str) -> bool:
    from minio.error import S3Error

    try:
        client().stat_object(bucket_name(), object_key)
        return True
    except S3Error:
        return False
