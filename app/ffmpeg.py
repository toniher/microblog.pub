"""Subprocess wrapper over ffprobe/ffmpeg: metadata probing, poster frame
extraction, and browser-playability classification.

No transcoding — ffmpeg is only ever asked to read metadata or extract a
single frame. Every call is argv-only (never `shell=True`), whitelists the
`file` protocol, and runs with an explicit timeout. A missing binary, a
timeout, or unparsable output all degrade to "unknown" (`None`/`False`)
rather than raising, so an instance without ffmpeg keeps accepting uploads —
just without duration/poster/compatibility checking.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")

_PROBE_TIMEOUT = 20
_POSTER_TIMEOUT = 20

# Rejection is a one-way door for an upload, so only reject on confident,
# well-understood incompatibilities. Verdict unavailable (no ffmpeg, probe
# failure, timeout) always means "accept" — see is_available()/probe().
_COMPATIBLE_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}
_COMPATIBLE_PIX_FMTS = {"yuv420p", "yuvj420p"}
_COMPATIBLE_10BIT_PIX_FMTS = {"yuv420p10le"}
# The `pcm_*` family (pcm_s16le, pcm_s24le, ...) is accepted wholesale: a
# plausible-looking allowlist without it rejects bog-standard 16-bit WAV.
_COMPATIBLE_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac", "alac"}


@dataclass(frozen=True)
class MediaInfo:
    duration: float | None
    width: int | None
    height: int | None
    has_video_stream: bool
    has_audio_stream: bool
    incompatible_reason: str | None  # None => plays in mainstream browsers


def is_available() -> bool:
    return FFPROBE is not None and FFMPEG is not None


def _select_video_stream(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        # Cover-art guard: an MP3 with embedded album art reports a
        # video/png stream with disposition.attached_pic=1. Without this
        # guard, an ordinary MP3 is treated as a video and, under the
        # reject policy, refused outright.
        if (stream.get("disposition") or {}).get("attached_pic") == 1:
            continue
        return stream
    return None


def _select_audio_stream(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    for stream in streams:
        if stream.get("codec_type") == "audio":
            return stream
    return None


def _is_rotated_90(stream: dict[str, Any]) -> bool:
    for side_data in stream.get("side_data_list") or []:
        rotation = side_data.get("rotation")
        if rotation is None:
            continue
        try:
            return abs(int(rotation)) == 90
        except (TypeError, ValueError):
            return False
    return False


def _parse_duration(fmt: dict[str, Any], stream: dict[str, Any] | None) -> float | None:
    raw = fmt.get("duration")
    if raw is None and stream is not None:
        raw = stream.get("duration")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def classify_compatibility(
    fmt: dict[str, Any],
    video_stream: dict[str, Any] | None,
    audio_stream: dict[str, Any] | None,
) -> str | None:
    """Pure classification logic, split out from probe() so it can be unit
    tested against synthetic ffprobe-shaped dicts without a real file.

    Returns None when the file is expected to play in mainstream browsers
    (Firefox/Chrome), otherwise a user-facing reason naming the problem and
    the fix.
    """
    major_brand = ((fmt.get("tags") or {}).get("major_brand") or "").strip()
    if major_brand == "qt":
        return (
            "QuickTime (.mov) container is not playable in Firefox or "
            "Chrome — re-encode as H.264/AAC in an MP4"
        )

    if video_stream is not None:
        codec_name = video_stream.get("codec_name")
        if codec_name not in _COMPATIBLE_VIDEO_CODECS:
            return (
                f"{codec_name or 'unknown'} video is not playable in most "
                "browsers — re-encode as H.264/AAC in an MP4"
            )

        pix_fmt = video_stream.get("pix_fmt")
        allowed_pix_fmts = _COMPATIBLE_PIX_FMTS | (
            _COMPATIBLE_10BIT_PIX_FMTS if codec_name in {"vp9", "av1"} else set()
        )
        if pix_fmt not in allowed_pix_fmts:
            return (
                f"{codec_name} with {pix_fmt or 'unknown'} chroma is not "
                "playable in browsers — re-encode as H.264/yuv420p in an MP4"
            )

    if audio_stream is not None:
        codec_name = audio_stream.get("codec_name") or ""
        if codec_name not in _COMPATIBLE_AUDIO_CODECS and not codec_name.startswith(
            "pcm_"
        ):
            return (
                f"{codec_name or 'unknown'} audio is not playable in most "
                "browsers — re-encode as AAC or MP3"
            )

    return None


def probe(path: Path) -> MediaInfo | None:
    if FFPROBE is None:
        return None

    try:
        proc = subprocess.run(
            [
                FFPROBE,
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                "--",
                str(path),
            ],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            check=True,
        )
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
    ) as exc:
        logger.warning(f"ffprobe failed for {path}: {exc}")
        return None

    try:
        data = json.loads(proc.stdout)
        fmt = data.get("format") or {}
        streams = data.get("streams") or []

        video_stream = _select_video_stream(streams)
        audio_stream = _select_audio_stream(streams)

        width = height = None
        if video_stream is not None:
            width = video_stream.get("width")
            height = video_stream.get("height")
            if width and height and _is_rotated_90(video_stream):
                width, height = height, width

        return MediaInfo(
            duration=_parse_duration(fmt, video_stream or audio_stream),
            width=width,
            height=height,
            has_video_stream=video_stream is not None,
            has_audio_stream=audio_stream is not None,
            incompatible_reason=classify_compatibility(fmt, video_stream, audio_stream),
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning(f"failed to parse ffprobe output for {path}: {exc}")
        return None


def _run_ffmpeg_poster(src: Path, dest: Path, seek_to: float) -> bool:
    assert FFMPEG is not None
    try:
        subprocess.run(
            [
                FFMPEG,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-protocol_whitelist",
                "file",
                "-ss",
                str(seek_to),
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-an",
                "-sn",
                "-dn",
                "-f",
                "image2",
                "-c:v",
                "png",
                "--",
                str(dest),
            ],
            capture_output=True,
            timeout=_POSTER_TIMEOUT,
            check=True,
        )
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
    ) as exc:
        logger.warning(f"ffmpeg poster extraction failed for {src}: {exc}")
        return False

    return dest.exists() and dest.stat().st_size > 0


def extract_poster(src: Path, dest: Path, duration: float | None) -> bool:
    if FFMPEG is None:
        return False

    seek_to = min(1.0, duration / 2) if duration else 0.0
    if _run_ffmpeg_poster(src, dest, seek_to):
        return True

    # Retry once at the very start — very short clips can have no frame at
    # the midpoint seek.
    if seek_to != 0.0:
        return _run_ffmpeg_poster(src, dest, 0.0)

    return False
