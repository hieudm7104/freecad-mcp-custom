FROM python:3.12-slim

# A headless FreeCAD CLI (freecadcmd) for execute_code_headless, independent
# of the full GUI FreeCAD in the separate "freecad" service that hosts the RPC
# server. Taken from the official AppImage rather than apt for the reasons in
# docker/freecad/Dockerfile (trixie is stuck on 1.0.0+dfsg and ships no ccx or
# gmsh at all) — and MUST stay on the same version as that service, since both
# read and write the same .FCStd files on the /data volume.
#
# Only freecadcmd is ever invoked, and always as a subprocess, so the
# AppImage's bundled Python 3.11 never has to agree with this image's 3.12.
ARG FC_VERSION=1.1.3
ARG FC_URL=https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD_1.1.3-Linux-x86_64-py311.AppImage

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libgl1 libglu1-mesa \
        libxrender1 libxi6 libxcursor1 libxrandr2 libxinerama1 libxkbcommon-x11-0 \
        libfontconfig1 libdbus-1-3 libegl1 libsm6 libice6 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --appimage-extract: mounting an AppImage needs FUSE, unavailable here.
RUN curl -fsSL -o /tmp/fc.AppImage "$FC_URL" \
    && chmod +x /tmp/fc.AppImage \
    && cd /opt && /tmp/fc.AppImage --appimage-extract >/dev/null \
    && mv /opt/squashfs-root /opt/freecad \
    && rm -f /tmp/fc.AppImage \
    && ln -s /opt/freecad/usr/bin/freecadcmd /usr/local/bin/freecadcmd

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
