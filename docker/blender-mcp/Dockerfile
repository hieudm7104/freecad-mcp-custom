FROM python:3.12-slim

WORKDIR /app

# blender-mcp itself pins mcp>=1.9.0,<2 (the v1 FastMCP API its server.py
# uses); pin the same range here so run_http.py's import of blender_mcp.server
# gets that API, not mcp 2.x's renamed/restructured one (see freecad_mcp's own
# server.py for the fallout of that rename).
RUN pip install --no-cache-dir "blender-mcp==1.9.1" "mcp>=1.9.0,<2" uvicorn minio

COPY docker/blender-mcp/http_auth.py docker/blender-mcp/oauth.py docker/blender-mcp/run_http.py ./
COPY docker/blender-mcp/storage_tools.py docker/blender-mcp/preview_api.py ./
# The same module the FreeCAD MCP server uses (freecad_mcp/storage.py),
# copied in rather than duplicated so bucket/key/path rules stay identical
# across both servers. It deliberately imports nothing FreeCAD-specific.
COPY src/freecad_mcp/storage.py ./storage.py

EXPOSE 8000

ENTRYPOINT ["python", "run_http.py"]
