# Docker deployment

[Back to README](../README.md)

Runs six containers on one server: the FreeCAD MCP server (reachable over
HTTP with an API key — clients need no local install), a headless FreeCAD
hosting the RPC addon, a persistent GUI Blender instance for live AI-driven
scene editing and rendering, that instance's own MCP server, a chat harness
that drives both from a browser, and a MinIO bucket both MCP servers save
projects into.

> **Service names changed on 2026-09-22.** `freecad` → `freecad-headless`,
> `mcp` → `mcp-freecad`, `blender` → `blender-cli`, `blender-mcp` →
> `mcp-blender`; the one-shot `render` service is gone, folded into
> `blender-cli` as an MCP tool. The `docker/` build-context folders keep
> their old names deliberately — nothing resolves a service by its build
> path, and renaming `docker/blender/` would break `git log --follow` on
> `addon.py`, 3,900 vendored lines with one local patch in them.
>
> **Hyphens, not underscores**, because these names are used as hostnames:
> `mcp-freecad` runs its `--host` through `validators.hostname()`, which
> rejects `_` (underscores are illegal in an RFC-1123 host name), so
> `--host freecad_headless` exits 2 and crash-loops under
> `restart: unless-stopped`.

## Architecture

- **`freecad-headless`** (`docker/freecad/`) — Debian (`debian:trixie-slim`)
  + FreeCAD **1.1.3 from the
  official AppImage** (see "FreeCAD version" below for why not apt), launched
  under a manually-started `Xvfb` (a virtual display; the addon still needs
  `FreeCADGui`/Coin3D for screenshots and views). Ubuntu 24.04 no longer
  packages `freecad` at all, and `xvfb-run`'s SIGUSR1 ready-handshake with
  `Xvfb` hangs forever on this base image, so `docker/freecad/entrypoint.sh`
  starts `Xvfb` itself and waits for its socket instead of using `xvfb-run`.
  The addon and a seeded `freecad_mcp_settings.json`
  (`auto_start_rpc: true`, `remote_enabled: true`) are baked into the image,
  so the RPC server on port 9875 comes up automatically — no manual toolbar
  click. Not published to the host; only `mcp-freecad` can reach it. No GPU
  device is requested for it, so it holds **0 MiB of VRAM** — it renders its
  viewport with Mesa llvmpipe on the CPU instead, which is not free either
  (see "Why the Blender viewport was 30x slower" below).
- **`mcp-freecad`** (`docker/mcp/`) — the MCP server, `--transport
  streamable-http` gated by
  `FREECAD_MCP_API_KEY` (see `src/freecad_mcp/http_auth.py`), connecting to
  `freecad-headless` over the compose network. Also carries its own headless
  FreeCAD CLI — `freecadcmd` from the same 1.1.3 AppImage, pinned to the same
  version as `freecad-headless` — for `execute_code_headless`, which runs it
  as a local subprocess independent of the RPC connection. Only the binary is
  used, always as a subprocess, so the AppImage's bundled Python 3.11 never
  has to agree with this image's 3.12. Also serves the `/preview` page and
  the preview routes the harness proxies.
- **`blender-cli`** (`docker/blender/`) — a persistent, GUI Blender 5.2.1
  instance (Xvfb, same reasoning as `freecad-headless`) with
  [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp)'s
  addon vendored in and auto-enabled, for live AI-driven scene editing
  (create/edit objects, materials, lights, arbitrary Python, viewport
  screenshots) **and** Cycles rendering — as of 2026-09-22 this is also where
  renders happen, the separate `render` container having been folded into it.
  Not published to the host; only `mcp-blender` can reach its socket (9876).
  Has the nvidia GPU reservation; holds ~180 MiB of VRAM while idle for its
  viewport GL context and ~2.5 GB more for the duration of a render.
