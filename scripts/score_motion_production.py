#!/usr/bin/env python3
"""Mint and authenticate production score-to-motion A/B machine evidence.

The tracked August receipts are deliberately retained as historical fixture
evidence.  They cannot satisfy this predicate.  Production evidence is minted
only for one clean Git HEAD and binds the selected score, choreography, complete
passage, deterministic audio receipt/master, renderer source tree, boundary
frames, and two full-speed review movies.

This tool never records artistic acceptance.  A valid receipt always carries
``human_review.status = not-attested``; the submission attestation remains a
separate human-owned gate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import wave
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SCHEMA = ROOT / "docs/evidence/score-to-motion-production.schema.json"
SAMPLE_SCHEMA = ROOT / "docs/evidence/score-to-motion-samples-production.schema.json"
FRAME_SCHEMA = ROOT / "docs/evidence/score-to-motion-frames-production.schema.json"
DEFAULT_AUDIO_RECEIPT = ROOT / ".work/music/competition/audio-render.json"
DEFAULT_EVIDENCE_DIR = ROOT / ".work/evidence/score-to-motion-production"
DEFAULT_SAMPLE_RECEIPT = DEFAULT_EVIDENCE_DIR / "score-to-motion-samples.json"
DEFAULT_FRAME_RECEIPT = DEFAULT_EVIDENCE_DIR / "score-to-motion-frames.json"
DEFAULT_PRODUCTION_RECEIPT = DEFAULT_EVIDENCE_DIR / "score-to-motion-production.json"

PRODUCTION_SCHEMA_ID = "danse.evidence.score-to-motion.production.v1"
SAMPLE_SCHEMA_ID = "danse.evidence.score-to-motion-samples.production.v1"
FRAME_SCHEMA_ID = "danse.evidence.score-to-motion-frames.production.v1"
CONTEXT_FIELDS = (
    "repository_head",
    "source_tree_sha256",
    "span",
    "score",
    "choreography",
    "audio_render_receipt",
    "audio_master",
)
FIXTURE_SCHEMAS = {
    "danse.evidence.score-to-motion-ab.fixture.v1",
    "danse.evidence.score-to-motion-frames.v1",
}
HEX64 = re.compile(r"[0-9a-f]{64}")
GIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
CHANNELS = ("divergence", "azimuth", "elevation", "spread", "projK", "turnover")
PRODUCTION_TIER = "film"
PRODUCTION_WIDTH = 1920
PRODUCTION_HEIGHT = 1080
PRODUCTION_FPS = 30
PRODUCTION_PASSAGE_SEED = 2943173797
REVIEW_ANCHOR_PSNR_FLOOR = 30.0
REVIEW_ANCHOR_MAX_COUNT = 256
PRODUCER_CONCAT_RECEIPT_MAX_BYTES = 2 << 20
PRODUCER_SEGMENT_RECEIPT_MAX_BYTES = 512 << 10
PRODUCER_SEGMENT_RECEIPT_TOTAL_MAX_BYTES = 16 << 20
PRODUCER_SEGMENT_MAX_COUNT = 256
# Production evidence is limited to 256 canonical ten-second (600-frame)
# renderer segments.  Besides bounding decoder work, this keeps hostile JSON
# durations from reaching float multiplication or round() as unbounded counts.
PRODUCER_SEGMENT_FRAME_MAX = 600
PRODUCTION_FRAME_MAX = PRODUCER_SEGMENT_MAX_COUNT * PRODUCER_SEGMENT_FRAME_MAX
PRODUCER_MEDIA_MAX_BYTES_PER_FRAME = 8 << 20
CANONICAL_REPLAY_TIMEOUT_SECONDS = 15 * 60
CANONICAL_REPLAY_CLEANUP_SECONDS = 10
CANONICAL_REPLAY_SOURCE_FILE_MAX_BYTES = 128 << 20
CANONICAL_REPLAY_SOURCE_TOTAL_MAX_BYTES = 4 << 30
CANONICAL_REPLAY_SOURCE_DIRECTORY_MAX_ENTRIES = 4096
FRAME_IMAGE_MAX_BYTES = 64 << 20
PRODUCTION_RECEIPT_MAX_BYTES = 4 << 20
SAMPLE_RECEIPT_MAX_BYTES = 16 << 20
FRAME_RECEIPT_MAX_BYTES = 16 << 20
CAPTURE_TOOL = ROOT / "scripts/score_motion_production.py"
BROWSER_CONTRACT = ROOT / "render/browser.py"

sys.path.insert(0, str(ROOT / "sound"))
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "render"))
from choreography import load_choreography  # noqa: E402
from corpus_contract import authorize_render_tier  # noqa: E402
from media_identity import (  # noqa: E402
    MediaIdentityError,
    decoded_video_chain_identities,
    decoded_video_identity,
    video_stream_info,
)
from music_score import load_score  # noqa: E402


class EvidenceError(ValueError):
    """A production claim cannot be authenticated."""


def finite_receipt_number(value: object) -> bool:
    """Accept exact JSON numbers while excluding bool and non-finite values."""
    return (
        type(value) in (int, float)
        and -sys.float_info.max <= value <= sys.float_info.max
    )


def production_frame_count(duration: object, label: str = "production duration") -> int:
    """Return the one bounded 30 fps frame count admitted by the evidence gate."""

    if not finite_receipt_number(duration) or duration <= 0:
        raise EvidenceError(f"{label} must be a finite positive JSON number")
    frame_value = float(duration) * PRODUCTION_FPS
    if not math.isfinite(frame_value):
        raise EvidenceError(f"{label} does not yield a finite production frame count")
    if frame_value > PRODUCTION_FRAME_MAX:
        raise EvidenceError(
            f"{label} exceeds the {PRODUCTION_FRAME_MAX}-frame production limit"
        )
    frames = round(frame_value)
    if frames < 1:
        raise EvidenceError(f"{label} does not contain one complete production frame")
    return frames


def production_audio_frame_count(
    duration: object,
    sample_rate: object,
    label: str = "production audio duration",
) -> int:
    """Return a bounded PCM count for the same maximum production span."""

    production_frame_count(duration, label)
    if type(sample_rate) is not int or sample_rate < 1:
        raise EvidenceError("production audio sample rate must be a positive integer")
    sample_value = float(duration) * sample_rate
    maximum_samples = PRODUCTION_FRAME_MAX * sample_rate / PRODUCTION_FPS
    if not math.isfinite(sample_value) or sample_value > maximum_samples:
        raise EvidenceError(f"{label} has an unreasonable production audio frame count")
    samples = int(
        (Decimal(str(duration)) * sample_rate).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    if samples < 1:
        raise EvidenceError(f"{label} does not contain one complete production audio frame")
    return samples


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"{label} is missing or is not a regular file: {path}")
    return path.resolve(strict=True)


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _verify_pinned_file(
    path: Path,
    directory_fd: int,
    file_fd: int,
    directory_identity: tuple[int, ...],
    file_identity: tuple[int, ...],
    label: str,
) -> None:
    try:
        current_directory = os.fstat(directory_fd)
        named_directory = os.stat(path.parent, follow_symlinks=False)
        current_file = os.fstat(file_fd)
        named_file = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise EvidenceError(f"{label} changed during authentication") from exc
    if (
        not stat.S_ISDIR(current_directory.st_mode)
        or not stat.S_ISDIR(named_directory.st_mode)
        or not stat.S_ISREG(current_file.st_mode)
        or not stat.S_ISREG(named_file.st_mode)
        or _stat_identity(current_directory) != directory_identity
        or _stat_identity(named_directory) != directory_identity
        or _stat_identity(current_file) != file_identity
        or _stat_identity(named_file) != file_identity
    ):
        raise EvidenceError(f"{label} changed during authentication")


@contextmanager
def _pinned_regular_file(path: Path, label: str):
    """Pin one no-follow file and its named parent for a complete measurement."""

    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required) or not hasattr(os, "pread"):
        raise EvidenceError(f"{label} cannot be descriptor-pinned on this platform")
    directory_fd = None
    file_fd = None
    try:
        path = path.absolute()
        directory_fd = os.open(
            os.sep,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        for part in path.parent.parts[1:]:
            next_directory_fd = os.open(
                part,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_directory_fd
        file_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        directory_info = os.fstat(directory_fd)
        file_info = os.fstat(file_fd)
        directory_identity = _stat_identity(directory_info)
        file_identity = _stat_identity(file_info)
        _verify_pinned_file(
            path,
            directory_fd,
            file_fd,
            directory_identity,
            file_identity,
            label,
        )
        try:
            yield file_fd, file_info
        except BaseException:
            raise
        else:
            _verify_pinned_file(
                path,
                directory_fd,
                file_fd,
                directory_identity,
                file_identity,
                label,
            )
    except EvidenceError:
        raise
    except OSError as exc:
        raise EvidenceError(f"{label} is missing, unsafe, or cannot be pinned") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _fd_sha256(file_fd: int, label: str) -> str:
    digest = hashlib.sha256()
    offset = 0
    try:
        while True:
            chunk = os.pread(file_fd, 1 << 20, offset)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
    except OSError as exc:
        raise EvidenceError(f"cannot read {label} for its encoded digest") from exc
    return digest.hexdigest()


def _read_pinned_bytes(file_fd: int, *, maximum: int, label: str) -> bytes:
    chunks = []
    offset = 0
    try:
        while offset <= maximum:
            chunk = os.pread(file_fd, min(1 << 20, maximum + 1 - offset), offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
    except OSError as exc:
        raise EvidenceError(f"cannot read {label}") from exc
    payload = b"".join(chunks)
    if len(payload) > maximum:
        raise EvidenceError(f"{label} exceeds its {maximum}-byte limit")
    return payload


def _safe_relative(relative: object, label: str) -> PurePosixPath:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise EvidenceError(f"{label} has no safe relative path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or "." in pure.parts or ".." in pure.parts:
        raise EvidenceError(f"{label} escapes its evidence boundary")
    return pure


def _bounded_file(root: Path, relative: object, label: str) -> Path:
    pure = _safe_relative(relative, label)
    boundary = root.resolve(strict=True)
    current = boundary
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise EvidenceError(f"{label} traverses a symlink")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(boundary)
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"{label} is missing or outside its evidence boundary") from exc
    if not resolved.is_file():
        raise EvidenceError(f"{label} is not a regular file")
    return resolved


def repository_file(root: Path, relative: object, label: str) -> Path:
    return _bounded_file(root, relative, label)


def local_artifact(receipt_path: Path, reference: object, label: str) -> Path:
    if not isinstance(reference, dict):
        raise EvidenceError(f"{label} reference is missing")
    return _bounded_file(receipt_path.parent, reference.get("path"), label)


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number {value}")


def _json_object_from_payload(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise EvidenceError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be a JSON object")
    return value


@contextmanager
def _pinned_json_snapshot(path: Path, label: str, *, max_bytes: int):
    """Hold a JSON file pinned while consumers validate its parsed snapshot."""

    with _pinned_regular_file(path, label) as (file_fd, info):
        if info.st_size > max_bytes:
            raise EvidenceError(f"{label} exceeds its {max_bytes}-byte limit")
        payload = _read_pinned_bytes(file_fd, maximum=max_bytes, label=label)
        if len(payload) != info.st_size:
            raise EvidenceError(f"{label} changed during authentication")
        yield _json_object_from_payload(payload, label), payload


@contextmanager
def _pinned_artifact_json_snapshot(
    receipt_path: Path,
    reference: object,
    label: str,
    *,
    max_bytes: int,
):
    """Hold a referenced JSON artifact pinned across its complete validation."""

    try:
        path = local_artifact(receipt_path, reference, label)
    except EvidenceError as exc:
        yield [str(exc)], None, None, None
        return
    assert isinstance(reference, dict)
    with _pinned_json_snapshot(path, label, max_bytes=max_bytes) as (value, payload):
        errors = []
        if reference.get("sha256") != hashlib.sha256(payload).hexdigest():
            errors.append(f"{label} digest is stale")
        if reference.get("bytes") != len(payload):
            errors.append(f"{label} byte count is stale")
        yield errors, path, value, payload


def _bounded_json_snapshot(
    path: Path,
    label: str,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any], bytes]:
    with _pinned_json_snapshot(path, label, max_bytes=max_bytes) as snapshot:
        return snapshot


def _bounded_json_snapshot_token(
    path: Path,
    label: str,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any], bytes, tuple[tuple[int, ...], str, int]]:
    """Return parsed bytes plus a file-identity token for later revalidation."""

    with _pinned_regular_file(path, label) as (file_fd, info):
        if info.st_size > max_bytes:
            raise EvidenceError(f"{label} exceeds its {max_bytes}-byte limit")
        payload = _read_pinned_bytes(file_fd, maximum=max_bytes, label=label)
        if len(payload) != info.st_size:
            raise EvidenceError(f"{label} changed during authentication")
        value = _json_object_from_payload(payload, label)
        token = (_stat_identity(info), hashlib.sha256(payload).hexdigest(), len(payload))
        return value, payload, token


def _revalidate_json_snapshot_token(
    path: Path,
    label: str,
    *,
    max_bytes: int,
    token: tuple[tuple[int, ...], str, int],
) -> None:
    """Reject a receipt replaced or rewritten after its parsed snapshot closed."""

    with _pinned_regular_file(path, label) as (file_fd, info):
        if _stat_identity(info) != token[0] or info.st_size > max_bytes:
            raise EvidenceError(f"{label} changed during authentication")
        payload = _read_pinned_bytes(file_fd, maximum=max_bytes, label=label)
        if (
            len(payload) != info.st_size
            or len(payload) != token[2]
            or hashlib.sha256(payload).hexdigest() != token[1]
        ):
            raise EvidenceError(f"{label} changed during authentication")


def _bounded_binary_snapshot(
    path: Path,
    label: str,
    *,
    max_bytes: int,
) -> bytes:
    """Read one bounded regular file through a stable no-follow descriptor."""

    with _pinned_regular_file(path, label) as (file_fd, info):
        if info.st_size > max_bytes:
            raise EvidenceError(f"{label} exceeds its {max_bytes}-byte limit")
        payload = _read_pinned_bytes(file_fd, maximum=max_bytes, label=label)
        if len(payload) != info.st_size:
            raise EvidenceError(f"{label} changed during authentication")
    return payload


def read_json(
    path: Path,
    label: str,
    *,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    path = regular_file(path, label)
    if max_bytes is not None:
        value, _ = _bounded_json_snapshot(path, label, max_bytes=max_bytes)
        return value
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise EvidenceError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be a JSON object")
    return value


def _schema_error(document: dict[str, Any], schema_path: Path, label: str) -> str | None:
    try:
        schema = read_json(schema_path, f"{label} schema")
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(document)
    except (EvidenceError, jsonschema.SchemaError, jsonschema.ValidationError) as exc:
        detail = exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc)
        return f"{label} schema failed: {detail}"
    return None


def git_identity(root: Path = ROOT, *, require_clean: bool = True) -> str:
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = head.stdout.strip().lower()
    if head.returncode or not GIT_OID.fullmatch(commit):
        raise EvidenceError("production A/B evidence requires a full Git HEAD identity")
    if require_clean:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if status.returncode:
            raise EvidenceError("cannot inspect the production A/B source worktree")
        if status.stdout.strip():
            raise EvidenceError("production A/B evidence requires a clean exact Git worktree")
    return commit


def renderer_source_tree(
    tier: str,
    root: Path = ROOT,
    *,
    with_score: bool = True,
) -> str:
    path = root / "render/render.py"
    spec = importlib.util.spec_from_file_location("danse_score_motion_renderer", path)
    if spec is None or spec.loader is None:
        raise EvidenceError("cannot load the canonical renderer source identity")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        args = SimpleNamespace(
            score="music/score.json" if with_score else None,
            choreography="render/choreography.json" if with_score else None,
            timing_score=None if with_score else "music/score.json",
            tier=tier,
        )
        value = module.source_tree_sha256(args)
    except (OSError, SystemExit, TypeError, ValueError) as exc:
        raise EvidenceError(f"cannot resolve the canonical renderer source identity: {exc}") from exc
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise EvidenceError("canonical renderer returned an invalid source-tree digest")
    return value


def capture_contract_identity(root: Path = ROOT) -> dict[str, Any]:
    """Bind the receipt to the exact Metal capture implementation being checked."""
    return {
        "tool": {
            "path": "scripts/score_motion_production.py",
            "sha256": sha256(regular_file(root / CAPTURE_TOOL.relative_to(ROOT), "score-motion capture tool")),
        },
        "browser": {
            "path": "render/browser.py",
            "sha256": sha256(regular_file(root / BROWSER_CONTRACT.relative_to(ROOT), "Metal browser contract")),
        },
    }


def _production_review_position(second: float) -> tuple[int, float]:
    if not finite_receipt_number(second) or second < 0:
        raise EvidenceError("production review second must be a finite nonnegative JSON number")
    frame_value = float(second) * PRODUCTION_FPS
    if not math.isfinite(frame_value) or frame_value > PRODUCTION_FRAME_MAX:
        raise EvidenceError("production review second exceeds the bounded production frame span")
    index = int(math.floor(frame_value + 0.5))
    return index, index / PRODUCTION_FPS


def hydrated_work_root(root: Path = ROOT) -> Path:
    configured = os.environ.get("DANSE_WORK")
    return Path(configured).expanduser() if configured else root / "pipeline/.work"


def require_production_tier(root: Path = ROOT) -> None:
    allowed, detail = authorize_render_tier(
        root / "corpus",
        hydrated_work_root(root),
        PRODUCTION_TIER,
    )
    if not allowed:
        raise EvidenceError(f"production film tier is not authorized: {detail}")


def wav_pcm_identity(path: Path) -> dict[str, Any]:
    path = regular_file(path, "competition audio master")
    digest = hashlib.sha256()
    try:
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            sample_rate = reader.getframerate()
            sample_width = reader.getsampwidth()
            frames = reader.getnframes()
            if channels != 2 or sample_rate != 48000 or sample_width != 2:
                raise EvidenceError("competition audio master must be 48 kHz stereo PCM s16")
            remaining = frames
            while remaining:
                chunk_frames = min(1 << 16, remaining)
                chunk = reader.readframes(chunk_frames)
                if len(chunk) != chunk_frames * channels * sample_width:
                    raise EvidenceError("competition audio master ended before its declared frame count")
                digest.update(chunk)
                remaining -= chunk_frames
    except (OSError, wave.Error) as exc:
        raise EvidenceError(f"cannot decode the competition audio master: {exc}") from exc
    return {
        "pcm_sha256": digest.hexdigest(),
        "frames": frames,
        "sample_rate": sample_rate,
        "channels": channels,
        "duration_seconds": frames / sample_rate,
    }


def _audio_master(receipt: dict[str, Any]) -> dict[str, Any]:
    outputs = receipt.get("outputs")
    master = outputs.get("master") if isinstance(outputs, dict) else None
    if not isinstance(master, dict):
        raise EvidenceError("audio-render receipt has no master output")
    return master


def current_context(
    audio_receipt: Path = DEFAULT_AUDIO_RECEIPT,
    *,
    tier: str = PRODUCTION_TIER,
    root: Path = ROOT,
    require_clean: bool = True,
) -> dict[str, Any]:
    """Authenticate the exact production inputs that every receipt must copy."""
    if tier != PRODUCTION_TIER:
        raise EvidenceError(f"production A/B evidence requires corpus tier {PRODUCTION_TIER}")
    require_production_tier(root)
    score_path = root / "music/score.json"
    choreography_path = root / "render/choreography.json"
    try:
        score = load_score(score_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot validate the production score: {exc}") from exc
    if score.get("release_status") != "production-selected":
        raise EvidenceError("production A/B evidence rejects a non-selected score")
    if score.get("time", {}).get("passage_mapping") != "native-tempo":
        raise EvidenceError("production A/B evidence rejects affine score timing")
    try:
        choreography = load_choreography(
            choreography_path,
            score=score,
            score_path=score_path,
            corpus_manifest=json.loads((root / "corpus/manifest.json").read_text()),
            corpus_manifest_path=root / "corpus/manifest.json",
            corpus_score_path=root / "corpus/score-2017.json",
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot validate production choreography: {exc}") from exc
    duration = float(score["time"]["duration_seconds"])
    if choreography["identity"]["score_contract_sha256"] != score["identity"]["contract_sha256"]:
        raise EvidenceError("choreography does not bind the current production score")

    audio_receipt = regular_file(audio_receipt, "competition audio-render receipt")
    try:
        audio_relative = audio_receipt.relative_to(root.resolve(strict=True)).as_posix()
    except ValueError as exc:
        raise EvidenceError("competition audio-render receipt must stay inside the repository") from exc
    audio = read_json(audio_receipt, "competition audio-render receipt")
    schema_error = _schema_error(audio, root / "music/audio-render.schema.json", "competition audio-render receipt")
    if schema_error:
        raise EvidenceError(schema_error)
    inputs = audio.get("inputs") or {}
    expected_score = {
        "path": "music/score.json",
        "sha256": sha256(score_path),
        "contract_sha256": score["identity"]["contract_sha256"],
        "duration_seconds": duration,
    }
    expected_choreography = {
        "path": "render/choreography.json",
        "sha256": sha256(choreography_path),
        "contract_sha256": choreography["identity"]["contract_sha256"],
    }
    if inputs.get("score") != expected_score:
        raise EvidenceError("competition audio-render receipt names a stale score")
    if inputs.get("choreography") != expected_choreography:
        raise EvidenceError("competition audio-render receipt names stale choreography")
    required_audio_checks = (
        "deterministic", "non_silent", "stems_non_silent", "polyphonic",
        "normalization_deterministic", "loudness_in_target", "true_peak_in_target",
        "duration_matches_score", "seek_safe",
    )
    verification = audio.get("verification") or {}
    if any(verification.get(name) is not True for name in required_audio_checks):
        raise EvidenceError("competition audio-render receipt has not passed every production check")

    master = _audio_master(audio)
    master_path = repository_file(root, master.get("path"), "competition audio master")
    if master.get("sha256") != sha256(master_path):
        raise EvidenceError("competition audio master digest is stale")
    pcm = wav_pcm_identity(master_path)
    expected_frames = production_audio_frame_count(
        duration,
        pcm["sample_rate"],
        "production score duration",
    )
    if master.get("frames") != expected_frames or pcm["frames"] != expected_frames:
        raise EvidenceError("competition audio master frame count does not match the exact score span")
    for field in ("sample_rate", "channels"):
        if master.get(field) != pcm[field]:
            raise EvidenceError(f"competition audio master has stale {field}")
    if abs(float(master.get("duration_seconds", -1)) - pcm["duration_seconds"]) > 1e-9:
        raise EvidenceError("competition audio master duration is stale")
    try:
        master_relative = master_path.relative_to(root.resolve(strict=True)).as_posix()
    except ValueError as exc:
        raise EvidenceError("competition audio master must stay inside the repository") from exc

    return {
        "repository_head": git_identity(root, require_clean=require_clean),
        "source_tree_sha256": renderer_source_tree(tier, root),
        "span": {
            "river_seed": 20170620,
            "stream": 0,
            "passage": 0,
            "t0": 0,
            "t1": duration,
            "duration_seconds": duration,
        },
        "score": {
            "path": "music/score.json",
            "file_sha256": expected_score["sha256"],
            "contract_sha256": expected_score["contract_sha256"],
            "duration_seconds": duration,
        },
        "choreography": {
            "path": "render/choreography.json",
            "file_sha256": expected_choreography["sha256"],
            "contract_sha256": expected_choreography["contract_sha256"],
        },
        "audio_render_receipt": {"path": audio_relative, "sha256": sha256(audio_receipt)},
        "audio_master": {
            "path": master_relative,
            "sha256": master["sha256"],
            **pcm,
        },
    }


def artifact_reference(path: Path, relative_to: Path) -> dict[str, Any]:
    path = regular_file(path, "evidence artifact")
    try:
        relative = path.relative_to(relative_to.resolve(strict=True)).as_posix()
    except ValueError as exc:
        raise EvidenceError("evidence artifacts must share the receipt directory") from exc
    if path.stat().st_size < 1:
        raise EvidenceError(f"evidence artifact is empty: {path}")
    return {"path": relative, "sha256": sha256(path), "bytes": path.stat().st_size}


def _artifact_errors(
    receipt_path: Path,
    reference: object,
    label: str,
) -> tuple[list[str], Path | None]:
    try:
        path = local_artifact(receipt_path, reference, label)
    except EvidenceError as exc:
        return [str(exc)], None
    assert isinstance(reference, dict)
    errors = []
    if reference.get("sha256") != sha256(path):
        errors.append(f"{label} digest is stale")
    if reference.get("bytes") != path.stat().st_size:
        errors.append(f"{label} byte count is stale")
    return errors, path


def _artifact_json_snapshot(
    receipt_path: Path,
    reference: object,
    label: str,
    *,
    max_bytes: int,
) -> tuple[list[str], Path | None, dict[str, Any] | None]:
    """Authenticate and parse one bounded artifact from the same pinned bytes."""

    try:
        path = local_artifact(receipt_path, reference, label)
        value, payload = _bounded_json_snapshot(path, label, max_bytes=max_bytes)
    except EvidenceError as exc:
        return [str(exc)], None, None
    assert isinstance(reference, dict)
    errors = []
    digest = hashlib.sha256(payload).hexdigest()
    if reference.get("sha256") != digest:
        errors.append(f"{label} digest is stale")
    if reference.get("bytes") != len(payload):
        errors.append(f"{label} byte count is stale")
    return errors, path, value


def _artifact_json_snapshot_token(
    receipt_path: Path,
    reference: object,
    label: str,
    *,
    max_bytes: int,
) -> tuple[
    list[str],
    Path | None,
    dict[str, Any] | None,
    tuple[tuple[int, ...], str, int] | None,
]:
    """Authenticate one JSON artifact and retain its exact identity token."""

    try:
        path = local_artifact(receipt_path, reference, label)
        value, payload, token = _bounded_json_snapshot_token(
            path,
            label,
            max_bytes=max_bytes,
        )
    except EvidenceError as exc:
        return [str(exc)], None, None, None
    assert isinstance(reference, dict)
    errors = []
    if reference.get("sha256") != token[1]:
        errors.append(f"{label} digest is stale")
    if reference.get("bytes") != token[2]:
        errors.append(f"{label} byte count is stale")
    return errors, path, value, token


def _artifact_binary_snapshot(
    receipt_path: Path,
    reference: object,
    label: str,
    *,
    max_bytes: int,
) -> tuple[list[str], Path | None, bytes | None]:
    """Authenticate one bounded artifact and return its exact pinned bytes."""

    try:
        path = local_artifact(receipt_path, reference, label)
        payload = _bounded_binary_snapshot(path, label, max_bytes=max_bytes)
    except EvidenceError as exc:
        return [str(exc)], None, None
    assert isinstance(reference, dict)
    errors = []
    if reference.get("sha256") != hashlib.sha256(payload).hexdigest():
        errors.append(f"{label} digest is stale")
    if reference.get("bytes") != len(payload):
        errors.append(f"{label} byte count is stale")
    return errors, path, payload


def _cached_review_artifact_errors(
    receipt_path: Path,
    reference: object,
    label: str,
    *,
    expected_frames: int,
) -> tuple[list[str], Path | None]:
    """Recheck a cached review measurement against one stable encoded snapshot."""

    try:
        path = local_artifact(receipt_path, reference, label)
        with _pinned_regular_file(path, label) as (file_fd, info):
            maximum = expected_frames * PRODUCER_MEDIA_MAX_BYTES_PER_FRAME
            if info.st_size > maximum:
                raise EvidenceError(f"{label} exceeds its {maximum}-byte media limit")
            digest = _fd_sha256(file_fd, label)
    except EvidenceError as exc:
        return [str(exc)], None
    assert isinstance(reference, dict)
    errors = []
    if reference.get("sha256") != digest:
        errors.append(f"{label} digest is stale")
    if reference.get("bytes") != info.st_size:
        errors.append(f"{label} byte count is stale")
    return errors, path


def media_pcm_identity(
    path: Path,
    *,
    sample_rate: int = 48000,
    channels: int = 2,
    expected_frames: int | None = None,
    source_fd: int | None = None,
) -> dict[str, Any]:
    """Hash the decoded PCM, rather than pretending a movie equals a WAV file."""
    if source_fd is None:
        source = str(regular_file(path, "A/B review media"))
        pass_fds: tuple[int, ...] = ()
    else:
        try:
            os.lseek(source_fd, 0, os.SEEK_SET)
        except OSError as exc:
            raise EvidenceError("A/B review media descriptor cannot be rewound") from exc
        source = f"/dev/fd/{source_fd}"
        pass_fds = (source_fd,)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise EvidenceError("ffmpeg is required to authenticate review-media audio")
    if expected_frames is not None and (type(expected_frames) is not int or expected_frames < 1):
        raise EvidenceError("expected review-media audio frame count must be a positive integer")
    command = [
        ffmpeg, "-nostdin", "-v", "error", "-i", source, "-map", "0:a:0", "-vn",
        "-ac", str(channels), "-ar", str(sample_rate), "-c:a", "pcm_s16le", "-f", "s16le", "-",
    ]
    digest = hashlib.sha256()
    byte_count = 0
    frame_bytes = channels * 2
    maximum_bytes = expected_frames * frame_bytes if expected_frames is not None else None
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=errors,
            pass_fds=pass_fds,
        )
        assert process.stdout is not None
        try:
            while True:
                read_size = 1 << 20
                if maximum_bytes is not None:
                    read_size = min(read_size, maximum_bytes + frame_bytes - byte_count)
                chunk = process.stdout.read(read_size)
                if not chunk:
                    break
                digest.update(chunk)
                byte_count += len(chunk)
                if maximum_bytes is not None and byte_count > maximum_bytes:
                    raise EvidenceError(
                        f"review-media audio has more than the expected {expected_frames} PCM frames"
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
            raise EvidenceError(f"ffmpeg cannot decode review-media audio: {detail}")
    if byte_count < frame_bytes or byte_count % frame_bytes:
        raise EvidenceError("review-media audio does not decode to complete stereo PCM frames")
    if expected_frames is not None and byte_count != maximum_bytes:
        raise EvidenceError(
            f"review-media audio has {byte_count // frame_bytes} PCM frames; "
            f"expected exactly {expected_frames}"
        )
    return {
        "audio_pcm_sha256": digest.hexdigest(),
        "audio_frames": byte_count // frame_bytes,
        "audio_sample_rate": sample_rate,
        "audio_channels": channels,
    }


def media_video_identity(
    path: Path,
    *,
    width: int,
    height: int,
    expected_frames: int | None = None,
    source_fd: int | None = None,
) -> dict[str, Any]:
    """Hash the canonical RGB pixels of every decoded frame in exact order."""
    try:
        identity = decoded_video_identity(
            path,
            width=width,
            height=height,
            expected_frames=expected_frames,
            source_fd=source_fd,
        )
    except MediaIdentityError as exc:
        raise EvidenceError(str(exc)) from exc
    return {
        # Keep the established public field while making its meaning canonical:
        # it is now the ordered decoded RGB byte stream, independent of PTS and
        # container metadata.  The explicit alias is what renderer producers use.
        "video_framehash_sha256": identity["sha256"],
        "decoded_rgb_sha256": identity["sha256"],
        "decoded_video_frames": identity["frames"],
    }


def ffprobe_media(
    path: Path,
    *,
    expected_video_frames: int | None = None,
    expected_audio_frames: int | None = None,
    source_fd: int | None = None,
) -> dict[str, Any]:
    if source_fd is None:
        path = regular_file(path, "A/B review media")
        with _pinned_regular_file(path, "A/B review media") as (file_fd, _info):
            return ffprobe_media(
                path,
                expected_video_frames=expected_video_frames,
                expected_audio_frames=expected_audio_frames,
                source_fd=file_fd,
            )
    try:
        info = os.fstat(source_fd)
    except OSError as exc:
        raise EvidenceError("A/B review media descriptor is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise EvidenceError("A/B review media descriptor is not a regular file")
    if expected_video_frames is not None and (
        type(expected_video_frames) is not int or expected_video_frames < 1
    ):
        raise EvidenceError("expected review-media frame count must be a positive integer")
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise EvidenceError("ffprobe is required to authenticate A/B review media")
    if expected_video_frames is not None:
        maximum = expected_video_frames * PRODUCER_MEDIA_MAX_BYTES_PER_FRAME
        if info.st_size > maximum:
            raise EvidenceError(f"A/B review media exceeds its {maximum}-byte media limit")
    source = f"/dev/fd/{source_fd}"
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise EvidenceError("A/B review media descriptor cannot be rewound") from exc
    done = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,avg_frame_rate,width,height",
            "-of", "json", source,
        ],
        capture_output=True,
        text=True,
        check=False,
        pass_fds=(source_fd,),
    )
    if done.returncode:
        raise EvidenceError(f"ffprobe cannot inspect {path}: {done.stderr.strip()}")
    try:
        probe = json.loads(done.stdout)
        duration = float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"ffprobe returned no finite duration for {path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise EvidenceError(f"ffprobe returned an invalid duration for {path}")
    streams = probe.get("streams")
    if not isinstance(streams, list) or not all(isinstance(row, dict) for row in streams):
        raise EvidenceError("ffprobe returned no exact A/B stream inventory")
    video = [row for row in streams if row.get("codec_type") == "video"]
    audio = [row for row in streams if row.get("codec_type") == "audio"]
    if len(streams) != 2 or len(video) != 1 or len(audio) != 1:
        raise EvidenceError("each A/B review movie must contain exactly one video and one audio stream")
    rate = str(video[0].get("avg_frame_rate", ""))
    try:
        numerator, denominator = rate.split("/", 1)
        fps = float(numerator) / float(denominator)
        width = video[0]["width"]
        height = video[0]["height"]
        if type(width) is not int or width < 1 or type(height) is not int or height < 1:
            raise ValueError("non-integer review movie dimensions")
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise EvidenceError(f"review movie has no exact numeric frame identity: {rate}") from exc
    if not math.isfinite(fps) or fps <= 0 or abs(fps - PRODUCTION_FPS) > 1e-9:
        raise EvidenceError(f"review movie must be a non-empty 30 fps stream, got {fps}")
    if width != PRODUCTION_WIDTH or height != PRODUCTION_HEIGHT:
        raise EvidenceError(
            f"review movie must be {PRODUCTION_WIDTH}x{PRODUCTION_HEIGHT}, got {width}x{height}"
        )
    video_identity = media_video_identity(
        path,
        width=width,
        height=height,
        expected_frames=expected_video_frames,
        source_fd=source_fd,
    )
    video_frames = video_identity["decoded_video_frames"]
    return {
        "sha256": _fd_sha256(source_fd, "A/B review media"),
        "bytes": info.st_size,
        "duration_seconds": duration,
        "fps": PRODUCTION_FPS,
        "width": width,
        "height": height,
        "video_frames": video_frames,
        "video_streams": 1,
        "audio_streams": 1,
        **media_pcm_identity(
            path,
            expected_frames=expected_audio_frames,
            source_fd=source_fd,
        ),
        **video_identity,
    }


def _context_errors(document: dict[str, Any], expected: dict[str, Any], label: str) -> list[str]:
    errors = []
    for field in CONTEXT_FIELDS:
        if document.get(field) != expected.get(field):
            errors.append(f"{label} has stale {field}")
    span = document.get("span")
    if isinstance(span, dict):
        try:
            if abs(float(span["t1"]) - float(span["t0"]) - float(span["duration_seconds"])) > 1e-9:
                errors.append(f"{label} span endpoints do not equal its duration")
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label} has no numeric exact span")
    return errors


def _sample_node_script() -> str:
    """Return the portable probe that calls the same step() as the renderer."""
    return r"""
