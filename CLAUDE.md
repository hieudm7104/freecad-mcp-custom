# CLAUDE.md — Project Conventions for freecad-mcp-custom

This is a fork of `neka-nat/freecad-mcp` that adds a Docker deployment
(MCP over HTTP with API-key auth, headless FreeCAD, on-demand Blender
render, and a live-controlled Blender instance via a vendored
`ahujasid/blender-mcp`) on top of upstream. See
[`docs/docker.md`](docs/docker.md) for the full architecture and
[`README.md`](README.md) for the general project.

## Live deployment on this server

- Public endpoint: **`https://freecad-mcp.hieudm.site`** (MCP-over-HTTP,
  `X-API-Key` or `Authorization: Bearer` header required).
- Reached via the shared Cloudflare Tunnel in
  `../cloudflared/config/config.yml` — that tunnel also serves 5 unrelated
  domains (`beszel`, `new-api`, `websearch`, `dsh`, `chat`) on this same host,
  so any change to `cloudflared`'s config or a `docker restart cloudflared`
  affects all of them, not just this project. Validate before restarting:
  `docker exec cloudflared cloudflared tunnel ingress validate`.
- `MCP_PORT` in `.env` is **`8001`**, not `8000` — `8000` is already reserved
  on this host for the `websearch.hieudm.site` route on the same tunnel (see
  `docs/docker.md`). Don't "fix" this back to 8000 without checking that
  route first.
- **MinIO** (`minio` service, bucket `cad`) is where saved FreeCAD documents
  and Blender scenes actually persist — both MCP servers get save/load tools
  backed by it. Console on `127.0.0.1:9001`, S3 API on `127.0.0.1:9000`,
  loopback only (root credentials, full access to every project). See the
  "Object storage" section below and `docs/docker.md`.
- **`https://blender-mcp.hieudm.site`** is a second, separate MCP server —
  a vendored `ahujasid/blender-mcp` fork giving live control of a real,
  running Blender GUI instance (create/edit objects, materials, lights,
  arbitrary Python, viewport screenshots), not the one-shot batch
  `render`/`render.py` service. Same API-key/OAuth pattern as freecad-mcp,
  on `BLENDER_MCP_PORT` (`8002`) → see the "Blender MCP integration" section
  below for the whole story (why two containers, what broke, what's
  disabled by default).
