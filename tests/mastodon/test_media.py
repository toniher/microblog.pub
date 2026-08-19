import io
import secrets
import subprocess

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app import ffmpeg
from app import models
from app import uploads as app_uploads


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
    # Enriched uniformly with size/aspect (real Mastodon puts these on
    # images too) — subset check since duration/small are video-only.
    original = data["meta"]["original"]
    assert original["width"] == 16
    assert original["height"] == 12
    assert original["size"] == "16x12"
    assert original["aspect"] == pytest.approx(16 / 12)
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
async def test_media_create_with_focus(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:media")

    response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("photo.png", _png_bytes((4, 4)), "image/png")},
        data={"focus": "-0.5,0.7"},
    )

    assert response.status_code == 200
    assert response.json()["meta"]["focus"] == {"x": -0.5, "y": 0.7}


@pytest.mark.asyncio
async def test_media_create_with_invalid_focus(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:media")

    response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("photo.png", _png_bytes((4, 4)), "image/png")},
        data={"focus": "2.0,0"},
    )

    assert response.status_code == 422


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
async def test_media_update_focus(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:media read:media")
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/api/v2/media",
        headers=headers,
        files={"file": ("photo.png", _png_bytes((6, 6)), "image/png")},
    ).json()
    assert "focus" not in created["meta"]

    updated = client.put(
        f"/api/v1/media/{created['id']}",
        headers=headers,
        data={"focus": "0.2,-0.4"},
    )
    assert updated.status_code == 200
    assert updated.json()["meta"]["focus"] == {"x": 0.2, "y": -0.4}

    cleared = client.put(
        f"/api/v1/media/{created['id']}",
        headers=headers,
        data={"focus": ""},
    )
    assert cleared.status_code == 200
    assert "focus" not in cleared.json()["meta"]


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


@pytest.mark.asyncio
async def test_media_create_rejects_oversized_upload(
    client: TestClient,
    async_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_uploads.config, "MAX_IMAGE_UPLOAD_SIZE", 10)
    token = await _make_access_token(async_db_session, "write:media")

    response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 422
    data = response.json()
    assert data["error"] == "validation_failed"
    assert "10" in data["error_description"]


@pytest.mark.asyncio
async def test_media_create_rejects_hevc_video(
    client: TestClient, async_db_session: AsyncSession, tmp_path
) -> None:
    if not ffmpeg.is_available():
        pytest.skip("ffmpeg/ffprobe not installed")
    ffmpeg_bin = ffmpeg.FFMPEG
    assert ffmpeg_bin

    hevc_path = tmp_path / "hevc.mp4"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=5",
            "-c:v",
            "libx265",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "--",
            str(hevc_path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )

    token = await _make_access_token(async_db_session, "write:media")
    response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("hevc.mp4", hevc_path.read_bytes(), "video/mp4")},
    )

    assert response.status_code == 422
    data = response.json()
    assert data["error"] == "validation_failed"
    assert "hevc" in data["error_description"].lower()


def _make_clip(tmp_path, name: str, *args: str) -> bytes:
    ffmpeg_bin = ffmpeg.FFMPEG
    assert ffmpeg_bin
    path = tmp_path / name
    subprocess.run(
        [ffmpeg_bin, "-y", "-v", "error", *args, "--", str(path)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return path.read_bytes()


@pytest.mark.asyncio
async def test_media_create_video_gets_poster_and_meta(
    client: TestClient, async_db_session: AsyncSession, tmp_path
) -> None:
    if not ffmpeg.is_available():
        pytest.skip("ffmpeg/ffprobe not installed")

    clip = _make_clip(
        tmp_path,
        "safe.mp4",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=2:size=64x64:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
    )

    token = await _make_access_token(async_db_session, "write:media")
    response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("safe.mp4", clip, "video/mp4")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["type"] == "video"
    assert data["meta"]["original"]["width"] == 64
    assert data["meta"]["duration"] == pytest.approx(2, abs=0.5)
    assert data["blurhash"]
    assert data["preview_url"] and "/attachments/thumbnails/" in data["preview_url"]


@pytest.mark.asyncio
async def test_media_create_audio_has_duration_no_blurhash(
    client: TestClient, async_db_session: AsyncSession, tmp_path
) -> None:
    if not ffmpeg.is_available():
        pytest.skip("ffmpeg/ffprobe not installed")

    clip = _make_clip(
        tmp_path,
        "audio.flac",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:a",
        "flac",
    )

    token = await _make_access_token(async_db_session, "write:media")
    response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("audio.flac", clip, "audio/flac")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["type"] == "audio"
    assert data["meta"]["duration"] == pytest.approx(1, abs=0.5)
    assert data["blurhash"] is None
    assert data["preview_url"] is None
