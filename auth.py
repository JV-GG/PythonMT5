"""
API Key authentication middleware.
"""
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from config import get_settings


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates X-API-Key header against configured API key.
    Skips authentication for health check endpoints.
    """

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()

        if not settings.api_key:
            return await call_next(request)

        excluded_paths = {"/", "/health", "/docs", "/openapi.json", "/redoc"}
        if request.url.path in excluded_paths:
            return await call_next(request)

        # Customer builds: the bot trades on its own from the signal feed, so the local control API is turned off.
        # (It would otherwise be unauthenticated here, since portal builds carry no api_key.)
        if settings.portal_mode:
            return JSONResponse(status_code=403, content={"success": False, "error": "The control API is disabled on this build."})

        api_key = request.headers.get("X-API-Key")
        if not api_key or api_key != settings.api_key:
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "Invalid or missing API key."},
            )

        return await call_next(request)
