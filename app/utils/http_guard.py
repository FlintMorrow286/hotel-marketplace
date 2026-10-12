"""HTTP-level guards that belong to no route: body size, absurd nesting, security headers.

All three are plain ASGI, so they cost nothing on streamed responses and see a request before
the framework starts parsing (and spooling) it.
"""

from __future__ import annotations

import re

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config.settings import settings

_PHOTO_UPLOAD = re.compile(r"^/v1/partner/properties/[^/]+/photos/?$")
# multipart framing around the file itself
_MULTIPART_OVERHEAD = 256 * 1024
_WRITE_METHODS = {"POST", "PUT", "PATCH"}


def body_limit(path: str) -> int:
    """The most bytes a request to `path` may carry."""
    if _PHOTO_UPLOAD.match(path):
        return settings.photo_max_bytes + _MULTIPART_OVERHEAD
    return settings.max_body_bytes


def _too_large() -> JSONResponse:
    return JSONResponse(status_code=413, content={"detail": "request body too large"})


class BodyLimitMiddleware:
    """Refuse an oversized body before the framework parses it.

    Starlette's multipart parser writes a 200 MiB upload to a temp file before the route sees
    the first byte, and a 5 MiB JSON body reached validation before it was refused. A declared
    Content-Length over the limit is answered at once; a body without one (chunked) is counted
    as it is read and raises an HTTPException(413), which FastAPI lets through where it would turn
    any other error raised while reading the body into a 400.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _WRITE_METHODS:
            await self.app(scope, receive, send)
            return

        limit = body_limit(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = False
            if too_big:
                await _too_large()(scope, receive, send)
                return

        received = 0

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail="request body too large")
            return message

        await self.app(scope, counted, send)


# Fonts come from Google Fonts (web/index.html); everything else is this origin. Swagger UI
# (/docs, /redoc) loads scripts from a CDN, so those pages are left without the policy.
_CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
_NO_CSP_PREFIXES = ("/docs", "/redoc", "/openapi.json")
_HSTS = "max-age=31536000"


class SecurityHeadersMiddleware:
    """Headers every response should carry; HSTS only when the request came in over https."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        https = scope.get("scheme") == "https"
        # Only while those pages exist: with the docs off, /docs is the SPA's own fallback page.
        with_csp = not (settings.docs_enabled and scope["path"].startswith(_NO_CSP_PREFIXES))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                if with_csp:
                    headers.setdefault("Content-Security-Policy", _CSP)
                if https:
                    headers.setdefault("Strict-Transport-Security", _HSTS)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install_http_guards(app: FastAPI) -> None:
    """Wire the middleware and the handler for absurd nesting."""

    async def on_nesting(request, exc: Exception) -> JSONResponse:
        # json.loads gives up at about a thousand levels with RecursionError, which was a 500.
        return JSONResponse(
            status_code=400, content={"detail": "request body is nested too deeply"}
        )

    app.add_exception_handler(RecursionError, on_nesting)
    # The last one added is the outermost: headers go on everything, even a refused body.
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
