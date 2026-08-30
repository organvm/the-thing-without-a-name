"""Canonical identities for decoded renderer video streams.

Container bytes are not a picture identity: two containers can decode to the
same frames, and one container can be remuxed without changing a pixel.  This
module normalizes one video stream to ordered ``rgb24`` frames and hashes the
pixel bytes while enforcing frame boundaries and, when supplied, an exact frame
count.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO


RGB24_STREAM_ALGORITHM = "rgb24-stream-sha256-v1"


class MediaIdentityError(ValueError):
    """A video cannot satisfy the canonical decoded-stream contract."""


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise MediaIdentityError(f"{label} must be a positive integer")
    return value


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise MediaIdentityError("decoded RGB stream did not return bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def rgb24_stream_identity(
    stream: BinaryIO,
    *,
    width: int,
    height: int,
    expected_frames: int | None = None,
    _aggregate_digest: Any | None = None,
) -> dict[str, object]:
    """Hash a complete ordered RGB24 stream without buffering the movie.

    A short final frame is evidence corruption, not an ignorable trailer.  When
    ``expected_frames`` is provided, both missing and surplus complete frames
    fail closed after the entire stream has been consumed.
    """

    width = _positive_integer(width, "decoded video width")
    height = _positive_integer(height, "decoded video height")
    if expected_frames is not None:
        expected_frames = _positive_integer(expected_frames, "expected decoded frame count")

    frame_bytes = width * height * 3
    digest = hashlib.sha256()
    frames = 0
    while True:
        payload = _read_exact(stream, frame_bytes)
        if not payload:
            break
        if len(payload) != frame_bytes:
            raise MediaIdentityError(
                f"decoded RGB stream ended with a partial frame: {len(payload)} of {frame_bytes} bytes"
            )
        digest.update(payload)
        if _aggregate_digest is not None:
            _aggregate_digest.update(payload)
        frames += 1
        if expected_frames is not None and frames > expected_frames:
            raise MediaIdentityError(
                f"decoded RGB stream has more than the expected {expected_frames} frames"
            )

    if frames < 1:
        raise MediaIdentityError("decoded RGB stream contains no complete frames")
    if expected_frames is not None and frames != expected_frames:
        raise MediaIdentityError(
            f"decoded RGB stream has {frames} frames; expected exactly {expected_frames}"
        )
    return {
        "algorithm": RGB24_STREAM_ALGORITHM,
        "sha256": digest.hexdigest(),
        "frames": frames,
        "width": width,
        "height": height,
    }


def _regular_media(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise MediaIdentityError(f"decoded video source is missing or unsafe: {path}")
    return path.resolve(strict=True)


def _rewind_media_descriptor(source_fd: int, label: str) -> None:
    """Start every descriptor-backed decoder at the pinned file's first byte."""

    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise MediaIdentityError(f"{label} descriptor cannot be rewound") from exc


def decoded_video_identity(
    path: Path,
    *,
    width: int,
    height: int,
    expected_frames: int | None = None,
    ffmpeg: str | None = None,
    source_fd: int | None = None,
    _aggregate_digest: Any | None = None,
) -> dict[str, object]:
    """Decode one movie to RGB24 and return its canonical ordered identity."""

    if source_fd is None:
        source = str(_regular_media(path))
        pass_fds: tuple[int, ...] = ()
    else:
        try:
            info = os.fstat(source_fd)
        except OSError as exc:
            raise MediaIdentityError("decoded video descriptor is unavailable") from exc
        if not stat.S_ISREG(info.st_mode):
            raise MediaIdentityError("decoded video descriptor is not a regular file")
        _rewind_media_descriptor(source_fd, "decoded video")
        source = f"/dev/fd/{source_fd}"
        pass_fds = (source_fd,)
    executable = ffmpeg or shutil.which("ffmpeg")
    if not executable:
        raise MediaIdentityError("ffmpeg is required to identify decoded renderer video")
    command = [
        executable,
        "-nostdin",
        "-v",
        "error",
        "-i",
        source,
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-fps_mode",
        "passthrough",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "-",
    ]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=errors,
            pass_fds=pass_fds,
        )
        assert process.stdout is not None
        try:
            identity = rgb24_stream_identity(
                process.stdout,
                width=width,
                height=height,
                expected_frames=expected_frames,
                _aggregate_digest=_aggregate_digest,
            )
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
        returncode = process.wait()
        if returncode:
            errors.seek(0)
            detail = errors.read(64 << 10).decode("utf-8", errors="replace").strip()
            raise MediaIdentityError(f"ffmpeg cannot decode renderer video: {detail}")
    return identity


