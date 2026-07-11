import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

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
    # No streaming API is implemented; clients must fall back to polling.
    assert "streaming_api" not in data["urls"]
    assert data["registrations"] is False


def test_instance_v2_shape(client: TestClient) -> None:
    response = client.get("/api/v2/instance")

    assert response.status_code == 200
    data = response.json()
    assert data["domain"]
    assert data["contact"]["account"]["id"] == "0"
    assert data["registrations"]["enabled"] is False
    assert "configuration" in data


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
async def test_markers_get_is_always_empty(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:statuses")

    response = client.get(
        "/api/v1/markers", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json() == {}


@pytest.mark.asyncio
async def test_markers_post_echoes_without_persisting(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    post_response = client.post(
        "/api/v1/markers",
        headers=headers,
        data={"home[last_read_id]": "42"},
    )
    assert post_response.status_code == 200
    data = post_response.json()
    assert data["home"]["last_read_id"] == "42"
    assert "notifications" not in data

    # Not actually persisted: a subsequent GET (even with a read-capable
    # token) reports nothing saved.
    read_token = await _make_access_token(async_db_session, "read:statuses")
    get_response = client.get(
        "/api/v1/markers", headers={"Authorization": f"Bearer {read_token}"}
    )
    assert get_response.json() == {}
