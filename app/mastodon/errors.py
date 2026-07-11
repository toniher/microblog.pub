"""Mastodon-shaped error envelope: {"error": ..., "error_description": ...}."""

from fastapi import Request
from fastapi.responses import JSONResponse


class MastodonError(Exception):
    def __init__(
        self,
        status_code: int,
        error: str,
        error_description: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.error = error
        self.error_description = error_description
        self.headers = headers
        super().__init__(error_description or error)


async def mastodon_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, MastodonError)
    content: dict[str, str] = {"error": exc.error}
    if exc.error_description:
        content["error_description"] = exc.error_description
    return JSONResponse(
        content=content,
        status_code=exc.status_code,
        headers=exc.headers,
    )
