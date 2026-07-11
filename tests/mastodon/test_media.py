import io
import secrets

import pytest
from fastapi.testclient import TestClient
from PIL import Image
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


def _png_bytes(size: tuple[int, int] = (16, 12)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def test_media_create_requires_auth(client: TestClient) -> None:
    response = client.post(
        "/api/v2/media",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_media_create_v2_returns_media_attachment(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:media")

    response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["type"] == "image"
    assert data["url"]
    assert data["meta"]["original"] == {"width": 16, "height": 12}
    assert data["description"] is None


@pytest.mark.asyncio
async def test_media_create_with_description(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:media")

    response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("photo.png", _png_bytes((4, 4)), "image/png")},
        data={"description": "a red square"},
    )

    assert response.status_code == 200
    assert response.json()["description"] == "a red square"


@pytest.mark.asyncio
async def test_media_create_v1_alias_behaves_the_same(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:media")

    response = client.post(
        "/api/v1/media",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("photo.png", _png_bytes((8, 8)), "image/png")},
    )

    assert response.status_code == 200
    assert response.json()["type"] == "image"


@pytest.mark.asyncio
async def test_media_show_and_not_found(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:media read:media")
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/api/v2/media",
        headers=headers,
        files={"file": ("photo.png", _png_bytes((5, 5)), "image/png")},
    ).json()

    response = client.get(f"/api/v1/media/{created['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]

    assert client.get("/api/v1/media/999999", headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_media_update_description(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:media read:media")
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/api/v2/media",
        headers=headers,
        files={"file": ("photo.png", _png_bytes((6, 6)), "image/png")},
    ).json()
    assert created["description"] is None

    updated = client.put(
        f"/api/v1/media/{created['id']}",
        headers=headers,
        data={"description": "updated alt text"},
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "updated alt text"

    refetched = client.get(f"/api/v1/media/{created['id']}", headers=headers)
    assert refetched.json()["description"] == "updated alt text"


@pytest.mark.asyncio
async def test_media_upload_dedupes_identical_content(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:media")
    headers = {"Authorization": f"Bearer {token}"}
    content = _png_bytes((7, 7))

    first = client.post(
        "/api/v2/media",
        headers=headers,
        files={"file": ("a.png", content, "image/png")},
    ).json()
    second = client.post(
        "/api/v2/media",
        headers=headers,
        files={"file": ("b.png", content, "image/png")},
    ).json()

    assert first["id"] == second["id"]
