# CUDA runtime base so this GUI Blender instance can render on the GPU.
#
# The official blender.org binary, not Ubuntu's apt `blender` package: that
# one is a "dfsg" repackaging with non-free bits stripped — no STEP importer,
# no OpenImageDenoiser, and — confirmed by running a GPU render and watching
# `nvidia-smi` stay flat — no working CUDA/OptiX Cycles kernels either, so it
# silently renders on CPU regardless of the device asked for.
#
# download.blender.org itself sits behind a Cloudflare challenge that returns
# HTTP 403 to a plain container `curl`; this official mirror works. 5.2 LTS
# specifically: 4.2.1's bundled OptiX SDK has no precompiled kernel for this
# host's RTX PRO 5000 Blackwell and JIT-compiled one on *every* render (~5.5
# min, not fixed by persisting OptiX's own disk cache); 5.2's newer SDK
# targets Blackwell natively and renders in 2-4 s.
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
