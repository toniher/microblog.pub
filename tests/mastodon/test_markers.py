import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


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


@pytest.mark.asyncio
async def test_markers_round_trip_persists_across_requests(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:statuses write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    empty = client.get("/api/v1/markers", headers=headers)
    assert empty.status_code == 200
    assert empty.json() == {}

    posted = client.post(
        "/api/v1/markers",
        headers=headers,
        data={"home[last_read_id]": "123"},
    )
    assert posted.status_code == 200
    data = posted.json()
    assert data["home"]["last_read_id"] == "123"
    assert data["home"]["version"] == 1
    assert "notifications" not in data

    fetched = client.get("/api/v1/markers", headers=headers)
    assert fetched.status_code == 200
    fetched_data = fetched.json()
    assert fetched_data["home"]["last_read_id"] == "123"
    assert fetched_data["home"]["version"] == 1
    assert "notifications" not in fetched_data


@pytest.mark.asyncio
async def test_markers_update_bumps_version(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:statuses write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    client.post("/api/v1/markers", headers=headers, data={"home[last_read_id]": "1"})
    second = client.post(
        "/api/v1/markers", headers=headers, data={"home[last_read_id]": "2"}
    )

    assert second.status_code == 200
    data = second.json()
    assert data["home"]["last_read_id"] == "2"
    assert data["home"]["version"] == 2


@pytest.mark.asyncio
async def test_markers_get_filters_by_requested_timeline(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:statuses write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    client.post(
        "/api/v1/markers",
        headers=headers,
        data={"home[last_read_id]": "1", "notifications[last_read_id]": "2"},
    )

    home_only = client.get("/api/v1/markers?timeline[]=home", headers=headers)
    assert home_only.status_code == 200
    assert set(home_only.json().keys()) == {"home"}
