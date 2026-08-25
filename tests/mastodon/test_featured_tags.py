import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import ObjectType
from app import config
from app import models
from app.mastodon import ids


async def _make_access_token(db_session: AsyncSession, scope: str) -> str:
    token = models.IndieAuthAccessToken(
        access_token=secrets.token_urlsafe(16),
        refresh_token=None,
        expires_in=3600,
        scope=scope,
    )
    db_session.add(token)
    await db_session.commit()
    return token.access_token


def test_featured_tags_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/featured_tags")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_featured_tags_empty_when_not_configured(
    client: TestClient,
    async_db_session: AsyncSession,
) -> None:
    token = await _make_access_token(async_db_session, "read:accounts")
    response = client.get(
        "/api/v1/featured_tags", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_featured_tags_reports_counts(
    client: TestClient,
    async_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "FEATURED_TAGS", ["Microblogging"])

    await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Post about #microblogging",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Another #microblogging post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Unrelated post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    token = await _make_access_token(async_db_session, "read:accounts")
    response = client.get(
        "/api/v1/featured_tags", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "microblogging"
    assert data[0]["url"] == f"{config.BASE_URL}/t/microblogging"
    assert data[0]["statuses_count"] == "2"
    assert data[0]["last_status_at"] is not None


def test_accounts_featured_tags_for_owner(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "FEATURED_TAGS", ["microblogging"])

    response = client.get(f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}/featured_tags")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "microblogging"


def test_accounts_featured_tags_empty_for_remote_actor(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "FEATURED_TAGS", ["microblogging"])

    response = client.get("/api/v1/accounts/some-remote-id/featured_tags")

    assert response.status_code == 200
    assert response.json() == []
