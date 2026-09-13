# CUDA runtime base so this GUI Blender instance can also render on the GPU
# (the same reasoning/setup as docker/render/Dockerfile — see that file's
# comments for the mirror URL and why the official binary is used instead of
# apt's stripped-down "dfsg" package).
FROM nvidia/cuda:12.6.3-runtime-ubuntu24.04

ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics

ARG BLENDER_VERSION=5.2.1
ARG BLENDER_URL=https://mirror.freedif.org/blender/release/Blender5.2/blender-${BLENDER_VERSION}-linux-x64.tar.xz

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        xz-utils \
        xvfb \
        xauth \
        libxi6 \
        libxrender1 \
        libxxf86vm1 \
        libxfixes3 \
        libxkbcommon0 \
        libxcursor1 \
        libsm6 \
        libice6 \
        libgl1 \
    && curl -fsSL "$BLENDER_URL" -o /tmp/blender.tar.xz \
    && mkdir -p /opt/blender \
    && tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 \
    && rm /tmp/blender.tar.xz \
    && ln -s /opt/blender/blender /usr/local/bin/blender \
    && apt-get purge -y curl xz-utils \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# addon.py is a vendored (with one intentional patch — see the file's own
# comment: default socket host changed from 'localhost' to '0.0.0.0') copy of
# https://github.com/ahujasid/blender-mcp's Blender addon. It bundles its own
# `requests` import; Blender 5.2's bundled Python already ships `requests`
# 2.32.3, so nothing extra needs installing for it.
COPY docker/blender/addon.py /opt/blender-mcp-addon/blender_mcp.py
COPY docker/blender/startup.py /opt/blender-mcp-addon/startup.py
COPY docker/blender/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV HOME=/root

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
