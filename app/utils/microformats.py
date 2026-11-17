import asyncio
from typing import Any

import httpx
import mf2py  # type: ignore
from loguru import logger

from app import config
from app import http_client


class URLNotFoundOrGone(Exception):
    pass


# Upper bound on the HTML read from a remote page. `POST /webmentions` is
# unauthenticated and lets the caller pick the URL, so the body size (and the
# mf2py parse time that follows) must not be theirs to choose either.
_MAX_HTML_BYTES = 2 * 1024 * 1024


async def fetch_and_parse(url: str) -> tuple[dict[str, Any], str]:
    async with http_client.get_client().stream(
        "GET",
        url,
        headers={
            "User-Agent": config.USER_AGENT,
        },
        follow_redirects=True,
    ) as resp:
        if resp.status_code in [404, 410]:
            raise URLNotFoundOrGone

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(
                f"Failed to parse microformats for {url}: " f"got {resp.status_code}"
            )
            raise

        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) >= _MAX_HTML_BYTES:
                logger.info(f"Truncating {url} at {_MAX_HTML_BYTES} bytes")
                del body[_MAX_HTML_BYTES:]
                break
        html = body.decode(resp.encoding or "utf-8", errors="replace")

    # mf2py parsing is pure CPU: keep it off the event loop.
    data = await asyncio.to_thread(lambda: _drop_img_alt(mf2py.parse(doc=html)))
    return data, html


def _drop_img_alt(data: Any) -> Any:
    """mf2py >= 2 parses `<img alt>` properties (u-photo, u-logo...) as
    {"value": url, "alt": text}. Keep them as plain URLs, the shape the
    callers and the webmentions already stored in the DB use."""
    if isinstance(data, dict):
        if data.keys() == {"value", "alt"}:
            return data["value"]
        return {k: _drop_img_alt(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_drop_img_alt(v) for v in data]
    return data