- **`mcp-blender`** (`docker/blender-mcp/`) — that project's MCP server
  (`pip install blender-mcp`,
  imported as a library, not forked), wrapped in the same streamable-http +
  API-key/OAuth layer as `mcp-freecad`, since upstream only
  ships a stdio transport. Adds this deployment's own storage tools and the
  `render_image` tool. See "Blender MCP integration" below.
- **`harness`** (`harness/`) — a Node/TypeScript agent loop
  (`@earendil-works/pi-agent-core`) that is an MCP *client* of both servers
  above, plus the static host for the React chat/preview UI built from
  `frontend/`. The only thing a browser talks to. See "Chat harness and
  frontend" below.
- **`minio`** — an S3-compatible bucket holding saved FreeCAD documents,
  Blender scenes, exports and renders. Both MCP servers get save/load tools
  backed by it. See "Object storage (MinIO)" below.

`freecad-headless`, `mcp-freecad`, `blender-cli` and `mcp-blender` all share
the `freecad_data` volume at `/data`, so document/export/render paths agree
between the RPC connection, any headless script and Blender — that's also the
staging area the storage tools upload from and download to. `blender-cli` and
`mcp-blender` additionally share a `blender_tmp` volume (see the Blender
section for why). `harness` mounts neither: it only speaks HTTP.

## Running it

```bash
cp .env.example .env
# edit .env: set FREECAD_MCP_API_KEY (e.g. `openssl rand -hex 32`)

docker compose up -d --build --remove-orphans
```

`--remove-orphans` matters on the first `up` after the 2026-09-22 rename: the
containers created under the old service names are orphans of the project now,
they keep holding host ports 8001/8002, and without it compose leaves them
running and then fails to create the renamed ones with `bind for
0.0.0.0:8001 failed: port is already allocated`. It is also what stops the
old containers, so anything open in FreeCAD or Blender and not saved to MinIO
is gone — save first (see "Object storage" below).

Point an MCP-over-HTTP client at `http://<server>:8001/mcp` (`MCP_PORT`), with
header `X-API-Key: <your key>` (or `Authorization: Bearer <your key>`). The
Blender MCP server is the same shape on `BLENDER_MCP_PORT` (8002), and the
browser UI is on `HARNESS_PORT` (8003), published on `127.0.0.1` only —
`ssh -L 8003:127.0.0.1:8003 <server>` to open it from your laptop.

Just the CAD half, without Blender or the chat UI:
`docker compose up -d --build --remove-orphans freecad-headless mcp-freecad minio`.

| `.env` variable | Default | What it is |
| --- | --- | --- |
| `FREECAD_MCP_API_KEY` | *(required)* | Key for the FreeCAD MCP server and its `/preview` page |
| `BLENDER_MCP_API_KEY` | *(required)* | Same, for the Blender MCP server |
| `FREECAD_MCP_OAUTH_CLIENT_ID` / `BLENDER_MCP_OAUTH_CLIENT_ID` | unset | Enables the OAuth shim (see below) |
| `MCP_PORT` / `BLENDER_MCP_PORT` / `HARNESS_PORT` | 8000 / 8002 / 8003 | Host ports. **`MCP_PORT` must stay 8001 here** — see the tunnel section. `HARNESS_PORT` is published on 127.0.0.1 only |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` / `MINIO_BUCKET` | *(required)* / *(required)* / `cad` | Object storage |
| `MINIO_API_PORT` / `MINIO_CONSOLE_PORT` | 9000 / 9001 | Published on 127.0.0.1 only |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `HARNESS_MODEL` | unset | The OpenAI-compatible endpoint the chat harness talks to |

`.env` must have **LF line endings**. `docker compose` copes with CRLF, but
any `grep VAR .env | cut -d= -f2` used to pull a value out for a manual test
picks up a trailing `\r`, and comparing against that value (the OAuth
`client_id`, for one) then fails in a way that looks like an auth bug.
`sed -i 's/\r$//' .env` fixes it; `cat -A .env` shows it.

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
`tools/list` returns the full tool set (22 FreeCAD, 33 Blender at the time;
the Blender side gained `render_image` since).

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

## Rendering

**There is no `render` service any more** (removed 2026-09-22, along with
`docker/render/`). Rendering is an MCP tool on the Blender server —
`render_image` (`docker/blender-mcp/render_tools.py`) — which sends a render
script down the existing `execute_code` socket into the live `blender-cli`
instance:

```
render_image(output="part.png", samples=128,
             resolution_x=1920, resolution_y=1080, frame=True)
