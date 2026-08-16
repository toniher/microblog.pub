import hashlib
import io
import subprocess
from typing import Iterator

import pytest
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers

import activitypub.models
from app import ffmpeg
from app.uploads import UPLOAD_DIR
from app.uploads import IncompatibleMediaError
from app.uploads import UploadTooLargeError
from app.uploads import save_upload


def _upload_file(
    data: bytes, content_type: str, filename: str = "upload.bin"
) -> UploadFile:
    return UploadFile(
        io.BytesIO(data),
        size=len(data),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


def _run(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, timeout=30)


@pytest.fixture(scope="session")
def clips(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, bytes]]:
    if not ffmpeg.is_available():
        pytest.skip("ffmpeg/ffprobe not installed")

    d = tmp_path_factory.mktemp("upload-clips")
    ffmpeg_bin = ffmpeg.FFMPEG
    assert ffmpeg_bin

    data: dict[str, bytes] = {}

    def clip(name: str, *args: str) -> None:
        path = d / name
        _run([ffmpeg_bin, "-y", "-v", "error", *args, "--", str(path)])
        data[name] = path.read_bytes()

    clip(
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
    clip(
        "hevc.mp4",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=2:size=64x64:rate=10",
        "-c:v",
        "libx265",
        "-pix_fmt",
        "yuv420p",
        "-an",
    )
    clip(
        "audio.flac",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:a",
        "flac",
    )

    yield data


@pytest.mark.asyncio
async def test_video_upload_gets_poster_blurhash_and_duration(
    async_db_session: AsyncSession, clips: dict[str, bytes]
) -> None:
    upload = await save_upload(
        async_db_session, _upload_file(clips["safe.mp4"], "video/mp4", "safe.mp4")
    )
    assert upload is not None
    assert upload.has_thumbnail
    assert upload.blurhash
    assert upload.width == 64 and upload.height == 64
    assert upload.duration is not None and upload.duration == pytest.approx(2, abs=0.5)
    assert upload.has_audio is True
    assert (UPLOAD_DIR / f"{upload.content_hash}_resized").exists()


@pytest.mark.asyncio
async def test_audio_upload_gets_duration_no_thumbnail(
    async_db_session: AsyncSession, clips: dict[str, bytes]
) -> None:
    upload = await save_upload(
        async_db_session,
        _upload_file(clips["audio.flac"], "audio/flac", "audio.flac"),
    )
    assert upload is not None
    assert not upload.has_thumbnail
    assert upload.blurhash is None
    assert upload.duration is not None and upload.duration == pytest.approx(1, abs=0.5)
    assert upload.has_audio is True


@pytest.mark.asyncio
async def test_hevc_upload_is_rejected_and_leaves_no_orphan(
    async_db_session: AsyncSession, clips: dict[str, bytes]
) -> None:
    with pytest.raises(IncompatibleMediaError) as exc_info:
        await save_upload(
            async_db_session,
            _upload_file(clips["hevc.mp4"], "video/mp4", "hevc.mp4"),
        )
    assert "hevc" in exc_info.value.reason.lower()

    content_hash = hashlib.blake2b(clips["hevc.mp4"], digest_size=32).hexdigest()

    # No Upload row, and the file written to disk before probing was cleaned
    # up.
    rows = (
        (await async_db_session.execute(select(activitypub.models.Upload)))
        .scalars()
        .all()
    )
    assert rows == []
    assert not (UPLOAD_DIR / content_hash).exists()
    assert not (UPLOAD_DIR / f"{content_hash}_poster.png").exists()
    assert not (UPLOAD_DIR / f"{content_hash}_resized").exists()


@pytest.mark.asyncio
async def test_fail_open_when_ffmpeg_unavailable(
    async_db_session: AsyncSession,
    clips: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protects existing installs without ffmpeg: an otherwise-incompatible
    (HEVC) file must still be accepted, just without metadata/rejection."""
    monkeypatch.setattr(ffmpeg, "FFPROBE", None)
    monkeypatch.setattr(ffmpeg, "FFMPEG", None)

    upload = await save_upload(
        async_db_session,
        _upload_file(clips["hevc.mp4"], "video/mp4", "hevc-failopen.mp4"),
    )
    assert upload is not None
    assert not upload.has_thumbnail
    assert upload.duration is None
    assert upload.has_audio is None


@pytest.mark.asyncio
async def test_oversized_upload_is_rejected_before_touching_disk(
    async_db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.uploads.config.MAX_IMAGE_UPLOAD_SIZE", 10)
    monkeypatch.setattr("app.uploads.config.MAX_VIDEO_UPLOAD_SIZE", 10)

    content_hash_dir_before = set(UPLOAD_DIR.glob("*"))

    with pytest.raises(UploadTooLargeError) as exc_info:
        await save_upload(
            async_db_session,
            _upload_file(b"x" * 1000, "image/png", "big.png"),
        )
    assert exc_info.value.limit == 10

    # Nothing new was written to disk.
    assert set(UPLOAD_DIR.glob("*")) == content_hash_dir_before


@pytest.mark.asyncio
async def test_image_upload_unaffected_by_restructure(
    async_db_session: AsyncSession,
) -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (10, 8), color=(0, 255, 0)).save(buf, format="PNG")

    upload = await save_upload(
        async_db_session, _upload_file(buf.getvalue(), "image/png", "x.png")
    )
    assert upload is not None
    assert upload.has_thumbnail
    assert upload.blurhash
    assert upload.width == 10 and upload.height == 8
    assert upload.duration is None
    assert upload.has_audio is None


@pytest.mark.asyncio
async def test_too_many_pixels_is_rejected_before_decoding(
    async_db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small file can still be a huge bitmap, so the pixel cap is checked
    from the header — independently of the byte cap, which this image passes.
    """
    from PIL import Image

    monkeypatch.setattr("app.uploads.config.MAX_IMAGE_PIXELS", 100)

    buf = io.BytesIO()
    Image.new("RGB", (40, 40), color=(1, 2, 3)).save(buf, format="PNG")
    assert len(buf.getvalue()) < 10_485_760  # well under the byte limit

    files_before = set(UPLOAD_DIR.glob("*"))

    with pytest.raises(IncompatibleMediaError) as exc_info:
        await save_upload(
            async_db_session, _upload_file(buf.getvalue(), "image/png", "huge.png")
        )
    assert "megapixel" in exc_info.value.reason

    # The rejection happens inside _process_image_upload, which runs after the
    # raw bytes are hashed but before anything is written for an image.
    assert set(UPLOAD_DIR.glob("*")) == files_before


@pytest.mark.asyncio
async def test_palette_png_keeps_its_colors(
    async_db_session: AsyncSession,
) -> None:
    """The EXIF strip used to rebuild a mode-"P" image without copying its
    palette, silently remapping every colour to the default palette.
    """
    from PIL import Image

    original = Image.new("RGB", (12, 12), color=(200, 40, 90)).convert(
        "P", palette=Image.Palette.ADAPTIVE
    )
    buf = io.BytesIO()
    original.save(buf, format="PNG")

    upload = await save_upload(
        async_db_session, _upload_file(buf.getvalue(), "image/png", "pal.png")
    )
    assert upload is not None
    assert upload.content_hash is not None

    with Image.open(UPLOAD_DIR / upload.content_hash) as stored:
        assert stored.mode == "P"
        assert stored.convert("RGB").getpixel((0, 0)) == (200, 40, 90)


def test_blurhash_is_stable_across_source_sizes() -> None:
    """The blurhash is now encoded from a downscaled copy; on real content
    that must land on the same hash as the full-size source would.
    """
    from PIL import Image

    from app.uploads import _blurhash_of

    # A smooth gradient, i.e. exactly what a 4x3-component blur represents.
    large = Image.new("RGB", (900, 600))
    large.putdata(
        [
            (int(255 * x / 900), int(255 * y / 600), 128)
            for y in range(600)
            for x in range(900)
        ]
    )
    small = large.resize((150, 100))

    assert _blurhash_of(large) == _blurhash_of(small)
