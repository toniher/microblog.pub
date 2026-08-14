import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import pytest

from app import ffmpeg

pytestmark = pytest.mark.skipif(
    not ffmpeg.is_available(), reason="ffmpeg/ffprobe not installed"
)


def _run(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, timeout=30)


@pytest.fixture(scope="session")
def clips(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Path]]:
    d = tmp_path_factory.mktemp("ffmpeg-clips")
    ffmpeg_bin = ffmpeg.FFMPEG
    assert ffmpeg_bin

    paths: dict[str, Path] = {}

    def clip(name: str, *args: str) -> Path:
        path = d / name
        _run([ffmpeg_bin, "-y", "-v", "error", *args, "--", str(path)])
        paths[name] = path
        return path

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
        "yuv444.mp4",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=2:size=64x64:rate=10",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv444p",
        "-an",
    )
    clip(
        "quicktime.mov",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=2:size=64x64:rate=10",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-an",
    )
    clip(
        "vp9.webm",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=2:size=64x64:rate=10",
        "-c:v",
        "libvpx-vp9",
        "-pix_fmt",
        "gbrp",
        "-an",
    )
    clip(
        "audio.mp3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:a",
        "libmp3lame",
    )
    clip(
        "std.wav",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:a",
        "pcm_s16le",
    )
    clip(
        "std.opus",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:a",
        "libopus",
    )
    clip(
        "std.flac",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:a",
        "flac",
    )

    cover = clip(
        "cover.png",
        "-f",
        "lavfi",
        "-i",
        "color=red:size=32x32:duration=1",
        "-frames:v",
        "1",
    )
    withcover_path = d / "withcover.mp3"
    _run(
        [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-i",
            str(paths["audio.mp3"]),
            "-i",
            str(cover),
            "-map",
            "0:a",
            "-map",
            "1:v",
            "-c",
            "copy",
            "-id3v2_version",
            "3",
            "-metadata:s:v",
            "title=Album cover",
            "-metadata:s:v",
            "comment=Cover (front)",
            "--",
            str(withcover_path),
        ]
    )
    paths["withcover.mp3"] = withcover_path

    yield paths

    shutil.rmtree(d, ignore_errors=True)


def test_pass_h264_yuv420p_with_aac(clips: dict[str, Path]) -> None:
    info = ffmpeg.probe(clips["safe.mp4"])
    assert info is not None
    assert info.incompatible_reason is None
    assert info.has_video_stream
    assert info.has_audio_stream
    assert info.width == 64 and info.height == 64
    assert info.duration is not None and info.duration == pytest.approx(2, abs=0.5)


def test_pass_pcm_wav(clips: dict[str, Path]) -> None:
    info = ffmpeg.probe(clips["std.wav"])
    assert info is not None
    assert info.incompatible_reason is None
    assert not info.has_video_stream
    assert info.has_audio_stream


def test_pass_mp3(clips: dict[str, Path]) -> None:
    info = ffmpeg.probe(clips["audio.mp3"])
    assert info is not None
    assert info.incompatible_reason is None


def test_pass_opus(clips: dict[str, Path]) -> None:
    info = ffmpeg.probe(clips["std.opus"])
    assert info is not None
    assert info.incompatible_reason is None


def test_pass_flac(clips: dict[str, Path]) -> None:
    info = ffmpeg.probe(clips["std.flac"])
    assert info is not None
    assert info.incompatible_reason is None


def test_pass_mp3_with_cover_art(clips: dict[str, Path]) -> None:
    """Regression test: an MP3 with embedded cover art reports an
    `attached_pic` video/png stream — without the cover-art guard this was
    rejected as "png video is not playable"."""
    info = ffmpeg.probe(clips["withcover.mp3"])
    assert info is not None
    assert info.incompatible_reason is None
    assert not info.has_video_stream
    assert info.has_audio_stream


def test_reject_hevc(clips: dict[str, Path]) -> None:
    info = ffmpeg.probe(clips["hevc.mp4"])
    assert info is not None
    assert info.incompatible_reason is not None
    assert "hevc" in info.incompatible_reason.lower()


def test_reject_yuv444p_chroma(clips: dict[str, Path]) -> None:
    info = ffmpeg.probe(clips["yuv444.mp4"])
    assert info is not None
    assert info.incompatible_reason is not None
    assert "yuv444p" in info.incompatible_reason


def test_reject_quicktime_container(clips: dict[str, Path]) -> None:
    info = ffmpeg.probe(clips["quicktime.mov"])
    assert info is not None
    assert info.incompatible_reason is not None
    assert "quicktime" in info.incompatible_reason.lower()


def test_reject_vp9_gbrp_chroma(clips: dict[str, Path]) -> None:
    info = ffmpeg.probe(clips["vp9.webm"])
    assert info is not None
    assert info.incompatible_reason is not None
    assert "gbrp" in info.incompatible_reason


def test_extract_poster(clips: dict[str, Path], tmp_path: Path) -> None:
    dest = tmp_path / "poster.png"
    assert ffmpeg.extract_poster(clips["safe.mp4"], dest, duration=2.0)
    assert dest.exists()
    assert dest.stat().st_size > 0


def test_probe_missing_file_returns_none(tmp_path: Path) -> None:
    assert ffmpeg.probe(tmp_path / "does-not-exist.mp4") is None


def test_extract_poster_missing_file_returns_false(tmp_path: Path) -> None:
    assert not ffmpeg.extract_poster(
        tmp_path / "does-not-exist.mp4", tmp_path / "out.png", duration=None
    )


def test_fail_open_when_ffprobe_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffmpeg, "FFPROBE", None)
    assert ffmpeg.probe(Path("/does/not/matter")) is None


def test_fail_open_when_ffmpeg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffmpeg, "FFMPEG", None)
    assert not ffmpeg.extract_poster(
        Path("/does/not/matter"), Path("/does/not/matter/out.png"), duration=1.0
    )


def test_is_available_reflects_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffmpeg, "FFPROBE", None)
    assert not ffmpeg.is_available()


# --- Pure classifier unit tests (no ffmpeg required) ------------------------


def test_classify_accepts_compatible_video() -> None:
    fmt = {"tags": {"major_brand": "isom"}}
    video = {"codec_name": "h264", "pix_fmt": "yuv420p"}
    assert ffmpeg.classify_compatibility(fmt, video, None) is None


def test_classify_accepts_pcm_audio_variants() -> None:
    for codec in ("pcm_s16le", "pcm_s24le", "pcm_f32le"):
        assert ffmpeg.classify_compatibility({}, None, {"codec_name": codec}) is None


def test_classify_ignores_cover_art_stream() -> None:
    # The caller (probe()) is responsible for excluding attached_pic streams
    # before calling classify_compatibility, so a bare audio-only view (no
    # video_stream passed) must pass cleanly.
    assert ffmpeg.classify_compatibility({}, None, {"codec_name": "mp3"}) is None


def test_classify_rejects_qt_brand() -> None:
    fmt = {"tags": {"major_brand": "qt  "}}
    video = {"codec_name": "h264", "pix_fmt": "yuv420p"}
    reason = ffmpeg.classify_compatibility(fmt, video, None)
    assert reason is not None and "quicktime" in reason.lower()


def test_classify_rejects_exotic_audio_codec() -> None:
    reason = ffmpeg.classify_compatibility({}, None, {"codec_name": "wmav2"})
    assert reason is not None and "wmav2" in reason
