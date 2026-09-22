# CLAUDE.md — Project Conventions for freecad-mcp-custom

This is a fork of `neka-nat/freecad-mcp` that adds a Docker deployment
(MCP over HTTP with API-key auth, headless FreeCAD, a live-controlled
Blender instance via a vendored `ahujasid/blender-mcp`, and a chat harness
serving a browser UI with both viewports live) on top of upstream. See
[`docs/docker.md`](docs/docker.md) for the full architecture and
[`README.md`](README.md) for the general project.

**Service names changed on 2026-09-22** — everything written below that date
uses the old ones. Read the mapping in "Service rename, render fold-in,
harness + frontend" before trusting a container name in this file.

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
  arbitrary Python, viewport screenshots). Same API-key/OAuth pattern as
  freecad-mcp, on `BLENDER_MCP_PORT` (`8002`) → see the "Blender MCP
  integration" section below for the whole story (why two containers, what
  broke, what's disabled by default). As of 2026-09-22 it also owns the
  `render_image` tool that used to be the separate `render` container.
- **The live preview page was DISABLED, and is ON again since 2026-09-22.**
  History, because the reason it was off still applies: it was turned off on
  2026-09-13 at the user's request, by setting `FREECAD_MCP_PREVIEW=0` on the
  `mcp` service and `BLENDER_MCP_PREVIEW=0` on `blender-mcp`; with those off
  the routes aren't registered *and* `ApiKeyMiddleware` drops its `/preview`
  exemption, so every `/preview*` path answers 401 even with a valid key
  (verified then that MCP tools are unaffected either way: 22 FreeCAD / 33
  Blender tools still listed over the public HTTPS endpoints and OAuth
  discovery still 200'd unauthenticated). Both flags are `1` again on
  `mcp-freecad`/`mcp-blender` because the new frontend's preview panel is
  those same routes, proxied — turning them off turns the panel dark.
  **The cost didn't go away**: a watching browser tab pulls a fresh
  screenshot out of each GUI process, both GUIs have a single command queue,
  so frames compete with MCP tool calls. That's what the self-paced polling
  below exists to contain, and why the Pause button is still the one
  guaranteed way to take the load off. Flipping either flag back to `0` and
  `docker compose up -d mcp-freecad mcp-blender` still works — no rebuild,
  the code ships in the image regardless.
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
  Unset `BLENDER_MCP_API_KEY` on the `mcp-freecad` service and the tab
  disappears. The browser frontend's preview panel (see below) is the same
  routes again, proxied a second time by the `harness` container so the page
  can be served without a key in the URL at all.

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
  `docker/freecad/entrypoint.sh` still forces software GL and the
  `freecad-headless` service has no GPU reservation at all — which is also
  the answer to "does FreeCAD eat VRAM": it holds **0 MiB**, it just burns
  CPU instead. Worth knowing: measured over 30 s with
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

  **One exception, added 2026-09-22**: `preview_png` passes `"Isometric"` for
  the *first* frame of a document it has not shown before, keyed on the
  document name in a per-process set. A freshly created document's camera is
  wherever FreeCAD left it, which is not looking at the geometry — so the
  panel served a 200, a valid PNG of the right size, and a **blank image**,
  indistinguishable from a working panel that has not drawn yet. (Found the
  only way these things ever are here: by opening the PNG instead of reading
  the status code.) Keying on the name means a later orbit is still never
  undone. It costs one extra `get_active_document()` round trip per frame — a
  bare attribute read, next to nothing beside the screenshot on the same
  queue.

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
- The OAuth shim supports **both** a static client_id (ChatGPT types it into a
  field) **and** Dynamic Client Registration (RFC 7591) at
  `POST /oauth/register`, advertised as `registration_endpoint` in the
  metadata. DCR is what **Claude.ai custom connectors** use — that flow is
  fully automatic and has no field to type a client_id into, so it
  self-registers, then runs authorize/token. Adding a connector on Claude web
  is therefore just: paste the `/mcp` URL, log in with the API key. The
  DCR-issued client_id is self-verifying (a random half + truncated HMAC, see
  `_issue_client_id`), so it needs no store and survives restarts. The
  client_id — static or DCR — is never a security boundary; the API key at the
  login page plus PKCE is. Both `authorize` and `token` accept a DCR client_id
  via `_client_id_ok`.

## Service rename, render fold-in, harness + frontend (2026-09-22)

| old service | new service | build folder (unchanged) |
| --- | --- | --- |
| `freecad` | `freecad-headless` | `docker/freecad/` |
| `mcp` | `mcp-freecad` | `docker/mcp/` |
| `blender` | `blender-cli` | `docker/blender/` |
| `blender-mcp` | `mcp-blender` | `docker/blender-mcp/` |
| `render` | *gone* — folded into `blender-cli` | `docker/render/` deleted |
| `minio` | `minio` | (image, no build) |
| — | `harness` (new) | `harness/Dockerfile` |

**Hyphens, not underscores, because these are hostnames.** `mcp-freecad`
passes its `--host` through `validators.hostname()` (`_validate_host` in
`src/freecad_mcp/server.py`), which rejects `_` — underscores aren't legal in
an RFC-1123 host name — so `--host freecad_headless` exits 2 and, under
`restart: unless-stopped`, crash-loops forever: no tools, no `/preview`, and
every harness chat 503s. Don't relax the validator to get underscores back.

**The `docker/` folders deliberately keep their old names.** Nothing resolves
a service by its build path, and renaming `docker/blender/` would break
`git log --follow` on `addon.py` — 3,900 vendored lines carrying exactly one
local patch, where diffing against upstream history is the only thing that
keeps that patch from being lost on the next vendor bump. The compose file
carries the mapping in a header comment for the same reason.

Old names survive in the prose of every section below dated before this one.
They were not mass-renamed: those sections are a record of what happened, and
rewriting them would make the history lie about the names in play at the
time. Hostnames, env vars and commands *are* all current.

**The first `up` after this rename needs `--remove-orphans`**: the containers
running under the old names are orphans of the project now and still hold
8001/8002, so a plain `docker compose up -d` leaves them up and then fails
with `bind for 0.0.0.0:8001 failed: port is already allocated`. It also stops
them, and stopping `freecad-headless`/`blender-cli` discards every document
and scene that was never saved to MinIO — save first.

### The `render` service is gone; `render_image` replaced it

`docker/render/` (its own Dockerfile + a ~200-line `render.py` driven by
`docker compose --profile render run --rm render …`) booted a second Blender,
imported a file and rendered it — blind to the materials, lights and camera
the agent had just built in the *live* instance, which is the scene a user
actually wants a picture of. It is now one MCP tool on the Blender server,
`render_image` (`docker/blender-mcp/render_tools.py`), which sends a render
script down the existing `execute_code` socket into `blender-cli`.

Only two things from `render.py` were worth keeping and both were kept: the
file-format enum override table (`.jpg` → `"JPEG"`, `.tif` → `"TIFF"`,
`.exr` → `"OPEN_EXR"` — Blender raises `TypeError` rather than guessing) and
the bounding-box camera framing that fixed every render coming out solid
black. The framing is *not* gated on `scene.camera is None` — that gate was itself
the bug: `blender-cli` boots Blender's factory startup file, which always has
a camera, so the gate meant framing never ran and every render pointed
wherever the default camera happened to look. `render_image` takes
`frame: bool = True` instead, so the caller states whether the scene's camera
is deliberate; `frame=False` renders through the camera and lighting you
placed. It frames the selection first when there is one, and names its objects
`MCPRenderCam` / `MCPRenderSun` so it reuses them instead of littering a
long-lived scene with `Camera.001`. Dropped as dead weight: `argparse`, the importer table
(`execute_blender_code` already imports meshes), `_clear_scene` (the live
scene is the point), and `use_denoising = False` (that was a workaround for
apt Blender's missing OpenImageDenoiser; the official build has it).

The tool lives in the `mcp-blender` image, not `blender-cli`, on purpose:
editing anything baked into `blender-cli` costs a rebuild and restart, and a
restart throws away the in-memory scene — the whole reason those two
containers are split in the first place.

`render_tools.py` has a `__main__` self-check that `ast.parse`s the script it
would send (including a path with quotes in it) and asserts the format table.
Worth running after any edit: a syntax error in that assembled script
otherwise surfaces only as an opaque socket reply in the middle of a render.

**Verified at runtime on 2026-09-22**, after the deploy: `render_image`
through the public-facing `mcp-blender` returned
`{"device":"GPU","backend":"OPTIX","camera":"MCPRenderCam","bytes":540198}`
in **1.8 s** at 64 samples / 960x720, and the PNG was **opened and looked
at** — a lit, correctly framed cube, not the black frame this pipeline has
produced before. So `bpy.ops.render.render()` does behave inside the addon's
`bpy.app.timers` command queue in a GUI Blender; the plain call is enough and
`bpy.ops.render.render('EXEC_DEFAULT', write_still=True)` was not needed.
Also note the
render runs *inline on that single queue*, so it blocks preview frames and
every other tool call for its duration (~2–4 s at 128 samples; both socket
ends time out at 180 s) — there is a `ponytail:` comment on it saying to move
it to a `blender -b` subprocess if renders ever get heavy.

### Lazy GPU init, and the VRAM numbers behind it

`docker/blender/startup.py`'s `_enable_gpu()` used to run at container start;
it is now `_gpu_on_render`, a `@persistent` handler on
`bpy.app.handlers.render_pre`. One copy of the logic now covers every render
path — the `render_image` tool, a hand-written `bpy.ops.render.render()`
through `execute_blender_code`, F12 — and the CUDA/OptiX driver is only
opened once something actually renders. `scene.cycles.device = "GPU"` is set
on *every* render rather than once, which covers the hazard that a `.blend`
restored from MinIO carries whatever device it was saved with
(`scene.cycles.device` lives inside the file; the backend choice lives in
preferences).

The `load_post` handler was dropped in that change and then **put back**: a
`.blend` saved in RENDERED viewport shading restores that shading on load,
and the viewport is the one Cycles path that never fires `render_pre`, so
without it the preview panel silently went back to CPU for exactly the files
most likely to be opened for a look. It is the same handler, so the cost is
one extra `get_devices()` per file load — ~0.03 s and, measured, 0 MiB.

The one Cycles path that does **not** fire `render_pre` is viewport RENDERED
shading, so `docker/blender-mcp/preview_api.py`'s shading route calls the
registered handlers itself — otherwise this change would have silently
dropped the preview panel's Rendered view back to CPU, with no error and a
correct-looking image, which is exactly how the original CPU-rendering bug
hid for a day.

**Be honest about what this bought: 0 MiB.** Measured on this host
(`nvidia-smi`, 2026-09-22), which was the user's actual question:

| process | idle VRAM | why |
| --- | --- | --- |
| `freecad-headless` | **0 MiB** | requests no GPU device at all; software GL, so it burns ~0.74 CPU cores 24/7 instead |
| `blender-cli` | **180 MiB** | the viewport's GL context — this is what makes the live preview 27x faster than llvmpipe, and the preview panel needs it |
| Cycles, during a render | **+~2.5 GB** | freed by Blender itself ~0.5 s after the render ends |

So there was never idle Cycles VRAM to reclaim: assigning
`compute_device_type` costs nothing, `get_devices()` dlopens libnvoptix and
opens `/dev/nvidia-uvm` but reserves nothing, and Cycles already gave its
working set back on its own. Expect before/after `nvidia-smi` to read the
same. The change ships because it was asked for and because it collapses two
copies of the enable logic into one — not because it frees memory. The 180
MiB is the price of the preview panel; the only way to get it back is to stop
running a GUI Blender.

**The real GPU risk here is contention, not footprint.** This host is shared:
38101 of 48935 MiB were already in use by neighbours at the time of
measuring — `sglang::scheduler` alone holds 31720 MiB — leaving ~10.8 GB of
headroom. A render spikes ~2.7 GB for a few seconds against that. If the
harness ever fires renders from a chat loop, serialise them there.

### `harness/` — the agent loop, and the only thing the browser talks to

New Node 22 + TypeScript service (`harness/`, published on
`HARNESS_PORT`=**8003**), built on `@earendil-works/pi-agent-core` +
`pi-ai` (both pinned `0.87.0`) with `@modelcontextprotocol/sdk` 1.30.0 as an
MCP *client*. It connects to both MCP servers over the compose network,
exposes their tools to the model, and serves the built frontend as static
files. It holds `FREECAD_MCP_API_KEY` and `BLENDER_MCP_API_KEY` server-side
and proxies the preview routes, so **no key ever reaches the browser** — that
is the whole reason the frontend has no origin of its own.

**Published on `127.0.0.1` only**, the same as `minio` and for more reason:
the page has no auth of its own, and anyone who opens it can ask the agent to
run arbitrary Python inside both FreeCAD and Blender with both keys supplied
for them. `ssh -L 8003:127.0.0.1:8003 <server>` to reach it from elsewhere;
putting it on the LAN/tailnet/tunnel means a key check in front of `route()`
first.

The HTTP contract, which both sides are written against:

| route | does |
| --- | --- |
| `POST /api/chat` | `{messages:[{role,content}]}` → `text/event-stream` of `Frame`s: `{t:"text",delta}`, `{t:"tool",phase,id,…}`, `{t:"error",message}` — defined in `harness/src/wire.ts`, mirrored in `frontend/src/Chat.tsx`. Never raw `AgentEvent`s; forwarding those *was* the bug |
| `GET /api/preview/{freecad,blender}.png` | proxied frame, no auth from the browser |
| `POST /api/preview/{freecad,blender}/{orbit,zoom,reset}` | query params only, never a body |
| `GET /api/health` | `{freecad:bool, blender:bool}` |
| `GET /*` | the built frontend; extensionless paths fall back to the page, anything with a file extension 404s |

**That 404 is load-bearing, not tidiness.** The fallback originally caught
every unreadable path, so a request for a stale `/assets/<hash>.js` — which is
what a cached `index.html` asks for the moment a rebuild changes the hashes —
was answered with `index.html` at HTTP 200 and `content-type: text/html`. The
browser then parses `<!doctype html>` as a module script, throws a
SyntaxError, and the app never mounts: a blank page in the body colour, which
reads as "the UI vanished" and not as "one file is missing". `index.html` is
now served `no-cache` and the hashed assets `immutable`, which is the pairing
that makes a redeploy land without a hard refresh. Any static server put in
front of this needs both halves.

Things in it that are decisions, not accidents:

- **Tool names are prefixed `freecad_` / `blender_`** because the two servers
  genuinely collide: `list_storage_files`, `upload_file_to_storage` and
  `download_file_from_storage` are registered by both
  (`src/freecad_mcp/storage_tools.py` and
  `docker/blender-mcp/storage_tools.py`). The unprefixed name stays in the
  closure — the remote server has never heard of the prefix.
- **Raw MCP JSON Schema is passed straight through as pi's `parameters`.**
  pi-ai's validator detects the missing TypeBox `Kind` symbol and takes its
  plain-JSON-Schema path, so no conversion layer is needed. `harness/src/
  mcp.test.ts` runs the real `Agent` loop against a faux provider and proves
  it; it was mutation-checked (calling the remote with the *prefixed* name
  makes it fail).
- **One long-lived `Agent` is the conversation**, reset when a POST carries
  ≤1 message. The `{role,content}` wire shape cannot carry pi's
  `AssistantMessage` (`usage`, `stopReason`, tool calls), so replaying the
  posted history per request would silently drop every tool call. The harness
  takes the last user message from the body and keeps the transcript itself.
  **Consequence: it is single-conversation and single-user.** Two browsers
  share one agent.
- **MCP connections are made on the first chat, not at boot**, and the cache
  clears on failure — a wrong key or a still-starting MCP server would
  otherwise crash-loop the container.
- **The SSE wire shape is the harness's own** (`harness/src/wire.ts`'s
  `Frame`: `text` / `tool` / `error`), mirrored verbatim in
  `frontend/src/Chat.tsx`. Forwarding pi-agent-core's `AgentEvent` instead
  made the frontend guess at a library's internal names — it read `event` for
  `assistantMessageEvent` and `tool_start` for `tool_execution_start`, so the
  chat panel rendered *nothing* — and `turn_end`/`agent_end` re-sent the whole
  transcript, base64 screenshots included, every turn. `normalise()` drops
  everything that is not new text or tool state and clips tool args/results;
  `harness/src/wire.test.ts` pins the frame set both sides agree on. The
  subscriber is synchronous on purpose — the agent loop awaits subscribers, so
  waiting for a socket drain there stalls the run.
- **A provider failure resolves `prompt()`, it does not reject it** — pi's
  StreamFn contract puts it on the final assistant message (and on
  `state.errorMessage`), so the try/catch around `prompt()` never fires. The
  error frame comes from `message_end` instead; without that, a bad
  `OPENAI_API_KEY` looks exactly like a working stream that says nothing.
- **Abort is wired to the *response's* `close`, not the request's**: `chat()`
  drains the body with `for await`, which destroys the IncomingMessage, so a
  `req.on("close")` registered afterwards can never fire and a closed tab
  would leave the agent running tool calls nobody is watching.
- The preview proxy appends `?key=` **last**, so a browser-supplied `key`
  can never win.

**Point `OPENAI_BASE_URL` at the endpoint's LAN/tailnet address, never its
public hostname, when that endpoint is served from this same host.** The first
configuration used `https://new-api.hieudm.site/v1`, which made the harness
hairpin: container -> NAT -> internet -> Cloudflare edge -> the tunnel -> back
into this host -> `new-api`. Measured from inside the container, six `fetch`
calls: 861 ms, **10489 ms FAIL `UND_ERR_CONNECT_TIMEOUT`**, 1557 ms, ... —
against 2-24 ms and 6/6 on `http://100.84.25.11:3000/v1`. The 10 s is undici's
default connect timeout, which the `openai` SDK (pi-ai uses it under
`openai-completions`) reports as `APIConnectionTimeoutError: Request timed
out.` — a message that reads like the model was slow and is nothing of the
kind. `new-api` listens on `100.84.25.11:3000` only, so `host-gateway` /
`host.docker.internal` cannot reach it; if that tailnet address ever moves,
join `new-api_new-api-network` as an external network and use
`http://new-api:3000/v1` instead.

**pi-ai hardcodes `maxRetries: 0`** on its OpenAI client and only retries when
the caller passes `options.maxRetries` (`openai-completions.js` hands it to its
own `retryProviderRequest`). Passing `models.streamSimple` straight through as
`streamFn` therefore meant one dropped connection ended an entire tool-driving
turn. The harness now wraps it with `maxRetries: 3`.

**The system prompt has to name `freecad_get_freecad_api_reference`.** Watching
a real run — "vẽ bánh răng bằng freecad đi" — the model went straight to
`freecad_execute_code`, burned four attempts on `AttributeError: module 'Part'
has no ...` and `TypeError: float() argument must be ...`, then gave up and
left a plain cylinder named "Test" plus five stray `Gear1..Gear5` documents
(`FreeCAD.newDocument` with an existing name silently makes a new one instead
of failing). It never called the reference tool, because nothing asked it to:
that guidance used to live in an MCP *prompt*, which connector-style clients
never surface. After adding the line, the same request called
`get_freecad_api_reference(topic="geometry")`, stayed in one document, and
produced a genuine 24-tooth spur gear — one valid solid, 82 faces,
110x110x25 mm, confirmed by looking at the render. The teeth are rectangular
rather than involute; that is the model's ceiling, not the harness's.

**Verified against a live endpoint on 2026-09-22.** `.env` now points at
`OPENAI_BASE_URL=https://new-api.hieudm.site/v1` with
`HARNESS_MODEL=nvidia/Qwen3.6-35B-A3B-NVFP4` (the key is in `.env`, which is
gitignored). Three capabilities were checked *before* wiring it up, because
a chat endpoint that lacks any one of them is useless to this harness:
**tool calling** (`finish_reason: tool_calls` with correct arguments),
**tool_calls deltas while streaming** (pi-agent-core drives everything
through `streamFn`, so a non-streaming tool call would never arrive), and
**image input** (it read a 1x1 red PNG back as "Red" — `get_view` and
`get_viewport_screenshot` return image blocks, and a text-only model would
break on them). pi-ai's
`openaiProvider()` hardcodes `api.openai.com` and the Responses API, so the
model is declared inline as `Model<"openai-completions">` and wrapped with
`createProvider({auth: envApiKeyAuth(…), api: openAICompletionsApi()})`
(all three imports confirmed present in the shipped `.d.ts`; it compiles).
`contextWindow`/`maxTokens`/`cost` are placeholders — the model id is
arbitrary and those only feed pi's accounting.

### `frontend/` — React + Vite, chat plus two live viewports

`frontend/` is built in the first stage of `harness/Dockerfile` and copied
into the harness image as `public/`; there is no frontend container and no
second origin. Deps are react + react-dom + vite + typescript, nothing else —
no `@vitejs/plugin-react` (esbuild does the automatic JSX runtime from
tsconfig; the cost is losing fast refresh), no state/UI/CSS/router library.

**The frame pacing was ported from `src/freecad_mcp/preview.py`, not
reinvented** — same reason, same numbers: one request in flight chained off
the `<img>`'s `load`/`error` (not `fetch`), zero requests while
`document.hidden`, ~900 ms idle / 120 ms for 2 s after an interaction, never
below half the measured frame cost — uncapped, because the cap defeated the
floor for exactly the slow frames it exists to protect (a 19.9 s software-GL
frame must yield a ~9.9 s gap, not 1.2 s) — 3000 ms backoff on
error, pause persisted in `localStorage`. It lives in `frontend/src/poll.js`
as plain JS + JSDoc purely so `node --test` can run it without a TypeScript
loader; `tsc` still type-checks it via `checkJs`. Its seven assertions are the
only unit test in the frontend, and one of them pins the slow-frame case
(19.9 s frame → 9945 ms gap) that the removed 1200 ms cap silently broke.

**Light by default, dark only under `prefers-color-scheme: dark`.** The first
version hardcoded `color-scheme: dark`, and the user's first look at it in a
real browser was "UI lỗi, chat thì bị nền đen" — because the panel beside the
chat shows two *light* viewports (FreeCAD's 3D view is near-white, Blender's
mid-grey), so a black chat column sat against a glaring white rectangle.
Every colour in `style.css` is a `:root` token for this reason: the dark block
only overrides `:root`, so a hardcoded hex anywhere else silently stops
following the theme.

An empty FreeCAD panel is **not** a bug — with no document open,
`get_active_screenshot` has nothing to capture and `preview_png` serves its
1x1 `_BLANK_PNG` (68 bytes, HTTP 200). Blender always has its startup scene,
so that tab always shows something, which makes the FreeCAD one look broken
by comparison. There is no hint for this case yet: the 200-with-a-blank-image
path is exactly the "dead panel indistinguishable from a working one" shape,
and the frontend would need to read `/preview/status` (which does report open
documents) to tell them apart.

**There is an error boundary in `main.tsx`, and it earns its keep.** React
unmounts the entire tree on an uncaught render error, so the failure mode is
an empty `#root` — a blank page that looks identical to a bundle that never
loaded, with the cause only in a console nobody has open. The boundary puts
the stack on screen instead. `Chat.tsx`'s `fold` is the related fix: it passes
an *updater* to `setMsgs`, and React runs an updater during render, so a throw
inside `apply()` escaped the `try/catch` wrapped around the stream and took the
page down rather than skipping one malformed frame. The updater now catches
and returns `prev` unchanged.

Two browser-specific traps already handled: React's root-level `wheel`
listener is passive, so zoom binds its own with `{passive:false}`; and
`<Preview key={tab}>` remounts on tab switch so a queued orbit for the other
backend is dropped rather than replayed against the wrong viewport.

**Not built**: the FreeCAD project dropdown (`/preview/activate`) and
Blender's Shading / Camera-view buttons. They are not in the harness'
contract — the three routes it proxies are orbit/zoom/reset. That matters
more than it sounds: `activate_document` is still not an MCP tool either (see
the 1.1.3 section below), so with the dropdown gone too, **nothing can switch
FreeCAD's active document** — and `get_view` stays broken for a document
whose `ActiveView` is a TechDraw page. Add the proxy route and the dropdown,
or expose the MCP tool, whichever comes first.

### Deployed and verified end-to-end (2026-09-22)

`docker compose up -d --build --remove-orphans` ran; all six services are up,
both public endpoints answer 200, and the whole chain was driven for real:

- **The harness drew CAD through the model.** A Vietnamese prompt produced
  three tool calls (`freecad_create_document`, `freecad_create_object`,
  `freecad_get_objects`), and the geometry was then checked **independently
  of both the harness and the model**, straight over XML-RPC:
  `volume = 9361.0` against an expected 9361 for 37x23x11, bbox 37/23/11,
  6 faces, 1 solid.
- **Tool surface**: 23 FreeCAD + 33 Blender = 56 tools, 56 unique names after
  prefixing. `list_storage_files` / `upload_file_to_storage` /
  `download_file_from_storage` really do collide across the two servers and
  the prefix really does resolve them. 0/56 failed `toToolDeclaration`, 0/56
  failed pi-ai's real `validateToolArguments`.
- **Both preview panels were opened as images and looked at**, not just
  curl'd for a 200 — FreeCAD 1014x803, Blender 1000x858. An orbit changed the
  frame's md5, so it is not the `success: true` no-op this project has been
  bitten by twice.
- **VRAM, sampled every 0.5 s across a render**: 231 MiB idle → 1608 → **2733
  peak** → 212 MiB, and flat at 212 for the following 18 s. Cycles takes the
  memory only while rendering and gives it back by itself.
- Latency for reference: `tools/list` 34–56 ms, `get_rpc_status` 6 ms,
  `execute_code_headless` with a boolean cut 287 ms, Blender viewport
  screenshot 895 ms, `render_image` 1.8 s. The harness's 600 s MCP timeout
  has ~200x headroom on all of it.

Still open:

- **Nobody has opened the frontend in a real browser.** Every route was
  exercised with curl and the bundle serves, but the polling scheduler, the
  Pause button, drag-to-orbit and the tool-call rendering have not been seen
  by a human eye. `ssh -L 8003:127.0.0.1:8003 <server>`, then
  `http://127.0.0.1:8003`.
- The FreeCAD project dropdown and Blender's Shading / Camera-view buttons
  are still not built, and `activate_document` is still not an MCP tool — see
  the note above; nothing can switch FreeCAD's active document.

### The GUI dispatch wedge, and why it took 32 hours to notice

Found while proving the harness could draw: every GUI-touching FreeCAD tool
had been dead since **2026-09-21 01:30:53**, the moment the `freecad`
container last started. `ping` and `get_rpc_status` answered instantly the
whole time, so nothing looked wrong until someone called a real tool.

The chain, in the order it was untangled:

1. Tools returned `GUI_DISPATCH_FAILED: gave up after 60.0s waiting for
   'list_documents' to start`. The task never *started*, so it was not a
   stuck task — confirmed by the error text, which omits the
   `(GUI thread has been busy for …)` hint `dispatch_to_gui` appends only
   when `_processing` is set.
2. `process_gui_tasks` defers a tick while
   `QtWidgets.QApplication.activeModalWidget()` is set. In a headless
   container nothing will ever dismiss a modal, so that deferral is
   permanent, not momentary.
3. The modal came from FreeCAD's own **auto-recovery**: the container is
   SIGKILLed on every restart, so FreeCAD never exits cleanly and leaves
   `~/.cache/FreeCAD/*/Cache/FreeCAD_Doc_<uuid>_<pid>/fc_recovery_file.*`
   behind for each open document. The next start found three of them, ran
   recovery, failed, and raised the dialog — the container log's only clue
   was three `Original project file is corrupted: "/data/….FCStd"` lines.
   **Those files are not corrupt**: all three are valid zips with a
   `Document.xml` (191/163/69 members). The message is about the recovery
   read, not the file on disk.

Three fixes, all shipped:

- `docker/freecad/entrypoint.sh` deletes the transient recovery dirs before
  launching. Only one FreeCAD ever runs in this container, so anything there
  at startup is by definition stale, and the data was only ever reachable
  through the dialog that wedges the process. **Durable storage is MinIO** —
  `save_document_to_storage`, not autosave.
- `get_dispatch_status()` now reports a `pump` block
  (`heartbeat`/`queued`/`draining`/`blocked_by`/`blocked_for_seconds`).
  `DispatchHealth` only ever tracked tasks that **started**, which is why it
  reported `state: healthy` for 32 hours while nothing drained at all — the
  one failure mode it could not see. `get_rpc_status` now says
  `blocked_by: "modal_dialog_open"` instead.
- The 500 ms heartbeat is a repeating `QTimer` (`start_heartbeat`) instead of
  a chain of self-arming `singleShot`s. `process_gui_tasks` returns early
  when `_processing` is set, and that `return` sat *above* the `try` whose
  `finally` armed the next tick, so a tick delivered by `processEvents()`
  inside a running task was swallowed without re-arming — and if the drain it
  interrupted was the wake path (`reschedule=False`, which never arms one
  either) the chain ended for good. A repeating timer cannot be lost.

Post-fix, `list_documents` answers in **0.01 s** and `pump` reads
`{heartbeat: true, queued: 0, draining: false, blocked_by: null}`.

**The rule this reinforces**: generated code must never open a modal dialog
here — which is why `gui-and-interface.md` is deliberately not vendored into
the API reference. It is also why a health check must report the queue, not
just the tasks in it.

## FreeCAD API reference tool (2026-09-16)

`get_freecad_api_reference(topic)` serves real FreeCAD Python API docs to the
connected model on demand. It exists because `execute_code` is where this
server's power is *and* where a model guessing at the FreeCAD API invents
methods — the same failure that produced `FreeCADGui.activateDocument` here.

- **Vendored** from github/awesome-copilot's `freecad-scripts` skill (MIT),
  verbatim, in `src/freecad_mcp/reference/` — see `NOTICE.md` there.
  `gui-and-interface.md` is **deliberately not vendored**: PySide dialogs,
  `QMessageBox` and `Gui.Control.showDialog()` assume a human at the window,
  and here a modal dialog blocks forever with nobody to dismiss it and can
  wedge the RPC connection. Don't add it "for completeness".
- **Served per topic**, not as one blob (index / fundamentals / geometry /
  parametric / advanced; ~3–11 KB each) so the model pays only for what it
  pulls. `DEPLOYMENT_RULES` in `src/freecad_mcp/api_reference.py` is the part
  upstream can't know — headless/no dialogs, `/data` is the only writable
  place, `/data` is *not* MinIO, always `recompute()`, mm units, STL-not-STEP
  for Blender, `setActiveDocument`.
- **A tool, not a prompt, on purpose.** The server also exposes an
  `asset_creation_strategy` MCP *prompt*, but connector-style clients
  (ChatGPT's connector UI) never surface prompts, so that guidance likely
  never reaches the model. Tools are the one channel every client uses. That
  prompt's `description` was also empty (FastMCP takes it from the
  docstring, which was missing) and is now filled in.
- **Verified against the real FreeCAD 1.0.0** in this deployment, not just
  assumed: every `Part.make*` and shape method the docs reference exists,
  `Units.Quantity` and `Part::Feature`+`recompute` work, a boolean cut
  returned the geometrically correct volume, and the docs correctly use
  `App.Vector` (not `Part.Vector`). Spot-checked for the `setActiveDocument`
  vs `activateDocument` distinction before trusting it at all.

## FreeCAD 1.1.3 from the official AppImage (2026-09-16)

Both `freecad` and `mcp` run **FreeCAD 1.1.3**, downloaded as the official
AppImage and `--appimage-extract`ed in their Dockerfiles (mounting an
AppImage needs FUSE, unavailable in an unprivileged container). They were on
Debian trixie's `1.0.0+dfsg` — the original Nov-2024 release, with no
backports available — so apt could never get past it.

**Keep the two `ARG FC_VERSION`/`FC_URL` pairs in lockstep.** Both services
read and write the same `.FCStd` files on `/data`; a document written by a
newer FreeCAD and opened by an older one is how data silently degrades.

Third time this pattern has bitten this project: the distro package is old
and *stripped*. Here the apt build had **no `ccx` (CalculiX) and no `gmsh`
anywhere on the image** (`find /` found nothing, `ccxBinaryPath` was empty),
so `run_fem_analysis` could never actually solve or mesh — an advertised
tool that was dead on arrival. The AppImage bundles both. Same shape as the
apt Blender missing its CUDA/OptiX kernels.

Cost: images went 2.56 GB → 4.66 GB (`freecad`) and 4.86 GB (`mcp`). The
783 MB AppImage is downloaded once per Dockerfile rather than shared, which
is deliberate — sharing it would couple the two builds' ordering for a few
GB on a 1.4 TB disk.

### What was verified before switching (in a throwaway container, `/data` read-only)

- Addon loads, RPC auto-starts, `gui_dispatch` healthy; **16/16 RPC methods
  pass**.
- **Camera controls survive** — the `view_manager.py` regex that parses
  `view.getCamera()`'s Coin3D string was the most brittle thing in the
  upgrade. Confirmed by camera *position* actually changing (146.8,-141.8 →
  185.7,92.5) and screenshot md5 differing at each step, not by trusting
  `success: True` (the Blender orbit bug taught that lesson).
- **Geometry identical across versions**: the gear reads 66132.2 mm³ /
  66×66×34 bbox in both 1.0 and 1.1.3.
- **Round-trips both ways**: a file saved by 1.1.3 reopens in 1.0 with the
  same volume, so a rollback does not strand the data.
- **One migration artifact**: opening a 1.0 document in 1.1 adds
  `Origin001` (`App::Point`) — verified to be a legitimate member of
  `Origin.OriginFeatures` (1.1's `App::Origin` gained an origin point).
  Harmless, ignored on the way back to 1.0, but it means `get_objects`
  returns one more object than it used to (17 → 18 for the gear).

### `get_view` failing right after the upgrade was NOT a regression

It returned "Cannot get screenshot in the current view type". Cause: the
active document was one containing a **TechDraw page**, whose `ActiveView`
is `MDIViewPagePy` — no `getCamera`, so the addon correctly refuses.
`FreeCADGui.setActiveDocument(...)` onto a document with a 3D view makes
`ActiveView` a `View3DInventorPy` again and screenshots work.

This surfaced a real gap, unrelated to the version: **`activate_document`
exists as an RPC method but is not exposed as an MCP tool**, so a connected
model has no way to switch the active document itself — and the preview page
that used to drive it is disabled. If an AI opens a TechDraw-bearing
document, `get_view` stays broken for it with no recourse.

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

> **`docker/render/` no longer exists** (2026-09-22). Everything below is
> still true and still load-bearing, it just lives elsewhere now: the
> blender.org-not-apt download and `ARG BLENDER_VERSION` are in
> `docker/blender/Dockerfile`, the try-each-backend selection is
> `docker/blender/startup.py`'s `render_pre` handler, and the format table
> plus the bounding-box camera framing are in
> `docker/blender-mcp/render_tools.py`. `docker compose --profile render run`
> is replaced by the `render_image` MCP tool.

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
`docker/render/render.py` (the one-shot batch service; deleted 2026-09-22,
and its `_enable_gpu()` here is now the lazy `_gpu_on_render` render_pre
handler — see the 2026-09-22 section). Blender ships with
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

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **freecad-mcp-custom** (1300 symbols, 2660 relationships, 111 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/freecad-mcp-custom/context` | Codebase overview, check index freshness |
| `gitnexus://repo/freecad-mcp-custom/clusters` | All functional areas |
| `gitnexus://repo/freecad-mcp-custom/processes` | All execution flows |
| `gitnexus://repo/freecad-mcp-custom/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