import fs from 'node:fs';
import crypto from 'node:crypto';
import { fromData } from './engine/corpus.js';
import { validate as validateChoreography } from './engine/choreography.js';
import { step } from './engine/engine.js';
import { fixedPassageTiming, validate as validateProgram } from './engine/program.js';
import { validate as validateScore } from './engine/score.js';

const bytes = (path) => fs.readFileSync(path);
const hash = (value) => crypto.createHash('sha256').update(value).digest('hex');
const scoreBytes = bytes('music/score.json');
const score = validateScore(JSON.parse(scoreBytes));
Object.defineProperty(score, 'fileSha256', {value: hash(scoreBytes), enumerable: false});
const choreography = validateChoreography(JSON.parse(bytes('render/choreography.json')), {score});
const controlTiming = fixedPassageTiming(score.time.duration_seconds);
const program = validateProgram(JSON.parse(bytes('render/program.json')));
const manifestBytes = bytes('corpus/manifest.json');
const manifest = JSON.parse(manifestBytes);
const solvedBytes = manifest.score ? bytes(`corpus/${manifest.score}`) : null;
const solved = solvedBytes ? JSON.parse(solvedBytes) : null;
const corpus = fromData('corpus/', manifest, solved, {
  manifest_sha256: hash(manifestBytes),
  score_sha256: solvedBytes ? hash(solvedBytes) : null,
});
validateChoreography(choreography, {score, corpus});

