FROM python:3.12-slim

# freecad-python3 provides a headless-only FreeCAD CLI (freecadcmd-python3)
# used locally by execute_code_headless. This is independent of the full GUI
# FreeCAD that runs in the separate "freecad" service and hosts the RPC
# server. Debian trixie no longer ships a "freecad-cmd" package; the binary
# is named freecadcmd-python3, so symlink it to the name freecad-mcp expects.
RUN apt-get update && apt-get install -y --no-install-recommends \
        freecad-python3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/lib/freecad/bin/freecadcmd-python3 /usr/local/bin/freecadcmd

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv pip install --system --no-cache .

# Installed here rather than added to pyproject.toml: object storage is a
# Docker-deployment feature (see src/freecad_mcp/storage.py), and keeping it
# out of the project metadata keeps this fork's upstream diff small — the
# import is lazy, so a plain `pip install .` elsewhere still works without it.
RUN uv pip install --system --no-cache minio

EXPOSE 8000

ENTRYPOINT ["freecad-mcp"]
CMD ["--transport", "streamable-http", "--port", "8000"]