```

`output` is relative to `/data`. `frame=True` (the default) aims a
`MCPRenderCam` at the scene's bounding box, or at the selection if there is
one; pass `frame=False` to render through a camera you placed yourself — the
default is on because Blender's startup scene always ships a camera aimed at
nothing in particular.

The tool returns the JSON the script prints (path, `camera`,
`scene.cycles.device`, backend, samples, resolution) plus `bytes` and
`verified: "file written; pixels not checked"`. It refuses to report success
if the file is missing *or* if its mtime did not move, since
`bpy.ops.render.render()` returns `{'CANCELLED'}` without raising and would
otherwise hand back the previous render's byte count. None of that is evidence
the picture is right — look at it. Keep the result with
`upload_file_to_storage`.

The old container booted a *second* Blender, imported a file and rendered
that — blind to the materials, lights and camera the agent had just built in
the live instance, which is the scene anyone actually wants a picture of.
Importing a mesh is already reachable through `execute_blender_code`, so all
that survived the fold-in is the render itself plus the two pieces of
hard-won knowledge below. `argparse`, the importer table, `_clear_scene` and
`use_denoising = False` (a workaround for apt Blender's missing
OpenImageDenoiser; the official build has it) all went.

The tool lives in the `mcp-blender` image rather than `blender-cli` on
purpose: editing anything baked into `blender-cli` costs a rebuild and a
restart, and a restart throws away the in-memory scene — which is the whole
reason those two containers are split.

**Camera and light are only added when the scene has none** (`scene.camera
is None`). An existing camera, lighting and world are left alone, so a scene
the agent lit deliberately renders the way it was lit. When it does frame,
it frames the *selection* if there is one and everything visible otherwise —
a studio scene's backdrop plane is orders of magnitude bigger than the
subject, and framing everything leaves the model a speck. The objects it
creates are named `MCPRenderCam` / `MCPRenderSun` and reused, rather than
`bpy.ops.object.camera_add` dropping a fresh `Camera.001` into the user's
scene on every render.

**To render a CAD part**, export it from FreeCAD to `/data` as a mesh
(STL/OBJ/glTF/FBX), **not STEP** — Blender has no STEP importer, official
builds included (`bpy.ops.import_scene.step` doesn't exist at all; STEP is a
B-rep exchange format, not something Blender's mesh pipeline reads). From
FreeCAD: `doc.getObject(name).Shape.exportStl("/data/part.stl")` (or
`.exportBrep` only if you need the raw B-rep elsewhere — never for this
render step). Then import it with `execute_blender_code` and call
`render_image`.

**Output isn't limited to PNG** — `output`'s extension picks the format, and
`.jpg`/`.jpeg`, `.tiff`/`.tif`, `.exr`, `.webp`, `.bmp`, `.tga` all work
(each tested end-to-end back when this was a CLI), alongside PNG. A naive
`extension.upper()` isn't enough, though: Blender's format enum doesn't
always match the extension's own spelling (`.jpg` → the enum is `"JPEG"`,
not `"JPG"`; `.tif` → `"TIFF"`; `.exr` → `"OPEN_EXR"` — each of those raises
`TypeError` rather than silently doing the wrong thing) —
`_FILE_FORMAT_OVERRIDES` in `render_tools.py` has the table. Still a
**single still image per call** — no animation/video output (would need
frame-range and `FFMPEG` container/codec settings this doesn't set up).

**Run the self-check after editing that file**: `python3
docker/blender-mcp/render_tools.py` `ast.parse`s the script it would send
(including a path with quotes in it) and asserts the format table. The script
is assembled from f-strings and only ever runs inside Blender, where a syntax
error comes back as an opaque socket reply in the middle of a render.

Two caveats worth knowing before leaning on it:

- The render runs **inline on the addon's single command queue**, so it
  blocks preview frames and every other tool call for its duration (~2–4 s at
  128 samples; both socket ends time out at 180 s). There's a `ponytail:`
  comment on it saying to move it to a `blender -b` subprocess if renders
  ever get heavy.
- **Not yet verified at runtime**: that `bpy.ops.render.render()` behaves
  inside `bpy.app.timers` in a GUI Blender. If it misbehaves, try
  `bpy.ops.render.render('EXEC_DEFAULT', write_still=True)` first.

### GPU rendering (Cycles OptiX/CUDA)

Cycles picks a backend by trying OPTIX, then CUDA, then HIP, then ONEAPI and
falling back to CPU. This needs the host to have `nvidia-container-toolkit`
and the `blender-cli` service in `docker-compose.yml` requesting a GPU device
(both already set up here) — confirmed working end-to-end by exporting a real
FreeCAD object, rendering it, visually checking the output image (not just
that a file appeared), and watching `nvidia-smi` during the run: **a simple
single-object scene used ~2.5 GB of VRAM** (`36144 MiB` baseline →
`38632 MiB` peak); scale that up for denser scenes.

Three non-obvious things this required (all found on the old `render.py`;
the code now lives in `docker/blender/Dockerfile` and
`docker/blender/startup.py`):

1. Ubuntu's apt `blender` package (used originally) is a stripped-down
   "dfsg" rebuild missing non-free bits: **no STEP importer, no
   `OpenImageDenoiser`, and no working CUDA/OptiX Cycles kernels** —
   confirmed by actually running a render and watching `nvidia-smi` stay
   completely flat regardless of `--device`. Fixed by switching the
   Dockerfile to download the official blender.org binary instead of
   `apt-get install blender` (see the Dockerfile for why a mirror URL is
   used — `download.blender.org` itself sits behind a Cloudflare challenge
   that blocks a plain `curl`/`wget`).
2. Cycles' denoising is on by default and needed `OpenImageDenoiser`,
   which the apt build lacked, so `render.py` set
   `scene.cycles.use_denoising = False`. The official build ships it, so
   `render_image` doesn't touch the setting at all — the scene's own choice
   stands.
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
   `docker/blender/Dockerfile`'s `ARG BLENDER_VERSION` controls this if a
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
existed. Fixed by computing the objects' real world-space bounding box
(`.bound_box` transformed by `.matrix_world`) and placing/aiming the camera
and sun light from that, which depends on no viewport existing. That
bounding-box framing is what `render_tools.py` carries forward — the one
piece of the old script worth keeping besides the format table.

### What holds VRAM, and when

Asked directly, and measured with `nvidia-smi` rather than reasoned about
(2026-09-22):

| process | idle VRAM | why |
| --- | --- | --- |
| `freecad-headless` | **0 MiB** | requests no GPU device at all; software GL, so it burns ~0.74 CPU cores round the clock instead |
| `blender-cli` | **180 MiB** | the viewport's GL context — what makes the live preview 27x faster than llvmpipe, and what the preview panel is drawing |
| Cycles, mid-render | **+~2.5 GB** | released by Blender itself ~0.5 s after the render finishes |

Cycles' GPU setup was moved out of container startup into a `render_pre`
handler (`docker/blender/startup.py`'s `_gpu_on_render`, `@persistent`) so
the CUDA/OptiX driver is only opened once something actually renders, and so
one copy of the logic covers every render path: the `render_image` tool, a
hand-written `bpy.ops.render.render()` through `execute_blender_code`, or
F12. It also sets `scene.cycles.device = "GPU"` on *every* render, not just
the first, because that setting lives inside the `.blend` while the backend
choice lives in preferences — a scene restored from MinIO that was saved on
CPU otherwise carries CPU back in with it. The old `load_post` handler that
existed for that hazard is gone.

**Be honest about what that bought: 0 MiB.** Assigning `compute_device_type`
is free, `get_devices()` dlopens libnvoptix and opens `/dev/nvidia-uvm` but
reserves nothing, and Cycles already freed its working set on its own. There
was never idle Cycles VRAM to reclaim — before/after `nvidia-smi` reads the
same. The 180 MiB that remains is the viewport context the preview panel
needs; the only way to get that back is to stop running a GUI Blender. The
change is worth having because it collapses two copies of the enable logic
into one and doesn't touch the driver at boot, not because it frees memory.

**The real risk is contention, not footprint.** This GPU is shared with
unrelated services: 38101 of 48935 MiB were already in use at the time of
measuring, `sglang::scheduler` alone holding 31720 MiB, leaving ~10.8 GB of
headroom. A render spikes ~2.7 GB against that for a few seconds. If the
chat harness ever fires renders in a loop, serialise them there.

## FreeCAD version: 1.1.3 from the official AppImage

The `freecad-headless` and `mcp-freecad` services both install
**FreeCAD 1.1.3** by
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

Cost: the images grow from 2.56 GB to 4.66 GB (`freecad-headless`) and
4.86 GB (`mcp-freecad`). The 783 MB download happens once per Dockerfile
instead of being
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

> **On.** The routes below are served when `FREECAD_MCP_PREVIEW=1` is set on
> `mcp-freecad` — plus `BLENDER_MCP_PREVIEW=1` on `mcp-blender` for the
> Blender tab, since that half serves the viewport routes this one proxies
> to. Both are `1` in `docker-compose.yml`, because the browser frontend's
> preview panel is these same routes, proxied again by the harness.
>
> They were `0` from 2026-09-13 to 2026-09-22, and the reason still stands:
> an open preview tab polls a screenshot continuously and a screenshot is
> never free — see the pacing and cost measurements below. With the flags off
> every `/preview*` path answers 401 even with a valid key, and the MCP tools
> are completely unaffected (verified then: 22 FreeCAD tools and 33 Blender
> tools still listed over the public HTTPS endpoints, and OAuth discovery
> still answered 200 unauthenticated). Turning them back off is a
> `docker compose up -d mcp-freecad mcp-blender` away; no rebuild needed, the
> code stays in the image either way. The cheaper lever is the page's own
> **⏸ Pause** button.

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
`blender-cli` container's GUI, see "Blender MCP integration" below), with the
same drag-to-orbit / scroll-to-zoom controls plus:

- a **Shading** dropdown — Solid / Material / Rendered / Wireframe, i.e.
  Blender's own viewport shading modes, so materials and lighting can be
  checked without doing a full Cycles render;
- a **Camera view** button — snaps the viewport back to what the scene
  camera sees, i.e. the framing an actual render will use.

Only the FreeCAD server serves HTML. The Blender viewport routes live on the
*`mcp-blender`* server (`docker/blender-mcp/preview_api.py`, same route names)
and this server **proxies** them at `/preview/blender*` via
`BLENDER_MCP_URL` (set in docker-compose.yml). That way the browser talks to
a single origin — no CORS for the control POSTs — and `BLENDER_MCP_API_KEY`
never leaves the server. Unset that variable and the tab simply doesn't
render.

One Blender-only wrinkle the lazy GPU init added: viewport **RENDERED**
shading is the one Cycles path that doesn't fire `render_pre`, so
`preview_api.py`'s shading route calls the registered `render_pre` handlers
itself. Without that, picking Rendered in this dropdown would quietly draw on
the CPU — no error, a correct-looking image, just slow. That is exactly how
the original CPU-rendering bug hid for a day.

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
frames). The `mcp-blender` proxy container's own cost is unmeasurable
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

`docker/freecad/entrypoint.sh` still forces software GL, and
`freecad-headless` has no GPU reservation at all — which is also the answer
to "does FreeCAD eat VRAM": no, 0 MiB. It burns CPU instead, and that is
worse than it looks: measured
over 30 seconds with **nothing at all calling it**, the container
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

## Chat harness and frontend

`harness/` is a Node 22 + TypeScript service built on
[`@earendil-works/pi-agent-core`](https://www.npmjs.com/package/@earendil-works/pi-agent-core)
(with `pi-ai` for the provider, both pinned `0.87.0`) that is an MCP *client*
of both servers above, and that serves the React UI built from `frontend/` as
static files. Open it at **`http://127.0.0.1:8003`** (`HARNESS_PORT`): a chat
box on the left, a panel on the right with FreeCAD and Blender tabs showing
the live viewports, drag to orbit and scroll to zoom in either. From anywhere
else, tunnel to it: `ssh -L 8003:127.0.0.1:8003 <server>`.