def decoded_video_chain_identities(
    paths: list[Path],
    *,
    width: int,
    height: int,
    expected_frames: list[int],
    ffmpeg: str | None = None,
    source_fds: list[int] | None = None,
    maximum_frames: int | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Decode ordered segment movies once, returning per-file and chain identities."""

    width = _positive_integer(width, "decoded video width")
    height = _positive_integer(height, "decoded video height")
    if not paths or len(paths) != len(expected_frames):
        raise MediaIdentityError("decoded video chain has no exact segment plan")
    planned = [_positive_integer(value, "expected decoded frame count") for value in expected_frames]
    if source_fds is not None and len(source_fds) != len(paths):
        raise MediaIdentityError("decoded video chain has no exact descriptor plan")
    total_planned = sum(planned)
    if maximum_frames is not None:
        maximum_frames = _positive_integer(maximum_frames, "maximum decoded frame count")
        if total_planned > maximum_frames:
            raise MediaIdentityError(
                f"decoded video chain plans {total_planned} frames; maximum is {maximum_frames}"
            )
    chain_digest = hashlib.sha256()
    segment_identities: list[dict[str, object]] = []
    descriptors: list[int | None] = (
        [None] * len(paths) if source_fds is None else list(source_fds)
    )
    for source, wanted, source_fd in zip(paths, planned, descriptors, strict=True):
        segment_identities.append(
            decoded_video_identity(
                source,
                width=width,
                height=height,
                expected_frames=wanted,
                ffmpeg=ffmpeg,
                source_fd=source_fd,
                _aggregate_digest=chain_digest,
            )
        )

    return segment_identities, {
        "algorithm": RGB24_STREAM_ALGORITHM,
        "sha256": chain_digest.hexdigest(),
        "frames": total_planned,
        "width": width,
        "height": height,
    }


def video_stream_info(
    path: Path,
    *,
    ffprobe: str | None = None,
    source_fd: int | None = None,
) -> dict[str, object]:
    """Return the decoded shape and rational average rate of one video stream."""

    if source_fd is None:
        source = str(_regular_media(path))
        pass_fds: tuple[int, ...] = ()
    else:
        try:
            info = os.fstat(source_fd)
        except OSError as exc:
            raise MediaIdentityError("renderer video descriptor is unavailable") from exc
        if not stat.S_ISREG(info.st_mode):
            raise MediaIdentityError("renderer video descriptor is not a regular file")
        _rewind_media_descriptor(source_fd, "renderer video")
        source = f"/dev/fd/{source_fd}"
        pass_fds = (source_fd,)
    executable = ffprobe or shutil.which("ffprobe")
    if not executable:
        raise MediaIdentityError("ffprobe is required to identify renderer video shape")
    done = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,profile,pix_fmt,width,height,avg_frame_rate",
            "-of",
            "json",
            source,
        ],
        capture_output=True,
        text=True,
        check=False,
        pass_fds=pass_fds,
    )
    if done.returncode:
        raise MediaIdentityError(f"ffprobe cannot inspect renderer video: {done.stderr.strip()}")
    try:
        document = json.loads(done.stdout)
        streams = document.get("streams")
        if not isinstance(streams, list) or not all(isinstance(row, dict) for row in streams):
            raise MediaIdentityError("ffprobe returned no exact renderer stream inventory")
        videos = [row for row in streams if row.get("codec_type") == "video"]
        if len(videos) != 1:
            raise MediaIdentityError("renderer output must contain exactly one video stream")
        video = videos[0]
        width = _positive_integer(video["width"], "decoded video width")
        height = _positive_integer(video["height"], "decoded video height")
        rate = Fraction(str(video["avg_frame_rate"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ZeroDivisionError) as exc:
        if isinstance(exc, MediaIdentityError):
            raise
        raise MediaIdentityError("ffprobe returned no exact renderer video shape or rate") from exc
    if rate <= 0:
        raise MediaIdentityError("ffprobe returned a non-positive renderer video rate")
    fps: int | float = rate.numerator if rate.denominator == 1 else float(rate)
    if not math.isfinite(float(fps)):
        raise MediaIdentityError("ffprobe returned a non-finite renderer video rate")
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "codec_name": video.get("codec_name"),
        "profile": video.get("profile"),
        "pix_fmt": video.get("pix_fmt"),
        "stream_count": len(streams),
        "video_streams": len(videos),
        "audio_streams": sum(row.get("codec_type") == "audio" for row in streams),
        "subtitle_streams": sum(row.get("codec_type") == "subtitle" for row in streams),
        "data_streams": sum(row.get("codec_type") == "data" for row in streams),
    }