- **The live preview page is DISABLED on this deployment** (2026-09-13, at the
  user's request). `FREECAD_MCP_PREVIEW=0` on the `mcp` service and
  `BLENDER_MCP_PREVIEW=0` on `blender-mcp` in `docker-compose.yml`; with those
  off the routes aren't registered *and* `ApiKeyMiddleware` drops its
  `/preview` exemption, so every `/preview*` path answers 401 even with a
  valid key. Flip both to `1` and `docker compose up -d mcp blender-mcp` to
  bring it back — no rebuild, the code ships in the image regardless. MCP
  tools are unaffected either way (verified after disabling: 22 FreeCAD / 33
  Blender tools still list over the public HTTPS endpoints, OAuth discovery
  still 200s unauthenticated). Everything below describes it as it works when
  enabled.
- **`https://freecad-mcp.hieudm.site/preview?key=<FREECAD_MCP_API_KEY>`** is a
  human-facing live-view page (`src/freecad_mcp/preview.py`) — auto-refreshing
  screenshot of whatever's currently active in FreeCAD. No view-angle
  dropdown: the image itself is mouse/touch-controlled — **drag to orbit**
  (`POST /preview/orbit?dx=&dy=`), **scroll to zoom**
  (`POST /preview/zoom?factor=`), and a **Reset view** button
  (`POST /preview/reset`) to snap back to Isometric. Also a **Project
  dropdown to switch the active document** (multiple documents can be open —
  FreeCAD only screenshots the active one), and an open-documents status
  line. Gated by `?key=` (not a header, since `<img>`/`fetch` from a plain
  page can't set one) checked against the same `FREECAD_MCP_API_KEY` — the
  whole `/preview` prefix is exempt from `ApiKeyMiddleware`'s header check
  for that reason. Not an MCP tool — just a convenience page to watch changes
  land while an MCP client drives FreeCAD.

  The page has a **second tab showing the live Blender viewport** (same
  drag/scroll controls, plus a Shading dropdown and a "Camera view" button).
  Those viewport routes live on the *blender-mcp* server
  (`docker/blender-mcp/preview_api.py`, identical route names) and this
  server **proxies** them at `/preview/blender*` using `BLENDER_MCP_URL` /
  `BLENDER_MCP_API_KEY` from docker-compose.yml — so the browser stays on one
  origin (no CORS on the control POSTs) and the Blender key never reaches it.
  Unset `BLENDER_MCP_API_KEY` on the `mcp` service and the tab disappears.

  **Frame polling is self-paced on purpose** — don't "simplify" it back to a
  `setInterval`. The original flat 2-second timer asked for frames faster
  than Blender could produce them (a capture cost ~2.4 s at the time). The
  addon has a single command queue, so the backlog blocked *everything*
  else — the page's own status polls and any MCP client's tool calls —
  making Blender look hung (a status call timing out at 30 s) while the
  container sat at ~1500% CPU rendering screenshots for a tab nobody was
  necessarily looking at. Each request is now chained off the previous
  frame's `load`/`error` (exactly one in flight), polling stops while
  `document.hidden`, a **⏸ Live / ▶ Paused** header button stops it on
  demand (paused still serves one frame per user action, so the page stays
  usable; choice remembered in `localStorage`), and the gap is two-speed:
  ~900 ms while the page is just being watched, 120 ms for 2 s after the
  user drags/scrolls/clicks, never below half the measured frame cost.
  Measured per frame (1000x858,
  real scene): **19.89 CPU-core-seconds on software GL** → 15.9 cores if
  polled continuously; **0.162 on hardware GL** → 0.17 cores while watching
  (~1 fps), 1.6 cores during a drag (~10 fps). The GPU removes the draw but
  not the read-back/float-conversion/PNG-encode, which is why the idle rate
  still matters. No memory leak — RSS rises ~13 MiB on the first capture
  then stays flat over dozens of frames and returns when idle. An
  already-open browser tab keeps running the old script until reloaded.

  **The Blender viewport was on software GL.** `docker/blender/entrypoint.sh`
  used to `export LIBGL_ALWAYS_SOFTWARE=1`, so Mesa llvmpipe rendered every
  capture on the CPU: 2.13 s in MATERIAL shading, 0.28 s in SOLID (measured
  at 1000x858 on a real scene; the GL draw is essentially the entire cost —
  read-back + numpy + PNG encode together are under 0.06 s). Lowering the
  resolution barely helped (1.83 s at 400 px) because it's EEVEE's per-frame
  shading work, not pixel count. The container already had the GPU and
  NVIDIA's GL libraries, but libglvnd picks Mesa's GLX by default since
  that's what Xvfb advertises; naming the vendor explicitly with
  `__GLX_VENDOR_LIBRARY_NAME=nvidia` routes GLX to the real GPU even under
  Xvfb → **0.079 s MATERIAL / 0.028 s SOLID**, a 27x speedup. The entrypoint
  sets it only when `libGLX_nvidia.so.0` exists and otherwise keeps the
  software fallback — forcing the nvidia vendor without those libraries
  leaves Blender with *no* usable GL rather than a slow one.
  `docker/freecad/entrypoint.sh` still forces software GL and the `freecad`
  service has no GPU reservation. Worth knowing: measured over 30 s with
  **nothing calling it at all**, that container burns 22.2 CPU-seconds —
  **0.74 cores continuously, 24/7** — and `top -H` shows dozens of
  `llvmpipe` threads with ~40 minutes of accumulated CPU each. FreeCAD
  redraws its viewport on a loop regardless of whether anyone is watching,
  and software GL makes each redraw expensive. Same one-line fix would
  apply, but that service needs a GPU reservation adding first.
  Two Blender gotchas, both found by looking at the pixels and not the HTTP
  status: (1) a render-ready scene has the viewport locked to the scene
  camera, where `view_rotation`/`view_distance` aren't what's on screen —
  orbit returned `success: true` and a byte-identical screenshot until it
  started leaving camera view first, re-seeding the free view's pivot *from
  the geometry* (the stale `view_distance` is Blender's 14.71-unit startup
  default, which in a 7 cm scene aims at empty space 15 m away); (2) Reset
  frames the *selected* objects when there is a selection, because a studio
  scene's backdrop plane is far larger than the subject and plain
  `view_all()` leaves the model a speck.

  The camera-control RPCs (`orbit_camera`, `zoom_camera`, `reset_view`,
  `activate_document`/`get_active_document`, all in
  `addon/FreeCADMCP/rpc_server/rpc_server.py` and
  `addon/FreeCADMCP/rpc_server/view_manager.py`) needed
  `get_active_screenshot`'s `view_name` to become optional (`None` = "leave
  the camera alone, just capture whatever it currently is"). This matters
  because `save_active_screenshot()` used to call `view.fitAll()`
  **unconditionally on every single screenshot** — since `/preview.png` is
  polled every ~2 seconds, any orbit/zoom the preview page set would have
  been silently wiped out on the very next poll if that fit weren't gated on
  `view_name` being truthy. Every other caller (the `get_view` tool,
  `create_object`'s screenshot, etc.) still always passes a real view name
  and is unaffected.

  `FreeCADGui.setActiveDocument(name)` — used by `activate_document` — is
  **not** `activateDocument`, which doesn't exist on `FreeCADGui` and was
  the first (wrong) guess; found the real name by running
  `dir(FreeCADGui)` through `execute_code` against the live GUI process,
  since `freecadcmd` (headless) doesn't fully initialize `FreeCADGui`'s
  document/view attributes to introspect offline. The orbit math (turntable
  yaw-around-world-Z + pitch-around-camera's-current-right-axis, via
  `FreeCAD.Rotation`) and the zoom math (regex-rescaling the `height`/
  `heightAngle` field out of `view.getCamera()`'s Coin3D camera string) were
  both verified empirically — confirmed a `dx=90` orbit call actually
  rotated the model (not a no-op) by watching a cone's visible seam line
  move roughly a quarter-turn between before/after screenshots, since a
  cone's silhouette alone doesn't reveal rotation around its own axis.
- `FREECAD_MCP_OAUTH_CLIENT_ID` is set, enabling the OAuth shim in
  `src/freecad_mcp/oauth.py` (see "Connecting a client that only supports
  OAuth" in `docs/docker.md`) — added specifically so ChatGPT's Connector UI
  (which only offers "No auth" or real OAuth, never a static API key) can
  reach this server. `.env` must use LF line endings, not CRLF — a stray
  trailing `\r` on that line once caused client-id comparisons to fail in a
  way that looked like an auth bug (see below).

## `.env` line endings

If `.env` ever ends up with CRLF (`\r\n`) line endings again (e.g. from a
Windows editor or a copy-paste), `docker compose` itself parses it fine —
but any shell command that does `grep VAR .env | cut -d= -f2` to extract a
value for manual testing will pick up a trailing `\r`, and comparisons
against that value (e.g. `client_id` in the OAuth shim) will mysteriously
fail. `sed -i 's/\r$//' .env` fixes it. Verify with `cat -A .env`.

## Things that broke during the Docker build and why (2026-09-12)

The upstream `docs/docker.md` predates some upstream Debian/Ubuntu package
changes. If a future FreeCAD/Debian bump breaks the build again, these are
the failure modes already hit once:

1. `freecad-cmd` (used in `docker/mcp/Dockerfile`, base `python:3.12-slim` →
   Debian trixie) no longer exists as a package. Use `freecad-python3`
   instead; its binary is `freecadcmd-python3`, symlinked to `freecadcmd` so
   `freecad-mcp`'s auto-detect (`src/freecad_mcp/headless.py`) finds it.
2. Ubuntu 24.04 (`ubuntu:24.04`) dropped the `freecad` package entirely — no
   installation candidate. `docker/freecad/Dockerfile` now uses
   `debian:trixie-slim` instead (matches the FreeCAD 1.0.0 version already
   used by the `mcp` container's `freecad-python3`).
3. On `debian:trixie-slim`, `xvfb-run`'s SIGUSR1 ready-handshake with `Xvfb`
   never fires — the container reports "Up" and never crashes, but FreeCAD
   is simply never launched (silent hang, no log output). Root-caused by
   checking `/proc` inside the container for a live `freecad-python3`
   process while `docker compose ps` showed "Up" — there wasn't one, only
   `xvfb-run` and `Xvfb` sitting idle. Fixed by having
   `docker/freecad/entrypoint.sh` start `Xvfb` itself and poll for its
   `/tmp/.X11-unix/X99` socket instead of going through `xvfb-run`.
4. The `mcp` SDK's `streamable_http_app()` auto-enables DNS-rebinding
   protection (`allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]`)
   whenever `host` isn't passed explicitly — which was always the case here,
   since the container binds `0.0.0.0` but the code never told the SDK that.
   Every request through any real hostname (the Cloudflare Tunnel domain,
   but also plain `curl http://<server-ip>:8001/mcp`) got rejected with
   `421 Invalid Host header`, regardless of a valid API key/OAuth token —
   this was **the actual reason ChatGPT saw zero tools**, not the OAuth shim
   or the connector's Server URL (both were fine). Fixed in
   `_run_streamable_http` (`src/freecad_mcp/server.py`) by passing
   `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)`
   — safe here because every request is already authenticated by
   `ApiKeyMiddleware` independent of the `Host` header. Confirmed fixed by
   driving a full `initialize` → `notifications/initialized` → `tools/list`
   call through the public HTTPS domain and getting back all 17 FreeCAD
   tools, not just a 200 on unrelated endpoints.
5. Behind the Cloudflare Tunnel, `cloudflared` terminates TLS at the edge and
   forwards plain HTTP to the `mcp` container, so anything built from
   `request.base_url` inside the app (e.g. the OAuth
   `/.well-known/oauth-authorization-server` `issuer`/endpoint URLs) comes
   out as `http://` even though the real client used `https://`. Fixed in
   `src/freecad_mcp/oauth.py` by preferring the `X-Forwarded-Proto` /
   `X-Forwarded-Host` headers (Cloudflare sets these) over `request.url`.
   Any future code that builds an absolute URL from an incoming request on
   this deployment needs the same treatment.

## GPU render (2026-09-13)

The host has a real GPU (NVIDIA RTX PRO 5000 **Blackwell**, 48 GB VRAM,
~36/48 GB already used by other services on this host — check
`nvidia-smi` before assuming headroom). `docker/render/Dockerfile`
originally installed Blender via `apt-get install blender` and hardcoded
`scene.cycles.device = "CPU"`; both had to change to actually use the GPU:

1. Ubuntu's apt `blender` package is a stripped "dfsg" rebuild: no STEP
   importer (`bpy.ops.import_scene.step` doesn't exist — not a
   Debian-only gap, Blender never had one; export meshes as STL/OBJ/glTF/
   FBX from FreeCAD instead, e.g. `Shape.exportStl(...)`), no
   `OpenImageDenoiser` (crashes `render.render()` unless
   `scene.cycles.use_denoising = False`), and — root-caused by actually
   running a GPU render and watching `nvidia-smi` stay completely flat
   regardless of `--device` — no working CUDA/OptiX Cycles kernels either.
   Fixed by downloading the official blender.org binary in the Dockerfile
   instead (see the Dockerfile comments — `download.blender.org` itself
   403s a plain container `curl` behind a Cloudflare challenge; a mirror,
   `mirror.freedif.org`, works).
2. `docker-compose.yml`'s `render` service needs a GPU device reservation
   (`deploy.resources.reservations.devices` with `driver: nvidia`,
   `capabilities: [gpu]`) — this project's other GPU-touching neighbors on
   this host use the same shape (`docker inspect <container> --format
   '{{json .HostConfig.DeviceRequests}}'` on `sglang-qwen36` or
   `nim-nemotron-rerank-1b` shows the equivalent).
3. Blender's device enum (`compute_device_type`) doesn't reliably list what
   it actually supports before you *try* setting it — the apt build raised
   `TypeError: enum "OPTIX" not found in (...)` on one run and, after
   adding a pre-check, reported an empty enum on the very next run despite
   `nvidia-smi` inside the same container proving the GPU was visible.
   `render.py`'s `_enable_gpu()` now just tries each backend in turn and
   catches `TypeError`, rather than trusting the enum list up front.

4. This GPU generation had no kernel precompiled into Blender 4.2.1, so
   Cycles fell back to a runtime OptiX/CUDA JIT-compile — observed at
   **~5.5 minutes**, on *every* render, not just the first. A
   `render_cache` volume + `CUDA_CACHE_PATH`/`OPTIX_CACHE_PATH` aimed at
   persisting that compile across `docker compose run --rm` invocations
   got populated on disk (a real ~57 MB `ComputeCache` entry) but did
   **not** measurably help — a second run with that cache already warm
   still took ~5.5 minutes, and the OptiX-specific cache file
   (`optix7cache.db`) stayed at its empty 1 MiB default size the whole
   time, suggesting OptiX's own disk cache specifically wasn't the thing
   actually being hit. **Fixed by upgrading to Blender 5.2.1 LTS**
   (`ARG BLENDER_VERSION` in the Dockerfile) instead of chasing the cache
   further — 5.2's newer OptiX SDK has a kernel that natively targets
   Blackwell, and render time dropped to **2–4 seconds**, consistently,
   with no warm-up needed. If a future GPU generation hits this same
   symptom (GPU visible in the container, OptiX backend selected, but
   render time stuck at several minutes regardless of caching), check
   whether a newer Blender release supports that architecture natively
   before spending more time on cache plumbing.
5. **Unrelated to any of the above, but far more consequential**: found by
   actually opening a rendered PNG instead of just checking the file
   existed — every render was a solid black frame. Root cause:
   `_frame_camera_and_light()`'s original framing call was
   `bpy.ops.view3d.camera_to_view_selected() if bpy.context.area else None`.
   `bpy.context.area` is **always `None`** under `blender -b` (background
   mode, which this render service always uses) — so that call silently
   no-op'd on literally every render ever done here, leaving the camera
   at a hardcoded fixed location regardless of the actual imported
   object's size or position. A render can produce a valid, correctly
   sized PNG with zero errors and still be pointing at nothing — file
   existing and file size being "reasonable" are not evidence the render
   is correct; always look at the pixels at least once when validating a
   render pipeline change. Fixed by computing the imported objects' real
   world-space bounding box (via each object's `.bound_box` transformed by
   `.matrix_world`) and placing/aiming the camera and sun light from that
   instead, which works identically in background mode since it doesn't
   depend on any viewport/area existing.

Confirmed working end-to-end (Blender 5.2.1, post camera-framing fix):
exported a real FreeCAD cone, rendered it with `--device AUTO`, sampled
`nvidia-smi` throughout, and **visually inspected the output image** —
Cycles reported `enabling 1 device(s): NVIDIA RTX PRO 5000 Blackwell`
(OptiX), VRAM rose from a 36144 MiB baseline to a 38632 MiB peak (**~2.5 GB**
for one simple object; scales with scene complexity), render took
2.3–4.4 seconds across repeated runs, and the output image correctly shows
a lit, properly framed cone — not the placeholder-passing-as-success black
frame from before.

**Also**: `--output`'s extension isn't limited to `.png` — JPEG/TIFF/OpenEXR/
WebP/BMP/TARGA all work — but a naive `.suffix.upper()` gets some of those
wrong (`.jpg` → Blender's enum is `"JPEG"` not `"JPG"`; `.tif` → `"TIFF"`;
`.exr` → `"OPEN_EXR"`; each raises `TypeError` rather than silently doing
the wrong thing). `_blender_file_format()` in `render.py` has the override
table; verified by actually rendering and inspecting one output in each of
five formats, not just checking each command exited 0.

## Blender MCP integration (2026-09-13)

Two new services, separate from the `render`/`render.py` one-shot batch
container above: `blender` (a persistent, GUI Blender instance with
[ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp)'s addon
vendored in) and `blender-mcp` (that project's MCP server, wrapped in the
same streamable-http + API-key/OAuth layer as `freecad_mcp`, since upstream
only ships stdio). Public endpoint:
**`https://blender-mcp.hieudm.site`** — same auth pattern as freecad-mcp
(`BLENDER_MCP_API_KEY`, `BLENDER_MCP_OAUTH_CLIENT_ID`).

**Why two containers, not one, unlike FreeCAD's `freecad`+`mcp` split for a
similar reason**: blender-mcp's own Docker story
(see its repo's Dockerfile) assumes Blender runs on the *host*, with only
the MCP server containerized — there's no official image that bundles both.
Kept them split here for the same reason as FreeCAD: the GUI Blender
process and the MCP/HTTP layer have very different dependency footprints
(CUDA + Xvfb + a 350MB Blender install vs. a slim Python + `pip install
blender-mcp`), and restarting one (e.g. rebuilding the HTTP wrapper after an
oauth.py tweak) shouldn't have to restart the other (which holds all
in-memory scene state — restarting it loses everything not saved, same
caveat as FreeCAD documents).

Vendored, not `pip install`ed from PyPI, for the addon half specifically
(`docker/blender/addon.py`) because it needed one line changed (below); the
MCP server half is plain `pip install blender-mcp==1.9.1` in
`docker/blender-mcp/Dockerfile` — unmodified, imported as a library (see
`run_http.py`'s docstring for why: avoids forking/tracking their ~1800-line
`server.py`).

**Three integration problems found and fixed, in the order they surfaced**:

1. `BlenderMCPServer.__init__`'s default `host='localhost'` (upstream,
   `addon.py`) only binds the loopback interface *inside the `blender`
   container* — invisible to `blender-mcp` connecting over the Docker
   network as `blender:9876`, even though both are on the same compose
   network. Fixed by changing that one default to `'0.0.0.0'` in the
   vendored copy (search the file for "Vendored fork" to find the comment
   marking the change). Everything else about the addon is unmodified
   upstream code.
2. `run_http.py`'s first attempt at disabling DNS-rebinding protection
   (the same issue documented in the freecad-mcp section above) used
   freecad_mcp's exact fix — `mcp.streamable_http_app(transport_security=…)`
   — and got `TypeError: unexpected keyword argument 'transport_security'`.
   Different mcp package version resolved here (1.30.0, from blender-mcp's
   own `>=1.9.0,<2` pin — newer than whatever freecad_mcp's Dockerfile
   resolves) moved that setting from a `streamable_http_app()` kwarg to a
   `FastMCP(transport_security=…)` *constructor* kwarg instead. Since
   `blender_mcp.server` already constructs its `mcp` object for us, fixed by
   mutating the existing instance instead of trying to pass a constructor
   arg: `mcp.settings.transport_security.enable_dns_rebinding_protection =
   False`. If a future freecad_mcp mcp-package bump hits the same shape of
   error, check `mcp.settings` for where the setting actually lives in that
   version before assuming the old kwarg-based fix still applies.
3. `get_viewport_screenshot` failed with "Screenshot file was not created"
   — not a bug in the tool logic, but a direct consequence of splitting
   Blender and the MCP server into separate containers, which upstream's
   design doesn't anticipate: `server.py` computes a temp file path in the
   `blender-mcp` container (`tempfile.gettempdir()`), sends that literal
   path string to the addon over the socket, and the addon writes the file
   at that path in the **`blender` container's own, separate filesystem** —
   so `blender-mcp` checking `os.path.exists()` on its side always fails,
   even though the file really was written successfully on the other side.
   Fixed with a shared `blender_tmp` volume mounted at the identical path
   (`/tmp/blendermcp`) in both containers, `TMPDIR` pointed at it in both.
   Any other addon capability that round-trips a file this way (e.g. the
   disabled-by-default asset-download tools, if ever enabled) needs the
   same treatment — the volume mount already covers it, nothing further to
   do unless a *different* mount path gets introduced somewhere.

**The live Blender instance rendered on CPU for its first day** — the GPU
work documented in the "GPU render" section above only ever touched
`docker/render/render.py` (the one-shot batch service). Blender ships with
`compute_device_type = 'NONE'` and every `.blend` stores its own
`scene.cycles.device`, so the `blender` container happily accepted the
compose GPU reservation and then rendered on CPU anyway, using **zero
VRAM** — `nvidia-smi` didn't list the Blender process at all, which is what
made it visible. Nothing errors; renders just take ~7× longer. Measured on
the user's real scene (2000×1700, 128 Cycles samples, one wooden gear on a
studio backdrop): **CPU 21.5 s vs OptiX GPU 3.6 s cold / 2.8 s warm**, GPU
peak +2.5 GB VRAM over the 36144 MiB baseline and 99% GPU utilisation.
Fixed in `docker/blender/startup.py`, which now picks a backend the same
try-and-catch-TypeError way `render.py` does and sets `scene.cycles.device`
— plus a `load_post` handler, because the preferences survive a file load
but the per-scene device does not, so opening any `.blend` saved before this
change would silently drop back to CPU.

**Safe by default**: `BLENDER_MCP_SAFE_MODE=1` is set (validates/blocks
risky generated code — direct file I/O, subprocess, network, persistent
hooks — before it runs in Blender), and every optional asset-integration
toggle in the addon (Poly Haven, Sketchfab, Hyper3D Rodin, Hunyuan3D, Poly
Pizza) already defaults to `False` upstream — nothing extra needed to keep
this instance from reaching out to third-party services on its own.
`blendermcp_auto_start_server` also already defaults to `True` upstream, so
no FreeCAD-style manual auto-start scripting was needed — `startup.py` just
calls `bpy.ops.preferences.addon_enable(module="blender_mcp")` and the
addon's own `register()` starts the socket server.

Confirmed working end-to-end **through the public HTTPS domain**, not just
localhost: `initialize` → `tools/list` (23 tools) → `execute_blender_code`
(created a sphere, built a red `Principled BSDF` material with custom
roughness, added a sun light and a camera) → `get_viewport_screenshot`,
then **visually inspected** the returned image twice — once in Blender's
default "Solid" viewport shading (material invisible by design in that
mode — not a bug, don't mistake it for one) and once after switching
`space.shading.type = 'MATERIAL'` via another `execute_blender_code` call,
which showed the correct red, lit sphere.

If containers report "Up" but the RPC connection to `freecad` on port 9875
still refuses, check for a live `freecad-python3`/`freecad` process inside
the `freecad` container before assuming it's a networking problem — the
symptom above (up but never actually started) doesn't show in `docker logs`.

## Object storage (2026-09-13)

Added because **nothing in this deployment persisted anything**: FreeCAD
documents and Blender scenes live only in their app's process memory, so
every rebuild of `freecad` or `blender` silently discarded open work (this
happened repeatedly during development, and once cost the user real work —
see the `.env`/RAM notes above). `/data` was persistent but empty, invisible
from outside Docker, and until this change wasn't even mounted into the
`blender` container.

Shape of the fix:

- **`minio` service** — `quay.io/minio/minio`, pinned to
  `RELEASE.2025-09-07T16-13-09Z`. Note the registry: Docker Hub's
  `minio/minio` now answers pulls with "repository does not exist or may
  require 'docker login'"; quay.io is MinIO's own and works. That release
  still ships the full console object browser (checked the JS bundle for
  "Object Browser"/"Create Bucket"/"Upload" before pinning, since MinIO has
  been trimming console features in community builds).
- **Ports are loopback-only** (`127.0.0.1:9000`/`9001`) — they carry the root
  credentials and full read/write access to every saved project. Not on the
  Cloudflare tunnel. Reach the console from elsewhere with
  `ssh -L 9001:127.0.0.1:9001 <server>`.
- **`freecad_data:/data` is now mounted in `blender` and `blender-mcp` too**,
  which it wasn't before — Blender previously couldn't even see files
  FreeCAD exported. All five app containers now share that staging area.
- **One storage module, two servers**: `src/freecad_mcp/storage.py` holds
  client/bucket/key/path rules and imports nothing FreeCAD-specific, so
  `docker/blender-mcp/Dockerfile` COPYs that same file in rather than
  keeping a second copy that would drift. Tool layers differ per server
  (`src/freecad_mcp/storage_tools.py`, `docker/blender-mcp/storage_tools.py`).
- **Tools register conditionally** on `storage.is_configured()`, before the
  Starlette app is built (the tool list is baked in there) — so stdio/local
  runs and any MinIO-less deployment keep exactly their previous tool set.
- **`minio` is installed in the two Dockerfiles, not `pyproject.toml`**, and
  imported lazily, keeping this fork's upstream diff small (same reasoning
  as the rest of the Docker-only surface).
- **Path guard**: `upload_file_to_storage`/`download_file_from_storage`
  resolve their argument and reject anything outside `/data`. The agent can
  already run arbitrary code in both apps, so this isn't a security boundary
  so much as refusing to make exfiltration a one-liner in the tool surface;
  verified it rejects `/etc/passwd` and `/etc/cron.d/evil`.

Verified end-to-end, not just "no errors": created a FreeCAD box (33×22×11),
saved it, confirmed the object in MinIO with `mc` independently of the tool's
own success report, closed the document **and deleted the `/data` copy** so
the restore had to come from MinIO, loaded it back and checked the
dimensions and volume matched (7986 mm³). Same for Blender with a gold
metallic torus (base color and metallic value verified after restore), plus
a cross-server hop — FreeCAD STL export → upload → listed and imported from
the Blender server — and a viewport screenshot inspected visually.

FreeCAD units are mm and Blender's are m, so a 33 mm box imports into
Blender as a 33-unit object. Expected, but it looks like a bug the first
time a part dwarfs everything else in the scene.
