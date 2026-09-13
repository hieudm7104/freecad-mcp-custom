"""Minimal OAuth 2.1 Authorization Code + PKCE shim for the streamable-http
transport.

Some MCP clients (e.g. ChatGPT's "Connector" UI) only support two auth
modes for a remote MCP server: no auth, or a real OAuth flow — there is no
plain static-API-key option. This module gives such clients that OAuth
flow while still ultimately gating access on the single
``FREECAD_MCP_API_KEY`` already used for ``--transport streamable-http``:

- ``GET/POST /oauth/authorize`` shows a one-field login page asking for that
  API key as a password (there is exactly one user of this server) and, on a
  correct submission, redirects back to the client with a short-lived
  authorization code.
- ``POST /oauth/token`` exchanges that code (or a refresh token) for a
  signed, short-lived access token.
- ``GET /.well-known/oauth-authorization-server`` publishes RFC 8414
  discovery metadata, for clients that support it.

Tokens are self-contained HMAC-signed JSON (a hand-rolled JWT-alike — no new
dependency), keyed off the API key itself, so no server-side session store
is needed beyond the few-second-lived authorization codes.

This is intentionally not a general-purpose OAuth server: there is exactly
one user (whoever knows ``FREECAD_MCP_API_KEY``). Two ways to register a
client are supported, because different MCP clients differ here:

- A static ``FREECAD_MCP_OAUTH_CLIENT_ID`` — register it on the client side
  as a "User-Defined OAuth Client" (what ChatGPT's Connector UI does; it has
  a field to type the id into).
- Dynamic Client Registration (RFC 7591) at ``POST /oauth/register`` — for
  clients that self-register and have no such field (what Claude.ai's custom
  connector flow does). The issued client_id is self-verifying (see
  ``_issue_client_id``), so it needs no server-side store and survives
  restarts. Neither path is a security boundary: the client_id is public and
  access is still gated entirely on the API key + PKCE.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

_AUTH_CODE_TTL = 120  # seconds; only needs to survive the redirect round trip
_ACCESS_TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days
_REFRESH_TOKEN_TTL = 60 * 60 * 24 * 365  # 1 year

# Authorization codes are single-use and live for seconds, so an in-memory
# dict (no persistence across restarts) is fine.
_auth_codes: dict[str, dict] = {}


def _signing_key(api_key: str) -> bytes:
    return hashlib.sha256(f"{api_key}|freecad-mcp-oauth-signing".encode()).digest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign_token(payload: dict, api_key: str) -> str:
    segments = [
        _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()),
        _b64url(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode()
    sig = hmac.new(_signing_key(api_key), signing_input, hashlib.sha256).digest()
    segments.append(_b64url(sig))
    return ".".join(segments)


def verify_token(token: str, api_key: str) -> dict | None:
    """Return the token's claims if valid and unexpired, else ``None``."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected_sig = hmac.new(_signing_key(api_key), signing_input, hashlib.sha256).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    if not hmac.compare_digest(expected_sig, actual_sig):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def _code_challenge_ok(verifier: str, challenge: str, method: str) -> bool:
    if method == "plain":
        return hmac.compare_digest(verifier, challenge)
    if method == "S256":
        digest = hashlib.sha256(verifier.encode()).digest()
        return hmac.compare_digest(_b64url(digest), challenge)
    return False


def _issue_client_id(api_key: str) -> str:
    """Mint a public client identifier for a Dynamic Client Registration.

    Made self-verifying (a random half plus a truncated HMAC of it) rather
    than stored, so it survives a server restart with no database — the same
    reason the tokens above are stateless. The client_id is not a secret and
    grants nothing on its own: /oauth/authorize still requires the API key,
    and the token exchange still requires the PKCE verifier.
    """
    rnd = secrets.token_hex(8)
    sig = hmac.new(_signing_key(api_key), f"dcr:{rnd}".encode(), hashlib.sha256).hexdigest()[:16]
    return f"dcr-{rnd}-{sig}"


def _client_id_ok(client_id: str, configured_client_id: str, api_key: str) -> bool:
    """Accept the statically configured client_id or any we issued via DCR."""
    if configured_client_id and hmac.compare_digest(client_id, configured_client_id):
        return True
    parts = client_id.split("-")
    if len(parts) == 3 and parts[0] == "dcr":
        expected = hmac.new(_signing_key(api_key), f"dcr:{parts[1]}".encode(), hashlib.sha256).hexdigest()[:16]
        return hmac.compare_digest(parts[2], expected)
    return False


_LOGIN_PAGE = """<!doctype html>
<html><head><title>FreeCAD MCP</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:420px;margin:80px auto;padding:0 16px">
<h2>Authorize access to FreeCAD MCP</h2>
{message}
<form method="post">
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
  <input type="hidden" name="scope" value="{scope}">
  <label>API key<br>
    <input type="password" name="api_key" autofocus style="width:100%;padding:8px;box-sizing:border-box">
  </label>
  <p><button type="submit" style="padding:8px 16px">Authorize</button></p>
</form>
</body></html>"""