const boundaries = new Map();
const add = (second, kind, id) => {
  const key = Number(second).toFixed(9);
  if (!boundaries.has(key)) boundaries.set(key, []);
  const rows = boundaries.get(key);
  if (!rows.some((row) => row.kind === kind && row.id === id)) rows.push({kind, id});
};
add(0, 'origin', 'production-origin');
for (const row of score.movements) add(row.start_second, 'movement', row.id);
for (const row of score.phrases) add(row.start_second, 'phrase', row.id);
for (const row of score.cues) add(row.second, 'cue', row.id);

const channel = (state) => Object.fromEntries(
  ['divergence', 'azimuth', 'elevation', 'spread', 'projK', 'turnover']
    .map((name) => [name, Number(state[name].toFixed(9))]),
);
const compact = (result) => ({
  channels: channel(result.state),
  movement: result.state.movement,
  cut: result.state.cut,
  material: result.state.material >>> 0,
  cast_sha256: hash(JSON.stringify(result.cast)),
  cast_count: result.cast.length,
  choreography_pose_sha256: result.pose ? hash(JSON.stringify(result.pose)) : null,
});

const rows = [];
for (const [key, at] of [...boundaries.entries()].sort((a, b) => Number(a[0]) - Number(b[0]))) {
  const second = Number(key);
  const withResult = step(corpus, 20170620, second, program, {
    quantise: 0, stream: 0, score, choreography,
  });
  const controlResult = step(corpus, 20170620, second, program, {
    quantise: 0, stream: 0, score: null, choreography: null, timing: controlTiming,
  });
  const withScore = compact(withResult);
  const control = compact(controlResult);
  const delta = Object.fromEntries(Object.keys(withScore.channels).map((name) => [
    name, Number((withScore.channels[name] - control.channels[name]).toFixed(9)),
  ]));
  const different = withScore.cast_sha256 !== control.cast_sha256
    || withScore.cut !== control.cut
    || withScore.material !== control.material
    || Object.values(delta).some((value) => Math.abs(value) > 1e-9);
  if (!different) throw new Error(`production boundary at ${second}s has no A/B state difference`);
  rows.push({
    sample_id: `sample-${String(rows.length).padStart(3, '0')}`,
    absolute_second: second,
    boundaries: at,
    movement: withResult.state.music.movement.id,
    phrase: withResult.state.music.phrase.id,
    with_score: withScore,
    control,
    score_delta: delta,
    score_delta_max: Math.max(...Object.values(delta).map(Math.abs)),
    observable_state_difference: true,
  });
}
process.stdout.write(JSON.stringify(rows));
"""


def generate_sample_rows(root: Path = ROOT) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", _sample_node_script()],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise EvidenceError(f"production score-to-motion sample probe failed: {result.stderr.strip()}")
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EvidenceError("production score-to-motion sample probe returned invalid JSON") from exc
    if not isinstance(rows, list):
        raise EvidenceError("production score-to-motion sample probe returned no rows")
    return rows


def sample_document(context: dict[str, Any], *, root: Path = ROOT) -> dict[str, Any]:
    document = {
        "schema": SAMPLE_SCHEMA_ID,
        "evidence_scope": "production-input-not-final",
        **context,
        "rows": generate_sample_rows(root),
    }
    error = _schema_error(document, root / SAMPLE_SCHEMA.relative_to(ROOT), "production sample receipt")
    if error:
        raise EvidenceError(error)
    return document


def write_sample_receipt(
    destination: Path,
    context: dict[str, Any],
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise EvidenceError("production sample receipt destination is unsafe")
    document = sample_document(context, root=root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document


def _load_sample_receipt(
    sample_path: Path,
    *,
    expected: dict[str, Any],
    schema_root: Path = ROOT,
    recompute_rows: bool = False,
    snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if snapshot is None:
        try:
            sample = read_json(sample_path, "production score-to-motion sample receipt")
        except EvidenceError as exc:
            return {}, [str(exc)]
    else:
        sample = snapshot
    schema_error = _schema_error(
        sample,
        schema_root / "docs/evidence/score-to-motion-samples-production.schema.json",
        "production sample receipt",
    )
    if schema_error:
        return sample, [schema_error]
    errors = _context_errors(sample, expected, "sample receipt")
    rows = sample.get("rows") or []
    identifiers = [row.get("sample_id") for row in rows if isinstance(row, dict)]
    if identifiers != [f"sample-{index:03d}" for index in range(len(rows))]:
        errors.append("sample receipt identifiers are not complete and ordered")
    times = [row.get("absolute_second") for row in rows if isinstance(row, dict)]
    if len(times) != len(rows) or times != sorted(set(times)):
        errors.append("sample receipt times are not strictly increasing and unique")
    duration = float(expected["span"]["duration_seconds"])
    if any(type(value) not in (int, float) or not 0 <= float(value) < duration for value in times):
        errors.append("sample receipt contains a time outside the exact production span")
    for row in rows:
        if not isinstance(row, dict):
            continue
        delta = row.get("score_delta") or {}
        if set(delta) == set(CHANNELS):
            actual = max(abs(float(delta[name])) for name in CHANNELS)
            if abs(actual - float(row.get("score_delta_max", -1))) > 1e-9:
                errors.append(f"sample receipt {row.get('sample_id')} has a stale score_delta_max")
    if recompute_rows:
        try:
            regenerated = generate_sample_rows(schema_root)
        except EvidenceError as exc:
            errors.append(str(exc))
        else:
            if rows != regenerated:
                errors.append("sample receipt rows do not equal the current renderer probe")
    return sample, errors


def _image_psnr(first: Path | bytes, second: Path | bytes, width: int, height: int) -> float:
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError as exc:
        raise EvidenceError("Pillow is required to authenticate production boundary frames") from exc
    try:
        left_input = BytesIO(first) if isinstance(first, bytes) else first
        right_input = BytesIO(second) if isinstance(second, bytes) else second
        with Image.open(left_input) as left_source, Image.open(right_input) as right_source:
            if left_source.size != (width, height) or right_source.size != (width, height):
                raise EvidenceError("production boundary frame dimensions are stale")
            left = left_source.convert("RGB")
            right = right_source.convert("RGB")
            difference = ImageChops.difference(left, right)
            squared = sum(ImageStat.Stat(difference).sum2)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise EvidenceError(f"cannot decode production boundary frames: {exc}") from exc
    if squared == 0:
        raise EvidenceError("production A/B boundary frames are pixel-identical")
    mse = squared / (width * height * 3)
    return 10 * math.log10((255 * 255) / mse)


def _raw_frame_psnr(source: Path | bytes, payload: bytes, width: int, height: int) -> float:
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError as exc:
        raise EvidenceError("Pillow is required to bind review frames to GPU captures") from exc
    if len(payload) != width * height * 3:
        raise EvidenceError("review anchor does not contain one complete RGB frame")
    try:
        source_input = BytesIO(source) if isinstance(source, bytes) else source
        with Image.open(source_input) as image:
            if image.size != (width, height):
                raise EvidenceError("review anchor source frame dimensions are stale")
            expected = image.convert("RGB")
            actual = Image.frombytes("RGB", (width, height), payload)
            squared = sum(ImageStat.Stat(ImageChops.difference(expected, actual)).sum2)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise EvidenceError(f"cannot decode review anchor source frame: {exc}") from exc
    if squared == 0:
        return 120.0
    mse = squared / (width * height * 3)
    return 10 * math.log10((255 * 255) / mse)


def _read_exact(stream: Any, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def review_frame_anchors(
    media_path: Path,
    *,
    frame_path: Path,
    frame: dict[str, Any],
    mode: str,
    expected_frames: int | None = None,
    source_fd: int | None = None,
) -> list[dict[str, Any]]:
    """Match each movie's 30 fps boundary frame to the Metal capture for its mode."""
    if source_fd is None:
        media_path = regular_file(media_path, "A/B review media")
        with _pinned_regular_file(media_path, "A/B review media") as (file_fd, _info):
            return review_frame_anchors(
                media_path,
                frame_path=frame_path,
                frame=frame,
                mode=mode,
                expected_frames=expected_frames,
                source_fd=file_fd,
            )
    try:
        info = os.fstat(source_fd)
        os.lseek(source_fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise EvidenceError("A/B review media descriptor cannot be read for frame anchors") from exc
    if not stat.S_ISREG(info.st_mode):
        raise EvidenceError("A/B review media descriptor is not a regular file")
    if mode not in {"with_score", "control"}:
        raise EvidenceError(f"unknown A/B review mode: {mode}")
    if expected_frames is None:
        span = frame.get("span") if isinstance(frame.get("span"), dict) else {}
        duration = span.get("duration_seconds")
        expected_frames = production_frame_count(
            duration,
            "frame receipt review-media duration",
        )
    if type(expected_frames) is not int or expected_frames < 1:
        raise EvidenceError("review-media frame span must be a positive integer")
    capture = frame.get("capture")
    rows = frame.get("rows")
    if (
        not isinstance(capture, dict)
        or type(capture.get("width")) is not int
        or capture["width"] != PRODUCTION_WIDTH
        or type(capture.get("height")) is not int
        or capture["height"] != PRODUCTION_HEIGHT
        or not isinstance(rows, list)
        or not rows
        or len(rows) > REVIEW_ANCHOR_MAX_COUNT
        or not all(isinstance(row, dict) for row in rows)
    ):
        raise EvidenceError("frame receipt has no exact bounded anchor shape or row plan")
    width = capture["width"]
    height = capture["height"]
    indexes = [row.get("review_frame_index") for row in rows if isinstance(row, dict)]
    if (
        len(indexes) != len(rows)
        or not indexes
        or any(type(index) is not int or not 0 <= index < expected_frames for index in indexes)
        or indexes != sorted(indexes)
    ):
        raise EvidenceError("frame receipt has no complete review-frame index chain")
    unique_indexes = sorted(set(indexes))
    rows_by_index: dict[int, list[dict[str, Any]]] = {}
    for row, index in zip(rows, indexes, strict=True):
        rows_by_index.setdefault(index, []).append(row)
    expression = "+".join(f"eq(n\\,{index})" for index in unique_indexes)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise EvidenceError("ffmpeg is required to bind review movies to GPU captures")
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                [
                    ffmpeg, "-nostdin", "-v", "error", "-i", f"/dev/fd/{source_fd}",
                    "-map", "0:v:0", "-vf", f"select={expression}", "-fps_mode", "passthrough",
                    "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
                ],
                stdout=subprocess.PIPE,
                stderr=errors,
                pass_fds=(source_fd,),
            )
        except OSError as exc:
            raise EvidenceError(f"cannot launch review boundary-frame decoder: {exc}") from exc
        assert process.stdout is not None
        frame_bytes = width * height * 3
        anchors = []
        try:
            for index in unique_indexes:
                payload = _read_exact(process.stdout, frame_bytes)
                if len(payload) != frame_bytes:
                    raise EvidenceError(f"review movie is missing boundary frame {index}")
                for row in rows_by_index[index]:
                    source_errors, source, source_payload = _artifact_binary_snapshot(
                        frame_path,
                        row.get(mode),
                        f"{row.get('sample_id')} {mode} frame anchor",
                        max_bytes=FRAME_IMAGE_MAX_BYTES,
                    )
                    if source_errors or source is None or source_payload is None:
                        raise EvidenceError(
                            "; ".join(
                                source_errors or ["review anchor source frame is absent"]
                            )
                        )
                    measured = _raw_frame_psnr(source_payload, payload, width, height)
                    if measured < REVIEW_ANCHOR_PSNR_FLOOR:
                        raise EvidenceError(
                            f"{mode} review frame {index} is only {measured:.2f} dB "
                            "from its Metal capture"
                        )
                    anchors.append(
                        {
                            "sample_id": row["sample_id"],
                            "frame_index": index,
                            "review_second": row["review_second"],
                            "source_frame_sha256": hashlib.sha256(source_payload).hexdigest(),
                            "decoded_rgb_sha256": hashlib.sha256(payload).hexdigest(),
                            "psnr_db": round(measured, 9),
                        }
                    )
            if process.stdout.read(1):
                raise EvidenceError("review movie emitted surplus selected boundary frames")
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
        if process.wait():
            errors.seek(0)
            detail = errors.read(64 << 10).decode("utf-8", errors="replace").strip()
            raise EvidenceError(f"ffmpeg cannot extract review boundary frames: {detail}")
    return anchors


