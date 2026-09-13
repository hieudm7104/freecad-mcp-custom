#!/bin/sh
set -eu

ADDON_DIR="${HOME}/.config/blender/5.2/scripts/addons"
mkdir -p "$ADDON_DIR"

if [ ! -e "$ADDON_DIR/blender_mcp.py" ]; then
    cp /opt/blender-mcp-addon/blender_mcp.py "$ADDON_DIR/blender_mcp.py"
fi

# Viewport OpenGL. This used to be an unconditional
# `export LIBGL_ALWAYS_SOFTWARE=1`, which pinned every viewport draw to Mesa's
# llvmpipe software rasteriser on the CPU — a `get_viewport_screenshot` in
# MATERIAL shading cost **2.1 s** (and barely got cheaper at lower
# resolution, because the cost is EEVEE's per-frame shading work, not the
# pixel count). The container already has the GPU and NVIDIA's GL libraries
# available (`NVIDIA_DRIVER_CAPABILITIES` includes `graphics`), but libglvnd
# still picks Mesa's GLX by default, since that is what Xvfb's X server
# advertises. Naming the NVIDIA vendor explicitly routes GLX to the real GPU
# even under Xvfb — same scene, same 1000x858 capture: **0.079 s**, a 27x
# speedup (SOLID shading: 0.28 s -> 0.028 s).
#
# Keep the software fallback for any host without the NVIDIA libraries —
# forcing the nvidia vendor there would leave Blender with no usable GL at
# all rather than a slow one.
if [ -e /usr/lib/x86_64-linux-gnu/libGLX_nvidia.so.0 ]; then
    export __GLX_VENDOR_LIBRARY_NAME=nvidia
    echo "[blender] viewport GL: NVIDIA (hardware)"
else
    export LIBGL_ALWAYS_SOFTWARE=1
    echo "[blender] viewport GL: llvmpipe (software) — no NVIDIA GLX in this container"
fi

# Same fix as docker/freecad/entrypoint.sh: xvfb-run's SIGUSR1 handshake with
# Xvfb hangs forever on this base image, so start Xvfb ourselves and just
# wait for its socket instead of going through xvfb-run.
DISPLAY_NUM=99
export DISPLAY=":${DISPLAY_NUM}"
Xvfb "$DISPLAY" -screen 0 1280x1024x24 -nolisten tcp &

for i in $(seq 1 50); do
    [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
    sleep 0.2
done

# No `-b` (background/render-only mode): the addon's own start() refuses to
# run under `bpy.app.background` (its socket server needs Blender's regular
# event loop, driven by bpy.app.timers, which -b mode doesn't keep running).
exec blender --python /opt/blender-mcp-addon/startup.py "$@"
