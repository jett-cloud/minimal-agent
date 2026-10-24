"""Stable JSON error envelopes and FastAPI exception handlers."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from minimal_agent.schemas import APIErrorBody, APIErrorResponse


class APIError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body = APIErrorResponse(error=APIErrorBody(code=code, message=message, details=details or {}))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def install_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(APIError)
    async def handle_api_error(_request: Request, error: APIError) -> JSONResponse:
        return error_response(
            error.status_code,
            error.code,
            error.message,
            details=error.details,
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        issues = [
            {
                "type": issue["type"],
                "location": [str(part) for part in issue["loc"]],
                "message": issue["msg"],
            }
            for issue in error.errors()
        ]
        return error_response(
            422,
            "VALIDATION_ERROR",
            "Request validation failed",
            details={"issues": issues},
        )

    @application.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        _request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        message = error.detail if isinstance(error.detail, str) else "HTTP request failed"
        code = {
            400: "INVALID_REQUEST",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
        }.get(error.status_code, "HTTP_ERROR")
        return error_response(error.status_code, code, message)

    @application.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, _error: Exception) -> JSONResponse:
        return error_response(500, "INTERNAL_ERROR", "Request failed unexpectedly")
