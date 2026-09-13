"""A live-view HTML page for the streamable-http transport.

Serves a small, self-refreshing page at ``/preview`` that shows a screenshot
of whatever is currently the active document/view in the running FreeCAD
instance (via the same ``get_active_screenshot`` RPC call the ``get_view``
MCP tool uses), polling every couple of seconds. Meant for a human to open
in a browser to watch changes land in FreeCAD in near-real-time while an
MCP client (a person, ChatGPT, etc.) drives it.

The page has a second tab for the *Blender* instance, whose viewport lives
behind a different server on a different domain (`blender-mcp`). Rather than
pointing the browser at both origins — which would need CORS for the control
calls and would put that server's API key in the page — the Blender tab hits
``/preview/blender/*`` here and this server proxies to
``docker/blender-mcp/preview_api.py`` over the compose network, so the
browser sees one origin and one key. The tab only appears when
``BLENDER_MCP_API_KEY`` is set for this container (docker-compose.yml).

The image itself is draggable/scrollable: drag orbits the camera
(``orbit_camera``), the wheel zooms (``zoom_camera``), and a Reset button
snaps back to a canned Isometric view (``reset_view``). Polling the
screenshot always passes ``view_name=None`` (see ``get_active_screenshot``
in ``freecad_client.py`` / ``view_manager.py`` on the addon side) so the
regular 2-second refresh never overwrites the camera state these controls
set.

Gated by the same ``FREECAD_MCP_API_KEY`` as everything else, but via a
``?key=`` query parameter instead of a header, since a browser tab (and an
``<img>`` tag in particular) can't set custom headers. This is why these
routes are exempted from ``ApiKeyMiddleware`` in ``http_auth.py`` and check
the key themselves.
"""

import base64
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .freecad_client import FreeCADConnection

# Where the Blender tab's viewport calls get forwarded. Set in
# docker-compose.yml for the `mcp` service; absent means no Blender tab.
_BLENDER_URL = os.environ.get("BLENDER_MCP_URL", "http://blender-mcp:8000").rstrip("/")
_BLENDER_KEY = os.environ.get("BLENDER_MCP_API_KEY", "")

# 1x1 transparent PNG, shown while there's nothing real to display yet.
_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

_KEY_FORM = """<!doctype html>
<html><head><title>FreeCAD live preview</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:420px;margin:80px auto;padding:0 16px">
<h2>FreeCAD live preview</h2>
{message}
<form method="get">
  <label>API key<br>
    <input type="password" name="key" autofocus style="width:100%;padding:8px;box-sizing:border-box">
  </label>
  <p><button type="submit" style="padding:8px 16px">View</button></p>
</form>
</body></html>"""

