"""API-key auth for the streamable-http transport.

Adapted from ``src/freecad_mcp/http_auth.py`` in this repo — same design.
See that file for the fuller design notes.
"""

import hmac
from typing import Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_UNAUTHENTICATED_PATHS = {"/oauth/authorize", "/oauth/token"}


def _is_public_path(path: str, allow_preview: bool) -> bool:
    # /.well-known/* is always meant to be publicly readable (RFC 8414,
    # RFC 9728) — clients probe it before they have a token. /preview* is
    # exempt from the *header* check only, and only while those routes are
    # actually served: each one checks the same API key itself as a "?key="
    # query parameter, because they're loaded by an <img> tag/proxy that
    # can't set headers (preview_api.py).
    if path in _UNAUTHENTICATED_PATHS or path.startswith("/.well-known/"):
        return True
    return allow_preview and (path == "/preview.png" or path.startswith("/preview/"))


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Rejects requests that don't present the configured API key.

    Accepts either an ``X-API-Key`` header or ``Authorization: Bearer <key>``.
    If *verify_token* is given (see ``oauth.py``), a Bearer value that isn't
    the raw API key is also checked against it — this lets OAuth-only
    clients (e.g. ChatGPT's Connector UI) authenticate with a token issued
    by the ``/oauth`` endpoints instead of the static key.
    """

    def __init__(
        self,
        app,
        api_key: str,
        verify_token: Optional[Callable[[str, str], Optional[dict]]] = None,
        allow_preview: bool = False,
    ) -> None:
        super().__init__(app)
        self._api_key = api_key
        self._verify_token = verify_token
        self._allow_preview = allow_preview

    async def dispatch(self, request: Request, call_next):
        if _is_public_path(request.url.path, self._allow_preview):
            return await call_next(request)

        provided = request.headers.get("x-api-key")
        if not provided:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[len("bearer "):]

        if provided:
            if hmac.compare_digest(provided, self._api_key):
                return await call_next(request)
            if self._verify_token and self._verify_token(provided, self._api_key):
                return await call_next(request)

        return JSONResponse({"error": "unauthorized"}, status_code=401)