It is the only thing the browser talks to. It holds `FREECAD_MCP_API_KEY` and
`BLENDER_MCP_API_KEY` server-side and proxies the preview routes, so **no key
ever reaches the page** and there is no second origin (hence no CORS on the
control POSTs).

**It has no auth of its own, which is why it is published on `127.0.0.1`
only** — the same treatment as `minio`, for strictly more: anyone who reaches
the page can ask the agent to run arbitrary Python inside both FreeCAD and
Blender, and read or overwrite the whole MinIO bucket, with both MCP keys
supplied for them. Putting it on the LAN, a Tailscale IP or the Cloudflare
tunnel means adding a key check in front of `route()` in `harness/src/
index.ts` first — the two MCP servers' `ApiKeyMiddleware` is the shape to
copy.

| route | does |
| --- | --- |
| `POST /api/chat` | `{messages:[{role,content}]}` → `text/event-stream` of `Frame`s: `{t:"text",delta}`, `{t:"tool",phase,id,…}`, `{t:"error",message}` (defined in `harness/src/wire.ts`, mirrored in `frontend/src/Chat.tsx`) |
| `GET /api/preview/{freecad,blender}.png` | proxied frame |
| `POST /api/preview/{freecad,blender}/{orbit,zoom,reset}` | query params only, never a body |
| `GET /api/health` | `{freecad:bool, blender:bool}` |
| `GET /*` | the built frontend, SPA fallback |

