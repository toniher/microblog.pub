"""Shared httpx clients, reused across requests instead of one-per-call.

A fresh ``httpx.AsyncClient()`` per call means a fresh connection pool, so no
TCP/TLS connection ever survives long enough to be reused. Clients are cached
per running event loop rather than as a single module-level instance: an httpx
connection pool is bound to the loop it was created on, and pytest-asyncio
gives each test its own loop, so a single shared instance would break (or
silently stop pooling) across tests.

The registries are weak-keyed so a finished loop (and with it its client and
pool) can be collected instead of pinned for the life of the process —
``aclose_all()`` only runs at app/worker shutdown, which never fires under
pytest.

All of this is tunable from ``data/profile.toml`` (see "Outgoing HTTP
connections" in the user guide); the defaults below are what the app does when
none of those settings are present.
"""

import asyncio
import weakref

import httpx

from app.config import HTTP_CLIENT_HTTP2
from app.config import HTTP_CLIENT_MAX_CONNECTIONS
from app.config import HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS
from app.config import HTTP_CLIENT_POOLING

# With pooling disabled, no connection is held open past the response that used
# it, which is the one-connection-per-request behaviour from before these
# clients were shared. HTTP/2 goes with it rather than being left on: running
# several requests over a single multiplexed connection is connection sharing
# too, so honouring `http_client_pooling = false` means dropping back to
# HTTP/1.1. The *client* stays shared either way — it is the connection reuse
# that the setting turns off, not the object caching.
_HTTP2 = HTTP_CLIENT_HTTP2 and HTTP_CLIENT_POOLING

# ALPN-negotiated with automatic HTTP/1.1 fallback, so enabling this is safe
# against servers that don't speak it. Only worth doing now that connections
# are actually reused — the `http2` extra was inert before.
_LIMITS = httpx.Limits(
    max_keepalive_connections=(
        HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS if HTTP_CLIENT_POOLING else 0
    ),
    max_connections=HTTP_CLIENT_MAX_CONNECTIONS,
)

_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary()
)
_proxy_clients: (
    "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]"
) = weakref.WeakKeyDictionary()


def get_client() -> httpx.AsyncClient:
    """Shared client for AP fetch/post, webfinger, OG/microformats scraping,
    and webmentions. Per-call kwargs (``follow_redirects``, ``auth``,
    ``timeout``) are left to the caller, unchanged from before."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(limits=_LIMITS, http2=_HTTP2)
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
            # `limits`/`http2` have to be set on the transport, not the client:
            # httpx returns an explicitly-passed `transport` as-is and drops
            # the client-level values on the floor.
            transport=httpx.AsyncHTTPTransport(retries=1, limits=_LIMITS, http2=_HTTP2),
        )
        _proxy_clients[loop] = client
    return client


async def aclose_all() -> None:
    for registry in (_clients, _proxy_clients):
        for client in list(registry.values()):
            # A client bound to an already-closed loop can't be awaited; it has
            # nothing left to release anyway, so don't let it abort shutdown.
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
        registry.clear()
