"""API-key auth for the streamable-http transport.

stdio transport (the default, used by local/uvx clients) never goes through
this file — it only matters when the MCP server is exposed over HTTP, e.g.
from the Docker setup in docker-compose.yml.
"""

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Rejects requests that don't present the configured API key.

    Accepts either an ``X-API-Key`` header or ``Authorization: Bearer <key>``.
    """

    def __init__(self, app, api_key: str) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next):
        provided = request.headers.get("x-api-key")
        if not provided:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[len("bearer "):]
        if not provided or not hmac.compare_digest(provided, self._api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)