def register_oauth_routes(app: Starlette, api_key: str, client_id: str) -> None:
    """Add the OAuth endpoints to *app* (a ``streamable_http_app()`` instance)."""

    async def authorize(request: Request):
        if request.method == "GET":
            params = request.query_params
        else:
            params = await request.form()

        req_client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "S256")
        scope = params.get("scope", "")

        if not _client_id_ok(req_client_id, client_id, api_key):
            return JSONResponse({"error": "unauthorized_client"}, status_code=400)
        if not redirect_uri:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri required"},
                status_code=400,
            )
        if not code_challenge:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "PKCE code_challenge required"},
                status_code=400,
            )

        page_fields = dict(
            client_id=req_client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
        )

        if request.method == "GET":
            return HTMLResponse(_LOGIN_PAGE.format(message="", **page_fields))

        submitted_key = params.get("api_key", "")
        if not submitted_key or not hmac.compare_digest(submitted_key, api_key):
            return HTMLResponse(
                _LOGIN_PAGE.format(
                    message='<p style="color:#c00"><b>Wrong API key.</b></p>', **page_fields
                ),
                status_code=401,
            )

        code = secrets.token_urlsafe(32)
        _auth_codes[code] = {
            "client_id": req_client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "scope": scope,
            "exp": time.time() + _AUTH_CODE_TTL,
        }
        return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302)

    async def token(request: Request):
        form = await request.form()
        grant_type = form.get("grant_type")

        if grant_type == "authorization_code":
            entry = _auth_codes.pop(form.get("code", ""), None)
            if entry is None or entry["exp"] < time.time():
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if form.get("redirect_uri") != entry["redirect_uri"]:
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "redirect_uri mismatch"},
                    status_code=400,
                )
            # A public client may omit client_id at the token step; it already
            # proved possession via the code + PKCE verifier. Default to the id
            # bound into the code, not the statically configured one, so a
            # DCR-issued client isn't rejected here.
            if form.get("client_id", entry["client_id"]) != entry["client_id"]:
                return JSONResponse({"error": "invalid_client"}, status_code=400)
            if not _code_challenge_ok(
                form.get("code_verifier", ""), entry["code_challenge"], entry["code_challenge_method"]
            ):
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                    status_code=400,
                )
            scope = entry["scope"]
        elif grant_type == "refresh_token":
            claims = verify_token(form.get("refresh_token", ""), api_key)
            if claims is None or claims.get("typ") != "refresh":
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            scope = claims.get("scope", "")
        else:
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        now = int(time.time())
        jti = secrets.token_hex(8)
        access_token = _sign_token(
            {"typ": "access", "scope": scope, "iat": now, "exp": now + _ACCESS_TOKEN_TTL, "jti": jti},
            api_key,
        )
        refresh_token = _sign_token(
            {"typ": "refresh", "scope": scope, "iat": now, "exp": now + _REFRESH_TOKEN_TTL, "jti": jti},
            api_key,
        )
        return JSONResponse(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": _ACCESS_TOKEN_TTL,
                "refresh_token": refresh_token,
                "scope": scope,
            }
        )

    def _public_base(request: Request) -> str:
        # Behind the Cloudflare Tunnel deployment, cloudflared terminates TLS
        # at the edge and forwards plain HTTP to this container, so
        # request.base_url reports scheme "http" even though the client used
        # "https". Trust X-Forwarded-Proto/-Host (set by Cloudflare) when
        # present so published URLs stay https.
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
        return f"{scheme}://{host}"

    async def register(request: Request):
        # RFC 7591 Dynamic Client Registration. Claude.ai's custom-connector
        # flow (unlike ChatGPT's) has no field to type a client_id into — it
        # registers itself here automatically, then runs the normal
        # authorize/token flow with the id we return. Public client, no
        # secret; the API key entered at /oauth/authorize stays the only real
        # gate, so we accept any registration and just hand back an id.
        try:
            body = await request.json()
        except Exception:
            body = {}
        now = int(time.time())
        response = {
            "client_id": _issue_client_id(api_key),
            "client_id_issued_at": now,
            "token_endpoint_auth_method": "none",
            "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
            "response_types": body.get("response_types", ["code"]),
            "redirect_uris": body.get("redirect_uris", []),
            "scope": body.get("scope", "mcp"),
        }
        if body.get("client_name"):
            response["client_name"] = body["client_name"]
        return JSONResponse(response, status_code=201)

    async def metadata(request: Request):
        base = _public_base(request)
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token",
                "registration_endpoint": f"{base}/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256", "plain"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": ["mcp"],
            }
        )

    async def protected_resource_metadata(request: Request):
        # RFC 9728: tells clients that the actual MCP endpoint lives at
        # "<base>/mcp" (not at the bare domain) and which authorization
        # server issues tokens for it. This is what lets a spec-compliant
        # client auto-discover the right path/resource instead of the user
        # having to type "https://.../mcp" and a "resource" value by hand.
        base = _public_base(request)
        return JSONResponse(
            {
                "resource": f"{base}/mcp",
                "authorization_servers": [base],
            }
        )

    app.add_route("/oauth/authorize", authorize, methods=["GET", "POST"])
    app.add_route("/oauth/token", token, methods=["POST"])
    app.add_route("/oauth/register", register, methods=["POST"])
    app.add_route("/.well-known/oauth-authorization-server", metadata, methods=["GET"])
    app.add_route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"])
    app.add_route("/.well-known/oauth-protected-resource/mcp", protected_resource_metadata, methods=["GET"])