Configure the model with `OPENAI_BASE_URL`, `OPENAI_API_KEY` and
`HARNESS_MODEL` in `.env` — any OpenAI-compatible
`/v1/chat/completions` endpoint, **provided it does all three of**: tool
calling, `tool_calls` deltas while streaming (pi-agent-core drives everything
through `streamFn`, so a tool call that only arrives non-streamed never
arrives at all), and image input (`get_view` and `get_viewport_screenshot`
hand back image blocks). Check those against the endpoint before wiring it
in — a model that chats fine and cannot do one of them looks like a broken
harness. This deployment uses `nvidia/Qwen3.6-35B-A3B-NVFP4` via
`https://new-api.hieudm.site/v1`, which passes all three. pi-ai's built-in `openaiProvider()` hardcodes
`api.openai.com` and the Responses API, so the model is declared inline as a
`Model<"openai-completions">` and wrapped with `createProvider({auth:
envApiKeyAuth(…), api: openAICompletionsApi()})`. Its `contextWindow` /
`maxTokens` / `cost` fields are placeholders that only feed pi's own
accounting.

Things worth knowing before changing it:

- **Tool names are prefixed `freecad_` / `blender_`** because the two servers
  genuinely collide: `list_storage_files`, `upload_file_to_storage` and
  `download_file_from_storage` are registered by both. The unprefixed name
  stays in the closure — the remote server has never heard of the prefix.
