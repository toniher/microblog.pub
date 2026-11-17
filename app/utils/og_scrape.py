"""OpenGraph HTML parsing, run in a pebble child process.

Kept apart from `app.utils.opengraph` on purpose: with the `forkserver`/`spawn`
start methods (`forkserver` is the Linux default since Python 3.14) the child
re-imports this module to find the function. It must therefore import neither
`activitypub` nor anything that does: imported on its own, that package hits
its circular import chain and every scrape fails.
"""

import re
import signal
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup  # type: ignore
from loguru import logger
from pebble import concurrent  # type: ignore
from pydantic import BaseModel

from app.utils.url import is_url_valid
from app.utils.url import make_abs


class OpenGraphMeta(BaseModel):
    url: str
    title: str
    image: str | None
    description: str | None
    site_name: str


@concurrent.process(timeout=5)
def scrap_og_meta_in_process(url: str, html: str) -> OpenGraphMeta | None:
    # Prevent SIGTERM to bubble up to the worker
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    soup = BeautifulSoup(html, "html5lib")
    assert soup.head is not None  # html5lib always builds a <head>
    # content is a single-valued attribute, so it is a str (or missing)
    ogs: dict[str, Any] = {
        str(og.attrs["property"]): og.get("content")
        for og in soup.head.find_all(property=re.compile(r"^og"))
    }
    title = soup.find("title")
    if title is None:
        return None
    raw = {
        "url": url,
        "title": title.text.strip(),
        "image": None,
        "description": None,
        "site_name": urlparse(url).hostname,
    }
    for field in OpenGraphMeta.model_fields.keys():
        og_field = f"og:{field}"
        if ogs.get(og_field):
            raw[field] = ogs.get(og_field, None)

    if "title" not in raw:
        return None

    for maybe_rel in {"url", "image"}:
        if u := raw.get(maybe_rel):
            raw[maybe_rel] = make_abs(u, url)

            if not is_url_valid(raw[maybe_rel] or ""):
                logger.info(f"Invalid url {raw[maybe_rel]}")
                if maybe_rel == "url":
                    raw["url"] = url
                elif maybe_rel == "image":
                    raw["image"] = None

    return OpenGraphMeta.model_validate(raw)
