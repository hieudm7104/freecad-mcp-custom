FROM debian:trixie-slim

# FreeCAD comes from the official AppImage, not apt. Debian trixie ships only
# 1.0.0+dfsg (the original Nov-2024 release) with no backports, while upstream
# is on 1.1.x — and that apt build has no CalculiX (`ccx`) or `gmsh` anywhere
# on the image, so `run_fem_analysis` could never actually solve or mesh. The
# AppImage bundles both. Same lesson as docker/blender/Dockerfile, where the
# apt Blender turned out to be missing its CUDA/OptiX kernels.
#
# Keep this version in lockstep with docker/mcp/Dockerfile: documents are
# shared through /data, and a .FCStd written by a newer FreeCAD opened by an
# older one is how data gets silently degraded.
ARG FC_VERSION=1.1.3
ARG FC_URL=https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD_1.1.3-Linux-x86_64-py311.AppImage

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        xauth \
        curl \
        mesa-utils \
        libgl1-mesa-dri \
        libglu1-mesa \
        libxrender1 libxi6 libxcursor1 libxrandr2 libxinerama1 libxkbcommon-x11-0 \
        libfontconfig1 libdbus-1-3 libegl1 libsm6 libice6 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --appimage-extract rather than running the AppImage directly: mounting one
# needs FUSE, which isn't available in an unprivileged container.
RUN curl -fsSL -o /tmp/fc.AppImage "$FC_URL" \
    && chmod +x /tmp/fc.AppImage \
    && cd /opt && /tmp/fc.AppImage --appimage-extract >/dev/null \
    && mv /opt/squashfs-root /opt/freecad \
    && rm -f /tmp/fc.AppImage \
    && printf '#!/bin/sh\nexec /opt/freecad/AppRun "$@"\n' > /usr/local/bin/freecad \
    && chmod +x /usr/local/bin/freecad

COPY addon/FreeCADMCP /opt/freecad-mcp-addon/FreeCADMCP
COPY docker/freecad/settings.seed.json /opt/freecad-mcp-addon/settings.seed.json
COPY docker/freecad/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV HOME=/root

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
