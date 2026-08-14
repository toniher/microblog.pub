import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from shutil import COPY_BUFSIZE  # type: ignore
from typing import BinaryIO

import blurhash  # type: ignore
from fastapi import UploadFile
from loguru import logger
from PIL import Image
from PIL import ImageOps
from sqlalchemy import select

import activitypub.models
from activitypub import activitypub as ap
from activitypub.ap_object import format_xsd_duration
from app import config
from app import ffmpeg
from app.config import BASE_URL
from app.config import ROOT_DIR
from app.database import AsyncSession

UPLOAD_DIR = ROOT_DIR / "data" / "uploads"

_THUMBNAIL_MAX_SIZE = (740, 740)


class UploadTooLargeError(Exception):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"upload exceeds the {limit} byte limit")


class IncompatibleMediaError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class _ProcessedUpload:
    has_thumbnail: bool = False
    blurhash: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    has_audio: bool | None = None


def _size_limit_for(content_type: str) -> int:
    if content_type.startswith("image"):
        return config.MAX_IMAGE_UPLOAD_SIZE
    # Video and audio share a limit (matching Mastodon); any other upload
    # (e.g. a PDF from the admin compose form) gets the more generous of the
    # two rather than a third knob.
    return config.MAX_VIDEO_UPLOAD_SIZE


def _hash_and_measure(f: BinaryIO, size_limit: int) -> tuple[str, int]:
    """`f` must be at position 0 on entry; rewound to 0 on return.

    Enforces `size_limit` while streaming (every byte is already being read
    to compute the hash, so the check is free) and raises before any byte of
    this upload is ever written to disk.
    """
    h = hashlib.blake2b(digest_size=32)
    size = 0
    while True:
        buf = f.read(COPY_BUFSIZE)
        if not buf:
            break
        size += len(buf)
        if size > size_limit:
            f.seek(0)
            raise UploadTooLargeError(size_limit)
        h.update(buf)

    f.seek(0)
    return h.hexdigest(), size


def _process_image_upload(f: UploadFile, dest_filename: Path) -> _ProcessedUpload:
    """Strip EXIF, generate a webp thumbnail and blurhash for a non-GIF image.

    `f.file` must be at position 0 on entry — `_hash_and_measure` rewinds it
    there.
    """
    with Image.open(f.file) as _original_image:
        # Fix image orientation (as we will remove the info from the EXIF
        # metadata)
        original_image = ImageOps.exif_transpose(_original_image)
        # exif_transpose only returns None for a None input.
        assert original_image is not None

        # Re-creating the image drop the EXIF metadata
        destination_image = Image.new(
            original_image.mode,
            original_image.size,
        )
        destination_image.putdata(original_image.getdata())
        destination_image.save(
            dest_filename,
            format=_original_image.format,  # type: ignore
        )

        with open(dest_filename, "rb") as dest_f:
            image_blurhash = blurhash.encode(dest_f, x_components=4, y_components=3)

        width, height = destination_image.size
        has_thumbnail = False
        try:
            destination_image.thumbnail(_THUMBNAIL_MAX_SIZE)
            destination_image.save(
                dest_filename.with_name(dest_filename.name + "_resized"),
                format="webp",
            )
        except Exception:
            logger.exception(f"Failed to created thumbnail for {dest_filename.name}")
        else:
            has_thumbnail = True
            logger.info("Thumbnail generated")

    return _ProcessedUpload(
        has_thumbnail=has_thumbnail,
        blurhash=image_blurhash,
        width=width,
        height=height,
    )


def _store_raw(fileobj: BinaryIO, dest_filename: Path) -> None:
    with open(dest_filename, "wb") as dest:
        while True:
            buf = fileobj.read(COPY_BUFSIZE)
            if not buf:
                break
            dest.write(buf)