_PREVIEW_PAGE = """<!doctype html>
<html><head><title>FreeCAD / Blender live preview</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; background: #1e1e1e; color: #ddd; }}
  header {{ display: flex; gap: 12px; align-items: center; padding: 10px 16px; background: #2a2a2a; flex-wrap: wrap; }}
  select, button {{ font-size: 14px; padding: 4px 8px; }}
  button {{ cursor: pointer; }}
  #tabs {{ display: flex; gap: 4px; }}
  .tab {{
    background: #1e1e1e; color: #9a9a9a; border: 1px solid #3a3a3a;
    border-radius: 4px; padding: 5px 14px; font-weight: 600;
  }}
  .tab.active {{ background: #3d6ea5; color: #fff; border-color: #3d6ea5; }}
  #live {{ min-width: 108px; border: 1px solid #3a3a3a; border-radius: 4px; background: #1e1e1e; color: #7fc47f; }}
  #live.paused {{ color: #d0a24c; }}
  #status {{ font-size: 13px; color: #9a9a9a; white-space: pre-wrap; }}
  main {{ display: flex; align-items: center; justify-content: center; min-height: calc(100vh - 60px); padding: 16px; box-sizing: border-box; overflow: hidden; }}
  img {{
    max-width: 100%; max-height: 80vh; background: #2a2a2a; border-radius: 4px;
    cursor: grab; touch-action: none; user-select: none;
  }}
  img.grabbing {{ cursor: grabbing; }}
  #hint {{ font-size: 12px; color: #777; margin-top: 8px; text-align: center; }}
  [hidden] {{ display: none !important; }}
</style></head>
<body>
<header>
  <div id="tabs">
    <button class="tab active" type="button" data-tab="freecad">FreeCAD</button>
    <button class="tab" type="button" data-tab="blender" id="blender-tab">Blender</button>
  </div>
  <label class="ctl-freecad">Project <select id="doc"><option value="">(none open)</option></select></label>
  <label class="ctl-blender" hidden>Shading <select id="shading">
    <option value="SOLID">Solid</option>
    <option value="MATERIAL">Material</option>
    <option value="RENDERED">Rendered</option>
    <option value="WIREFRAME">Wireframe</option>
  </select></label>
  <button class="ctl-blender" id="camera" type="button" hidden>Camera view</button>
  <button id="reset" type="button">Reset view</button>
  <button id="live" type="button" title="Stop/resume the automatic refresh">⏸ Live</button>
  <span id="status">connecting…</span>
</header>
<main>
  <div>
    <img id="shot" alt="3D view" draggable="false">
    <div id="hint">drag to orbit · scroll to zoom</div>
  </div>
</main>
<script>
const key = {key_json};
const HAS_BLENDER = {blender_json};

// Both tabs speak the exact same little REST dialect, so everything below
// (drag/orbit, wheel/zoom, reset, polling) is written once against whichever
// backend is active. The blender/* paths are proxied by this server to the
// blender-mcp container — see preview.py's module docstring.
const BACKENDS = {{
  freecad: {{ png: 'preview.png', base: 'preview' }},
  blender: {{ png: 'preview/blender.png', base: 'preview/blender' }},
}};
let tab = 'freecad';

const img = document.getElementById('shot');
const statusEl = document.getElementById('status');
const docSel = document.getElementById('doc');
const shadingSel = document.getElementById('shading');
const resetBtn = document.getElementById('reset');
const cameraBtn = document.getElementById('camera');
const hintEl = document.getElementById('hint');
const liveBtn = document.getElementById('live');
let knownDocs = [];
let switching = false;

// Poll one frame at a time, chained off the previous frame's completion —
// NOT on a fixed timer. The original flat `setInterval(…, 2000)` asked for
// frames faster than Blender could produce them (a capture cost ~2.4 s on
// software GL): the addon's single command queue grew without bound, every
// other call — a status poll here, a tool call from an MCP client — waited
// behind the backlog, and Blender looked hung while sitting at ~1500% CPU
// doing nothing but screenshots for a page nobody was necessarily even
// looking at.
//
// The gap is then two-speed, because a capture is never free: even on
// hardware GL a Blender frame costs ~0.16 CPU-seconds (the pixel read-back,
// the float conversion and the PNG encode, none of which the GPU helps
// with), so polling flat out would burn ~2 cores around the clock to redraw
// a page that is usually only being *watched*. Idling at ~1 fps costs ~0.17
// cores; the fast rate only kicks in for a couple of seconds after the user
// actually touches the image, which is the only time the latency shows.
// On top of that the gap never drops below half the measured frame cost, so
// an expensive backend (a software-GL container, a heavy FreeCAD document)
// backs off further on its own and keeps leaving room in the single command
// queue for everyone else.
const IDLE_GAP_MS = 900, ACTIVE_GAP_MS = 120, MAX_GAP_MS = 1200;
const ACTIVE_WINDOW_MS = 2000;
let frameTimer = null;
let frameInFlight = false;
let frameStart = 0;
let lastInteraction = 0;
let pendingManual = false;

// "Live" off stops the automatic refresh entirely — the one guaranteed way
// to take the load off FreeCAD/Blender without closing the page. It does not
// freeze the page: anything the user does (orbit, zoom, reset, switching
// project or shading) still fetches exactly one frame, so a paused page is
// still usable, just not self-updating. Remembered per browser.
let live = true;
try {{ live = localStorage.getItem('preview-live') !== '0'; }} catch (e) {{}}

function noteInteraction() {{ lastInteraction = Date.now(); }}

function gapFor(took) {{
  const active = Date.now() - lastInteraction < ACTIVE_WINDOW_MS;
  return Math.min(MAX_GAP_MS, Math.max(active ? ACTIVE_GAP_MS : IDLE_GAP_MS, took * 0.5));
}}

function setLive(next) {{
  live = next;
  liveBtn.textContent = live ? '⏸ Live' : '▶ Paused';
  liveBtn.classList.toggle('paused', !live);
  try {{ localStorage.setItem('preview-live', live ? '1' : '0'); }} catch (e) {{}}
  if (live) refreshImage(true);
  else clearTimeout(frameTimer);
}}

function scheduleFrame(delay) {{
  clearTimeout(frameTimer);
  frameTimer = setTimeout(() => refreshImage(), delay);
}}

// `manual` = the user asked for this one (a click/drag, a tab switch), so it
// happens even while paused. It never bypasses frameInFlight though — two
// captures at once is exactly the pile-up this pacing exists to prevent — so
// a manual request that arrives mid-frame is remembered and run right after.
function refreshImage(manual) {{
  clearTimeout(frameTimer);
  if (frameInFlight) {{ if (manual) pendingManual = true; return; }}
  // A hidden tab still paid the full render cost on the server.
  if (document.hidden) {{ if (live) scheduleFrame(1000); return; }}
  if (!live && !manual) return;
  frameInFlight = true;
  frameStart = Date.now();
  img.src = `${{BACKENDS[tab].png}}?key=${{encodeURIComponent(key)}}&t=${{Date.now()}}`;
}}

function onFrameSettled(ok) {{
  frameInFlight = false;
  if (!ok) {{ if (live) scheduleFrame(3000); return; }}
  const took = Date.now() - frameStart;
  const gap = gapFor(took);
  hintEl.textContent = `drag to orbit · scroll to zoom · ${{(took / 1000).toFixed(2)}}s/frame`
    + (live ? ` · ${{(1000 / (took + gap)).toFixed(1)}} fps` : ' · paused');
  if (pendingManual) {{ pendingManual = false; refreshImage(true); return; }}
  if (live) scheduleFrame(gap);
}}
img.addEventListener('load', () => onFrameSettled(true));
img.addEventListener('error', () => onFrameSettled(false));
document.addEventListener('visibilitychange', () => {{
  if (!document.hidden) refreshImage(true);
}});

async function post(action) {{
  const path = `${{BACKENDS[tab].base}}/${{action}}`;
  const res = await fetch(`${{path}}${{path.includes('?') ? '&' : '?'}}key=${{encodeURIComponent(key)}}`, {{ method: 'POST' }});
  return res.json();
}}

// --- Tabs ---
function selectTab(next) {{
  if (next === tab) return;
  tab = next;
  for (const btn of document.querySelectorAll('.tab')) {{
    btn.classList.toggle('active', btn.dataset.tab === tab);
  }}
  for (const el of document.querySelectorAll('.ctl-freecad')) el.hidden = tab !== 'freecad';
  for (const el of document.querySelectorAll('.ctl-blender')) el.hidden = tab !== 'blender';
  // Anything queued for the old backend would be applied to the new one.
  queuedAz = 0; queuedEl = 0;
  statusEl.textContent = 'connecting…';
  refreshImage(true);
  refreshStatus();
}}
for (const btn of document.querySelectorAll('.tab')) {{
  btn.addEventListener('click', () => selectTab(btn.dataset.tab));
}}
if (!HAS_BLENDER) document.getElementById('blender-tab').hidden = true;

// --- Orbit (drag) and zoom (wheel) ---
const SENSITIVITY = 0.4; // degrees per pixel dragged
let dragging = false, lastX = 0, lastY = 0;
let queuedAz = 0, queuedEl = 0, sendingOrbit = false;

async function flushOrbit() {{
  if (sendingOrbit) return;
  sendingOrbit = true;
  while (queuedAz !== 0 || queuedEl !== 0) {{
    const az = queuedAz, el = queuedEl;
    queuedAz = 0; queuedEl = 0;
    try {{
      await post(`orbit?dx=${{az}}&dy=${{el}}`);
      refreshImage(true);
    }} catch (e) {{
      statusEl.textContent = 'orbit failed: ' + e;
      break;
    }}
  }}
  sendingOrbit = false;
}}

img.addEventListener('pointerdown', e => {{
  dragging = true;
  lastX = e.clientX; lastY = e.clientY;
  img.classList.add('grabbing');
  img.setPointerCapture(e.pointerId);
}});
img.addEventListener('pointermove', e => {{
  if (!dragging) return;
  const dx = e.clientX - lastX, dy = e.clientY - lastY;
  lastX = e.clientX; lastY = e.clientY;
  noteInteraction();
  queuedAz += -dx * SENSITIVITY;
  queuedEl += dy * SENSITIVITY;
  flushOrbit();
}});
function endDrag() {{ dragging = false; img.classList.remove('grabbing'); }}
img.addEventListener('pointerup', endDrag);
img.addEventListener('pointercancel', endDrag);

img.addEventListener('wheel', e => {{
  e.preventDefault();
  noteInteraction();
  const factor = e.deltaY > 0 ? 1.1 : 0.9;
  post(`zoom?factor=${{factor}}`).then(() => refreshImage(true)).catch(err => {{
    statusEl.textContent = 'zoom failed: ' + err;
  }});
}}, {{ passive: false }});

resetBtn.addEventListener('click', () => {{
  noteInteraction();
  post('reset').then(() => refreshImage(true)).catch(err => {{
    statusEl.textContent = 'reset failed: ' + err;
  }});
}});

// Blender only: back to what the scene camera sees, i.e. the render framing.
// Orbiting/zooming leaves camera view (as Blender's own viewport does), so
// there has to be a way back.
cameraBtn.addEventListener('click', () => {{
  noteInteraction();
  post('camera').then(data => {{
    if (!data.success) statusEl.textContent = `camera view failed: ${{data.error || 'unknown error'}}`;
    refreshImage(true);
  }}).catch(err => {{
    statusEl.textContent = 'camera view failed: ' + err;
  }});
}});

// --- FreeCAD: project (document) switching ---
async function switchDocument() {{
  const doc = docSel.value;
  if (!doc || switching) return;
  switching = true;
  statusEl.textContent = `switching to ${{doc}}…`;
  try {{
    const data = await post(`activate?doc=${{encodeURIComponent(doc)}}`);
    if (!data.success) statusEl.textContent = `failed to switch: ${{data.error || 'unknown error'}}`;
  }} catch (e) {{
    statusEl.textContent = 'failed to switch: ' + e;
  }} finally {{
    switching = false;
    refreshImage(true);
    refreshStatus();
  }}
}}

// --- Blender: viewport shading ---
async function switchShading() {{
  try {{
    const data = await post(`shading?mode=${{encodeURIComponent(shadingSel.value)}}`);
    if (!data.success) statusEl.textContent = `failed to set shading: ${{data.error || 'unknown error'}}`;
  }} catch (e) {{
    statusEl.textContent = 'failed to set shading: ' + e;
  }}
  refreshImage(true);
}}

function renderFreecadStatus(data) {{
  const docs = data.documents || [];
  const docsChanged = docs.join('\\u0001') !== knownDocs.join('\\u0001');
  if (docsChanged) {{
    knownDocs = docs;
    const prevSelection = docSel.value;
    docSel.innerHTML = docs.length
      ? docs.map(d => `<option value="${{d}}">${{d}}</option>`).join('')
      : '<option value="">(none open)</option>';
    docSel.value = docs.includes(prevSelection) ? prevSelection : (data.active_document || docs[0] || '');
  }} else if (!switching && data.active_document && docSel.value !== data.active_document) {{
    docSel.value = data.active_document;
  }}
  statusEl.textContent = docs.length
    ? `active: ${{data.active_document || '(unknown)'}} — ${{docs.length}} project(s) open`
    : 'no open documents';
}}

function renderBlenderStatus(data) {{
  if (data.shading && shadingSel.value !== data.shading) shadingSel.value = data.shading;
  const view = data.view === 'CAMERA' ? 'camera view' : 'free view';
  statusEl.textContent =
    `scene: ${{data.scene || '(unknown)'}} — ${{data.objects ?? '?'}} object(s) — `
    + `${{data.blend || '(unsaved)'}} — ${{view}}`;
}}

async function refreshStatus() {{
  if (document.hidden) return;
  const requestedTab = tab;
  try {{
    const res = await fetch(`${{BACKENDS[tab].base}}/status?key=${{encodeURIComponent(key)}}`);
    const data = await res.json();
    // A slow status call can land after the user has already switched tabs.
    if (requestedTab !== tab) return;
    if (!data.connected) {{
      statusEl.textContent = `${{tab}} not connected: ` + (data.error || 'unknown error');
      return;
    }}
    if (tab === 'freecad') renderFreecadStatus(data); else renderBlenderStatus(data);
  }} catch (e) {{
    if (requestedTab === tab) statusEl.textContent = 'status check failed: ' + e;
  }}
}}

docSel.addEventListener('change', switchDocument);
shadingSel.addEventListener('change', switchShading);
liveBtn.addEventListener('click', () => setLive(!live));

setLive(live);  // paints the button, and fetches the first frame when live
if (!live) refreshImage(true);  // paused: still show one frame to start from
refreshStatus();
// No interval for the image — it re-schedules itself as each frame lands.
// The status poll stays on a timer: it's cheap (a tiny execute_code, ~3% of
// one backend command slot every 5 s) and it's what tells you the app is
// still alive while the image is paused.
setInterval(refreshStatus, 5000);
</script>
</body></html>"""


