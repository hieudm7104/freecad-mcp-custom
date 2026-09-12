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
exec xvfb-run --auto-servernum --server-args="-screen 0 1280x1024x24" freecad "$@"