def _process_av_upload(dest_filename: Path, content_hash: str) -> _ProcessedUpload:
    """Probe a video/audio file already written to `dest_filename`, extract a
    poster frame + blurhash for video, and return duration/has_audio.

    Never raises except the deliberate `IncompatibleMediaError` — on that
    path the original file (no partial poster can exist yet, since
    compatibility is decided before poster extraction) is deleted, since no
    `Upload` row exists yet to roll back.
    """
    info = ffmpeg.probe(dest_filename)
    if info is None:
        # No ffmpeg, or the probe failed: fail open, no metadata.
        return _ProcessedUpload()

    if info.incompatible_reason is not None:
        dest_filename.unlink(missing_ok=True)
        raise IncompatibleMediaError(info.incompatible_reason)

    processed = _ProcessedUpload(
        duration=info.duration, has_audio=info.has_audio_stream
    )
    if not info.has_video_stream:
        return processed

    poster_path = UPLOAD_DIR / f"{content_hash}_poster.png"
    resized_path = UPLOAD_DIR / f"{content_hash}_resized"
    try:
        if not ffmpeg.extract_poster(dest_filename, poster_path, info.duration):
            return processed

        width, height = info.width, info.height
        with Image.open(poster_path) as poster_image:
            if not width or not height:
                width, height = poster_image.size

            with open(poster_path, "rb") as poster_f:
                image_blurhash = blurhash.encode(
                    poster_f, x_components=4, y_components=3
                )

            thumbnail_image = poster_image.copy()
            thumbnail_image.thumbnail(_THUMBNAIL_MAX_SIZE)
            thumbnail_image.save(resized_path, format="webp")

        return _ProcessedUpload(
            has_thumbnail=True,
            blurhash=image_blurhash,
            width=width,
            height=height,
            duration=info.duration,
            has_audio=info.has_audio_stream,
        )
    except Exception:
        logger.exception(f"Failed to create poster for {dest_filename}")
        return processed
    finally:
        poster_path.unlink(missing_ok=True)


async def save_upload(
    db_session: AsyncSession, f: UploadFile
) -> activitypub.models.Upload | None:
    if f.content_type is None:
        return None

    size_limit = _size_limit_for(f.content_type)

    # Fast path when starlette already knows the fully-received size; the
    # streaming counter in _hash_and_measure is the actual guarantee.
    if f.size is not None and f.size > size_limit:
        raise UploadTooLargeError(size_limit)

    content_hash, _size = await asyncio.to_thread(_hash_and_measure, f.file, size_limit)

    existing_upload = (
        await db_session.execute(
            select(activitypub.models.Upload).where(
                activitypub.models.Upload.content_hash == content_hash
            )
        )
    ).scalar_one_or_none()
    if existing_upload:
        logger.info(f"Upload with {content_hash=} already exists")
        return existing_upload

    logger.info(f"Creating new Upload with {content_hash=}")
    dest_filename = UPLOAD_DIR / content_hash

    if f.content_type.startswith("image") and f.content_type != "image/gif":
        processed = await asyncio.to_thread(_process_image_upload, f, dest_filename)
    else:
        await asyncio.to_thread(_store_raw, f.file, dest_filename)
        if f.content_type.startswith(("video", "audio")):
            processed = await asyncio.to_thread(
                _process_av_upload, dest_filename, content_hash
            )
        else:
            processed = _ProcessedUpload()

    new_upload = activitypub.models.Upload(
        content_type=f.content_type,
        content_hash=content_hash,
        has_thumbnail=processed.has_thumbnail,
        blurhash=processed.blurhash,
        width=processed.width,
        height=processed.height,
        duration=processed.duration,
        has_audio=processed.has_audio,
    )
    db_session.add(new_upload)
    await db_session.commit()

    return new_upload


def upload_to_attachment(
    upload: activitypub.models.Upload,
    filename: str,
    alt_text: str | None,
) -> ap.RawObject:
    extra_attachment_fields: dict[str, object] = {}
    if upload.blurhash:
        extra_attachment_fields["blurhash"] = upload.blurhash
    if upload.width and upload.height:
        extra_attachment_fields.update(
            {
                "height": upload.height,
                "width": upload.width,
            }
        )
    if upload.duration is not None:
        extra_attachment_fields["duration"] = format_xsd_duration(
            float(upload.duration)
        )
    if not upload.is_image and upload.has_thumbnail:
        extra_attachment_fields["icon"] = {
            "type": "Image",
            "mediaType": "image/webp",
            "url": (
                BASE_URL + f"/attachments/thumbnails/{upload.content_hash}/{filename}"
            ),
        }
    return {
        "type": "Document",
        "mediaType": upload.content_type,
        "name": alt_text or filename,
        "url": BASE_URL + f"/attachments/{upload.content_hash}/{filename}",
        **extra_attachment_fields,
    }