def preview_enabled() -> bool:
    """Whether to serve the live-view page at all.

    Off unless ``FREECAD_MCP_PREVIEW`` is explicitly truthy. An open preview
    tab polls a screenshot continuously, and a screenshot is never free — it
    is real, sustained load on the FreeCAD/Blender GUI process for something
    only a human watching a browser needs. Opt in per deployment rather than
    leaving it on for everyone.
    """
    return os.environ.get("FREECAD_MCP_PREVIEW", "").strip().lower() in {"1", "true", "yes", "on"}


def _check_key(request: Request, api_key: str) -> bool:
    provided = request.query_params.get("key", "")
    return bool(provided) and hmac.compare_digest(provided, api_key)


def blender_preview_enabled() -> bool:
    return bool(_BLENDER_KEY)


def _blender_fetch(path: str, params: dict, method: str) -> tuple[int, bytes, str]:
    """Call the blender-mcp server's matching ``/preview*`` route.

    Blocking on purpose — every caller runs it through
    ``anyio.to_thread.run_sync`` (``urllib`` is used rather than ``httpx``
    because the ``mcp`` image doesn't have httpx's client deps installed and
    this is the only outbound HTTP call the server makes).
    """
    query = dict(params)
    query["key"] = _BLENDER_KEY
    url = f"{_BLENDER_URL}{path}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return response.status, body, response.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("content-type", "")


