"""Shared httpx clients, reused across requests instead of one-per-call.

A fresh ``httpx.AsyncClient()`` per call means a fresh connection pool, so no
TCP/TLS connection (and no HTTP/2 session, despite the ``http2`` extra being
installed) ever survives long enough to be reused. Clients are cached per
running event loop rather than as a single module-level instance: an httpx
connection pool is bound to the loop it was created on, and pytest-asyncio
gives each test its own loop, so a single shared instance would break (or
silently stop pooling) across tests.
"""

import asyncio

import httpx

_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=100)

_clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}
_proxy_clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}


def get_client() -> httpx.AsyncClient:
    """Shared client for AP fetch/post, webfinger, OG/microformats scraping,
    and webmentions. Per-call kwargs (``follow_redirects``, ``auth``,
    ``timeout``) are left to the caller, unchanged from before."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(limits=_LIMITS)
        _clients[loop] = client
    return client


def get_proxy_client() -> httpx.AsyncClient:
    """Shared client for the media proxy. ``follow_redirects=False`` is a
    security requirement, not a performance knob: main.py's ``_proxy_get``
    re-validates every redirect hop against the SSRF guard (``check_url``)
    itself, so httpx must never follow one on its own."""
    loop = asyncio.get_running_loop()
    client = _proxy_clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout=10.0),
            transport=httpx.AsyncHTTPTransport(retries=1),
            limits=_LIMITS,
        )
        _proxy_clients[loop] = client
    return client


async def aclose_all() -> None:
    for registry in (_clients, _proxy_clients):
        for client in registry.values():
            await client.aclose()
        registry.clear()
