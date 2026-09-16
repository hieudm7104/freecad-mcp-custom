# Docker deployment

[Back to README](../README.md)

Runs six containers on one server: the FreeCAD MCP server (reachable over
HTTP with an API key — clients need no local install), a headless FreeCAD
hosting the RPC addon, an on-demand Blender batch renderer, a persistent
GUI Blender instance for live AI-driven scene editing, that instance's own
MCP server, and a MinIO bucket both MCP servers save projects into.

## Architecture

- **`freecad`** — Debian (`debian:trixie-slim`) + FreeCAD **1.1.3 from the
  official AppImage** (see "FreeCAD version" below for why not apt), launched
  under a manually-started `Xvfb` (a virtual display; the addon still needs
  `FreeCADGui`/Coin3D for screenshots and views). Ubuntu 24.04 no longer
  packages `freecad` at all, and `xvfb-run`'s SIGUSR1 ready-handshake with
  `Xvfb` hangs forever on this base image, so `docker/freecad/entrypoint.sh`
  starts `Xvfb` itself and waits for its socket instead of using `xvfb-run`.
  The addon and a seeded `freecad_mcp_settings.json`
  (`auto_start_rpc: true`, `remote_enabled: true`) are baked into the image,
  so the RPC server on port 9875 comes up automatically — no manual toolbar
  click. Not published to the host; only the `mcp` container can reach it.
- **`mcp`** — the MCP server, `--transport streamable-http` gated by
  `FREECAD_MCP_API_KEY` (see `src/freecad_mcp/http_auth.py`), connecting to
  `freecad` over the compose network. Also carries its own headless FreeCAD
  CLI — `freecadcmd` from the same 1.1.3 AppImage, pinned to the same version
  as the `freecad` service — for `execute_code_headless`, which runs it as a
  local subprocess independent of the RPC connection. Only the binary is
  used, always as a subprocess, so the AppImage's bundled Python 3.11 never
  has to agree with this image's 3.12.
- **`render`** — Ubuntu + Blender, run on demand (`docker compose run`), not a
  long-running service. Renders a model exported to the shared volume.
- **`blender`** — a persistent, GUI Blender instance (Xvfb, same reasoning as
  `freecad`) with [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp)'s
  addon vendored in and auto-enabled, for live AI-driven scene editing
  (create/edit objects, materials, lights, arbitrary Python, viewport
  screenshots) — a different thing from `render`'s one-shot batch rendering.
  Not published to the host; only `blender-mcp` can reach its socket (9876).
- **`blender-mcp`** — that project's MCP server (`pip install blender-mcp`,
  imported as a library, not forked), wrapped in the same streamable-http +
  API-key/OAuth layer as the FreeCAD `mcp` container, since upstream only
  ships a stdio transport. See "Blender MCP integration" below.
- **`minio`** — an S3-compatible bucket holding saved FreeCAD documents,
  Blender scenes, exports and renders. Both MCP servers get save/load tools
  backed by it. See "Object storage (MinIO)" below.

All five of `freecad`, `mcp`, `render`, `blender` and `blender-mcp` share the
`freecad_data` volume at `/data`, so document/export paths agree between the
RPC connection, any headless script, the renderer and Blender — that's also
the staging area the storage tools upload from and download to. `blender` and
`blender-mcp` additionally share a `blender_tmp` volume (see the Blender
section for why).

## Running it

```bash
cp .env.example .env
# edit .env: set FREECAD_MCP_API_KEY (e.g. `openssl rand -hex 32`)

docker compose up -d --build freecad mcp
```

Point an MCP-over-HTTP client at `http://<server>:8000`, with header
`X-API-Key: <your key>` (or `Authorization: Bearer <your key>`).

## Public access via Cloudflare Tunnel

This server is also reachable publicly at **`https://freecad-mcp.hieudm.site`**,
proxied through the shared `cloudflared` tunnel (see
`../cloudflared/README.md`). The tunnel forwards to `127.0.0.1:8001` on the
host, so `MCP_PORT` in `.env` must stay `8001` — **not** `8000` — since
`8000` is already reserved for the `websearch.hieudm.site` route on that same
tunnel. If `MCP_PORT` changes, update the matching `service:` entry in
`../cloudflared/config/config.yml` and run `docker exec cloudflared cloudflared
tunnel ingress validate` before restarting the `cloudflared` container.

Point a remote MCP-over-HTTP client at `https://freecad-mcp.hieudm.site`, with
the same `X-API-Key`/`Authorization: Bearer` header as above.

### Connecting a client that only supports OAuth (e.g. ChatGPT Connectors)

Some MCP clients — ChatGPT's "Connector" UI in particular — only offer "No
auth" or a full OAuth flow for a remote MCP server; there's no field for a
plain static API key. `src/freecad_mcp/oauth.py` adds a minimal OAuth 2.1
(Authorization Code + PKCE) shim for exactly this case, enabled by setting
`FREECAD_MCP_OAUTH_CLIENT_ID` in `.env` (see `.env.example`). It's a single-
user, single-client shim: "logging in" just means typing the existing
`FREECAD_MCP_API_KEY` into a one-field page, and the token it issues is
ultimately gated on that same key (see the module docstring for the full
design). Static `X-API-Key`/`Bearer <key>` auth keeps working unchanged
whether or not this is enabled.

