import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app import models

_SCOPE_GATED_ENDPOINTS = [
    "/api/v1/lists",
    "/api/v1/filters",
    "/api/v2/filters",
    "/api/v1/suggestions",
    "/api/v2/suggestions",
    "/api/v1/follow_requests",
    # These four used to 404, which clients surface as an error dialog rather
    # than an empty section — the official Mastodon app hits all of them on
    # its profile and hashtag screens.
    "/api/v1/endorsements",
    "/api/v1/followed_tags",
    "/api/v1/accounts/0/lists",
]

# `GET /api/v2/notifications` is deliberately NOT stubbed: grouped
# notifications aren't implemented, and the 404 is exactly what makes a
# 4.3-aware client fall back to `/api/v1/notifications`. Stubbing it empty
# would show the user an empty notifications screen instead.
_DELIBERATELY_ABSENT = [
    "/api/v2/notifications",
    # No storage behind them, so they could only report success while
    # persisting nothing — see `tags_show`.
    "/api/v1/tags/hashtag/follow",
    "/api/v1/tags/hashtag/unfollow",
]

# /api/v1/mutes and /api/v1/blocks used to live in the list above; they're
# real lists now (tests/mastodon/test_social.py), empty only when nothing is
# muted/blocked.

_PUBLIC_ENDPOINTS = [
    "/api/v1/directory",
    "/api/v1/trends/tags",
    "/api/v1/trends/statuses",
    "/api/v1/trends/links",
]


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


@pytest.mark.parametrize("path", _SCOPE_GATED_ENDPOINTS)
def test_scope_gated_degradation_requires_auth(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _SCOPE_GATED_ENDPOINTS)
async def test_scope_gated_degradation_returns_empty_list(
    client: TestClient, async_db_session: AsyncSession, path: str
) -> None:
    token = await _make_access_token(async_db_session, "read")
    response = client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("path", _PUBLIC_ENDPOINTS)
def test_public_degradation_returns_empty_list_without_auth(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _DELIBERATELY_ABSENT)
async def test_deliberately_absent_endpoints_stay_absent(
    client: TestClient, async_db_session: AsyncSession, path: str
) -> None:
    """Guards the reasoning above: these must NOT be turned into empty stubs.

    A future "close the remaining 404s" pass would otherwise silently break
    grouped-notification fallback and start reporting fake success on tag
    follows.
    """
    token = await _make_access_token(async_db_session, "read write follow")
    response = client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_tags_show_returns_a_tag_entity(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    response = client.get("/api/v1/tags/%23MicroBlog")
    assert response.status_code == 200
    data = response.json()
    # Normalized the same way the timeline queries compare tags.
    assert data["name"] == "microblog"
    assert data["id"] == "microblog"
    assert data["url"].endswith("/t/microblog")
    assert data["history"] == []
    # Honest: following a hashtag isn't implemented.
    assert data["following"] is False
    # 4.4-only, and we advertise 4.3 — omitted rather than hardcoded.
    assert "featuring" not in data


def test_tags_show_rejects_a_blank_hashtag(client: TestClient) -> None:
    assert client.get("/api/v1/tags/%20").status_code == 404
    assert client.get("/api/v1/tags/%23").status_code == 404


@pytest.mark.asyncio
async def test_search_hashtag_result_matches_tag_entity(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    """The two places a Tag is emitted must agree (shared `_serialize_tag`)."""
    token = await _make_access_token(async_db_session, "read")
    tag_response = client.get("/api/v1/tags/microblog")
    search_response = client.get(
        "/api/v2/search?q=%23microblog&type=hashtags",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert search_response.status_code == 200
    assert search_response.json()["hashtags"] == [tag_response.json()]