- **Raw MCP JSON Schema goes straight through** as pi's `parameters`: pi-ai's
  validator sees the missing TypeBox `Kind` symbol and takes its
  plain-JSON-Schema path, so no conversion layer is needed. `npm test` in
  `harness/` runs the real `Agent` loop against a faux provider to prove
  exactly that.
- **One long-lived `Agent` is the conversation**, reset when a POST carries
  ≤1 message. The `{role,content}` wire shape can't carry pi's
  `AssistantMessage` (`usage`, `stopReason`, tool calls), so replaying the
  posted history per request would silently drop every tool call — the
  harness takes the last user message from the body and keeps the transcript
  itself. **It is therefore single-conversation and single-user**: two
  browsers share one agent.
- **MCP connections are made on the first chat, not at boot**, with the cache
  cleared on failure. A wrong key or a still-starting MCP server would
  otherwise crash-loop the container; instead the first request gets a 503
  and the next one retries.
- **The preview panel's frame pacing is ported from
  `src/freecad_mcp/preview.py`, not reinvented** — same chaining off the
  `<img>`'s `load`/`error`, same 900/120 ms two-speed gap, same half-frame
  floor, same pause button. It lives in `frontend/src/poll.js` as plain JS +
  JSDoc so `node --test` can run its assertions without a TypeScript loader;
  `tsc` still checks it via `checkJs`. Read "Live preview page" above for
  *why* it is shaped that way before touching it.

