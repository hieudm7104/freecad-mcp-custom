#!/bin/sh
set -eu

SETTINGS_DIR="${HOME}/.local/share/FreeCAD"
MOD_DIR="${SETTINGS_DIR}/Mod"

mkdir -p "$SETTINGS_DIR" "$MOD_DIR"

if [ ! -e "$MOD_DIR/FreeCADMCP" ]; then
    cp -r /opt/freecad-mcp-addon/FreeCADMCP "$MOD_DIR/FreeCADMCP"
fi

# Seed RPC settings only on first run so a user who edits allowed_ips via the
# FreeCAD UI (persisted to this same file) keeps their change across restarts.
if [ ! -e "$SETTINGS_DIR/freecad_mcp_settings.json" ]; then
    cp /opt/freecad-mcp-addon/settings.seed.json "$SETTINGS_DIR/freecad_mcp_settings.json"
fi

export LIBGL_ALWAYS_SOFTWARE=1

# Run Xvfb directly instead of via xvfb-run: on this base image xvfb-run's
# SIGUSR1 ready-handshake with Xvfb never fires, so it hangs forever before
# ever launching FreeCAD. Start Xvfb ourselves and just wait for its socket.
DISPLAY_NUM=99
export DISPLAY=":${DISPLAY_NUM}"
Xvfb "$DISPLAY" -screen 0 1280x1024x24 -nolisten tcp &

for i in $(seq 1 50); do
    [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
    sleep 0.2
done

exec freecad "$@"
