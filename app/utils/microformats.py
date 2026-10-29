from typing import Any

import httpx
import mf2py  # type: ignore
from loguru import logger

from app import config
from app import http_client


class URLNotFoundOrGone(Exception):
    pass


async def fetch_and_parse(url: str) -> tuple[dict[str, Any], str]:
    resp = await http_client.get_client().get(
        url,
        headers={
            "User-Agent": config.USER_AGENT,
        },
        follow_redirects=True,
    )
    if resp.status_code in [404, 410]:
        raise URLNotFoundOrGone

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        logger.error(
            f"Failed to parse microformats for {url}: " f"got {resp.status_code}"
        )
        raise

    return _drop_img_alt(mf2py.parse(doc=resp.text)), resp.text


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
