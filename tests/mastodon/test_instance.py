import secrets
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app import models


async def _make_access_token(db_session: AsyncSession, scope: str) -> str:
    # `indieauth_authorization_request_id` is nullable for exactly this case
    # ("personal access tokens" per app/models.py) — no OAuth dance needed to
    # test a scope-gated endpoint in isolation.
    token = models.IndieAuthAccessToken(
        access_token=secrets.token_urlsafe(16),
        refresh_token=None,
        expires_in=3600,
        scope=scope,
    )
    db_session.add(token)
    await db_session.commit()
    return token.access_token


def test_instance_v1_shape(client: TestClient) -> None:
    response = client.get("/api/v1/instance")

    assert response.status_code == 200
    data = response.json()
    assert data["uri"]
    assert data["title"]
    assert "microblogpub" in data["version"]
    assert data["stats"] == {
        "user_count": 1,
        "status_count": 0,
        "domain_count": 1,
    }
    assert data["contact_account"]["username"]
    assert data["contact_account"]["id"] == "0"
    # Streaming is advertised over WebSocket, matching the wss:// origin
    # streaming clients connect to (app/mastodon/streaming.py).
    assert data["urls"]["streaming_api"].startswith("ws")
    assert data["registrations"] is False


def test_instance_v2_shape(client: TestClient) -> None:
    response = client.get("/api/v2/instance")

    assert response.status_code == 200
    data = response.json()
    assert data["domain"]
    assert data["contact"]["account"]["id"] == "0"
    assert data["registrations"]["enabled"] is False
    assert "configuration" in data
    # This is the key Mastodon 4.x clients actually read (v1's urls.streaming_api
    # is legacy compatibility) — both must agree.
    assert (
        data["configuration"]["urls"]["streaming"]
        == client.get("/api/v1/instance").json()["urls"]["streaming_api"]
    )


def test_version_advertises_mastodon_compat_not_our_own_version() -> None:
    """The leading number in `version` is what clients gate features on.

    It used to be `config.VERSION` — microblog.pub's own number — which read
    as "Mastodon 2.x" and made clients hide working features (status editing
    needs 3.5, bookmarks 3.1, markers 3.0). It would also have drifted on its
    own once this project reached 3.x. Both instance endpoints must report the
    API compatibility level, with our real version in the suffix.
    """
    from app import config
    from app.mastodon.router import _MASTODON_COMPAT_VERSION
    from app.mastodon.router import _VERSION_STRING

    mastodon_major, mastodon_minor = (
        int(part) for part in _MASTODON_COMPAT_VERSION.split(".")[:2]
    )
    # Above every feature gate the surface actually implements.
    assert (mastodon_major, mastodon_minor) >= (4, 3)
    assert _VERSION_STRING.startswith(_MASTODON_COMPAT_VERSION)
    assert f"microblogpub {config.VERSION}" in _VERSION_STRING
    # The whole point: our version must not be the number clients parse.
    assert not _VERSION_STRING.startswith(config.VERSION)


def test_both_instance_endpoints_report_the_same_version(client: TestClient) -> None:
    from app.mastodon.router import _VERSION_STRING

    assert client.get("/api/v1/instance").json()["version"] == _VERSION_STRING
    assert client.get("/api/v2/instance").json()["version"] == _VERSION_STRING


def test_instance_omits_api_versions(client: TestClient) -> None:
    """`api_versions` is an opaque fast-moving counter (4.7.0 reports 11) with
    no published version-to-value mapping, so any value we picked would be a
    guess clients act on. Omitting it makes them parse `version` instead."""
    assert "api_versions" not in client.get("/api/v2/instance").json()


def test_instance_rules_is_empty(client: TestClient) -> None:
    response = client.get("/api/v1/instance/rules")

    assert response.status_code == 200
    assert response.json() == []


def test_instance_extended_description(client: TestClient) -> None:
    response = client.get("/api/v1/instance/extended_description")

    assert response.status_code == 200
    data = response.json()
    assert data["updated_at"]
    assert isinstance(data["content"], str)


def test_instance_extended_description_prefers_about_field(
    client: TestClient,
) -> None:
    with mock.patch(
        "app.mastodon.serializers.config.ABOUT_HTML", "<p>a longer intro</p>"
    ):
        response = client.get("/api/v1/instance/extended_description")

    assert response.status_code == 200
    assert response.json()["content"] == "<p>a longer intro</p>"


def test_instance_peers_is_empty_for_privacy(client: TestClient) -> None:
    response = client.get("/api/v1/instance/peers")

    assert response.status_code == 200
    assert response.json() == []


def test_instance_domain_blocks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config.CONFIG,
        "blocked_servers",
        [
            config._BlockedServer(hostname="spam.example", reason="spam"),
            config._BlockedServer(hostname="abuse.example"),
        ],
    )

    response = client.get("/api/v1/instance/domain_blocks")

    assert response.status_code == 200
    data = response.json()
    assert [block["domain"] for block in data] == ["abuse.example", "spam.example"]
    spam_block = next(block for block in data if block["domain"] == "spam.example")
    assert spam_block["severity"] == "suspend"
    assert spam_block["comment"] == "spam"
    assert len(spam_block["digest"]) == 64


def test_instance_activity_shape(client: TestClient) -> None:
    response = client.get("/api/v1/instance/activity")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 12
    for week in data:
        assert isinstance(week["week"], str)
        assert isinstance(week["statuses"], str)
        assert isinstance(week["logins"], str)
        assert week["registrations"] == "0"
    # Most recent week first.
    weeks = [int(week["week"]) for week in data]
    assert weeks == sorted(weeks, reverse=True)


def test_custom_emojis_returns_a_list(client: TestClient) -> None:
    response = client.get("/api/v1/custom_emojis")

    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_preferences_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/preferences")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_preferences_returns_defaults(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read")

    response = client.get(
        "/api/v1/preferences", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["posting:default:visibility"] == "public"
    assert data["reading:expand:spoilers"] is False


@pytest.mark.asyncio
async def test_announcements_requires_scope_and_returns_empty_list(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    unauthorized = client.get("/api/v1/announcements")
    assert unauthorized.status_code == 401

    token = await _make_access_token(async_db_session, "read")
    response = client.get(
        "/api/v1/announcements", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_markers_get_is_empty_when_none_saved(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:statuses")

    response = client.get(
        "/api/v1/markers", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json() == {}


# Markers are now genuinely persisted (see tests/mastodon/test_markers.py for
# the full round-trip coverage) — this used to be an echo-only stub.
