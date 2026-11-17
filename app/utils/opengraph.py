import asyncio
import mimetypes
from collections.abc import Iterable
from concurrent.futures import TimeoutError
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup  # type: ignore
from loguru import logger
from sqlalchemy import select

from activitypub import activitypub as ap
from activitypub import ap_object
from activitypub.actor import LOCAL_ACTOR
from activitypub.models import Actor
from activitypub.models import InboxObject
from activitypub.models import OutboxObject
from app import config
from app import http_client
from app.database import AsyncSession
from app.utils.og_scrape import OpenGraphMeta
from app.utils.og_scrape import scrap_og_meta_in_process
from app.utils.url import is_url_valid_async
from app.utils.webmentions import webmention_endpoint_from_response


async def scrap_og_meta(url: str, html: str) -> OpenGraphMeta | None:
    # Await the pebble future instead of blocking on `.result()`: the parse
    # runs in a child process for up to 5s, and a blocking wait would stall
    # the event loop for that long. Cancelling the wrapper (e.g. from
    # `asyncio.wait_for`) also cancels the pebble process.
    return await asyncio.wrap_future(scrap_og_meta_in_process(url, html))


async def external_urls(
    db_session: AsyncSession,
    ro: ap_object.RemoteObject | OutboxObject | InboxObject,
) -> set[str]:
    note_host = urlparse(ro.ap_id).hostname

    # Mentioned actors' profile links must not become link cards. Only
    # cached actors are consulted (no fetch): this runs before the caller
    # writes, and fetching (and saving) each mentioned actor would make it
    # either write or hold SQLite's writer lock across the network. Profile
    # links of unknown actors are caught by the `mention` class below.
    tags_hrefs = {LOCAL_ACTOR.ap_id, LOCAL_ACTOR.url}
    mention_hrefs = set()
    for tag in ro.tags:
        if tag_href := tag.get("href"):
            tags_hrefs.add(tag_href)
            if tag.get("type") == "Mention":
                mention_hrefs.add(tag_href)
    if mention_hrefs:
        for mentioned_actor in await db_session.scalars(
            select(Actor).where(Actor.ap_id.in_(mention_hrefs))
        ):
            if mentioned_actor.url:
                tags_hrefs.add(mentioned_actor.url)

    candidates = []
    if ro.content:
        soup = BeautifulSoup(ro.content, "html5lib")
        candidates = [
            link.get("href")
            for link in soup.find_all("a")
            # Mastodon, Pleroma/Akkoma and Misskey mark mention (and hashtag)
            # links with a `mention` class.
            if "mention" not in (link.get("class") or [])
        ]
    # FEP-8967: a `Link` attachment names the URL to build the card from.
    candidates += [
        obj.get("href")
        for obj in ap.as_list(ro.ap_object.get("attachment", []))
        if isinstance(obj, dict) and obj.get("type") == "Link"
    ]

    urls = set()
    for h in candidates:
        if not isinstance(h, str):
            continue

        try:
            ph = urlparse(h)
            mimetype, _ = mimetypes.guess_type(h)
            if (
                ph.scheme in {"http", "https"}
                and ph.hostname != note_host
                and await is_url_valid_async(h)
                and (
                    not mimetype
                    or mimetype.split("/")[0] not in ["image", "video", "audio"]
                )
            ):
                urls.add(h)
        except Exception:
            logger.exception(f"Failed to check {h}")

    return urls - tags_hrefs


async def _og_meta_from_url(url: str) -> OpenGraphMeta | None:
    resp = await http_client.get_client().get(
        url,
        headers={
            "User-Agent": config.USER_AGENT,
        },
        follow_redirects=True,
    )

    resp.raise_for_status()

    if not (ct := resp.headers.get("content-type")) or not ct.startswith("text/html"):
        return None

    try:
        return await scrap_og_meta(url, resp.text)
    except TimeoutError:
        logger.info(f"Timed out when scraping OG meta for {url}")
        return None
    except Exception:
        logger.info(f"Failed to scrap OG meta for {url}")
        return None


@dataclass
class LinkInfo:
    og_meta: dict[str, Any] | None = None
    webmention_endpoint: str | None = None


async def _link_info(url: str, discover_webmentions: bool, scrape_og: bool) -> LinkInfo:
    resp = await http_client.get_client().get(
        url,
        headers={
            "User-Agent": config.USER_AGENT,
        },
        follow_redirects=True,
    )
    resp.raise_for_status()

    info = LinkInfo()
    ct = resp.headers.get("content-type")
    if scrape_og and ct and ct.startswith("text/html"):
        try:
            if og_meta := await scrap_og_meta(url, resp.text):
                info.og_meta = og_meta.model_dump()
        except Exception:
            logger.info(f"Failed to scrap OG meta for {url}")

    if discover_webmentions:
        endpoint = await webmention_endpoint_from_response(resp, url)
        if endpoint and await is_url_valid_async(endpoint):
            info.webmention_endpoint = endpoint

    return info


async def fetch_link_info(
    urls: Iterable[str],
    discover_webmentions: bool,
    scrape_og: bool = True,
) -> dict[str, LinkInfo]:
    """One fetch per link, all at once: both the link card and the webmention
    endpoint come from the same response. A link that fails or takes over 5s
    gets an empty `LinkInfo`."""

    async def _one(url: str) -> LinkInfo:
        try:
            return await asyncio.wait_for(
                _link_info(url, discover_webmentions, scrape_og), timeout=5
            )
        except asyncio.TimeoutError:
            logger.info(f"Timing out fetching {url}")
        except Exception:
            logger.exception(f"Failed to fetch {url}")
        return LinkInfo()

    urls = list(urls)
    return dict(zip(urls, await asyncio.gather(*(_one(url) for url in urls))))


def og_meta_from_link_info(links: dict[str, LinkInfo]) -> list[dict[str, Any]]:
    return [info.og_meta for info in links.values() if info.og_meta]


async def og_meta_from_note(
    db_session: AsyncSession,
    ro: ap_object.RemoteObject,
) -> list[dict[str, Any]]:
    urls = await external_urls(db_session, ro)
    logger.debug(f"Lookig OG metadata in {urls=}")
    return og_meta_from_link_info(
        await fetch_link_info(urls, discover_webmentions=False)
    )
