"""Standardized /api/v1 response envelope and structured domain error codes."""
from __future__ import annotations

from typing import Any
import uuid

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

class ApiError(Exception):
    """Domain exception carrying a stable error code and HTTP status for /api/v1."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers

def get_request_id(request: Request | None) -> str:
    if request is not None:
        rid = getattr(request.state, "request_id", None)
        if rid:
            return str(rid)
    return uuid.uuid4().hex

def ok(
    request: Request,
    data: Any = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {
        "success": True,
        "data": jsonable_encoder(data),
        "error": None,
        "requestId": get_request_id(request),
    }
    return JSONResponse(status_code=status_code, content=body, headers=headers)

def fail(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    err_obj: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err_obj["details"] = jsonable_encoder(details)
    body = {
        "success": False,
        "data": None,
        "error": err_obj,
        "requestId": get_request_id(request),
    }
    return JSONResponse(status_code=status_code, content=body, headers=headers)

_STATUS_DEFAULT_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    410: "GONE",
    413: "REQUEST_TOO_LARGE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_SERVER_ERROR",
    502: "UPSTREAM_ERROR",
    503: "SERVICE_UNAVAILABLE",
}

def is_v1_request(request: Request) -> bool:
    return request.url.path.startswith("/api/v1")

def http_exception_to_v1(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        code = str(detail["code"])
        message = str(detail["message"])
        extra = detail.get("details")
    else:
        code = _STATUS_DEFAULT_CODES.get(exc.status_code, "ERROR")
        message = str(detail) if detail else "Request failed"
        extra = None
    return fail(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        details=extra,
        headers=dict(exc.headers) if exc.headers else None,
    )

def validation_exception_to_v1(request: Request, exc: RequestValidationError) -> JSONResponse:
    return fail(
        request,
        status_code=422,
        code="VALIDATION_ERROR",
        message="The request contains invalid fields.",
        details=exc.errors(),
    )
