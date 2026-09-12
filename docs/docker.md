# Docker deployment

[Back to README](../README.md)

Runs three containers on one server: the MCP server (reachable over HTTP with
an API key — clients need no local install), a headless FreeCAD hosting the
RPC addon, and an on-demand Blender renderer.

## Architecture

- **`freecad`** — Ubuntu + FreeCAD, launched under `xvfb-run` (a virtual
  display; the addon still needs `FreeCADGui`/Coin3D for screenshots and
  views). The addon and a seeded `freecad_mcp_settings.json`
  (`auto_start_rpc: true`, `remote_enabled: true`) are baked into the image,
  so the RPC server on port 9875 comes up automatically — no manual toolbar
  click. Not published to the host; only the `mcp` container can reach it.
- **`mcp`** — the MCP server, `--transport streamable-http` gated by
  `FREECAD_MCP_API_KEY` (see `src/freecad_mcp/http_auth.py`), connecting to
  `freecad` over the compose network. Also has its own headless FreeCAD CLI
  install for `execute_code_headless`, which runs `freecadcmd` as a local
  subprocess independent of the RPC connection.
- **`render`** — Ubuntu + Blender, run on demand (`docker compose run`), not a
  long-running service. Renders a model exported to the shared volume.

Both `freecad` and `mcp` share the `freecad_data` volume at `/data` so
document/export paths agree between the RPC connection and any headless
script.

## Running it

```bash
cp .env.example .env
# edit .env: set FREECAD_MCP_API_KEY (e.g. `openssl rand -hex 32`)

docker compose up -d --build freecad mcp
```

Point an MCP-over-HTTP client at `http://<server>:8000`, with header
`X-API-Key: <your key>` (or `Authorization: Bearer <your key>`).

Render a model that's been exported to the shared volume (e.g. via
`execute_code_headless` + `Shape.exportStep`/`exportBrep`):

```bash
docker compose --profile render run --rm render \
  --input /data/part.step --output /data/part.png
```

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
