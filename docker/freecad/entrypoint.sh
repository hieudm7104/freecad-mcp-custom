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

# Clear FreeCAD's auto-recovery transient dirs before launching.
#
# This container is SIGKILLed on every restart, so FreeCAD never exits
# cleanly and always leaves `FreeCAD_Doc_<uuid>_<pid>/fc_recovery_file.*`
# behind for each open document. On the next start it finds them, runs
# document recovery, and — when the recovery cannot read a document back —
# raises a MODAL error dialog. There is no human here to dismiss it, and
# `process_gui_tasks` (addon/FreeCADMCP/rpc_server/gui_dispatch.py) defers
# every tick while `activeModalWidget()` is set, so the GUI task queue never
# drains again: every GUI-touching MCP tool then times out at its
# queue_timeout while `ping` and `get_rpc_status` keep answering, which is
# what made this take 32 hours to notice on 2026-09-21.
#
# Only one FreeCAD ever runs in this container, so anything here at startup
# is by definition stale. Nothing of value is lost: recovery data is only
# reachable through that same dialog. Durable storage is MinIO — use
# save_document_to_storage.
rm -rf "${HOME}/.cache/FreeCAD"/*/Cache/FreeCAD_* 2>/dev/null || true

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
