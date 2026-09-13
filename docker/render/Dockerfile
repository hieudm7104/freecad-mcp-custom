# CUDA runtime base so Cycles can use CUDA/OptiX on the host's NVIDIA GPU
# instead of falling back to CPU-only rendering. Requires the host to have
# nvidia-container-toolkit and the `render` service in docker-compose.yml to
# request a GPU device (both already true on this deployment).
FROM nvidia/cuda:12.6.3-runtime-ubuntu24.04

# Required by nvidia-container-toolkit to expose the driver's CUDA/OptiX
# libraries (not just "compute") into the container.
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics

# Use the official blender.org binary, not Ubuntu's apt `blender` package:
# the apt build (a Debian/Ubuntu "dfsg" repackaging with non-free bits
# stripped) has no STEP importer, no OpenImageDenoiser, and — confirmed by
# actually running a GPU render and watching `nvidia-smi` stay flat — no
# working CUDA/OptiX Cycles kernels either, so it silently renders on CPU
# regardless of --device. The official tarball bundles all of that.
#
# download.blender.org itself sits behind a Cloudflare challenge that blocks
# a plain container `curl`/`wget` (returns HTTP 403, a JS challenge page) —
# an official mirror works instead.
# 4.2.1's bundled OptiX SDK doesn't ship a precompiled kernel for the RTX
# PRO 5000 Blackwell on this host, so Cycles has to JIT-compile at runtime —
# ~5.5 minutes on the first render, observed to NOT get any faster on
# repeat runs even with OptiX's own disk cache mounted persistently
# (optix7cache.db stayed at its empty 1 MiB default size — the writes
# aren't landing). Blender 5.2 LTS ships a newer OptiX SDK with native
# Blackwell support; try that before spending more time on the cache path.
ARG BLENDER_VERSION=5.2.1
ARG BLENDER_URL=https://mirror.freedif.org/blender/release/Blender5.2/blender-${BLENDER_VERSION}-linux-x64.tar.xz

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        xz-utils \
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

COPY docker/render/render.py /opt/render/render.py

ENTRYPOINT ["blender", "-b", "-P", "/opt/render/render.py", "--"]
