"""Serve the pip-installed `blender-mcp` package's MCP server over HTTP.

Upstream (https://github.com/ahujasid/blender-mcp) only ships a stdio
transport (spawned by an MCP client as a subprocess) — there's no remote/HTTP
mode. Rather than forking their ~1800-line server.py, this imports their
already-built `FastMCP` instance (`blender_mcp.server.mcp`) and wraps it in
the same streamable-http + API-key/OAuth layer already built for
freecad-mcp in this repo (see src/freecad_mcp/server.py's
_run_streamable_http and src/freecad_mcp/oauth.py — http_auth.py/oauth.py
here are adapted copies of those).

BLENDER_HOST/BLENDER_PORT (upstream's own env vars) point this at the
`blender` service's addon socket — see docker-compose.yml.
"""

import os

import uvicorn
from blender_mcp.server import mcp

from http_auth import ApiKeyMiddleware
from oauth import register_oauth_routes, verify_token


def main() -> None:
    api_key = os.environ.get("BLENDER_MCP_API_KEY")
    if not api_key:
        raise SystemExit("BLENDER_MCP_API_KEY must be set")

    # Same DNS-rebinding-protection gotcha as freecad_mcp/server.py: FastMCP
    # defaults to host="127.0.0.1", which auto-enables an allowed_hosts check
    # that rejects any request through a real hostname (a Cloudflare Tunnel
    # domain, or even a plain container-to-container request) with 421
    # Invalid Host header. This mcp version (1.30.0, resolved from
    # blender-mcp's own ">=1.9.0,<2" pin — newer than freecad_mcp's pinned
    # version) takes this as a `transport_security=` constructor kwarg on
    # FastMCP, not on streamable_http_app() like freecad_mcp's older mcp
    # release — but blender_mcp.server already constructed `mcp` for us, so
    # mutate the setting on the existing instance instead. Every request here
    # is already authenticated by ApiKeyMiddleware regardless of Host, so
    # disabling it is safe.
    mcp.settings.transport_security.enable_dns_rebinding_protection = False

    # Registered before the app is built (the tool list is baked in there),
    # and only when MinIO is configured — upstream blender-mcp has no
    # storage tools, so without this a scene lives only in Blender's memory.
    import storage

    if storage.is_configured():
        from storage_tools import register_storage_tools

        register_storage_tools(mcp)
        print(
            f"[blender-mcp] object storage tools enabled "
            f"(bucket '{storage.bucket_name()}' on {storage.endpoint()})",
            flush=True,
        )

    app = mcp.streamable_http_app()

    # Viewport routes for the Blender tab of the live preview page, which the
    # FreeCAD MCP server serves and proxies to (see preview_api.py). Opt-in
    # and off by default: an open preview tab polls a screenshot continuously,
    # which is sustained load on the Blender GUI process. The MCP tools are
    # unaffected either way.
    from preview_api import preview_enabled, register_preview_routes

    serve_preview = preview_enabled()
    if serve_preview:
        register_preview_routes(app, api_key=api_key)
        print("[blender-mcp] viewport preview routes enabled", flush=True)
    else:
        print(
            "[blender-mcp] viewport preview routes disabled "
            "(set BLENDER_MCP_PREVIEW=1 to enable)",
            flush=True,
        )

    oauth_client_id = os.environ.get("BLENDER_MCP_OAUTH_CLIENT_ID")
    verify = None
    if oauth_client_id:
        register_oauth_routes(app, api_key=api_key, client_id=oauth_client_id)
        verify = verify_token

    app.add_middleware(
        ApiKeyMiddleware, api_key=api_key, verify_token=verify, allow_preview=serve_preview
    )

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