def _load_frame_receipt(
    frame_path: Path,
    *,
    sample_path: Path,
    sample: dict[str, Any],
    expected: dict[str, Any],
    schema_root: Path = ROOT,
    snapshot: dict[str, Any] | None = None,
    sample_payload: bytes | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if snapshot is None:
        try:
            frame = read_json(frame_path, "production score-to-motion frame receipt")
        except EvidenceError as exc:
            return {}, [str(exc)]
    else:
        frame = snapshot
    schema_error = _schema_error(
        frame,
        schema_root / "docs/evidence/score-to-motion-frames-production.schema.json",
        "production frame receipt",
    )
    if schema_error:
        return frame, [schema_error]
    errors = _context_errors(frame, expected, "frame receipt")
    if sample_payload is None:
        sample_errors, referenced_sample = _artifact_errors(
            frame_path,
            frame.get("sample_receipt"),
            "frame sample receipt",
        )
    else:
        try:
            referenced_sample = local_artifact(
                frame_path,
                frame.get("sample_receipt"),
                "frame sample receipt",
            )
        except EvidenceError as exc:
            sample_errors = [str(exc)]
            referenced_sample = None
        else:
            sample_reference = frame.get("sample_receipt")
            assert isinstance(sample_reference, dict)
            sample_errors = []
            if sample_reference.get("sha256") != hashlib.sha256(sample_payload).hexdigest():
                sample_errors.append("frame sample receipt digest is stale")
            if sample_reference.get("bytes") != len(sample_payload):
                sample_errors.append("frame sample receipt byte count is stale")
    errors.extend(sample_errors)
    if referenced_sample is not None and referenced_sample != sample_path.resolve(strict=True):
        errors.append("frame receipt names a different production sample receipt")

    sheet_errors, sheet_path, sheet_payload = _artifact_binary_snapshot(
        frame_path,
        frame.get("contact_sheet"),
        "contact sheet",
        max_bytes=FRAME_IMAGE_MAX_BYTES,
    )
    errors.extend(sheet_errors)
    if sheet_path is not None and sheet_payload is not None:
        try:
            from PIL import Image
        except ImportError as exc:
            errors.append(f"contact sheet cannot be decoded: {exc}")
        else:
            try:
                with Image.open(BytesIO(sheet_payload)) as sheet:
                    if sheet.width < 1 or sheet.height < 1:
                        errors.append("contact sheet is empty")
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                errors.append(f"contact sheet cannot be decoded: {exc}")

    capture = frame.get("capture") or {}
    width = int(capture.get("width", 0))
    height = int(capture.get("height", 0))
    expected_capture = {
        "tier": PRODUCTION_TIER,
        "width": PRODUCTION_WIDTH,
        "height": PRODUCTION_HEIGHT,
        "fps": PRODUCTION_FPS,
        **capture_contract_identity(schema_root),
    }
    for field, value in expected_capture.items():
        if capture.get(field) != value:
            errors.append(f"frame capture has stale {field}")
    renderer = str(capture.get("renderer", "")).lower()
    if "apple" not in renderer or "metal" not in renderer:
        errors.append("frame capture is not authenticated as Apple Metal")
    expected_rows = [
        {
            "sample_id": row["sample_id"],
            "absolute_second": row["absolute_second"],
            "review_frame_index": _production_review_position(row["absolute_second"])[0],
            "review_second": _production_review_position(row["absolute_second"])[1],
            "boundaries": row["boundaries"],
            "movement": row["movement"],
            "phrase": row["phrase"],
        }
        for row in sample.get("rows") or []
    ]
    actual_rows = [
        {key: row.get(key) for key in expected_rows[0]} if expected_rows else {}
        for row in frame.get("rows") or []
    ]
    if actual_rows != expected_rows:
        errors.append("boundary frames do not exactly cover the production score-motion samples")

    resolved_rows: list[tuple[Path, Path]] = []
    for row in frame.get("rows") or []:
        sample_id = row.get("sample_id", "unknown")
        with_errors, with_path, with_payload = _artifact_binary_snapshot(
            frame_path,
            row.get("with_score"),
            f"{sample_id} with-score frame",
            max_bytes=FRAME_IMAGE_MAX_BYTES,
        )
        control_errors, control_path, control_payload = _artifact_binary_snapshot(
            frame_path,
            row.get("control"),
            f"{sample_id} control frame",
            max_bytes=FRAME_IMAGE_MAX_BYTES,
        )
        errors.extend(with_errors)
        errors.extend(control_errors)
        if (
            with_path is None
            or with_payload is None
            or control_path is None
            or control_payload is None
        ):
            continue
        resolved_rows.append((with_path, control_path))
        try:
            measured = _image_psnr(with_payload, control_payload, width, height)
        except EvidenceError as exc:
            errors.append(f"{sample_id}: {exc}")
        else:
            if abs(measured - float(row.get("psnr_db", -1))) > 1e-9:
                errors.append(f"{sample_id} PSNR is stale")
            if measured >= 60:
                errors.append(f"{sample_id} has no observable pixel difference")

    determinism = frame.get("determinism") or {}
    first_errors, first, first_payload = _artifact_binary_snapshot(
        frame_path,
        determinism.get("first"),
        "determinism first frame",
        max_bytes=FRAME_IMAGE_MAX_BYTES,
    )
    repeat_errors, repeat, repeat_payload = _artifact_binary_snapshot(
        frame_path,
        determinism.get("repeat"),
        "determinism repeat frame",
        max_bytes=FRAME_IMAGE_MAX_BYTES,
    )
    errors.extend(first_errors)
    errors.extend(repeat_errors)
    if (
        first is not None
        and first_payload is not None
        and repeat is not None
        and repeat_payload is not None
    ):
        if first_payload != repeat_payload:
            errors.append("production boundary-frame renderer is not deterministic")
        if resolved_rows and first != resolved_rows[0][0]:
            errors.append("determinism first frame is not the first with-score boundary frame")
    if sample.get("rows") and determinism.get("absolute_second") != sample["rows"][0]["absolute_second"]:
        errors.append("determinism check does not own the first exact sample time")
    return frame, errors


def _producer_media_for_receipt(receipt_path: Path, label: str) -> Path:
    """Resolve the sibling media named by a renderer ``*.receipt.json`` file."""

    suffix = ".receipt.json"
    if not receipt_path.name.endswith(suffix) or receipt_path.name == suffix:
        raise EvidenceError(f"{label} receipt has no exact media name")
    return _bounded_file(receipt_path.parent, receipt_path.name[: -len(suffix)], label)


def _producer_segment_chain(
    concat_path: Path,
    *,
    concat: dict[str, Any] | None = None,
    maximum_segments: int | None = None,
    snapshot_tokens: list[
        tuple[Path, str, int, tuple[tuple[int, ...], str, int]]
    ] | None = None,
) -> list[tuple[Path, Path, dict[str, Any]]]:
    """Resolve the exact ordered renderer media/receipt chain owned by one concat."""

    concat_receipt_suffix = ".mov.receipt.json"
    if not concat_path.name.endswith(concat_receipt_suffix):
        raise EvidenceError("review-media render concat receipt has no exact .mov sidecar name")
    concat_stem = concat_path.name[: -len(concat_receipt_suffix)]
    if concat is None:
        concat = read_json(
            concat_path,
            "review-media render concat receipt",
            max_bytes=PRODUCER_CONCAT_RECEIPT_MAX_BYTES,
        )
    rows = concat.get("segments")
    if not isinstance(rows, list) or not rows:
        raise EvidenceError("review-media render concat receipt has no segment chain")
    if len(rows) > PRODUCER_SEGMENT_MAX_COUNT:
        raise EvidenceError("review-media render concat receipt exceeds its segment-count limit")
    if maximum_segments is not None and len(rows) > maximum_segments:
        raise EvidenceError(
            "review-media render concat receipt has more segments than decoded frames"
        )
    chain = []
    names: set[str] = set()
    receipt_bytes = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"name", "receipt_sha256"}:
            raise EvidenceError(f"review-media render segment {index} has no exact reference")
        name = row.get("name")
        if not isinstance(name, str) or PurePosixPath(name).name != name:
            raise EvidenceError(f"review-media render segment {index} has an unsafe name")
        if name in names:
            raise EvidenceError(f"review-media render segment {index} reuses media {name}")
        expected_name = f"{concat_stem}-seg-{index:03d}.mov"
        if name != expected_name:
            raise EvidenceError(
                f"review-media render segment {index} is not the canonical {expected_name}"
            )
        names.add(name)
        media_path = _bounded_file(
            concat_path.parent,
            name,
            f"review-media render segment {index} media",
        )
        receipt_path = _bounded_file(
            concat_path.parent,
            f"{name}.receipt.json",
            f"review-media render segment {index} receipt",
        )
        label = f"review-media render segment {index} receipt"
        segment, payload, token = _bounded_json_snapshot_token(
            receipt_path,
            label,
            max_bytes=PRODUCER_SEGMENT_RECEIPT_MAX_BYTES,
        )
        if snapshot_tokens is not None:
            snapshot_tokens.append(
                (receipt_path, label, PRODUCER_SEGMENT_RECEIPT_MAX_BYTES, token)
            )
        receipt_bytes += len(payload)
        if receipt_bytes > PRODUCER_SEGMENT_RECEIPT_TOTAL_MAX_BYTES:
            raise EvidenceError(
                "review-media render segment receipts exceed their aggregate byte limit"
            )
        if row.get("receipt_sha256") != hashlib.sha256(payload).hexdigest():
            raise EvidenceError(f"review-media render segment {index} receipt digest is stale")
        chain.append((media_path, receipt_path, segment))
    return chain


def _producer_stream_info(
    path: Path,
    label: str,
    *,
    source_fd: int | None = None,
) -> dict[str, object]:
    """Enforce the exact video-only ProRes production encoding contract."""

    try:
        stream = video_stream_info(path, source_fd=source_fd)
        if (
            stream.get("width") != PRODUCTION_WIDTH
            or stream.get("height") != PRODUCTION_HEIGHT
            or not finite_receipt_number(stream.get("fps"))
            or abs(float(stream["fps"]) - PRODUCTION_FPS) > 1e-9
            or stream.get("codec_name") != "prores"
            or stream.get("profile") != "HQ"
            or stream.get("pix_fmt") != "yuv422p10le"
            or stream.get("stream_count") != 1
            or stream.get("video_streams") != 1
            or stream.get("audio_streams") != 0
            or stream.get("subtitle_streams") != 0
            or stream.get("data_streams") != 0
        ):
            raise MediaIdentityError(
                f"producer video must be video-only ProRes HQ yuv422p10le at "
                f"{PRODUCTION_WIDTH}x{PRODUCTION_HEIGHT} and {PRODUCTION_FPS} fps"
            )
    except (MediaIdentityError, OSError, TypeError, ValueError) as exc:
        raise EvidenceError(f"{label} media cannot be authenticated: {exc}") from exc
    return stream


def _producer_decoded_video_identity(
    path: Path,
    *,
    expected_frames: int,
    include_fps: bool,
    label: str,
    source_fd: int | None = None,
    aggregate_digest: Any | None = None,
) -> dict[str, object]:
    """Decode one producer movie and enforce its exact production stream shape."""

    _producer_stream_info(path, label, source_fd=source_fd)
    try:
        identity = decoded_video_identity(
            path,
            width=PRODUCTION_WIDTH,
            height=PRODUCTION_HEIGHT,
            expected_frames=expected_frames,
            source_fd=source_fd,
            _aggregate_digest=aggregate_digest,
        )
    except (MediaIdentityError, OSError, TypeError, ValueError) as exc:
        raise EvidenceError(f"{label} media cannot be authenticated: {exc}") from exc
    if include_fps:
        identity["fps"] = PRODUCTION_FPS
    return identity


def _producer_media_identity_errors(
    path: Path,
    receipt: dict[str, Any],
    *,
    expected_frames: int | None,
    include_fps: bool,
    label: str,
) -> tuple[list[str], dict[str, object] | None]:
    """Recompute encoded and decoded identities from actual renderer media."""

    errors: list[str] = []
    try:
        with _pinned_regular_file(path, label) as (file_fd, info):
            if expected_frames is not None:
                maximum = expected_frames * PRODUCER_MEDIA_MAX_BYTES_PER_FRAME
                if info.st_size > maximum:
                    raise EvidenceError(f"{label} exceeds its {maximum}-byte media limit")
            file_bytes = receipt.get("file_bytes")
            if type(file_bytes) is not int or file_bytes < 1:
                errors.append(f"{label} has no exact encoded output byte count")
            elif file_bytes != info.st_size:
                errors.append(f"{label} encoded output byte count is stale")
            file_digest = receipt.get("file_sha256")
            if not isinstance(file_digest, str) or not HEX64.fullmatch(file_digest):
                errors.append(f"{label} has no encoded output digest")
            elif file_digest != _fd_sha256(file_fd, label):
                errors.append(f"{label} encoded output digest is stale")
            if expected_frames is None:
                return errors, None
            decoded = _producer_decoded_video_identity(
                path,
                expected_frames=expected_frames,
                include_fps=include_fps,
                label=label,
                source_fd=file_fd,
            )
    except EvidenceError as exc:
        errors.append(str(exc))
        return errors, None
    if receipt.get("decoded_video") != decoded:
        errors.append(f"{label} decoded video identity is stale")
    return errors, decoded


def _encoded_media_revalidation_errors(
    path: Path,
    receipt: dict[str, Any],
    *,
    expected_frames: int,
    label: str,
) -> list[str]:
    """Re-pin encoded bytes after a long replay so path swaps cannot bridge it."""

    try:
        with _pinned_regular_file(path, label) as (file_fd, info):
            maximum = expected_frames * PRODUCER_MEDIA_MAX_BYTES_PER_FRAME
            if info.st_size > maximum:
                raise EvidenceError(f"{label} exceeds its {maximum}-byte media limit")
            if (
                receipt.get("file_bytes") != info.st_size
                or receipt.get("file_sha256") != _fd_sha256(file_fd, label)
            ):
                raise EvidenceError(f"{label} changed during authentication")
    except EvidenceError as exc:
        return [str(exc)]
    return []


def _producer_segment_media_identity_errors(
    paths: list[Path],
    receipts: list[dict[str, Any]],
    *,
    expected_frames: list[int],
    maximum_frames: int,
    label: str,
) -> tuple[list[str], dict[str, object] | None]:
    """Authenticate every encoded segment and its continuous RGB chain once."""

    errors: list[str] = []
    if (
        not paths
        or len(paths) != len(receipts)
        or len(paths) != len(expected_frames)
        or type(maximum_frames) is not int
        or maximum_frames < 1
        or any(type(value) is not int or value < 1 for value in expected_frames)
        or sum(expected_frames) != maximum_frames
    ):
        return [f"{label} has no exact bounded frame plan"], None
    try:
        chain_digest = hashlib.sha256()
        actual_segments = []
        for index, (path, receipt, frames) in enumerate(
            zip(paths, receipts, expected_frames, strict=True)
        ):
            with _pinned_regular_file(path, f"{label} {index}") as (file_fd, info):
                maximum = frames * PRODUCER_MEDIA_MAX_BYTES_PER_FRAME
                if info.st_size > maximum:
                    raise EvidenceError(
                        f"{label} {index} exceeds its {maximum}-byte media limit"
                    )
                file_bytes = receipt.get("file_bytes")
                if type(file_bytes) is not int or file_bytes < 1:
                    errors.append(f"{label} {index} has no exact encoded output byte count")
                elif file_bytes != info.st_size:
                    errors.append(f"{label} {index} encoded output byte count is stale")
                file_digest = receipt.get("file_sha256")
                if not isinstance(file_digest, str) or not HEX64.fullmatch(file_digest):
                    errors.append(f"{label} {index} has no encoded output digest")
                elif file_digest != _fd_sha256(file_fd, f"{label} {index}"):
                    errors.append(f"{label} {index} encoded output digest is stale")
                actual = _producer_decoded_video_identity(
                    path,
                    expected_frames=frames,
                    include_fps=False,
                    label=f"{label} {index}",
                    source_fd=file_fd,
                    aggregate_digest=chain_digest,
                )
                actual_segments.append(actual)
                if receipt.get("decoded_video") != actual:
                    errors.append(f"{label} {index} decoded video identity is stale")
        chain_decoded = {
            "algorithm": "rgb24-stream-sha256-v1",
            "sha256": chain_digest.hexdigest(),
            "frames": maximum_frames,
            "width": PRODUCTION_WIDTH,
            "height": PRODUCTION_HEIGHT,
        }
    except EvidenceError as exc:
        errors.append(str(exc))
        return errors, None
    return errors, chain_decoded