Two client styles are supported, because MCP clients differ in how they get
a `client_id`:

**Claude.ai custom connectors (and anything else that self-registers).**
Claude's "Add custom connector" flow is fully automatic: it reads the
discovery metadata, registers itself via Dynamic Client Registration
(RFC 7591) at `POST /oauth/register`, then runs the normal authorize/token
flow. There is no field to type a `client_id` into. Just give it the `/mcp`
URL and log in with the API key — nothing else to fill in. (DCR is enabled
whenever the OAuth shim is; the issued `client_id` is self-verifying so it
needs no server-side store and survives restarts.)

**ChatGPT connectors (manual client entry).** ChatGPT has no DCR; choose
**OAuth**, registration method **User-Defined OAuth Client**, and fill in:

| Field | Value |
| --- | --- |
| OAuth Client ID | value of `FREECAD_MCP_OAUTH_CLIENT_ID` |
| OAuth Client Secret | leave blank (public client, PKCE-only) |
| Token endpoint auth method | `none` |
| Auth URL | `https://freecad-mcp.hieudm.site/oauth/authorize` |
| Token URL | `https://freecad-mcp.hieudm.site/oauth/token` |
| Registration URL | leave blank |
| Authorization server base | `https://freecad-mcp.hieudm.site` |
| OIDC enabled | No |

`GET /.well-known/oauth-authorization-server` publishes RFC 8414 discovery
metadata (now including `registration_endpoint`), and
`GET /.well-known/oauth-protected-resource` publishes the RFC 9728 resource
metadata pointing at `/mcp` and this authorization server, for clients that
auto-discover instead of taking manually-entered endpoint URLs.

Neither client style is a security boundary on its own: the `client_id` is
public (a static configured one, or a DCR-issued one), token endpoint auth is
`none`, and access is gated entirely on the API key entered at the login page
plus PKCE. Verified end-to-end on both servers: DCR → authorize → token →
`tools/list` returns the full tool set (22 FreeCAD, 33 Blender).

**Note the exact path**: the MCP endpoint is `https://freecad-mcp.hieudm.site/mcp`,
not the bare domain — a client configured with just the domain gets `404` on
every tool call. Also, `mcp`'s `streamable_http_app()` has a DNS-rebinding
protection feature that rejects any request whose `Host` header isn't
`localhost`/`127.0.0.1` (`421 Invalid Host header`) unless explicitly
disabled — `_run_streamable_http` in `server.py` disables it, since every
request here is already authenticated by `ApiKeyMiddleware` regardless of
`Host`. Without that, **no remote client — OAuth or plain API key — can ever
reach `/mcp` through a real hostname**, which is easy to misdiagnose as an
auth or connector-configuration problem instead of what it is.

Render a model that's been exported to the shared volume:

```bash
docker compose --profile render run --rm render \
  --input /data/part.stl --output /data/part.png \
  --device AUTO --samples 128
```

**Export as a mesh (STL/OBJ/glTF/FBX), not STEP** — Blender has no STEP
importer, official builds included (`bpy.ops.import_scene.step` doesn't
exist at all; STEP is a B-rep exchange format, not something Blender's mesh
pipeline reads). From FreeCAD:
`doc.getObject(name).Shape.exportStl("/data/part.stl")` (or `.exportBrep`
only if you need the raw B-rep elsewhere — never for this render step).

