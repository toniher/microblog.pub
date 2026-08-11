import importlib
from typing import Any
from typing import Iterator

import pytest

from app import config
from app import http_client


@pytest.fixture
def reloaded_http_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Reload `app.http_client` so it picks up patched config values.

    The module reads its settings once at import time, like the rest of the app
    does with `profile.toml`, so exercising them means re-importing it. The
    teardown reload restores the defaults for every other test.
    """

    def _reload(**overrides: Any) -> Any:
        for name, value in overrides.items():
            monkeypatch.setattr(config, name, value)
        return importlib.reload(http_client)

    yield _reload

    monkeypatch.undo()
    importlib.reload(http_client)


def test_http_client__pooling_enabled_by_default() -> None:
    # Absent from data/tests.toml, so this is what an unconfigured instance gets
    assert http_client._HTTP2 is True
    assert http_client._LIMITS.max_keepalive_connections == 20
    assert http_client._LIMITS.max_connections == 100


def test_http_client__pooling_can_be_disabled(reloaded_http_client: Any) -> None:
    mod = reloaded_http_client(HTTP_CLIENT_POOLING=False)

    # No connection outlives the response that used it, and HTTP/2 goes with it
    assert mod._LIMITS.max_keepalive_connections == 0
    assert mod._HTTP2 is False


def test_http_client__http2_can_be_disabled_while_pooling(
    reloaded_http_client: Any,
) -> None:
    mod = reloaded_http_client(HTTP_CLIENT_HTTP2=False)

    assert mod._HTTP2 is False
    assert mod._LIMITS.max_keepalive_connections == 20


def test_http_client__limits_are_configurable(reloaded_http_client: Any) -> None:
    mod = reloaded_http_client(
        HTTP_CLIENT_MAX_CONNECTIONS=5,
        HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS=2,
    )

    assert mod._LIMITS.max_connections == 5
    assert mod._LIMITS.max_keepalive_connections == 2


@pytest.mark.asyncio
async def test_http_client__clients_are_still_shared_without_pooling(
    reloaded_http_client: Any,
) -> None:
    # Disabling pooling turns off connection reuse, not the per-loop client cache
    mod = reloaded_http_client(HTTP_CLIENT_POOLING=False)

    assert mod.get_client() is mod.get_client()
    assert mod.get_proxy_client() is mod.get_proxy_client()

    await mod.aclose_all()