### Deployed 2026-09-22 — what was actually proven

- **The harness draws CAD.** A prompt through `POST /api/chat` produced
  `freecad_create_document` → `freecad_create_object` → `freecad_get_objects`,
  and the result was checked over XML-RPC independently of both the harness
  and the model: `volume = 9361.0` vs an expected 9361 for a 37x23x11 box,
  bbox 37/23/11, 6 faces, 1 solid.
- **57 tools merge cleanly** (23 FreeCAD + 34 Blender — the 34th is
  `render_image`), 57 unique names
  after prefixing; the three genuinely colliding storage tools are resolved
  by it. 0/56 fail pi-ai's real argument validator.
- **Both preview images were opened and inspected**, and an orbit changed the
  frame's md5 — not a `success: true` no-op.
- **`render_image`**: OPTIX, 1.8 s at 64 samples / 960x720, output inspected
  (a lit, framed cube). VRAM sampled every 0.5 s: 231 MiB idle → 2733 peak →
  212 MiB, flat for the next 18 s.
- **The model endpoint was vetted for three things before being wired in**:
  tool calling, `tool_calls` deltas while *streaming*, and image input. The
  harness needs all three — image input because `get_view` and
  `get_viewport_screenshot` return image blocks.

### Known gaps

- **Nobody has opened the frontend in a real browser.** Every route answers
  and the bundle serves, but the polling scheduler, the Pause button,
  drag-to-orbit and tool-call rendering have not been seen by a human.
  `ssh -L 8003:127.0.0.1:8003 <server>` → `http://127.0.0.1:8003`.
- **No FreeCAD project dropdown, no Blender shading/camera buttons** in the
  new UI — the harness proxies orbit/zoom/reset only. That matters because
  `activate_document` is *also* still not an MCP tool, so with the dropdown
  gone nothing can switch FreeCAD's active document, and `get_view` stays
  broken for a document whose `ActiveView` is a TechDraw page. Fix by adding
  the proxy route and the dropdown, or by exposing the tool.
- **A modal dialog in FreeCAD wedges every GUI tool, permanently and
  silently.** It happened for 32 hours (2026-09-21 01:30 → 2026-09-22 09:50)
  because FreeCAD's auto-recovery raised one at startup with nobody to
  dismiss it. `docker/freecad/entrypoint.sh` now clears the stale recovery
  dirs, `get_rpc_status` now reports `pump.blocked_by`, and the dispatch
  heartbeat is a repeating timer that cannot be lost — but the underlying
  hazard is unchanged: **generated code must never open a dialog here**.
  Symptom to recognise: `ping` and `get_rpc_status` answer instantly while
  every real tool times out at its `queue_timeout`. CLAUDE.md has the full
  autopsy.

