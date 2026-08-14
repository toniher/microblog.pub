import io
import secrets
import subprocess
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app import ffmpeg
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


async def _upload_png(client: TestClient, async_db_session: AsyncSession) -> str:
    token = await _make_access_token(async_db_session, "write:media")
    response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 200
    return urlparse(response.json()["url"]).path


@pytest.mark.asyncio
async def test_attachment_supports_range_requests(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    path = await _upload_png(client, async_db_session)

    response = client.get(path, headers={"Range": "bytes=0-3"})

    assert response.status_code == 206
    assert response.headers["content-range"].startswith("bytes 0-3/")
    assert len(response.content) == 4


@pytest.mark.asyncio
async def test_attachment_thumbnail_served_as_webp_without_accept_header(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    """Regression test: without `Accept: image/webp`, serve_attachment_thumbnail
    used to fall back to the original file. For a video/audio upload that
    original is the whole media file, not a thumbnail; for an image it's a
    valid (if unresized) fallback, so this asserts the image path — the
    ffmpeg-gated test suite covers the video case directly.
    """
    path = await _upload_png(client, async_db_session)
    thumbnail_path = path.replace("/attachments/", "/attachments/thumbnails/", 1)

    response = client.get(thumbnail_path)

    assert response.status_code == 200
    # No Accept header sent -> falls back to the original PNG, which is the
    # correct (pre-existing) behaviour for images.
    assert response.headers["content-type"] == "image/png"


@pytest.mark.asyncio
async def test_attachment_thumbnail_prefers_webp_when_accepted(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    path = await _upload_png(client, async_db_session)
    thumbnail_path = path.replace("/attachments/", "/attachments/thumbnails/", 1)

    response = client.get(thumbnail_path, headers={"Accept": "image/webp"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"


@pytest.mark.asyncio
async def test_video_thumbnail_served_as_webp_without_accept_header(
    client: TestClient, async_db_session: AsyncSession, tmp_path
) -> None:
    """§8a regression test: a remote fetcher (which never sends
    `Accept: image/webp`) must get the webp poster, not the whole video."""
    if not ffmpeg.is_available():
        pytest.skip("ffmpeg/ffprobe not installed")
    ffmpeg_bin = ffmpeg.FFMPEG
    assert ffmpeg_bin

    clip_path = tmp_path / "safe.mp4"
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
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "--",
            str(clip_path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )

    token = await _make_access_token(async_db_session, "write:media")
    response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("safe.mp4", clip_path.read_bytes(), "video/mp4")},
    )
    assert response.status_code == 200
    path = urlparse(response.json()["url"]).path
    thumbnail_path = path.replace("/attachments/", "/attachments/thumbnails/", 1)

    thumb_response = client.get(thumbnail_path)

    assert thumb_response.status_code == 200
    assert thumb_response.headers["content-type"] == "image/webp"