def register_preview_routes(
    app: Starlette, api_key: str, get_connection: Callable[[], FreeCADConnection]
) -> None:
    async def preview_page(request: Request):
        provided = request.query_params.get("key", "")
        if not provided or not hmac.compare_digest(provided, api_key):
            message = '<p style="color:#c00"><b>Wrong API key.</b></p>' if provided else ""
            return HTMLResponse(_KEY_FORM.format(message=message))

        return HTMLResponse(
            _PREVIEW_PAGE.format(
                key_json=_js_string(provided),
                blender_json="true" if blender_preview_enabled() else "false",
            )
        )

    async def preview_png(request: Request):
        if not _check_key(request, api_key):
            return Response(status_code=401)

        width = _int_or_none(request.query_params.get("w"))
        height = _int_or_none(request.query_params.get("h"))

        try:
            freecad = get_connection()
            # view_name=None: never force a canned orientation here — it
            # would undo whatever orbit_camera/zoom_camera/reset_view last
            # set, since this route is polled continuously.
            encoded = freecad.get_active_screenshot(None, width, height, None)
        except Exception:
            encoded = None

        png_bytes = base64.b64decode(encoded) if encoded else _BLANK_PNG
        return Response(content=png_bytes, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def preview_status(request: Request):
        if not _check_key(request, api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            freecad = get_connection()
            documents = freecad.list_documents()
            active_document = freecad.get_active_document()
        except Exception as e:
            return JSONResponse({"connected": False, "error": str(e)})

        return JSONResponse(
            {"connected": True, "documents": documents, "active_document": active_document}
        )

    async def preview_activate(request: Request):
        if not _check_key(request, api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        doc_name = request.query_params.get("doc", "")
        if not doc_name:
            return JSONResponse({"success": False, "error": "missing 'doc' parameter"}, status_code=400)

        try:
            freecad = get_connection()
            result = freecad.activate_document(doc_name)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})

        return JSONResponse(result)

    async def preview_orbit(request: Request):
        if not _check_key(request, api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            delta_azimuth = float(request.query_params.get("dx", "0"))
            delta_elevation = float(request.query_params.get("dy", "0"))
        except ValueError:
            return JSONResponse({"success": False, "error": "dx/dy must be numbers"}, status_code=400)

        try:
            freecad = get_connection()
            result = freecad.orbit_camera(delta_azimuth, delta_elevation)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})

        return JSONResponse(result)

    async def preview_zoom(request: Request):
        if not _check_key(request, api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            factor = float(request.query_params.get("factor", "1"))
        except ValueError:
            return JSONResponse({"success": False, "error": "factor must be a number"}, status_code=400)

        try:
            freecad = get_connection()
            result = freecad.zoom_camera(factor)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})

        return JSONResponse(result)

    async def preview_reset(request: Request):
        if not _check_key(request, api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            freecad = get_connection()
            result = freecad.reset_view("Isometric")
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})

        return JSONResponse(result)

    # --- Blender tab: straight pass-through to the blender-mcp server's own
    # identically-shaped /preview* routes (docker/blender-mcp/preview_api.py).
    async def blender_png(request: Request):
        if not _check_key(request, api_key):
            return Response(status_code=401)

        body = _BLANK_PNG
        if blender_preview_enabled():
            params = {k: v for k, v in (("w", request.query_params.get("w")),) if v}
            try:
                status, content, _ = await anyio.to_thread.run_sync(
                    _blender_fetch, "/preview.png", params, "GET"
                )
                if status == 200 and content:
                    body = content
            except Exception:
                pass  # fall through to the blank PNG; /status reports the reason

        return Response(content=body, media_type="image/png", headers={"Cache-Control": "no-store"})

    def _blender_json_proxy(remote_path: str, method: str, forward: tuple[str, ...]):
        async def handler(request: Request):
            if not _check_key(request, api_key):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            if not blender_preview_enabled():
                return JSONResponse(
                    {
                        "connected": False,
                        "success": False,
                        "error": "Blender preview not configured (BLENDER_MCP_API_KEY unset)",
                    }
                )

            params = {}
            for name in forward:
                value = request.query_params.get(name)
                if value is not None:
                    params[name] = value

            try:
                status, body, _ = await anyio.to_thread.run_sync(
                    _blender_fetch, remote_path, params, method
                )
            except Exception as e:
                return JSONResponse({"connected": False, "success": False, "error": str(e)})

            try:
                payload = json.loads(body)
            except ValueError:
                payload = {
                    "connected": False,
                    "success": False,
                    "error": f"unexpected reply from blender-mcp (HTTP {status})",
                }
            return JSONResponse(payload, status_code=status if status >= 400 else 200)

        return handler

    app.add_route("/preview", preview_page, methods=["GET"])
    app.add_route("/preview.png", preview_png, methods=["GET"])
    app.add_route("/preview/status", preview_status, methods=["GET"])
    app.add_route("/preview/activate", preview_activate, methods=["POST"])
    app.add_route("/preview/orbit", preview_orbit, methods=["POST"])
    app.add_route("/preview/zoom", preview_zoom, methods=["POST"])
    app.add_route("/preview/reset", preview_reset, methods=["POST"])

    app.add_route("/preview/blender.png", blender_png, methods=["GET"])
    app.add_route(
        "/preview/blender/status",
        _blender_json_proxy("/preview/status", "GET", ()),
        methods=["GET"],
    )
    app.add_route(
        "/preview/blender/orbit",
        _blender_json_proxy("/preview/orbit", "POST", ("dx", "dy")),
        methods=["POST"],
    )
    app.add_route(
        "/preview/blender/zoom",
        _blender_json_proxy("/preview/zoom", "POST", ("factor",)),
        methods=["POST"],
    )
    app.add_route(
        "/preview/blender/reset",
        _blender_json_proxy("/preview/reset", "POST", ()),
        methods=["POST"],
    )
    app.add_route(
        "/preview/blender/camera",
        _blender_json_proxy("/preview/camera", "POST", ()),
        methods=["POST"],
    )
    app.add_route(
        "/preview/blender/shading",
        _blender_json_proxy("/preview/shading", "POST", ("mode",)),
        methods=["POST"],
    )


def _int_or_none(value: Any) -> int | None:
    return int(value) if value else None


def _js_string(value: str) -> str:
    """Safely embed *value* as a JS string literal inside an HTML <script> block."""
    return json.dumps(value).replace("</", "<\\/")
