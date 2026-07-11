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
    "/api/v1/mutes",
    "/api/v1/follow_requests",
]

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
