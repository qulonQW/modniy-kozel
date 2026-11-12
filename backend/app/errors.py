"""One error format for every API response:

    {"detail": "<English text for developers>", "code": "<machine code>", ...}

"detail" stays a string (the admin page shows Ukrainian texts by status and
code). Validation errors list the failing fields but never echo the
submitted values back (a phone number or name would otherwise be returned
in the response). Unexpected errors are logged by exception type and path
only: no bodies, headers, initData or tokens.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("tovarka.errors")

STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    411: "length_required",
    413: "too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
}

# JSON bodies (orders, admin edits) are small; uploads have their own limit.
MAX_JSON_BODY = 64 * 1024


def error_body(status: int, detail: str, code: str | None = None, **extra) -> dict:
    if code is None:
        # "favorites_full: at most 200" -> favorites_full
        prefix = detail.split(":", 1)[0]
        code = prefix if prefix.isidentifier() and prefix.islower() and ":" in detail else None
    return {"detail": detail, "code": code or STATUS_CODES.get(status, "error"), **extra}


def install(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else STATUS_CODES.get(exc.status_code, "error")
        return JSONResponse(
            error_body(exc.status_code, detail), status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        fields = [
            {"loc": [str(part) for part in err.get("loc", ())], "msg": str(err.get("msg", ""))[:200]}
            for err in exc.errors()[:20]
        ]
        return JSONResponse(
            error_body(422, "Validation failed", "validation_error", errors=fields), status_code=422
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        log.error("unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
        return JSONResponse(error_body(500, "Internal server error", "internal_error"), status_code=500)

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):
        """Rejects oversized JSON bodies before they are read. The image
        upload route checks its own (larger) limit."""
        if request.method in ("POST", "PUT", "PATCH") and not request.url.path.endswith("/images"):
            length = request.headers.get("content-length")
            if request.headers.get("transfer-encoding", "").lower() == "chunked" and length is None:
                return JSONResponse(error_body(411, "Content-Length required"), status_code=411)
            if length is not None and (not length.isdigit() or int(length) > MAX_JSON_BODY):
                return JSONResponse(error_body(413, "Request body too large"), status_code=413)
        return await call_next(request)


__all__ = ["install", "error_body", "HTTPException", "MAX_JSON_BODY"]