## Blender MCP integration

**`https://blender-mcp.hieudm.site`** gives an MCP client live control of a
real, running Blender scene, and (since the `render` service was folded in)
renders it.
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
binary as the old `render` container did (for the same reasons — see the GPU
rendering section above), under a manually-started `Xvfb` (the same `xvfb-run` hang applies
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
than one for the same reason as `freecad-headless`/`mcp-freecad`: very
different dependency
footprints (CUDA + Xvfb + Blender vs. a slim Python + one pip install), and
restarting the HTTP wrapper (e.g. after an `oauth.py` tweak) shouldn't have
to restart the GUI Blender process, which holds all in-memory scene state —
losing that on every unrelated restart would be the same footgun as
FreeCAD's own documents, which likewise live only in RAM until explicitly
saved — every rebuild of the `freecad-headless` container during this
project's
development wiped whatever was open, more than once.

Three problems surfaced getting this actually working, each confirmed by
driving the real integration rather than just checking it started cleanly:

1. **The addon's socket only listened on `localhost`** inside the
   `blender-cli` container by default (upstream's
   `BlenderMCPServer.__init__(self, host='localhost', ...)`), invisible to
   `mcp-blender` connecting over the Docker network as `blender-cli:9876`. Fixed by changing that one default to
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
   (`/tmp/blendermcp`) in both `blender-cli` and `mcp-blender`, `TMPDIR` set to
   that path in both. Confirmed fixed by actually creating a red-material
   sphere via `execute_blender_code` and visually inspecting a real
   `get_viewport_screenshot` PNG (twice — once in default "Solid" viewport
   shading, where materials are invisible by design, and again after
   switching to `'MATERIAL'` shading via another `execute_blender_code`
   call, which showed the correct red, lit sphere).

### The live instance rendered on CPU (fixed in `startup.py`)

The "GPU rendering" section above was originally about
`docker/render/render.py`, the one-shot batch service (deleted 2026-09-22).
The persistent `blender-cli` container got the same GPU
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

`docker/blender/startup.py` selects a backend (try it and catch `TypeError`,
same reason as above) and sets `scene.cycles.device = 'GPU'`. Since
2026-09-22 it does this from a `render_pre` handler rather than at startup,
so the driver isn't opened until something renders and one copy of the logic
covers every render path — see "What holds VRAM, and when". The
`scene.cycles.device` assignment runs on every render, which also covers the
hazard the now-deleted `load_post` handler existed for: the backend and
enabled-devices choices live in preferences and survive a file load, but
`scene.cycles.device` is stored **inside the .blend**, so opening a file
saved on CPU — including ones already in MinIO — would otherwise silently put
renders back on the CPU.

Applying a change here needs a `docker compose up -d blender-cli`, which
**discards the in-memory scene** of the running instance (save to MinIO
first). A running instance can be switched without a restart by setting the
same preferences through `execute_blender_code`.

## Object storage (MinIO)

**A FreeCAD document or a Blender scene exists only in that app's process
memory until something writes it to disk.** Nothing in this deployment did
that automatically, so every `docker compose up --build` of
`freecad-headless` or `blender-cli` — routine during development — silently
threw away whatever was
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
only used inside the `mcp-freecad` container.

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
`harness/`, `frontend/`, `docker-compose.yml`, `.env.example`,
`.dockerignore`, this doc), so they won't conflict. `src/freecad_mcp/server.py` and `pyproject.toml` gained a
small, additive diff (a new `--transport`/`--port` branch in `main()`, one new
dependency) to keep any future conflict there small and easy to resolve.