**Output isn't limited to PNG** — `--output`'s extension picks the format,
and `.jpg`/`.jpeg`, `.tiff`/`.tif`, `.exr`, `.webp`, `.bmp`, `.tga` all work
(tested each end-to-end), alongside PNG. A naive `extension.upper()` isn't
enough, though: Blender's format enum doesn't always match the extension's
own spelling (`.jpg` → the enum is `"JPEG"`, not `"JPG"`; `.tif` →
`"TIFF"`; `.exr` → `"OPEN_EXR"` — each of those raises rather than
silently doing the wrong thing) — `_blender_file_format()` in `render.py`
has the override table. This is still a **single still image per
invocation** — no animation/video output (would need frame-range and
`FFMPEG` container/codec settings this script doesn't set up).

### GPU rendering (Cycles OptiX/CUDA)

`render.py` defaults to `--device AUTO`, trying OptiX then CUDA then falling
back to CPU. This needs the host to have `nvidia-container-toolkit` and the
`render` service in `docker-compose.yml` requesting a GPU device (both
already set up here) — confirmed working end-to-end by exporting a real
FreeCAD object, rendering it, visually checking the output image (not just
that a file appeared), and watching `nvidia-smi` during the run: **a simple
single-object scene used ~2.5 GB of VRAM** (`36144 MiB` baseline →
`38632 MiB` peak); scale that up for denser scenes.

Three non-obvious things this required:

1. Ubuntu's apt `blender` package (used originally) is a stripped-down
   "dfsg" rebuild missing non-free bits: **no STEP importer, no
   `OpenImageDenoiser`, and no working CUDA/OptiX Cycles kernels** —
   confirmed by actually running a render and watching `nvidia-smi` stay
   completely flat regardless of `--device`. Fixed by switching the
   Dockerfile to download the official blender.org binary instead of
   `apt-get install blender` (see the Dockerfile for why a mirror URL is
   used — `download.blender.org` itself sits behind a Cloudflare challenge
   that blocks a plain `curl`/`wget`).
2. Cycles' denoising is on by default and needs `OpenImageDenoiser`;
   `render.py` sets `scene.cycles.use_denoising = False` to avoid that
   crashing the render.
3. **Blender 4.2.1 LTS has no precompiled kernel for this GPU** (an RTX PRO
   5000 **Blackwell** — a very new architecture), so Cycles fell back to
   JIT-compiling its OptiX kernel at runtime — observed at ~5.5 minutes,
   every single render, not just the first (a `render_cache` volume +
   `CUDA_CACHE_PATH`/`OPTIX_CACHE_PATH` env vars aimed at persisting that
   compile across `docker compose run --rm` invocations did get populated
   on disk but didn't measurably help — the OptiX disk cache
   (`optix7cache.db`) stayed at its empty 1 MiB default size run after run,
   while the separate CUDA JIT cache did fill up with ~57 MB of real data,
   suggesting the OptiX-specific cache path wasn't actually the bottleneck
   or wasn't being hit for a reason not otherwise root-caused). **Fixed by
   upgrading to Blender 5.2.1 LTS**, which ships a newer OptiX SDK with a
   kernel that natively targets Blackwell: render time dropped to
   **2–4 seconds**, consistently, with no cache warm-up needed at all.
   `docker/render/Dockerfile`'s `ARG BLENDER_VERSION` controls this if a
   future GPU generation needs a newer Blender again.

**Also fixed, unrelated to any of the above**: the original camera-framing
code called `bpy.ops.view3d.camera_to_view_selected()` guarded by
`if bpy.context.area else None` — `bpy.context.area` is *always* `None`
under `blender -b` (background/headless mode), so that call silently
no-op'd on **every** render regardless of Blender version or GPU/CPU, and
the camera stayed at its hardcoded fixed position. Any model not
coincidentally sized/placed to fall inside that specific fixed framing
rendered as a solid black frame — a render that "succeeds" (valid PNG,
reasonable file size, no errors) can still be pointing at nothing. Caught
by actually opening the rendered image rather than just checking the file
existed. Fixed in `_frame_camera_and_light()` by computing the imported
objects' real world-space bounding box and placing/aiming the camera (and
sun light) from that, which works identically in background mode.

## FreeCAD version: 1.1.3 from the official AppImage

The `freecad` and `mcp` services both install **FreeCAD 1.1.3** by
downloading the official AppImage and running `--appimage-extract` on it
(mounting an AppImage needs FUSE, which an unprivileged container doesn't
have). `apt` is not an option: Debian trixie carries only `1.0.0+dfsg`, the
original November-2024 release, and has no backports.

Each Dockerfile pins the version in its own `ARG FC_VERSION` / `ARG FC_URL`.
**Bump both together** — the two services read and write the same `.FCStd`
files on the `/data` volume, and a document written by a newer FreeCAD and
then opened by an older one is how data quietly degrades.

Beyond being two years newer, the apt build was also *stripped*: it shipped
**no CalculiX (`ccx`) and no `gmsh` anywhere on the image**, so
`run_fem_analysis` had no solver and no mesher and could never have worked.
The AppImage bundles both. (Third time here that a distro package turned out
to be both old and missing pieces — see the Blender notes above.)

Cost: the images grow from 2.56 GB to 4.66 GB (`freecad`) and 4.86 GB
(`mcp`). The 783 MB download happens once per Dockerfile instead of being
shared, deliberately: sharing it would couple the two builds' ordering to
save a few GB on a 1.4 TB disk.

### Upgrading safely

The 1.0 → 1.1.3 move was rehearsed in a throwaway container with `/data`
mounted read-only before anything live was touched, and all 16 RPC methods
passed. Two results worth keeping in mind for the next bump:

- **The camera code is the fragile part.** `view_manager.py` parses
  `view.getCamera()`'s Coin3D string with a regex. Verify a bump by checking
  the camera *position actually moves* and the screenshot bytes change — not
  by trusting `{"success": true}`, which an orbit will happily return while
  doing nothing.
- **Documents round-trip both ways** between 1.0 and 1.1.3 with identical
  geometry, so a rollback doesn't strand work. Opening a 1.0 document in 1.1
  does add one `App::Point` named `Origin001` to the document's `Origin`
  group (1.1's `App::Origin` gained an origin point); it's legitimate, 1.0
  ignores it on the way back, but it makes `get_objects` return one more
  object than before.

If `get_view` starts answering "Cannot get screenshot in the current view
type", the active document's `ActiveView` is a TechDraw page or spreadsheet
(`MDIViewPagePy`), not a 3D view — the addon is correct to refuse.
`FreeCADGui.setActiveDocument()` onto a document with a 3D view fixes it.
Note that `activate_document` exists only as an addon RPC method and is
**not** exposed as an MCP tool, so a connected model currently cannot do
this for itself.

## FreeCAD API reference for the connected model

`get_freecad_api_reference(topic)` returns real FreeCAD Python API
documentation, so the model writing `execute_code` scripts isn't recalling
the API from memory. Topics: `index`, `fundamentals`, `geometry`,
`parametric`, `advanced` (~3–11 KB each; served one at a time rather than as
one ~36 KB blob).

The text is vendored verbatim from [github/awesome-copilot][ac]'s
`freecad-scripts` skill (MIT) into `src/freecad_mcp/reference/`. Its
`gui-and-interface.md` is **not** vendored: it documents PySide dialogs,
`QMessageBox` and `Gui.Control.showDialog()`, which assume a human at a
FreeCAD window. Here FreeCAD runs under Xvfb with nobody to dismiss a modal
dialog, so such code blocks indefinitely and can wedge the RPC connection
these tools run over.

[ac]: https://github.com/github/awesome-copilot/tree/main/skills/freecad-scripts

What upstream can't know lives in `DEPLOYMENT_RULES`
(`src/freecad_mcp/api_reference.py`) and is returned with `index` (plus a
one-line reminder on every topic): no dialogs, `/data` is the only writable
path, `/data` is *not* object storage (call the storage tools), documents
live in RAM until saved, always `doc.recompute()`, units are mm,
`Shape.exportStl` rather than STEP for Blender, `setActiveDocument` not
`activateDocument`, and `include_screenshot=False` on intermediate steps.

**It's a tool rather than an MCP prompt deliberately.** This server also
exposes an `asset_creation_strategy` prompt, but connector-style clients
(ChatGPT's connector UI) don't surface MCP prompts at all, so that guidance
plausibly never reaches the model — tools are the only channel every client
uses. (That prompt's `description` was also empty, since FastMCP reads it
from the function docstring and there wasn't one; it now has one.)

Verified against the FreeCAD 1.0.0 actually running here rather than taken
on trust: every `Part.make*` and shape method the docs use exists,
`Units.Quantity` and `Part::Feature` + `recompute` behave as documented, a
boolean cut produced the geometrically correct volume, and the docs use
`App.Vector` correctly. The deciding spot-check was that they say
`setActiveDocument` and never `activateDocument` — the exact call this
project originally got wrong by guessing.

## Live preview page

> **Disabled by default.** The routes below are only served when
> `FREECAD_MCP_PREVIEW=1` is set on the `mcp` service — plus
> `BLENDER_MCP_PREVIEW=1` on `blender-mcp` for the Blender tab, since that
> half serves the viewport routes this one proxies to. Both are `0` in
> `docker-compose.yml`. With it off every `/preview*` path answers 401,
> including with a valid key, and the MCP tools are completely unaffected
> (verified: 22 FreeCAD tools and 33 Blender tools still list over the public
> HTTPS endpoints, and OAuth discovery still answers 200 unauthenticated).
>
> It's off because an open preview tab polls a screenshot continuously and a
> screenshot is never free — see the pacing and cost measurements below.
> Turning it on is an `docker compose up -d mcp blender-mcp` away; no rebuild
> needed, the code stays in the image either way.

`GET /preview?key=<FREECAD_MCP_API_KEY>` serves a small self-refreshing HTML
page showing a screenshot of whatever's currently the active document/view
in the running FreeCAD instance (the same call the `get_view` tool uses),
updating every ~2 seconds. It's meant for a human to keep open in a browser
while an MCP client (a person, ChatGPT, etc.) drives FreeCAD, to watch
changes land without repeatedly calling `get_view` yourself. Two tabs —
**FreeCAD** and **Blender** — share the page; only the visible one polls.

The image itself is directly mouse/touch-controllable — no view-angle
dropdown:
- **Drag** orbits the camera (`POST /preview/orbit?dx=&dy=`, degrees per
  pixel dragged).
- **Scroll/pinch** zooms (`POST /preview/zoom?factor=`, <1 zooms in).
- **Reset view** button snaps back to a canned Isometric orientation
  (`POST /preview/reset`).

These map to three new RPC methods on the addon side
(`addon/FreeCADMCP/rpc_server/rpc_server.py` /
`addon/FreeCADMCP/rpc_server/view_manager.py`): `orbit_camera` (a
"turntable"-style rotation — yaw around the world Z axis, pitch around the
camera's current right axis — followed by `fitAll()` with the previous zoom
level restored, so the model stays framed as it orbits instead of drifting
off-screen or the zoom resetting), `zoom_camera` (rescales the camera's
`height`/`heightAngle` field, parsed out of `view.getCamera()`'s Coin3D
string), and `reset_view` (forces a canned orientation via the existing
`apply_view_orientation` + `fitAll()`).

**Important**: `get_active_screenshot`'s `view_name` can now be `None` (not
just a canned name), meaning "capture whatever the camera currently is,
don't touch it." `/preview.png` always polls with `view_name=None` — every
other caller (the `get_view` MCP tool, `create_object`'s screenshot, etc.)
still passes a real name and behaves exactly as before. This distinction is
load-bearing: `save_active_screenshot()` used to call `view.fitAll()`
unconditionally on every single capture, which would have silently undone
any orbit/zoom the preview page set on its very next 2-second poll. Skipping
that fit (and the orientation-forcing) precisely when `view_name` is falsy
is what lets a drag/zoom persist across polls.

FreeCAD only has one *active* document at a time (screenshots always show
that one), so with more than one document open the page also has a
**Project** dropdown to switch which one is active — same effect as clicking
a document tab in the desktop GUI. This calls `activate_document`
(`FreeCADGui.setActiveDocument(name)`; there's also a matching read-only
`get_active_document`), exposed as `POST /preview/activate?doc=`. Switching
the active document (like orbiting/zooming) is global GUI state — it
affects what any other client doing `get_view` sees too, not just this page.

None of this is part of the MCP protocol — just extra Starlette routes
(`/preview`, `/preview.png`, `/preview/*`) registered alongside the `/mcp`
endpoint in `_run_streamable_http`. They check the API key as a `?key=`
query parameter instead of a header (a browser `<img>`/`fetch` from a plain
page can't set custom headers), so the whole `/preview` prefix is exempted
from `ApiKeyMiddleware`'s header check in `http_auth.py` — see
`src/freecad_mcp/preview.py` for the full implementation.

### The Blender tab

The same page has a second tab showing the live Blender viewport (the
`blender` container's GUI, see "Blender MCP integration" below), with the
same drag-to-orbit / scroll-to-zoom controls plus:

- a **Shading** dropdown — Solid / Material / Rendered / Wireframe, i.e.
  Blender's own viewport shading modes, so materials and lighting can be
  checked without doing a full Cycles render;
- a **Camera view** button — snaps the viewport back to what the scene
  camera sees, i.e. the framing an actual render will use.

Only the FreeCAD server serves HTML. The Blender viewport routes live on the
*blender-mcp* server (`docker/blender-mcp/preview_api.py`, same route names)
and this server **proxies** them at `/preview/blender*` via
`BLENDER_MCP_URL` (set in docker-compose.yml). That way the browser talks to
a single origin — no CORS for the control POSTs — and `BLENDER_MCP_API_KEY`
never leaves the server. Unset that variable and the tab simply doesn't
render.

**Frame polling is self-paced, deliberately** — don't replace it with a flat
`setInterval`. The first version refreshed every 2000 ms regardless of how
long a frame took, and at the time a Blender capture cost ~2.4 s, so it
asked for frames faster than Blender could produce them: the addon's single
command queue grew without bound, and every other call — the page's own
status poll, and any tool call from an MCP client — sat behind that backlog.
The symptom was Blender appearing to hang (a `/preview/blender/status` call
timing out after 30 s) while the container burned ~1500% CPU doing nothing
but screenshots, for a browser tab nobody was necessarily even looking at.

Each request is now chained off the previous frame's `load`/`error`, so
exactly one capture is ever in flight, and polling stops entirely while
`document.hidden`. The gap between frames is two-speed: ~900 ms while the
page is merely being *watched*, dropping to 120 ms for two seconds after the
user actually drags/scrolls/clicks, and never less than half the measured
frame cost so an expensive backend backs off further on its own. The
measured cost and resulting frame rate are shown under the image. An
already-open browser tab keeps running the old script until it's reloaded.

A **⏸ Live / ▶ Paused** button in the header turns the automatic refresh off
outright — the one guaranteed way to take the load off FreeCAD/Blender
without closing the page. Pausing doesn't freeze the page: every user action
(orbit, zoom, reset, camera view, switching project or shading) still fetches
exactly one frame, so a paused page stays usable, just not self-updating. A
manual request that arrives while a capture is already running is remembered
and run immediately after rather than started alongside it — two at once is
the pile-up this pacing exists to prevent. The choice is remembered per
browser in `localStorage` (wrapped in try/catch; a private window that
refuses storage just starts live), and the status line keeps polling either
way, since it's cheap and it's what tells you the app is still alive while
the image is paused.

The two speeds exist because a capture is never free, even on the GPU —
measured per frame at 1000x858 on a real scene:

| | CPU per frame | continuous cost |
|---|---|---|
| software GL (llvmpipe), as it shipped | 19.89 core-s | **15.9 cores** |
| hardware GL, polled flat out | 0.162 core-s | 2.0 cores |
| hardware GL, watching (~1 fps) | 0.162 core-s | **0.17 cores** |
| hardware GL, during a drag (~10 fps) | 0.162 core-s | 1.6 cores (transient) |

The GPU removes the *draw* but not the pixel read-back, the float conversion
or the PNG encode, which is why a frame still costs ~0.16 CPU-seconds and
why the idle rate matters. Blender's RSS is unaffected: it rises ~13 MiB on
the first capture, then stays flat across dozens of frames and returns when
idle — the buffers are reused, not leaked (verified over 36 consecutive
frames). The `blender-mcp` proxy container's own cost is unmeasurable
(0.11 CPU-seconds over a 30-second flat-out run).

### Why the Blender viewport was 30x slower than it needed to be

`docker/blender/entrypoint.sh` used to `export LIBGL_ALWAYS_SOFTWARE=1`,
pinning every viewport draw to Mesa's llvmpipe software rasteriser. Measured
on a real scene (1048x900 viewport, 1000x858 capture, 6 objects), broken
down stage by stage — the GL draw is essentially the whole cost, and the
pixel read-back, the numpy conversion and the PNG encode together account
for under 0.06 s:

| capture | llvmpipe (CPU) | NVIDIA GLX (GPU) |
|---|---|---|
| MATERIAL shading | 2.13 s | **0.079 s** |
| SOLID shading | 0.28 s | **0.028 s** |

Note that llvmpipe's MATERIAL cost barely moved with resolution (2.13 s at
1000 px vs 1.83 s at 400 px) — it's EEVEE's per-frame shading work, not the
pixel count, so shrinking the image was never going to fix it.

The container already had the GPU and NVIDIA's GL libraries
(`NVIDIA_DRIVER_CAPABILITIES` includes `graphics`), but libglvnd still
selects Mesa's GLX by default because that's what Xvfb's X server
advertises. Naming the vendor explicitly —
`__GLX_VENDOR_LIBRARY_NAME=nvidia` — routes GLX to the real GPU even under
Xvfb. The entrypoint sets it only when `libGLX_nvidia.so.0` is actually
present and otherwise keeps the software fallback, since forcing the nvidia
vendor on a host without those libraries leaves Blender with no usable GL
at all rather than a slow one. (Blender's Vulkan backend,
`--gpu-backend vulkan`, also reaches the GPU once the host's
`nvidia_icd.json` is mounted, but GLX needs no extra mounts.)

`docker/freecad/entrypoint.sh` still forces software GL, and the `freecad`
service has no GPU reservation. That one is worse than it looks: measured
over 30 seconds with **nothing at all calling it**, the `freecad` container
burns 22.2 CPU-seconds — **0.74 cores, continuously, around the clock** —
and `top -H` on it shows dozens of `llvmpipe` worker threads that have each
accumulated ~40 minutes of CPU time. FreeCAD redraws its viewport on a loop
whether or not anyone is watching, and software GL makes each redraw
expensive. Giving that service the same `__GLX_VENDOR_LIBRARY_NAME=nvidia`
treatment would need a GPU reservation added to it in docker-compose.yml.

Two more Blender-specific behaviours worth knowing, both discovered by
looking at the returned pixels rather than the HTTP status:

- A scene set up for rendering usually has the viewport locked to the scene
  camera (`region_3d.view_perspective == 'CAMERA'`), and in that mode
  `view_rotation`/`view_distance` aren't what's on screen. The first working
  version of orbit therefore returned `{"success": true}` while the
  screenshot came back byte-identical every time. Orbit/zoom now leave
  camera view first, exactly as Blender's own viewport does — and the free
  view has to be *re-seeded from the geometry*, because `view_distance`
  while in camera view still holds whatever the last free view left there
  (Blender's 14.71-unit startup default, which in a 7 cm scene aimed the
  viewport at empty space ~15 m away).
- **Reset view** frames the *selected* objects when there's a selection and
  only falls back to `view_all()` otherwise. A studio scene has a backdrop
  plane orders of magnitude larger than the subject (2 m vs a 7 cm gear
  here), so a plain "frame all" fills the viewport with backdrop and leaves
  the model a speck.

## Blender MCP integration

**`https://blender-mcp.hieudm.site`** gives an MCP client live control of a
real, running Blender scene — not just the one-shot batch rendering above.
It's [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp) (MIT
license), which exposes tools like `execute_blender_code`, `get_scene_info`,
`get_object_info`, and `get_viewport_screenshot`, plus optional asset
integrations (Poly Haven, Sketchfab, Hyper3D Rodin, Hunyuan3D, Poly Pizza —
all **off by default** here, unchanged from upstream's own defaults).

**Registering it as a ChatGPT connector** works exactly like freecad-mcp
(see "Connecting a client that only supports OAuth" above) — use
`BLENDER_MCP_OAUTH_CLIENT_ID` from `.env` as the Client ID, and
`https://blender-mcp.hieudm.site/oauth/authorize` /
`.../oauth/token` as the endpoints.

### Why this needed two new containers, and three fixes

`blender-mcp`'s addon needs a real Blender GUI process — its `start()`
explicitly refuses to run under `blender -b` (background mode; its socket
server depends on `bpy.app.timers`, which needs Blender's normal event loop
running). `docker/blender/Dockerfile` runs the same official Blender 5.2.1
binary as `render` (for the same reasons — see the GPU rendering section
above), under a manually-started `Xvfb` (the same `xvfb-run` hang applies
here too), with `docker/blender/addon.py` — a vendored copy of upstream's
`addon.py` — dropped into the addons folder and enabled at startup via
`docker/blender/startup.py`. Upstream's `blendermcp_auto_start_server`
already defaults to `True`, so enabling the addon is all that's needed; no
FreeCAD-style manual auto-start scripting was required.

`docker/blender-mcp/Dockerfile` is a second, separate container: a slim
Python image with `pip install blender-mcp` (unmodified — imported as a
library from `run_http.py`, not forked, since upstream's `server.py` is
~1800 lines and this only needs to add an HTTP transport around the
`FastMCP` instance it already builds). Split into two containers rather
than one for the same reason as `freecad`/`mcp`: very different dependency
footprints (CUDA + Xvfb + Blender vs. a slim Python + one pip install), and
restarting the HTTP wrapper (e.g. after an `oauth.py` tweak) shouldn't have
to restart the GUI Blender process, which holds all in-memory scene state —
losing that on every unrelated restart would be the same footgun as
FreeCAD's own documents, which likewise live only in RAM until explicitly
saved — every rebuild of the `freecad` container during this project's
development wiped whatever was open, more than once.

Three problems surfaced getting this actually working, each confirmed by
driving the real integration rather than just checking it started cleanly:

1. **The addon's socket only listened on `localhost`** inside the `blender`
   container by default (upstream's `BlenderMCPServer.__init__(self,
   host='localhost', ...)`), invisible to `blender-mcp` connecting over the
   Docker network as `blender:9876`. Fixed by changing that one default to
   `'0.0.0.0'` in the vendored `addon.py` (search it for "Vendored fork" —
   that's the only intentional change from upstream).
2. **DNS-rebinding protection** (the same `mcp` SDK feature documented in
   the "streamable_http_app() auto-enables DNS-rebinding protection" issue
   above) applies here too, but with a different fix shape: the `mcp`
   version this container resolves (1.30.0, from blender-mcp's own
   `mcp>=1.9.0,<2` pin) exposes it as `FastMCP(transport_security=...)`, a
   constructor argument — not a `streamable_http_app()` keyword like
   freecad-mcp's older resolved version. Since `blender_mcp.server` already
   constructs its `FastMCP` instance for us, `run_http.py` mutates it
   post-construction instead:
   `mcp.settings.transport_security.enable_dns_rebinding_protection = False`.
3. **`get_viewport_screenshot` failed** with "Screenshot file was not
   created" — not a logic bug, a direct consequence of splitting Blender
   and its MCP server into separate containers (a split upstream's own
   design doesn't anticipate, since their own Docker instructions assume
   Blender runs on the host machine, not in a container at all). The MCP
   server computes a temp file path with `tempfile.gettempdir()` in its own
   container, sends that path to the addon over the socket, and the addon
   writes the screenshot there — in the **other** container's separate
   filesystem, so the MCP server's `os.path.exists()` check on its own side
   always failed even though the file really was written. Fixed with a
   shared `blender_tmp` volume mounted at the identical path
   (`/tmp/blendermcp`) in both `blender` and `blender-mcp`, `TMPDIR` set to
   that path in both. Confirmed fixed by actually creating a red-material
   sphere via `execute_blender_code` and visually inspecting a real
   `get_viewport_screenshot` PNG (twice — once in default "Solid" viewport
   shading, where materials are invisible by design, and again after
   switching to `'MATERIAL'` shading via another `execute_blender_code`
   call, which showed the correct red, lit sphere).

### The live instance rendered on CPU (fixed in `startup.py`)

The "GPU rendering" section above is about `docker/render/render.py`, the
one-shot batch service. The persistent `blender` container got the same GPU
reservation in `docker-compose.yml` but nothing ever configured Cycles in
it — Blender's default `compute_device_type` is `NONE` — so every render an
MCP client drove ran on the CPU and used **zero VRAM**. Nothing errors and
the output images are correct, which is why this went unnoticed for a day;
what gave it away was that `nvidia-smi` didn't list the Blender process at
all while renders were being produced.

Measured on a real scene (2000×1700, 128 Cycles samples, one wooden gear on
a studio backdrop, denoising off on both sides for a like-for-like
comparison):

| | time | VRAM |
|---|---|---|
| CPU (Threadripper 9960X, 24 cores) | 21.5 s | none |
| GPU, OptiX, cold | 3.6 s | +2.5 GB, 99% util |
| GPU, OptiX, warm | 2.8 s | +2.5 GB, 99% util |

`docker/blender/startup.py` now selects a backend (same
try-it-and-catch-`TypeError` approach as `render.py`, for the same reason)
and sets `scene.cycles.device = 'GPU'`. It also registers a `load_post`
handler: the backend and enabled-devices choices live in preferences and
survive a file load, but `scene.cycles.device` is stored **inside the
.blend**, so opening any file saved before this change — including the ones
already in MinIO — would silently put renders back on the CPU.

Applying it needs a `docker compose up -d blender`, which **discards the
in-memory scene** of the running instance (save to MinIO first). A running
instance can be switched without a restart by setting the same preferences
through `execute_blender_code`.

## Object storage (MinIO)

**A FreeCAD document or a Blender scene exists only in that app's process
memory until something writes it to disk.** Nothing in this deployment did
that automatically, so every `docker compose up --build` of `freecad` or
`blender` — routine during development — silently threw away whatever was
open. The `minio` service plus the storage tools below are what make work
survive that, and make it reachable from outside Docker at all.

Two layers, don't confuse them:

- **`/data`** (the `freecad_data` volume) — a *staging area* shared by every
  container. Persistent across restarts, but invisible from outside Docker
  and easy to overwrite. Exports, renders and the working copies of saved
  files live here.
- **the MinIO bucket** (`cad` by default) — the actual archive. Browsable and
  downloadable from a browser, independent of any container's lifetime.

### Tools

Registered on **both** MCP servers, but only when `MINIO_ENDPOINT`,
`MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` are set (docker-compose.yml sets
them from `.env`) — without those the servers expose exactly the tool set
they had before, so the stdio/local path is unaffected.

| FreeCAD MCP | Blender MCP | Does |
| --- | --- | --- |
| `save_document_to_storage` | `save_blend_to_storage` | Save the live document/scene into the bucket (`freecad/<name>.FCStd`, `blender/<name>.blend`) |
| `load_document_from_storage` | `load_blend_from_storage` | Fetch it back and open it |
| `list_storage_files` | `list_storage_files` | List what's stored (one shared bucket — see below) |
| `upload_file_to_storage` | `upload_file_to_storage` | Archive any file already in `/data` (an export, a render) |
| `download_file_from_storage` | `download_file_from_storage` | Pull a stored file into `/data` |

Both servers use **the same bucket**, so a mesh exported from FreeCAD is
listed and importable on the Blender side and vice versa — verified
end-to-end: exported a `Part::Box` to STL, uploaded it, listed it from the
Blender server, downloaded and imported it. Watch the units when you do
that: FreeCAD works in millimetres and Blender in metres, so a 33 mm box
imports as a 33-unit object that dwarfs a 2-unit torus next to it.

`upload_file_to_storage`/`download_file_from_storage` refuse any path
outside `/data` (`Path must be inside /data`) — object storage shouldn't
double as a way to read arbitrary container files out, or drop files into
arbitrary locations in.

### Getting at the files yourself

The web console is on **`http://127.0.0.1:9001`** (loopback only — it carries
the root credentials and full read/write access to every saved project), log
in with `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` from `.env`. From another
machine, tunnel it: `ssh -L 9001:127.0.0.1:9001 <server>`. The S3 API is on
`127.0.0.1:9000` for `mc`, `aws s3`, rclone, boto3 and friends. Nothing is
published beyond loopback and nothing is on the Cloudflare tunnel — expose
it further only deliberately.

Implementation: `src/freecad_mcp/storage.py` holds the client/bucket/path
rules and is deliberately free of FreeCAD imports, so the blender-mcp image
COPYs that *same file* rather than keeping a second copy that could drift
(`docker/blender-mcp/Dockerfile`). The tool layers on top differ per server:
`src/freecad_mcp/storage_tools.py` and `docker/blender-mcp/storage_tools.py`.

## Local/stdio usage is unaffected

`--transport` defaults to `stdio`; running `freecad-mcp` directly (uvx, or
from a checkout) behaves exactly as before. `--transport streamable-http` is
only used inside the `mcp` container.

## Keeping this fork mergeable with upstream

This fork tracks `neka-nat/freecad-mcp` for FreeCAD/MCP feature updates,
while keeping the Docker setup on top. One-time setup:

```bash
git remote add upstream https://github.com/neka-nat/freecad-mcp.git
```

To pull upstream changes:

```bash
git fetch upstream
git merge upstream/main
docker compose build   # on the server, after pushing/pulling the merge
```

Everything Docker-specific lives in files upstream doesn't have (`docker/`,
`docker-compose.yml`, `.env.example`, `.dockerignore`, this doc), so they
won't conflict. `src/freecad_mcp/server.py` and `pyproject.toml` gained a
small, additive diff (a new `--transport`/`--port` branch in `main()`, one new
dependency) to keep any future conflict there small and easy to resolve.