class _CanonicalReplaySourceSnapshot:
    """Descriptor-pinned repository closure consumed by one canonical replay."""

    def __init__(self, root: Path, *, mode: str, original_inputs: dict[str, Any]):
        self.root = root.absolute()
        self.mode = mode
        self.original_inputs = original_inputs
        self.source_tree_sha256 = ""
        self._directories: list[dict[str, Any]] = []
        self._repository_directories: dict[tuple[str, ...], dict[str, Any]] = {}
        self._files: dict[str, dict[str, Any]] = {}
        self._listings: list[tuple[dict[str, Any], str, tuple[str, ...], str]] = []
        self._closed = False

    @staticmethod
    def _directory_flags() -> int:
        required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
        if any(not hasattr(os, name) for name in required) or not hasattr(os, "pread"):
            raise EvidenceError(
                "canonical replay sources cannot be descriptor-pinned on this platform"
            )
        return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW

    @staticmethod
    def _file_flags() -> int:
        return os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW

    def _record_directory(
        self,
        fd: int,
        *,
        parent_fd: int | None,
        name: str | None,
        label: str,
    ) -> dict[str, Any]:
        try:
            info = os.fstat(fd)
            named = (
                os.stat(os.sep, follow_symlinks=False)
                if parent_fd is None
                else os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            )
        except OSError as exc:
            raise EvidenceError(f"{label} changed during authentication") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or _stat_identity(info) != _stat_identity(named)
        ):
            raise EvidenceError(f"{label} is missing, unsafe, or not a directory")
        record = {
            "fd": fd,
            "parent_fd": parent_fd,
            "name": name,
            "identity": _stat_identity(info),
            "label": label,
        }
        self._directories.append(record)
        return record

    def _open_directory_edge(
        self,
        parent: dict[str, Any],
        name: str,
        *,
        label: str,
    ) -> dict[str, Any]:
        if not name or name in {".", ".."} or os.sep in name:
            raise EvidenceError(f"{label} has an unsafe named directory edge")
        try:
            fd = os.open(name, self._directory_flags(), dir_fd=parent["fd"])
        except OSError as exc:
            raise EvidenceError(f"{label} is missing, unsafe, or cannot be pinned") from exc
        try:
            return self._record_directory(
                fd,
                parent_fd=parent["fd"],
                name=name,
                label=label,
            )
        except BaseException:
            os.close(fd)
            raise

    def _open_repository_root(self) -> None:
        try:
            root_fd = os.open(os.sep, self._directory_flags())
        except OSError as exc:
            raise EvidenceError("canonical replay filesystem root cannot be pinned") from exc
        try:
            current = self._record_directory(
                root_fd,
                parent_fd=None,
                name=None,
                label="canonical replay filesystem root",
            )
        except BaseException:
            os.close(root_fd)
            raise
        current_path = Path(os.sep)
        for part in self.root.parts[1:]:
            current_path /= part
            current = self._open_directory_edge(
                current,
                part,
                label=f"canonical replay source ancestor {current_path}",
            )
        self._repository_directories[()] = current

    def _repository_directory(self, parts: tuple[str, ...]) -> dict[str, Any]:
        current = self._repository_directories[()]
        traversed: list[str] = []
        for part in parts:
            if not part or part in {".", ".."} or os.sep in part:
                raise EvidenceError("canonical replay source has an unsafe directory edge")
            traversed.append(part)
            key = tuple(traversed)
            existing = self._repository_directories.get(key)
            if existing is None:
                existing = self._open_directory_edge(
                    current,
                    part,
                    label=(
                        "canonical replay source directory "
                        + PurePosixPath(*traversed).as_posix()
                    ),
                )
                self._repository_directories[key] = existing
            current = existing
        return current

    def _named_regular_file_exists(self, relative: str, label: str) -> bool:
        pure = _safe_relative(relative, label)
        parent = self._repository_directory(tuple(pure.parts[:-1]))
        try:
            info = os.stat(pure.name, dir_fd=parent["fd"], follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise EvidenceError(f"{label} cannot be inspected safely") from exc
        if not stat.S_ISREG(info.st_mode):
            raise EvidenceError(f"{label} exists but is not a no-follow regular file")
        return True

    def _open_file(self, relative: str, label: str) -> dict[str, Any]:
        pure = _safe_relative(relative, label)
        canonical = pure.as_posix()
        existing = self._files.get(canonical)
        if existing is not None:
            return existing
        parent = self._repository_directory(tuple(pure.parts[:-1]))
        try:
            fd = os.open(pure.name, self._file_flags(), dir_fd=parent["fd"])
        except OSError as exc:
            raise EvidenceError(f"{label} is missing, unsafe, or cannot be pinned") from exc
        try:
            info = os.fstat(fd)
            named = os.stat(pure.name, dir_fd=parent["fd"], follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or not stat.S_ISREG(named.st_mode)
                or _stat_identity(info) != _stat_identity(named)
            ):
                raise EvidenceError(f"{label} is not a no-follow regular file")
            if info.st_size > CANONICAL_REPLAY_SOURCE_FILE_MAX_BYTES:
                raise EvidenceError(f"{label} exceeds the canonical source-file limit")
            digest = _fd_sha256(fd, label)
            current = os.fstat(fd)
            renamed = os.stat(
                pure.name,
                dir_fd=parent["fd"],
                follow_symlinks=False,
            )
            if (
                _stat_identity(current) != _stat_identity(info)
                or _stat_identity(renamed) != _stat_identity(info)
            ):
                raise EvidenceError(f"{label} changed during authentication")
        finally:
            os.close(fd)
        record = {
            "parent_fd": parent["fd"],
            "name": pure.name,
            "identity": _stat_identity(info),
            "digest": digest,
            "bytes": info.st_size,
            "label": label,
            "relative": canonical,
        }
        self._files[canonical] = record
        return record

    def _revalidate_directories(self) -> None:
        for record in self._directories:
            current = os.fstat(record["fd"])
            named = (
                os.stat(os.sep, follow_symlinks=False)
                if record["parent_fd"] is None
                else os.stat(
                    record["name"],
                    dir_fd=record["parent_fd"],
                    follow_symlinks=False,
                )
            )
            if (
                not stat.S_ISDIR(current.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or _stat_identity(current) != record["identity"]
                or _stat_identity(named) != record["identity"]
            ):
                raise EvidenceError(f"{record['label']} changed during authentication")

    def _revalidate_file(self, record: dict[str, Any]) -> None:
        try:
            fd = os.open(
                record["name"],
                self._file_flags(),
                dir_fd=record["parent_fd"],
            )
        except OSError as exc:
            raise EvidenceError(
                f"{record['label']} changed during authentication"
            ) from exc
        try:
            before = os.fstat(fd)
            named_before = os.stat(
                record["name"],
                dir_fd=record["parent_fd"],
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or not stat.S_ISREG(named_before.st_mode)
                or _stat_identity(before) != record["identity"]
                or _stat_identity(named_before) != record["identity"]
                or before.st_size > CANONICAL_REPLAY_SOURCE_FILE_MAX_BYTES
            ):
                raise EvidenceError(
                    f"{record['label']} changed during authentication"
                )
            digest = _fd_sha256(fd, record["label"])
            after = os.fstat(fd)
            named_after = os.stat(
                record["name"],
                dir_fd=record["parent_fd"],
                follow_symlinks=False,
            )
            if (
                _stat_identity(after) != record["identity"]
                or _stat_identity(named_after) != record["identity"]
                or digest != record["digest"]
            ):
                raise EvidenceError(
                    f"{record['label']} changed during authentication"
                )
        finally:
            os.close(fd)

    def _matching_names(
        self,
        directory: dict[str, Any],
        suffix: str,
        label: str,
    ) -> tuple[str, ...]:
        selected = []
        try:
            with os.scandir(directory["fd"]) as entries:
                for count, entry in enumerate(entries, start=1):
                    if count > CANONICAL_REPLAY_SOURCE_DIRECTORY_MAX_ENTRIES:
                        raise EvidenceError(
                            f"{label} exceeds the canonical source-directory entry limit"
                        )
                    if entry.name.endswith(suffix):
                        selected.append(entry.name)
        except EvidenceError:
            raise
        except (OSError, TypeError) as exc:
            raise EvidenceError(f"{label} cannot be enumerated safely") from exc
        return tuple(sorted(selected))

    def _matching_files(
        self,
        directory: str,
        suffix: str,
        label: str,
    ) -> list[str]:
        pure = _safe_relative(directory, label)
        record = self._repository_directory(tuple(pure.parts))
        selected = self._matching_names(record, suffix, label)
        for name in selected:
            if not name or name in {".", ".."} or os.sep in name:
                raise EvidenceError(f"{label} contains an unsafe named edge")
            try:
                info = os.stat(name, dir_fd=record["fd"], follow_symlinks=False)
            except OSError as exc:
                raise EvidenceError(f"{label} changed during enumeration") from exc
            if not stat.S_ISREG(info.st_mode):
                raise EvidenceError(f"{label} contains a non-regular {suffix} entry")
        self._listings.append((record, suffix, selected, label))
        return [(pure / name).as_posix() for name in selected]

    def capture(self) -> None:
        self._open_repository_root()
        fixed = [
            "film.html",
            "render/program.json",
            "render/render.py",
            "render/browser.py",
            "render/media_identity.py",
            "pipeline/corpus_contract.py",
            "corpus/manifest.json",
            "corpus/room.webp",
            "corpus/score-2017.json",
            f"corpus/tier-receipts/{PRODUCTION_TIER}.json",
        ]
        identity_paths = list(fixed)
        if self._named_regular_file_exists(
            "corpus/manifest.local.json",
            "canonical replay local corpus manifest",
        ):
            identity_paths.append("corpus/manifest.local.json")
        identity_paths.extend(
            self._matching_files("engine", ".js", "canonical replay engine sources")
        )

        if self.mode == "with_score":
            score = self.original_inputs.get("music_score")
            choreography = self.original_inputs.get("choreography")
            if not isinstance(score, dict) or not isinstance(choreography, dict):
                raise EvidenceError("canonical replay has no exact score source closure")
            identity_paths.append(
                _safe_relative(
                    score.get("path"),
                    "canonical replay music score",
                ).as_posix()
            )
            identity_paths.append(
                _safe_relative(
                    choreography.get("path"),
                    "canonical replay choreography",
                ).as_posix()
            )
        elif self.mode == "control":
            timing = self.original_inputs.get("timing_score")
            if not isinstance(timing, dict):
                raise EvidenceError("canonical replay has no exact timing-score source closure")
            identity_paths.append(
                _safe_relative(
                    timing.get("path"),
                    "canonical replay timing score",
                ).as_posix()
            )
        else:
            raise EvidenceError("canonical replay has an invalid A/B source mode")

        for kind in ("plates", "mattes"):
            relative = f"corpus/{kind}/{PRODUCTION_TIER}"
            identity_paths.extend(
                self._matching_files(
                    relative,
                    ".webp",
                    f"canonical replay film {kind}",
                )
            )

        identity_records = [
            self._open_file(path, f"canonical replay source {path}")
            for path in identity_paths
        ]
        for path in ("sound/choreography.py", "sound/music_score.py"):
            self._open_file(path, f"canonical replay validator {path}")
        if (
            sum(record["bytes"] for record in self._files.values())
            > CANONICAL_REPLAY_SOURCE_TOTAL_MAX_BYTES
        ):
            raise EvidenceError("canonical replay source graph exceeds its aggregate byte limit")

        digest = hashlib.sha256()
        for record in identity_records:
            digest.update(record["relative"].encode())
            digest.update(bytes.fromhex(record["digest"]))
        self.source_tree_sha256 = digest.hexdigest()

    def revalidate(self) -> None:
        if self._closed:
            raise EvidenceError("canonical replay source snapshot is already closed")
        try:
            self._revalidate_directories()
            for record in self._files.values():
                self._revalidate_file(record)
            for directory, suffix, expected, label in self._listings:
                current = self._matching_names(directory, suffix, label)
                if current != expected:
                    raise EvidenceError(f"{label} changed during authentication")
            # A rename while file bytes were being re-read changes one of these
            # pinned parent/ancestor ctime tokens even if every name was restored.
            self._revalidate_directories()
        except EvidenceError:
            raise
        except OSError as exc:
            raise EvidenceError("canonical replay sources changed during authentication") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for record in reversed(self._directories):
            try:
                os.close(record["fd"])
            except OSError:
                pass


@contextmanager
def _pinned_canonical_replay_source_snapshot(
    root: Path,
    *,
    mode: str,
    original_inputs: dict[str, Any],
):
    """Hold the complete no-follow source graph stable for one replay process."""

    snapshot = _CanonicalReplaySourceSnapshot(
        root,
        mode=mode,
        original_inputs=original_inputs,
    )
    try:
        snapshot.capture()
        snapshot.revalidate()
        yield snapshot
    except BaseException:
        raise
    else:
        snapshot.revalidate()
    finally:
        snapshot.close()


def _canonical_segment_replay(
    *,
    mode: str,
    ordinal: int,
    frames: int,
    segment_frames: int,
    expected: dict[str, Any],
    original_inputs: dict[str, Any],
    root: Path,
) -> tuple[dict[str, Any], dict[str, object]]:
    """Replay one segment and bind its GPU capture to independently decoded pixels.

    A producer receipt can describe an Apple-Metal capture without proving that
    the described raw GPU frames made the referenced, lossy ProRes segment.  The
    production predicate therefore runs the canonical renderer again from the
    validated context, then requires both its raw capture identity and its full
    decoded RGB identity to agree with the retained producer graph.
    """

    if sys.platform != "darwin":
        raise EvidenceError(
            "canonical producer replay requires the authorized macOS Apple-Metal host"
        )
    if mode not in {"with_score", "control"}:
        raise EvidenceError("canonical producer replay has an invalid A/B mode")
    if (
        type(ordinal) is not int
        or ordinal < 0
        or type(frames) is not int
        or frames < 1
        or type(segment_frames) is not int
        or not 1 <= segment_frames <= PRODUCER_SEGMENT_FRAME_MAX
        or frames > segment_frames
        or not isinstance(original_inputs, dict)
    ):
        raise EvidenceError("canonical producer replay has no exact bounded segment plan")

    span = expected.get("span") if isinstance(expected.get("span"), dict) else {}
    seed = span.get("river_seed")
    stream = span.get("stream")
    if (
        type(seed) is not int
        or not 0 <= seed <= 0xFFFFFFFF
        or type(stream) is not int
        or stream < 0
    ):
        raise EvidenceError("canonical producer replay has no exact river identity")
    total_frames = production_frame_count(
        span.get("duration_seconds"),
        "canonical producer replay duration",
    )
    remaining = total_frames - ordinal * segment_frames
    if remaining < 1 or frames != min(segment_frames, remaining):
        raise EvidenceError("canonical producer replay segment does not own its exact frame range")
    if (
        (expected.get("score") or {}).get("path") != "music/score.json"
        or (expected.get("choreography") or {}).get("path")
        != "render/choreography.json"
    ):
        raise EvidenceError("canonical producer replay has non-canonical score inputs")

    require_production_tier(root)
    render_tool = repository_file(root, "render/render.py", "canonical renderer")
    stream_suffix = f"-stream-{stream}" if stream else ""
    control_suffix = "-control" if mode == "control" else ""
    stem = f"passage-{seed}{stream_suffix}{control_suffix}"
    media_name = f"{stem}-seg-{ordinal:03d}.mov"

    with (
        tempfile.TemporaryDirectory(prefix="danse-canonical-replay-") as temporary,
        _pinned_canonical_replay_source_snapshot(
            root,
            mode=mode,
            original_inputs=original_inputs,
        ) as source_snapshot,
    ):
        if source_snapshot.source_tree_sha256 != original_inputs.get(
            "source_tree_sha256"
        ):
            raise EvidenceError(
                f"canonical {mode} segment {ordinal} replay source tree differs "
                "from its producer inputs"
            )
        replay_root = Path(temporary)
        command = [
            sys.executable,
            "-I",
            "-B",
            "-X",
            f"pycache_prefix={os.devnull}",
            str(render_tool),
            "--capture",
            "passage",
            "--start",
            "0",
            "--tier",
            PRODUCTION_TIER,
            "--seed",
            str(seed),
            "--stream",
            str(stream),
            "--codec",
            "prores",
            "--width",
            str(PRODUCTION_WIDTH),
            "--height",
            str(PRODUCTION_HEIGHT),
            "--fps",
            str(PRODUCTION_FPS),
            "--segment",
            str(ordinal),
            "--segment-frames",
            str(segment_frames),
            "--out",
            str(replay_root),
            "--quiet",
        ]
        if mode == "with_score":
            command.extend(
                [
                    "--score",
                    "music/score.json",
                    "--choreography",
                    "render/choreography.json",
                ]
            )
        else:
            command.extend(["--timing-score", "music/score.json"])

        environment = os.environ.copy()
        # The flags above are authoritative even if a same-UID actor controls
        # inherited Python environment variables.  Keep these as belt-and-
        # suspenders for runtimes that also expose the settings to child tools.
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPYCACHEPREFIX"] = os.devnull
        waitid_requirements = (
            "waitid",
            "P_PID",
            "WEXITED",
            "WNOWAIT",
            "WNOHANG",
            "CLD_EXITED",
            "CLD_KILLED",
            "CLD_DUMPED",
        )
        if any(not hasattr(os, name) for name in waitid_requirements):
            raise EvidenceError(
                "canonical replay cannot hold its process identity through exit on this host"
            )
        waitid_flags = os.WEXITED | os.WNOWAIT | os.WNOHANG
        process: subprocess.Popen[bytes] | None = None

        def observed_replay_returncode(result: object) -> int:
            code = getattr(result, "si_code", None)
            status = getattr(result, "si_status", None)
            if type(status) is not int:
                raise EvidenceError("canonical replay returned an invalid wait identity")
            if code == os.CLD_EXITED:
                return status
            if code in {os.CLD_KILLED, os.CLD_DUMPED}:
                return -status
            raise EvidenceError("canonical replay returned an unexpected wait state")

        def observe_replay_exit() -> int | None:
            assert process is not None
            deadline = time.monotonic() + CANONICAL_REPLAY_TIMEOUT_SECONDS
            while True:
                try:
                    result = os.waitid(os.P_PID, process.pid, waitid_flags)
                except InterruptedError:
                    continue
                except (ChildProcessError, OSError) as exc:
                    raise EvidenceError(
                        f"cannot observe canonical {mode} segment {ordinal} replay exit: {exc}"
                    ) from exc
                if result is not None and getattr(result, "si_pid", 0) == process.pid:
                    return observed_replay_returncode(result)
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.05)

        def terminate_replay_group() -> int:
            if process is None:
                raise EvidenceError("canonical replay process was not launched")
            group_gone = False
            kill_failure: EvidenceError | None = None
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                group_gone = True
            except OSError as exc:
                kill_failure = EvidenceError(
                    f"cannot terminate canonical {mode} segment {ordinal} replay workers: {exc}"
                )
            try:
                returncode = process.wait(timeout=CANONICAL_REPLAY_CLEANUP_SECONDS)
            except subprocess.TimeoutExpired as exc:
                raise EvidenceError(
                    f"canonical {mode} segment {ordinal} replay workers did not terminate"
                ) from exc
            if type(returncode) is not int:
                raise EvidenceError("canonical replay returned no exact process status")
            if kill_failure is not None:
                raise kill_failure
            if group_gone:
                return returncode
            deadline = time.monotonic() + CANONICAL_REPLAY_CLEANUP_SECONDS
            while True:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                except OSError as exc:
                    raise EvidenceError(
                        f"cannot verify canonical {mode} segment {ordinal} replay "
                        f"worker termination: {exc}"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise EvidenceError(
                        f"canonical {mode} segment {ordinal} replay process group "
                        "did not terminate"
                    )
                time.sleep(0.05)
            return returncode

        with open(os.devnull, "wb") as diagnostic:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=diagnostic,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                raise EvidenceError(
                    f"cannot launch canonical {mode} segment {ordinal} replay: {exc}"
                ) from exc
            try:
                try:
                    observed_returncode = observe_replay_exit()
                except BaseException:
                    # waitid leaves the leader unreaped, so its PID/PGID cannot
                    # be reused while this cleanup signal is sent.
                    terminate_replay_group()
                    raise
                if observed_returncode is None:
                    terminate_replay_group()
                    raise EvidenceError(
                        f"canonical {mode} segment {ordinal} replay timed out"
                    )
                # waitid+WNOWAIT keeps the exited leader as a zombie and reserves
                # its PID/PGID.  Kill possible descendants first, then reap it.
                returncode = terminate_replay_group()
                if returncode != observed_returncode:
                    raise EvidenceError(
                        f"canonical {mode} segment {ordinal} replay exit identity changed"
                    )
            finally:
                if process is not None:
                    # The renderer has exited (or was killed) before any receipt
                    # can be trusted.  Recheck every held source descriptor and
                    # named edge now so swap-and-restore cannot bridge Popen.
                    source_snapshot.revalidate()
            if returncode:
                raise EvidenceError(
                    f"canonical {mode} segment {ordinal} replay failed"
                )

        replay_media = _bounded_file(
            replay_root,
            media_name,
            f"canonical {mode} segment {ordinal} replay media",
        )
        replay_receipt_path = _bounded_file(
            replay_root,
            f"{media_name}.receipt.json",
            f"canonical {mode} segment {ordinal} replay receipt",
        )
        replay_receipt, _payload = _bounded_json_snapshot(
            replay_receipt_path,
            f"canonical {mode} segment {ordinal} replay receipt",
            max_bytes=PRODUCER_SEGMENT_RECEIPT_MAX_BYTES,
        )
        if (
            replay_receipt.get("schema") != "danse.render.segment.v1"
            or replay_receipt.get("segment") != ordinal
            or replay_receipt.get("frames") != frames
            or replay_receipt.get("inputs") != original_inputs
        ):
            raise EvidenceError(
                f"canonical {mode} segment {ordinal} replay did not reproduce its exact inputs"
            )
        capture = replay_receipt.get("capture")
        if not isinstance(capture, dict) or set(capture) != {
            "renderer",
            "raw_rgba_sha256",
            "missing",
            "signature",
            "passage",
        }:
            raise EvidenceError(
                f"canonical {mode} segment {ordinal} replay has no exact GPU capture"
            )
        renderer = str(capture.get("renderer", "")).lower()
        if (
            "apple" not in renderer
            or "metal" not in renderer
            or type(capture.get("missing")) is not int
            or capture.get("missing") != 0
            or not isinstance(capture.get("raw_rgba_sha256"), str)
            or not HEX64.fullmatch(capture["raw_rgba_sha256"])
            or not isinstance(capture.get("signature"), str)
            or not capture["signature"]
            or not isinstance(capture.get("passage"), dict)
        ):
            raise EvidenceError(
                f"canonical {mode} segment {ordinal} replay is not an exact Apple-Metal capture"
            )
        replay_errors, replay_decoded = _producer_media_identity_errors(
            replay_media,
            replay_receipt,
            expected_frames=frames,
            include_fps=False,
            label=f"canonical {mode} segment {ordinal} replay",
        )
        if replay_errors or replay_decoded is None:
            detail = "; ".join(replay_errors) or "decoded identity is absent"
            raise EvidenceError(
                f"canonical {mode} segment {ordinal} replay cannot be authenticated: {detail}"
            )
        # Receipt/media inspection is also attacker-controlled work.  Keep the
        # same source closure pinned until immediately before acceptance.
        source_snapshot.revalidate()
        return dict(capture), replay_decoded


def _producer_receipt_errors(
    receipt_path: Path,
    reference: object,
    *,
    mode: str,
    expected: dict[str, Any],
    review_identity: dict[str, Any],
    root: Path,
) -> tuple[list[str], Path | None]:
    """Bind every review frame to one exact canonical renderer receipt chain."""
    concat_label = f"{mode} review-media producer receipt"
    errors, concat_path, concat, concat_token = _artifact_json_snapshot_token(
        receipt_path,
        reference,
        concat_label,
        max_bytes=PRODUCER_CONCAT_RECEIPT_MAX_BYTES,
    )
    if concat_path is None or concat is None or concat_token is None:
        return errors, None
    if concat.get("schema") != "danse.render.concat.v1":
        errors.append(f"{mode} review-media producer has the wrong schema")
    if concat.get("codec") != "prores":
        errors.append(f"{mode} review-media producer is not the lossless-evidence codec")
    decoded = concat.get("decoded_video") if isinstance(concat.get("decoded_video"), dict) else {}
    span = expected.get("span") if isinstance(expected.get("span"), dict) else {}
    try:
        expected_frames = production_frame_count(
            span.get("duration_seconds"),
            "expected production duration",
        )
    except EvidenceError as exc:
        errors.append(str(exc))
        return errors, concat_path
    expected_decoded = {
        "algorithm": "rgb24-stream-sha256-v1",
        "sha256": review_identity.get("decoded_rgb_sha256"),
        "frames": review_identity.get("decoded_video_frames"),
        "width": PRODUCTION_WIDTH,
        "height": PRODUCTION_HEIGHT,
        "fps": PRODUCTION_FPS,
    }
    if decoded != expected_decoded:
        errors.append(f"{mode} review media full decoded video differs from its canonical producer")
    if decoded.get("frames") != expected_frames:
        errors.append(f"{mode} review-media producer does not cover the exact frame span")

    segment_tokens: list[
        tuple[Path, str, int, tuple[tuple[int, ...], str, int]]
    ] = []
    try:
        segment_chain = _producer_segment_chain(
            concat_path,
            concat=concat,
            maximum_segments=expected_frames,
            snapshot_tokens=segment_tokens,
        )
    except EvidenceError as exc:
        errors.append(str(exc))
        return errors, concat_path

    concat_media = None
    try:
        concat_media = _producer_media_for_receipt(
            concat_path,
            f"{mode} render concat media",
        )
    except EvidenceError as exc:
        errors.append(str(exc))
        concat_decoded = None
    else:
        concat_media_errors, concat_decoded = _producer_media_identity_errors(
            concat_media,
            concat,
            expected_frames=expected_frames,
            include_fps=True,
            label=f"{mode} render concat",
        )
        errors.extend(concat_media_errors)
    segment_frames = None
    total_frames = 0
    segment_media_plan: list[Path] = []
    segment_receipt_plan: list[dict[str, Any]] = []
    segment_frame_plan: list[int] = []
    frame_plan_valid = True
    control_source = None
    if mode == "control":
        try:
            control_source = renderer_source_tree(PRODUCTION_TIER, root, with_score=False)
        except EvidenceError as exc:
            errors.append(str(exc))
    for ordinal, (segment_media, _segment_path, segment) in enumerate(segment_chain):
        if segment.get("schema") != "danse.render.segment.v1":
            errors.append(f"{mode} render segment {ordinal} has the wrong schema")
            frame_plan_valid = False
        if type(segment.get("segment")) is not int or segment.get("segment") != ordinal:
            errors.append(f"{mode} render segment chain is not ordered and contiguous at {ordinal}")
            frame_plan_valid = False
        frames = segment.get("frames")
        if type(frames) is not int or frames < 1:
            errors.append(f"{mode} render segment {ordinal} has no exact frame count")
            frame_plan_valid = False
            continue
        segment_media_plan.append(segment_media)
        segment_receipt_plan.append(segment)
        segment_frame_plan.append(frames)
        inputs = segment.get("inputs") if isinstance(segment.get("inputs"), dict) else {}
        current_segment_frames = inputs.get("segment_frames")
        if type(current_segment_frames) is not int or current_segment_frames < 1:
            errors.append(f"{mode} render segment {ordinal} has no segment-span identity")
            frame_plan_valid = False
        elif segment_frames is None:
            segment_frames = current_segment_frames
        elif current_segment_frames != segment_frames:
            errors.append(f"{mode} render segments do not share one segment span")
            frame_plan_valid = False
        if segment_frames is not None:
            remaining = expected_frames - ordinal * segment_frames
            wanted = min(segment_frames, max(remaining, 0))
            if frames != wanted:
                errors.append(f"{mode} render segment {ordinal} does not own its exact frame range")
                frame_plan_valid = False
        total_frames += frames
        common = {
            "window": "passage",
            "start": 0,
            "tier": PRODUCTION_TIER,
            "seed": expected["span"]["river_seed"],
            "stream": expected["span"]["stream"],
            "codec": "prores",
            "width": PRODUCTION_WIDTH,
            "height": PRODUCTION_HEIGHT,
            "fps": PRODUCTION_FPS,
        }
        for field, value in common.items():
            actual = inputs.get(field)
            wrong_type = (
                (field in {"start", "fps"} and not finite_receipt_number(actual))
                or (
                    field in {"seed", "stream", "width", "height"}
                    and type(actual) is not int
                )
            )
            if wrong_type or actual != value:
                errors.append(f"{mode} render segment {ordinal} has stale {field}")
        if mode == "with_score":
            if any(field in inputs for field in ("duration_seconds", "timing_score", "passage_timing")):
                errors.append("with_score render segment timing is not owned solely by its score")
            if inputs.get("source_tree_sha256") != expected["source_tree_sha256"]:
                errors.append(f"{mode} render segment {ordinal} has a stale source tree")
            score = inputs.get("music_score") if isinstance(inputs.get("music_score"), dict) else {}
            for field in ("path", "file_sha256", "contract_sha256"):
                if score.get(field) != expected["score"].get(field):
                    errors.append(f"with_score render segment {ordinal} has stale score {field}")
            choreography = (
                inputs.get("choreography") if isinstance(inputs.get("choreography"), dict) else {}
            )
            if choreography != expected["choreography"]:
                errors.append(f"with_score render segment {ordinal} has stale choreography")
        else:
            if "music_score" in inputs or "choreography" in inputs:
                errors.append(f"control render segment {ordinal} is not score-free")
            expected_timing_score = {
                "path": expected["score"]["path"],
                "file_sha256": expected["score"]["file_sha256"],
                "contract_sha256": expected["score"]["contract_sha256"],
                "passage_mapping": "native-tempo",
                "duration_seconds": expected["span"]["duration_seconds"],
            }
            if inputs.get("timing_score") != expected_timing_score:
                errors.append(f"control render segment {ordinal} has stale timing-score identity")
            if inputs.get("passage_timing") != {
                "mode": "fixed-passage",
                "seconds": expected["span"]["duration_seconds"],
            }:
                errors.append(
                    f"control render segment {ordinal} does not bind the selected production span"
                )
            if "duration_seconds" in inputs:
                errors.append(f"control render segment {ordinal} uses the obsolete length-only override")
            if control_source is not None and inputs.get("source_tree_sha256") != control_source:
                errors.append(f"control render segment {ordinal} has a stale no-score source tree")
        capture = segment.get("capture") if isinstance(segment.get("capture"), dict) else {}
        renderer = str(capture.get("renderer", "")).lower()
        if "apple" not in renderer or "metal" not in renderer:
            errors.append(f"{mode} render segment {ordinal} is not authenticated as Apple Metal")
        if type(capture.get("missing")) is not int or capture.get("missing") != 0:
            errors.append(f"{mode} render segment {ordinal} has missing photographic plates")
        if not isinstance(capture.get("raw_rgba_sha256"), str) or not HEX64.fullmatch(
            capture.get("raw_rgba_sha256", "")
        ):
            errors.append(f"{mode} render segment {ordinal} has no GPU-frame sequence digest")
        if not isinstance(capture.get("signature"), str) or not capture.get("signature"):
            errors.append(f"{mode} render segment {ordinal} has no renderer signature")
        passage = capture.get("passage") if isinstance(capture.get("passage"), dict) else {}
        if (
            set(passage) != {"index", "seed", "t0", "seconds"}
            or type(passage.get("index")) is not int
            or passage.get("index") != expected["span"]["passage"]
            or type(passage.get("seed")) is not int
            or passage.get("seed") != PRODUCTION_PASSAGE_SEED
            or not finite_receipt_number(passage.get("t0"))
            or passage.get("t0") != expected["span"]["t0"]
            or not finite_receipt_number(passage.get("seconds"))
            or passage.get("seconds") <= 0
            or passage.get("seconds") != expected["span"]["duration_seconds"]
        ):
            errors.append(f"{mode} render segment {ordinal} left the selected production passage")
        segment_decoded = (
            segment.get("decoded_video") if isinstance(segment.get("decoded_video"), dict) else {}
        )
        if (
            segment_decoded.get("algorithm") != "rgb24-stream-sha256-v1"
            or type(segment_decoded.get("frames")) is not int
            or segment_decoded.get("frames") != frames
            or type(segment_decoded.get("width")) is not int
            or segment_decoded.get("width") != PRODUCTION_WIDTH
            or type(segment_decoded.get("height")) is not int
            or segment_decoded.get("height") != PRODUCTION_HEIGHT
            or not isinstance(segment_decoded.get("sha256"), str)
            or not HEX64.fullmatch(segment_decoded.get("sha256", ""))
        ):
            errors.append(f"{mode} render segment {ordinal} has no full decoded-frame identity")
    if total_frames != expected_frames:
        errors.append(f"{mode} render producer segment chain has {total_frames}, expected {expected_frames} frames")
        frame_plan_valid = False
    if frame_plan_valid and len(segment_media_plan) == len(segment_chain):
        media_errors, chain_decoded = _producer_segment_media_identity_errors(
            segment_media_plan,
            segment_receipt_plan,
            expected_frames=segment_frame_plan,
            maximum_frames=expected_frames,
            label=f"{mode} render segment",
        )
        errors.extend(media_errors)
        if chain_decoded is not None and concat_decoded is not None:
            actual_concat_frames = {
                key: value for key, value in concat_decoded.items() if key != "fps"
            }
            if chain_decoded != actual_concat_frames:
                errors.append(
                    f"{mode} render segment decoded chain differs from its actual concat"
                )
    if not errors and segment_frames is not None:
        for ordinal, segment in enumerate(segment_receipt_plan):
            inputs = segment["inputs"]
            capture = segment["capture"]
            decoded = segment["decoded_video"]
            try:
                replay_capture, replay_decoded = _canonical_segment_replay(
                    mode=mode,
                    ordinal=ordinal,
                    frames=segment_frame_plan[ordinal],
                    segment_frames=segment_frames,
                    expected=expected,
                    original_inputs=inputs,
                    root=root,
                )
            except EvidenceError as exc:
                errors.append(str(exc))
                break
            if capture.get("raw_rgba_sha256") != replay_capture.get(
                "raw_rgba_sha256"
            ):
                errors.append(
                    f"{mode} render segment {ordinal} GPU-frame sequence digest "
                    "differs from canonical replay"
                )
            for field in ("renderer", "missing", "signature", "passage"):
                if capture.get(field) != replay_capture.get(field):
                    errors.append(
                        f"{mode} render segment {ordinal} capture {field} "
                        "differs from canonical replay"
                    )
            if decoded != replay_decoded:
                errors.append(
                    f"{mode} render segment {ordinal} decoded pixels differ from "
                    "canonical Apple-Metal replay"
                )
    if concat_media is not None:
        errors.extend(
            _encoded_media_revalidation_errors(
                concat_media,
                concat,
                expected_frames=expected_frames,
                label=f"{mode} render concat",
            )
        )
    for ordinal, (segment_media, segment, frames) in enumerate(
        zip(
            segment_media_plan,
            segment_receipt_plan,
            segment_frame_plan,
            strict=True,
        )
    ):
        errors.extend(
            _encoded_media_revalidation_errors(
                segment_media,
                segment,
                expected_frames=frames,
                label=f"{mode} render segment {ordinal}",
            )
        )
    try:
        _revalidate_json_snapshot_token(
            concat_path,
            concat_label,
            max_bytes=PRODUCER_CONCAT_RECEIPT_MAX_BYTES,
            token=concat_token,
        )
        for segment_path, label, maximum, token in segment_tokens:
            _revalidate_json_snapshot_token(
                segment_path,
                label,
                max_bytes=maximum,
                token=token,
            )
    except EvidenceError as exc:
        errors.append(str(exc))
    return errors, concat_path


def _media_errors(
    receipt_path: Path,
    receipt: dict[str, Any],
    *,
    expected: dict[str, Any],
    frame_path: Path | None,
    frame: dict[str, Any],
    root: Path,
    review_probe_cache: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    errors = []
    span = expected.get("span") if isinstance(expected.get("span"), dict) else {}
    try:
        expected_video_frames = production_frame_count(
            span.get("duration_seconds"),
            "expected production duration",
        )
    except EvidenceError as exc:
        return [str(exc)]
    duration = float(span["duration_seconds"])
    master = expected["audio_master"]
    identities: dict[str, tuple[dict[str, Any], Path]] = {}
    for name in ("with_score", "control"):
        reference = (receipt.get("review_media") or {}).get(name)
        cached_measurement = (review_probe_cache or {}).get(name)
        cached_probe = (
            cached_measurement.get("probe")
            if isinstance(cached_measurement, dict)
            else None
        )
        cached_anchors = (
            cached_measurement.get("anchors")
            if isinstance(cached_measurement, dict)
            else None
        )
        if isinstance(cached_probe, dict) and isinstance(cached_anchors, list):
            artifact_errors, path = _cached_review_artifact_errors(
                receipt_path,
                reference,
                f"{name} review media",
                expected_frames=expected_video_frames,
            )
        else:
            try:
                path = local_artifact(receipt_path, reference, f"{name} review media")
            except EvidenceError as exc:
                errors.append(str(exc))
                continue
            artifact_errors = []
        errors.extend(artifact_errors)
        if path is None or not isinstance(reference, dict):
            continue
        measured_anchors: list[dict[str, Any]] | None = None
        if isinstance(cached_probe, dict) and isinstance(cached_anchors, list):
            probed = cached_probe
            measured_anchors = cached_anchors
        else:
            try:
                with _pinned_regular_file(path, f"{name} review media") as (file_fd, _info):
                    probed = ffprobe_media(
                        path,
                        expected_video_frames=expected_video_frames,
                        expected_audio_frames=master["frames"],
                        source_fd=file_fd,
                    )
                    if frame_path is not None and frame:
                        try:
                            measured_anchors = review_frame_anchors(
                                path,
                            frame_path=frame_path,
                            frame=frame,
                            mode=name,
                            expected_frames=expected_video_frames,
                            source_fd=file_fd,
                            )
                        except EvidenceError as exc:
                            errors.append(str(exc))
            except EvidenceError as exc:
                errors.append(str(exc))
                continue
            if reference.get("sha256") != probed.get("sha256"):
                errors.append(f"{name} review media digest is stale")
            if reference.get("bytes") != probed.get("bytes"):
                errors.append(f"{name} review media byte count is stale")
        for field in (
            "sha256", "bytes", "duration_seconds", "fps", "width", "height", "video_frames",
            "video_streams", "audio_streams", "audio_pcm_sha256", "audio_frames",
            "audio_sample_rate", "audio_channels", "video_framehash_sha256",
            "decoded_rgb_sha256", "decoded_video_frames",
        ):
            if reference.get(field) != probed.get(field):
                errors.append(f"{name} review media has stale {field}")
        if probed["video_frames"] != expected_video_frames:
            errors.append(f"{name} review media does not contain the exact rounded production frame span")
        if abs(float(probed["duration_seconds"]) - duration) > 0.5 / 30:
            errors.append(f"{name} review media duration differs from the exact score span")
        if probed["audio_pcm_sha256"] != master["pcm_sha256"]:
            errors.append(f"{name} review media audio differs from the competition master PCM")
        if probed["audio_frames"] != master["frames"]:
            errors.append(f"{name} review media audio frame count differs from the competition master")
        if probed["decoded_video_frames"] != expected_video_frames:
            errors.append(f"{name} decoded review video has a stale frame count")
        if reference.get("mode") != name:
            errors.append(f"{name} review media has the wrong A/B mode")
        if frame_path is None or not frame:
            errors.append(f"{name} review media has no authenticated Metal frame receipt")
        elif measured_anchors is not None and reference.get("anchors") != measured_anchors:
            errors.append(f"{name} review-media frame anchors are stale")
        producer_errors, _ = _producer_receipt_errors(
            receipt_path,
            reference.get("producer_receipt"),
            mode=name,
            expected=expected,
            review_identity=probed,
            root=root,
        )
        errors.extend(producer_errors)
        errors.extend(
            _encoded_media_revalidation_errors(
                path,
                {
                    "file_bytes": reference.get("bytes"),
                    "file_sha256": reference.get("sha256"),
                },
                expected_frames=expected_video_frames,
                label=f"{name} review media",
            )
        )
        identities[name] = (probed, path)
    if set(identities) == {"with_score", "control"}:
        with_probe, with_path = identities["with_score"]
        control_probe, control_path = identities["control"]
        if with_path == control_path or with_probe["sha256"] == control_probe["sha256"]:
            errors.append("with-score and control review movies are byte-identical")
        if with_probe["video_framehash_sha256"] == control_probe["video_framehash_sha256"]:
            errors.append("with-score and control review movies have identical decoded video")
    return errors


def _historical_fixture(document: dict[str, Any]) -> bool:
    return (
        document.get("schema") in FIXTURE_SCHEMAS
        or document.get("evidence_scope") == "historical-fixture-only"
        or (
            "contract" in document
            and "passage" in document
            and "repository_head" not in document
        )
    )


def production_receipt_errors(
    receipt_path: Path,
    *,
    expected: dict[str, Any] | None = None,
    root: Path = ROOT,
    require_clean: bool = True,
    recompute_samples: bool = True,
    _review_probe_cache: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Return every reason a receipt cannot satisfy the production machine gate."""
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return [f"production A/B receipt is absent: {receipt_path}"]
    try:
        with _pinned_json_snapshot(
            receipt_path,
            "production A/B receipt",
            max_bytes=PRODUCTION_RECEIPT_MAX_BYTES,
        ) as (receipt, _receipt_payload):
            if _historical_fixture(receipt):
                return ["historical fixture evidence cannot satisfy the production A/B gate"]
            schema_error = _schema_error(
                receipt,
                root / "docs/evidence/score-to-motion-production.schema.json",
                "production A/B receipt",
            )
            if schema_error:
                return [schema_error]

            with _pinned_artifact_json_snapshot(
                receipt_path,
                receipt.get("frame_receipt"),
                "frame receipt",
                max_bytes=FRAME_RECEIPT_MAX_BYTES,
            ) as (frame_ref_errors, frame_path, frame_document, _frame_payload), \
                 _pinned_artifact_json_snapshot(
                     receipt_path,
                     receipt.get("sample_receipt"),
                     "sample receipt",
                     max_bytes=SAMPLE_RECEIPT_MAX_BYTES,
                 ) as (sample_ref_errors, sample_path, sample_document, sample_payload):
                errors: list[str] = [*frame_ref_errors, *sample_ref_errors]
                if expected is None:
                    tier = PRODUCTION_TIER
                    if isinstance(frame_document, dict):
                        capture = frame_document.get("capture")
                        if isinstance(capture, dict):
                            tier = capture.get("tier", tier)
                    try:
                        audio_path = repository_file(
                            root,
                            receipt["audio_render_receipt"]["path"],
                            "audio-render receipt",
                        )
                        expected = current_context(
                            audio_path,
                            tier=tier,
                            root=root,
                            require_clean=require_clean,
                        )
                    except (EvidenceError, KeyError, TypeError) as exc:
                        return [*errors, str(exc)]
                expected_span = (
                    expected.get("span") if isinstance(expected.get("span"), dict) else {}
                )
                try:
                    production_frame_count(
                        expected_span.get("duration_seconds"),
                        "expected production duration",
                    )
                except EvidenceError as exc:
                    return [*errors, str(exc)]
                errors.extend(_context_errors(receipt, expected, "production A/B receipt"))

                sample: dict[str, Any] = {}
                if sample_path is not None and sample_document is not None:
                    sample, sample_errors = _load_sample_receipt(
                        sample_path,
                        expected=expected,
                        schema_root=root,
                        recompute_rows=recompute_samples,
                        snapshot=sample_document,
                    )
                    errors.extend(sample_errors)
                frame: dict[str, Any] = {}
                if (
                    frame_path is not None
                    and frame_document is not None
                    and sample_path is not None
                    and sample_payload is not None
                    and sample
                ):
                    loaded_frame, frame_errors = _load_frame_receipt(
                        frame_path,
                        sample_path=sample_path,
                        sample=sample,
                        expected=expected,
                        schema_root=root,
                        snapshot=frame_document,
                        sample_payload=sample_payload,
                    )
                    errors.extend(frame_errors)
                    if not frame_errors:
                        frame = loaded_frame
                errors.extend(
                    _media_errors(
                        receipt_path,
                        receipt,
                        expected=expected,
                        frame_path=frame_path,
                        frame=frame,
                        root=root,
                        review_probe_cache=_review_probe_cache,
                    )
                )
                if receipt.get("human_review") != {"status": "not-attested"}:
                    errors.append("machine evidence must not claim human artistic acceptance")
                return errors
    except EvidenceError as exc:
        return [str(exc)]


def evidence_artifact_paths(receipt_path: Path) -> list[Path]:
    """Resolve exactly the files owned by one final receipt; never copy strays."""
    receipt = read_json(receipt_path, "production A/B receipt")
    paths = {regular_file(receipt_path, "production A/B receipt")}
    sample_path = local_artifact(receipt_path, receipt.get("sample_receipt"), "sample receipt")
    frame_path = local_artifact(receipt_path, receipt.get("frame_receipt"), "frame receipt")
    paths.update({sample_path, frame_path})
    frame = read_json(frame_path, "production frame receipt")
    for label, reference in (
        ("frame sample receipt", frame.get("sample_receipt")),
        ("contact sheet", frame.get("contact_sheet")),
        ("determinism first frame", (frame.get("determinism") or {}).get("first")),
        ("determinism repeat frame", (frame.get("determinism") or {}).get("repeat")),
    ):
        paths.add(local_artifact(frame_path, reference, label))
    for row in frame.get("rows") or []:
        sample_id = row.get("sample_id", "unknown")
        paths.add(local_artifact(frame_path, row.get("with_score"), f"{sample_id} with-score frame"))
        paths.add(local_artifact(frame_path, row.get("control"), f"{sample_id} control frame"))
    for name in ("with_score", "control"):
        media = receipt["review_media"][name]
        paths.add(local_artifact(receipt_path, media, f"{name} review media"))
        producer_errors, producer, concat = _artifact_json_snapshot(
            receipt_path,
            media.get("producer_receipt"),
            f"{name} review-media producer receipt",
            max_bytes=PRODUCER_CONCAT_RECEIPT_MAX_BYTES,
        )
        if producer_errors:
            raise EvidenceError("; ".join(producer_errors))
        if producer is None or concat is None:
            raise EvidenceError(f"{name} review-media producer receipt is absent")
        paths.add(producer)
        paths.add(_producer_media_for_receipt(producer, f"{name} render concat media"))
        decoded = concat.get("decoded_video") if isinstance(concat.get("decoded_video"), dict) else {}
        maximum_segments = decoded.get("frames")
        if type(maximum_segments) is not int or maximum_segments < 1:
            raise EvidenceError(f"{name} review-media producer has no exact decoded frame count")
        for segment_media, segment_receipt, _ in _producer_segment_chain(
            producer,
            concat=concat,
            maximum_segments=maximum_segments,
        ):
            paths.update({segment_media, segment_receipt})
    return sorted(paths)


def _manifest_items(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["name"]: item
        for item in manifest.get("items") or []
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _package_renderer_source(
    package_root: Path,
    manifest: dict[str, Any],
) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    reference = manifest.get("production")
    if not isinstance(reference, dict):
        return None, ["package manifest has no production receipt"]
    try:
        production_path = _bounded_file(package_root, reference.get("path"), "package production receipt")
        production = read_json(production_path, "package production receipt")
    except EvidenceError as exc:
        return None, [str(exc)]
    if reference.get("sha256") != sha256(production_path):
        errors.append("package production receipt digest is stale")
    if production.get("repository_head") != manifest.get("repository_head"):
        errors.append("package production receipt names a different repository head")
    if production.get("source_tree_sha256") != manifest.get("source_tree_sha256"):
        errors.append("package production receipt names a different renderer source tree")
    source_trees = set()
    render_segments = 0
    for producer in production.get("producers") or []:
        if not isinstance(producer, dict) or producer.get("kind") != "render-segment":
            continue
        render_segments += 1
        receipt_ref = producer.get("receipt")
        if not isinstance(receipt_ref, dict):
            errors.append("render producer has no exact receipt")
            continue
        try:
            producer_path = _bounded_file(package_root, receipt_ref.get("path"), "render producer receipt")
            value = read_json(producer_path, "render producer receipt")
        except EvidenceError as exc:
            errors.append(str(exc))
            continue
        if receipt_ref.get("sha256") != sha256(producer_path):
            errors.append("render producer receipt digest is stale")
        inputs = value.get("inputs") or {}
        if value.get("schema") != "danse.render.segment.v1":
            errors.append("render producer receipt has the wrong schema")
        if inputs.get("tier") != PRODUCTION_TIER:
            errors.append("render producer receipt does not use the production film tier")
        source = inputs.get("source_tree_sha256")
        if not isinstance(source, str) or not HEX64.fullmatch(source):
            errors.append("render producer receipt has no source-tree identity")
        else:
            source_trees.add(source)
    if not render_segments:
        errors.append("package production receipt has no render-segment producer")
    if len(source_trees) != 1:
        errors.append("package render producers do not share one source-tree identity")
        return None, errors
    return next(iter(source_trees)), errors


def packaged_receipt_errors(
    package_root: Path,
    manifest: dict[str, Any],
    *,
    schema_root: Path = ROOT,
) -> list[str]:
    """Authenticate a staged package copy without consulting ignored source paths."""
    reference = manifest.get("score_motion_evidence")
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        return ["package manifest has no exact production A/B evidence reference"]
    try:
        receipt_path = _bounded_file(package_root, reference.get("path"), "packaged production A/B receipt")
        receipt = read_json(receipt_path, "packaged production A/B receipt")
    except EvidenceError as exc:
        return [str(exc)]
    errors: list[str] = []
    if reference.get("sha256") != sha256(receipt_path):
        errors.append("packaged production A/B receipt digest is stale")
    items = _manifest_items(manifest)
    receipt_item = items.get(reference.get("path"))
    if not isinstance(receipt_item, dict):
        errors.append("packaged production A/B receipt is absent from manifest.items")
    else:
        if receipt_item.get("sha256") != sha256(receipt_path):
            errors.append("packaged production A/B manifest item digest is stale")
        if receipt_item.get("bytes") != receipt_path.stat().st_size:
            errors.append("packaged production A/B manifest item byte count is stale")
    if _historical_fixture(receipt):
        return [*errors, "historical fixture evidence cannot satisfy the production A/B gate"]

    source_tree, source_errors = _package_renderer_source(package_root, manifest)
    errors.extend(source_errors)
    try:
        require_production_tier(schema_root)
    except EvidenceError as exc:
        errors.append(str(exc))
    try:
        checkout_head = git_identity(schema_root, require_clean=False)
        checkout_source_tree = renderer_source_tree(PRODUCTION_TIER, schema_root)
    except EvidenceError as exc:
        errors.append(str(exc))
        checkout_head = None
        checkout_source_tree = None
    if manifest.get("repository_head") != checkout_head:
        errors.append("package manifest does not name the exact checker Git HEAD")
    if source_tree != checkout_source_tree:
        errors.append("package render producers do not name the exact checker source tree")
    if manifest.get("corpus_tier") != PRODUCTION_TIER:
        errors.append("package manifest does not select the production film tier")
    sound = manifest.get("sound") if isinstance(manifest.get("sound"), dict) else {}
    try:
        score_path = _bounded_file(package_root, "provenance/passage-score.wav", "packaged score master")
        pcm = wav_pcm_identity(score_path)
    except EvidenceError as exc:
        return [*errors, str(exc)]
    if sha256(score_path) != sound.get("master_sha256"):
        errors.append("packaged score master differs from manifest sound identity")
    try:
        river_seed = int(str(manifest.get("seed")), 0)
        span = {
            "river_seed": river_seed,
            "stream": 0,
            "passage": manifest["passage"],
            "t0": manifest["t0"],
            "t1": manifest["t1"],
            "duration_seconds": manifest["duration"],
        }
    except (KeyError, TypeError, ValueError) as exc:
        return [*errors, f"package manifest has no exact A/B span: {exc}"]
    expected = {
        "repository_head": manifest.get("repository_head"),
        "source_tree_sha256": source_tree,
        "span": span,
        "score": {
            "path": "music/score.json",
            "file_sha256": sound.get("score_file_sha256"),
            "contract_sha256": sound.get("score_contract_sha256"),
            "duration_seconds": manifest.get("duration"),
        },
        "choreography": {
            "path": "render/choreography.json",
            "file_sha256": sound.get("choreography_file_sha256"),
            "contract_sha256": sound.get("choreography_contract_sha256"),
        },
        "audio_render_receipt": {
            "path": (receipt.get("audio_render_receipt") or {}).get("path"),
            "sha256": sound.get("audio_render_receipt_sha256"),
        },
        "audio_master": {
            "path": (receipt.get("audio_master") or {}).get("path"),
            "sha256": sound.get("master_sha256"),
            **pcm,
        },
    }
    errors.extend(
        production_receipt_errors(
            receipt_path,
            expected=expected,
            root=schema_root,
            require_clean=False,
            recompute_samples=True,
        )
    )
    try:
        owned = evidence_artifact_paths(receipt_path)
    except EvidenceError as exc:
        errors.append(str(exc))
    else:
        for path in owned:
            relative = path.relative_to(package_root.resolve(strict=True)).as_posix()
            item = items.get(relative)
            if not isinstance(item, dict):
                errors.append(f"production A/B artifact is absent from manifest.items: {relative}")
            elif item.get("sha256") != sha256(path) or item.get("bytes") != path.stat().st_size:
                errors.append(f"production A/B artifact manifest identity is stale: {relative}")
    return errors


CAPTURE_JS = r"""
() => {
  const gl = document.getElementById('stage').getContext('webgl2');
  window.danseEvidenceCapture = async function capture(url) {
    const width = gl.drawingBufferWidth, height = gl.drawingBufferHeight;
    const need = width * height * 4;
    const pbo = gl.createBuffer();
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, pbo);
    gl.bufferData(gl.PIXEL_PACK_BUFFER, need, gl.STREAM_READ);
    gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, 0);
    const fence = gl.fenceSync(gl.SYNC_GPU_COMMANDS_COMPLETE, 0);
    gl.flush();
    for (;;) {
      const status = gl.clientWaitSync(fence, 0, 0);
      if (status === gl.ALREADY_SIGNALED || status === gl.CONDITION_SATISFIED) break;
      if (status === gl.WAIT_FAILED) throw new Error('frame evidence fence wait failed');
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    gl.deleteSync(fence);
    const payload = new Uint8Array(need);
    gl.getBufferSubData(gl.PIXEL_PACK_BUFFER, 0, payload);
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null);
    gl.deleteBuffer(pbo);
    const response = await fetch(url, {method: 'POST', body: new Blob([payload])});
    if (!response.ok) throw new Error(`frame evidence sink ${response.status}`);
    return need;
  };
  return true;
}
"""


def _rgba_png(payload: bytes, width: int, height: int) -> bytes:
    try:
        from PIL import Image
    except ImportError as exc:
        raise EvidenceError("Pillow is required to capture production boundary frames") from exc
    if len(payload) != width * height * 4:
        raise EvidenceError("GPU boundary frame has an unexpected byte count")
    image = Image.frombytes("RGBA", (width, height), payload)
    image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM).convert("RGB")
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def _capture_query(
    *,
    tier: str,
    width: int,
    height: int,
    with_score: bool,
    passage_seconds: object,
) -> dict[str, str]:
    if not finite_receipt_number(passage_seconds) or passage_seconds <= 0:
        raise EvidenceError("production boundary capture has no finite positive passage clock")
    query = {
        "capture": "passage",
        "from": "0",
        "tier": tier,
        "s": "20170620",
        "u": "0",
        "width": str(width),
        "height": str(height),
    }
    if with_score:
        query |= {"score": "music/score.json", "choreography": "render/choreography.json"}
    else:
        # The control may share the selected score's clock, but never its score or
        # choreography inputs.  Without this the natural unscored passage crosses
        # into a new material seed before the production score has finished.
        query["passage-seconds"] = str(passage_seconds)
    return query


def _assert_control_frame_identity(rendered: object, passage_seconds: object) -> None:
    """Prove every captured control frame stayed in the selected score-free passage."""
    if (
        not isinstance(rendered, dict)
        or rendered.get("hasMusic") is not False
        or rendered.get("hasChoreography") is not False
        or type(rendered.get("passage")) is not int
        or rendered.get("passage") != 0
        or type(rendered.get("passageSeed")) is not int
        or rendered.get("passageSeed") != PRODUCTION_PASSAGE_SEED
        or not finite_receipt_number(rendered.get("passageT0"))
        or rendered.get("passageT0") != 0
        or not finite_receipt_number(rendered.get("passageSeconds"))
        or rendered.get("passageSeconds") != passage_seconds
    ):
        raise EvidenceError(
            "production control frame left its selected score-free passage identity"
        )


def _capture_boundary_payloads(
    rows: list[dict[str, Any]],
    *,
    tier: str,
    width: int,
    height: int,
    passage_seconds: object,
) -> tuple[dict[str, dict[str, bytes]], str, bytes]:
    try:
        sys.path.insert(0, str(ROOT / "render"))
        from browser import browser, serve
    except (ImportError, OSError) as exc:
        raise EvidenceError(f"production frame capture needs the macOS browser stack: {exc}") from exc
    collected: dict[str, bytes] = {}

    def sink(path: str, body: bytes) -> None:
        collected[path] = body

    output: dict[str, dict[str, bytes]] = {row["sample_id"]: {} for row in rows}
    renderer = "unknown"
    with serve(sink=sink) as base:
        with browser(headless=True, width=width, height=height) as page:
            for mode, with_score in (("control", False), ("with_score", True)):
                page.goto(
                    f"{base}/film.html?{urlencode(_capture_query(tier=tier, width=width, height=height, with_score=with_score, passage_seconds=passage_seconds))}",
                    wait_until="load",
                )
                page.wait_for_function("() => window.danseFilmReady === true", timeout=300_000)
                renderer = str(page.gl_renderer)
                page.evaluate(CAPTURE_JS)
                for index, row in enumerate(rows):
                    rendered = page.evaluate("(t) => window.danseFilm.renderAt(t)", row["review_second"])
                    if mode == "control":
                        _assert_control_frame_identity(rendered, passage_seconds)
                    if rendered.get("missing"):
                        raise EvidenceError(
                            f"production frame {row['sample_id']} has {rendered['missing']} missing plates"
                        )
                    endpoint = f"/production-frame/{mode}/{index}"
                    page.evaluate("(url) => window.danseEvidenceCapture(url)", f"{base}{endpoint}")
                    output[row["sample_id"]][mode] = _rgba_png(collected.pop(endpoint), width, height)

        # A second browser process repeats the first exact input.  This is the
        # deterministic control; WITH versus control is expected to differ.
        first = rows[0]
        with browser(headless=True, width=width, height=height) as page:
            page.goto(
                f"{base}/film.html?{urlencode(_capture_query(tier=tier, width=width, height=height, with_score=True, passage_seconds=passage_seconds))}",
                wait_until="load",
            )
            page.wait_for_function("() => window.danseFilmReady === true", timeout=300_000)
            page.evaluate(CAPTURE_JS)
            page.evaluate("(t) => window.danseFilm.renderAt(t)", first["review_second"])
            endpoint = "/production-frame/repeat/0"
            page.evaluate("(url) => window.danseEvidenceCapture(url)", f"{base}{endpoint}")
            repeat = _rgba_png(collected.pop(endpoint), width, height)
    return output, renderer, repeat


def _compose_contact_sheet(
    rows: list[dict[str, Any]],
    frame_paths: dict[str, dict[str, Path]],
    destination: Path,
) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise EvidenceError("Pillow is required to compose the production contact sheet") from exc
    with Image.open(next(iter(frame_paths.values()))["with_score"]) as source:
        ratio = min(1.0, 420 / source.width)
        thumb = (max(1, round(source.width * ratio)), max(1, round(source.height * ratio)))
    pad, gap, label_height = 14, 10, 30
    width = pad * 2 + thumb[0] * 2 + gap
    row_height = label_height + thumb[1] + pad
    sheet = Image.new("RGB", (width, pad + row_height * len(rows)), (14, 14, 18))
    draw = ImageDraw.Draw(sheet)
    for index, row in enumerate(rows):
        y = pad + index * row_height
        labels = ", ".join(f"{item['kind']}:{item['id']}" for item in row["boundaries"])
        draw.text((pad, y), f"{row['absolute_second']:.6f}s · {labels}", fill=(225, 225, 232))
        for column, mode in enumerate(("control", "with_score")):
            with Image.open(frame_paths[row["sample_id"]][mode]) as image:
                rendered = image.convert("RGB").resize(thumb, Image.Resampling.LANCZOS)
            sheet.paste(rendered, (pad + column * (thumb[0] + gap), y + label_height))
    sheet.save(destination, "PNG")


def write_frame_receipt(
    destination: Path,
    *,
    context: dict[str, Any],
    sample_path: Path,
    tier: str = PRODUCTION_TIER,
    width: int = 1920,
    height: int = 1080,
    root: Path = ROOT,
) -> dict[str, Any]:
    if tier != PRODUCTION_TIER or width != PRODUCTION_WIDTH or height != PRODUCTION_HEIGHT:
        raise EvidenceError(
            "production frame evidence requires film tier at "
            f"{PRODUCTION_WIDTH}x{PRODUCTION_HEIGHT}"
        )
    sample, sample_errors = _load_sample_receipt(
        sample_path,
        expected=context,
        schema_root=root,
        recompute_rows=True,
    )
    if sample_errors:
        raise EvidenceError("; ".join(sample_errors))
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise EvidenceError("production frame receipt destination is unsafe")
    destination.parent.mkdir(parents=True, exist_ok=True)
    base = destination.parent.resolve(strict=True)
    try:
        regular_file(sample_path, "production sample receipt").relative_to(base)
    except ValueError as exc:
        raise EvidenceError("frame and sample receipts must share one evidence directory") from exc

    capture_rows = []
    for row in sample["rows"]:
        frame_index, review_second = _production_review_position(row["absolute_second"])
        capture_rows.append(
            {**row, "review_frame_index": frame_index, "review_second": review_second}
        )
    payloads, renderer, repeat = _capture_boundary_payloads(
        capture_rows,
        tier=tier,
        width=width,
        height=height,
        passage_seconds=context["span"]["duration_seconds"],
    )
    frames_root = base / "boundary-frames"
    if frames_root.is_symlink() or (frames_root.exists() and not frames_root.is_dir()):
        raise EvidenceError("production boundary-frame destination is unsafe")
    frames_root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, dict[str, Path]] = {}
    frame_rows = []
    for row in capture_rows:
        sample_id = row["sample_id"]
        paths[sample_id] = {}
        for mode in ("with_score", "control"):
            path = frames_root / f"{sample_id}-{mode.replace('_', '-')}.png"
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise EvidenceError(f"production boundary-frame destination is unsafe: {path}")
            path.write_bytes(payloads[sample_id][mode])
            paths[sample_id][mode] = path
        measured = _image_psnr(paths[sample_id]["with_score"], paths[sample_id]["control"], width, height)
        if measured >= 60:
            raise EvidenceError(f"{sample_id} has no observable production A/B pixel difference")
        frame_rows.append(
            {
                "sample_id": sample_id,
                "absolute_second": row["absolute_second"],
                "review_frame_index": row["review_frame_index"],
                "review_second": row["review_second"],
                "boundaries": row["boundaries"],
                "movement": row["movement"],
                "phrase": row["phrase"],
                "psnr_db": measured,
                "with_score": artifact_reference(paths[sample_id]["with_score"], base),
                "control": artifact_reference(paths[sample_id]["control"], base),
            }
        )
    repeat_path = frames_root / "sample-000-with-score-repeat.png"
    repeat_path.write_bytes(repeat)
    if repeat_path.read_bytes() != paths[sample["rows"][0]["sample_id"]]["with_score"].read_bytes():
        raise EvidenceError("fresh-browser production boundary render is not byte-identical")
    contact_sheet = base / "score-to-motion-contact-sheet.png"
    _compose_contact_sheet(sample["rows"], paths, contact_sheet)
    document = {
        "schema": FRAME_SCHEMA_ID,
        "evidence_scope": "production-boundary-frame-evidence",
        **context,
        "sample_receipt": artifact_reference(sample_path, base),
        "capture": {
            "tier": tier,
            "width": width,
            "height": height,
            "fps": PRODUCTION_FPS,
            "renderer": renderer,
            **capture_contract_identity(root),
        },
        "contact_sheet": artifact_reference(contact_sheet, base),
        "determinism": {
            "absolute_second": capture_rows[0]["review_second"],
            "identical": True,
            "first": artifact_reference(paths[sample["rows"][0]["sample_id"]]["with_score"], base),
            "repeat": artifact_reference(repeat_path, base),
        },
        "rows": frame_rows,
    }
    schema_error = _schema_error(document, root / FRAME_SCHEMA.relative_to(ROOT), "production frame receipt")
    if schema_error:
        raise EvidenceError(schema_error)
    destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    _, errors = _load_frame_receipt(
        destination,
        sample_path=sample_path,
        sample=sample,
        expected=context,
        schema_root=root,
    )
    if errors:
        raise EvidenceError("; ".join(errors))
    return document


def write_receipt(
    *,
    destination: Path,
    context: dict[str, Any],
    sample_path: Path,
    frame_path: Path,
    with_score: Path,
    control: Path,
    with_score_producer: Path,
    control_producer: Path,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Finalize only already-measured machine artifacts; never mint acceptance."""
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise EvidenceError("production A/B receipt destination is unsafe")
    span = context.get("span") if isinstance(context.get("span"), dict) else {}
    expected_video_frames = production_frame_count(
        span.get("duration_seconds"),
        "production receipt duration",
    )
    duration = float(span["duration_seconds"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    base = destination.parent.resolve(strict=True)
    for path in (
        sample_path,
        frame_path,
        with_score,
        control,
        with_score_producer,
        control_producer,
    ):
        try:
            regular_file(path, "production evidence artifact").relative_to(base)
        except ValueError as exc:
            raise EvidenceError("all production evidence artifacts must share the final receipt directory") from exc

    sample, sample_errors = _load_sample_receipt(
        sample_path,
        expected=context,
        schema_root=root,
        recompute_rows=True,
    )
    if sample_errors:
        raise EvidenceError("; ".join(sample_errors))
    frame, frame_errors = _load_frame_receipt(
        frame_path,
        sample_path=sample_path,
        sample=sample,
        expected=context,
        schema_root=root,
    )
    if frame_errors:
        raise EvidenceError("; ".join(frame_errors))

    media = {}
    review_probe_cache: dict[str, dict[str, Any]] = {}
    producer_paths = {
        "with_score": with_score_producer,
        "control": control_producer,
    }
    for name, path in (("with_score", with_score), ("control", control)):
        path = regular_file(path, f"{name} review media")
        with _pinned_regular_file(path, f"{name} review media") as (file_fd, _info):
            probe = ffprobe_media(
                path,
                expected_video_frames=expected_video_frames,
                expected_audio_frames=context["audio_master"]["frames"],
                source_fd=file_fd,
            )
            anchors = review_frame_anchors(
                path,
                frame_path=frame_path,
                frame=frame,
                mode=name,
                expected_frames=expected_video_frames,
                source_fd=file_fd,
            )
        if probe["video_frames"] != expected_video_frames:
            raise EvidenceError(f"{name} review movie does not contain the exact rounded production frame span")
        if abs(float(probe["duration_seconds"]) - duration) > 0.5 / 30:
            raise EvidenceError(f"{name} review movie does not span the exact production duration")
        if probe["audio_pcm_sha256"] != context["audio_master"]["pcm_sha256"]:
            raise EvidenceError(f"{name} review movie does not carry the exact competition master PCM")
        if probe["audio_frames"] != context["audio_master"]["frames"]:
            raise EvidenceError(f"{name} review movie has a stale audio frame count")
        if probe["decoded_video_frames"] != expected_video_frames:
            raise EvidenceError(f"{name} review movie has a stale decoded frame count")
        review_probe_cache[name] = {"probe": probe, "anchors": anchors}
        media[name] = {
            "path": path.relative_to(base).as_posix(),
            "mode": name,
            **probe,
            "producer_receipt": artifact_reference(producer_paths[name], base),
            "anchors": anchors,
        }
    if media["with_score"]["sha256"] == media["control"]["sha256"]:
        raise EvidenceError("with-score and control review movies are byte-identical")
    if (
        media["with_score"]["video_framehash_sha256"]
        == media["control"]["video_framehash_sha256"]
    ):
        raise EvidenceError("with-score and control review movies have identical decoded video")

    document = {
        "schema": PRODUCTION_SCHEMA_ID,
        "evidence_scope": "production-machine-evidence-only",
        **context,
        "sample_receipt": artifact_reference(sample_path, base),
        "frame_receipt": artifact_reference(frame_path, base),
        "review_media": media,
        "human_review": {"status": "not-attested"},
    }
    schema_error = _schema_error(document, root / PRODUCTION_SCHEMA.relative_to(ROOT), "production A/B receipt")
    if schema_error:
        raise EvidenceError(schema_error)
    destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    errors = production_receipt_errors(
        destination,
        expected=context,
        root=root,
        require_clean=False,
        recompute_samples=True,
        _review_probe_cache=review_probe_cache,
    )
    if errors:
        raise EvidenceError("; ".join(errors))
    return document


def _context_from_sample(
    sample_path: Path,
    *,
    root: Path,
    tier: str,
    require_clean: bool = True,
) -> dict[str, Any]:
    sample = read_json(sample_path, "production sample receipt")
    audio = repository_file(root, (sample.get("audio_render_receipt") or {}).get("path"), "audio-render receipt")
    return current_context(audio, tier=tier, root=root, require_clean=require_clean)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    samples = subparsers.add_parser("samples", help="write the portable production boundary sample receipt")
    samples.add_argument("--audio-receipt", type=Path, default=DEFAULT_AUDIO_RECEIPT)
    samples.add_argument(
        "--tier",
        default=PRODUCTION_TIER,
        choices=[PRODUCTION_TIER],
        help="corpus/source tier; production evidence must bind the film master",
    )
    samples.add_argument("--out", type=Path, default=DEFAULT_SAMPLE_RECEIPT)

    frames = subparsers.add_parser("frames", help="capture production A/B boundary frames on the Mac/GPU")
    frames.add_argument("--samples", type=Path, default=DEFAULT_SAMPLE_RECEIPT)
    frames.add_argument(
        "--tier",
        default=PRODUCTION_TIER,
        choices=[PRODUCTION_TIER],
        help="corpus/source tier (review raster remains 1920x1080)",
    )
    frames.add_argument("--width", type=int, default=1920)
    frames.add_argument("--height", type=int, default=1080)
    frames.add_argument("--out", type=Path, default=DEFAULT_FRAME_RECEIPT)

    final = subparsers.add_parser("finalize", help="bind samples, frames, and synchronized review movies")
    final.add_argument("--samples", type=Path, default=DEFAULT_SAMPLE_RECEIPT)
    final.add_argument("--frames", type=Path, default=DEFAULT_FRAME_RECEIPT)
    final.add_argument("--with-score", type=Path, required=True)
    final.add_argument("--control", type=Path, required=True)
    final.add_argument("--with-score-producer", type=Path, required=True)
    final.add_argument("--control-producer", type=Path, required=True)
    final.add_argument("--out", type=Path, default=DEFAULT_PRODUCTION_RECEIPT)

    check = subparsers.add_parser("check", help="authenticate an existing production A/B receipt")
    check.add_argument("receipt", type=Path, nargs="?", default=DEFAULT_PRODUCTION_RECEIPT)

    args = parser.parse_args()
    try:
        if args.command == "samples":
            context = current_context(args.audio_receipt, tier=args.tier)
            document = write_sample_receipt(args.out, context)
            print(f"ok: {args.out} · {len(document['rows'])} exact production boundaries")
            return 0
        if args.command == "frames":
            if args.width < 1 or args.height < 1:
                parser.error("--width and --height must be positive")
            context = _context_from_sample(args.samples, root=ROOT, tier=args.tier)
            document = write_frame_receipt(
                args.out,
                context=context,
                sample_path=args.samples,
                tier=args.tier,
                width=args.width,
                height=args.height,
            )
            print(f"ok: {args.out} · {len(document['rows'])} observable boundary pairs")
            return 0
        if args.command == "finalize":
            frame = read_json(args.frames, "production frame receipt")
            tier = (frame.get("capture") or {}).get("tier", PRODUCTION_TIER)
            context = _context_from_sample(args.samples, root=ROOT, tier=tier)
            write_receipt(
                destination=args.out,
                context=context,
                sample_path=args.samples,
                frame_path=args.frames,
                with_score=args.with_score,
                control=args.control,
                with_score_producer=args.with_score_producer,
                control_producer=args.control_producer,
            )
            print(f"ok: {args.out} · machine evidence only; human review remains not-attested")
            return 0
        errors = production_receipt_errors(args.receipt)
        if errors:
            for error in errors:
                print(f"FAIL: {error}", file=sys.stderr)
            return 1
        print(f"ok: {args.receipt} · production A/B machine evidence; human review not attested")
        return 0
    except EvidenceError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
