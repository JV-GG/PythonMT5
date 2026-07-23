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

        api_key = request.headers.get("X-API-Key")
        if not api_key or api_key != settings.api_key:
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "Invalid or missing API key."},
            )

        return await call_next(request)
