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
import shutil
import subprocess
import sys
import wave
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
REVIEW_ANCHOR_PSNR_FLOOR = 30.0
CAPTURE_TOOL = ROOT / "scripts/score_motion_production.py"
BROWSER_CONTRACT = ROOT / "render/browser.py"

sys.path.insert(0, str(ROOT / "sound"))
sys.path.insert(0, str(ROOT / "pipeline"))
from choreography import load_choreography  # noqa: E402
from corpus_contract import authorize_render_tier  # noqa: E402
from music_score import load_score  # noqa: E402


class EvidenceError(ValueError):
    """A production claim cannot be authenticated."""


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


def read_json(path: Path, label: str) -> dict[str, Any]:
    regular_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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


def renderer_source_tree(tier: str, root: Path = ROOT) -> str:
    path = root / "render/render.py"
    spec = importlib.util.spec_from_file_location("danse_score_motion_renderer", path)
    if spec is None or spec.loader is None:
        raise EvidenceError("cannot load the canonical renderer source identity")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        args = SimpleNamespace(
            score="music/score.json",
            choreography="render/choreography.json",
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
    index = int(math.floor(float(second) * PRODUCTION_FPS + 0.5))
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
    expected_frames = round(duration * pcm["sample_rate"])
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


def media_pcm_identity(path: Path, *, sample_rate: int = 48000, channels: int = 2) -> dict[str, Any]:
    """Hash the decoded PCM, rather than pretending a movie equals a WAV file."""
    path = regular_file(path, "A/B review media")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise EvidenceError("ffmpeg is required to authenticate review-media audio")
    command = [
        ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a:0", "-vn",
        "-ac", str(channels), "-ar", str(sample_rate), "-c:a", "pcm_s16le", "-f", "s16le", "-",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None and process.stderr is not None
    digest = hashlib.sha256()
    byte_count = 0
    while True:
        chunk = process.stdout.read(1 << 20)
        if not chunk:
            break
        digest.update(chunk)
        byte_count += len(chunk)
    stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
    returncode = process.wait()
    if returncode:
        raise EvidenceError(f"ffmpeg cannot decode review-media audio: {stderr}")
    frame_bytes = channels * 2
    if byte_count < frame_bytes or byte_count % frame_bytes:
        raise EvidenceError("review-media audio does not decode to complete stereo PCM frames")
    return {
        "audio_pcm_sha256": digest.hexdigest(),
        "audio_frames": byte_count // frame_bytes,
        "audio_sample_rate": sample_rate,
        "audio_channels": channels,
    }


def media_video_identity(path: Path, *, width: int, height: int) -> dict[str, Any]:
    """Hash every decoded frame so container-only differences cannot fake A/B."""
    path = regular_file(path, "A/B review media")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise EvidenceError("ffmpeg is required to authenticate review-media video")
    done = subprocess.run(
        [
            ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0", "-an", "-sn",
            "-f", "framehash", "-hash", "sha256", "-",
        ],
        capture_output=True,
        check=False,
    )
    if done.returncode:
        detail = done.stderr.decode("utf-8", errors="replace").strip()
        raise EvidenceError(f"ffmpeg cannot hash decoded review-media video: {detail}")
    rows = [line.strip() for line in done.stdout.splitlines() if line and not line.startswith(b"#")]
    if width < 1 or height < 1 or not rows:
        raise EvidenceError("review-media video has no complete decoded-frame identity")
    return {
        "video_framehash_sha256": hashlib.sha256(b"\n".join(rows) + b"\n").hexdigest(),
        "decoded_video_frames": len(rows),
    }


def ffprobe_media(path: Path) -> dict[str, Any]:
    path = regular_file(path, "A/B review media")
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise EvidenceError("ffprobe is required to authenticate A/B review media")
    done = subprocess.run(
        [
            ffprobe, "-v", "error", "-count_packets",
            "-show_entries", "format=duration:stream=codec_type,avg_frame_rate,nb_read_packets,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
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
    streams = probe.get("streams") or []
    video = [row for row in streams if row.get("codec_type") == "video"]
    audio = [row for row in streams if row.get("codec_type") == "audio"]
    if len(video) != 1 or len(audio) != 1:
        raise EvidenceError("each A/B review movie must contain exactly one video and one audio stream")
    rate = str(video[0].get("avg_frame_rate", ""))
    try:
        numerator, denominator = rate.split("/", 1)
        fps = float(numerator) / float(denominator)
        video_frames = int(video[0]["nb_read_packets"])
        width = int(video[0]["width"])
        height = int(video[0]["height"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise EvidenceError(f"review movie has no exact numeric frame identity: {rate}") from exc
    if abs(fps - PRODUCTION_FPS) > 1e-9 or video_frames < 1:
        raise EvidenceError(f"review movie must be a non-empty 30 fps stream, got {fps}")
    if width != PRODUCTION_WIDTH or height != PRODUCTION_HEIGHT:
        raise EvidenceError(
            f"review movie must be {PRODUCTION_WIDTH}x{PRODUCTION_HEIGHT}, got {width}x{height}"
        )
    return {
        "sha256": sha256(path),
        "bytes": path.stat().st_size,
        "duration_seconds": duration,
        "fps": PRODUCTION_FPS,
        "width": width,
        "height": height,
        "video_frames": video_frames,
        "video_streams": 1,
        "audio_streams": 1,
        **media_pcm_identity(path),
        **media_video_identity(path, width=width, height=height),
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
import { validate as validateProgram } from './engine/program.js';
import { validate as validateScore } from './engine/score.js';

const bytes = (path) => fs.readFileSync(path);
const hash = (value) => crypto.createHash('sha256').update(value).digest('hex');
const scoreBytes = bytes('music/score.json');
const score = validateScore(JSON.parse(scoreBytes));
Object.defineProperty(score, 'fileSha256', {value: hash(scoreBytes), enumerable: false});
const choreography = validateChoreography(JSON.parse(bytes('render/choreography.json')), {score});
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
    quantise: 0, stream: 0, score: null, choreography: null,
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
) -> tuple[dict[str, Any], list[str]]:
    try:
        sample = read_json(sample_path, "production score-to-motion sample receipt")
    except EvidenceError as exc:
        return {}, [str(exc)]
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


def _image_psnr(first: Path, second: Path, width: int, height: int) -> float:
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError as exc:
        raise EvidenceError("Pillow is required to authenticate production boundary frames") from exc
    try:
        with Image.open(first) as left_source, Image.open(second) as right_source:
            left = left_source.convert("RGB")
            right = right_source.convert("RGB")
            if left.size != (width, height) or right.size != (width, height):
                raise EvidenceError("production boundary frame dimensions are stale")
            difference = ImageChops.difference(left, right)
            squared = sum(ImageStat.Stat(difference).sum2)
    except OSError as exc:
        raise EvidenceError(f"cannot decode production boundary frames: {exc}") from exc
    if squared == 0:
        raise EvidenceError("production A/B boundary frames are pixel-identical")
    mse = squared / (width * height * 3)
    return 10 * math.log10((255 * 255) / mse)


def _raw_frame_psnr(source: Path, payload: bytes, width: int, height: int) -> float:
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError as exc:
        raise EvidenceError("Pillow is required to bind review frames to GPU captures") from exc
    if len(payload) != width * height * 3:
        raise EvidenceError("review anchor does not contain one complete RGB frame")
    try:
        with Image.open(source) as image:
            expected = image.convert("RGB")
            if expected.size != (width, height):
                raise EvidenceError("review anchor source frame dimensions are stale")
            actual = Image.frombytes("RGB", (width, height), payload)
            squared = sum(ImageStat.Stat(ImageChops.difference(expected, actual)).sum2)
    except OSError as exc:
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
) -> list[dict[str, Any]]:
    """Match each movie's 30 fps boundary frame to the Metal capture for its mode."""
    if mode not in {"with_score", "control"}:
        raise EvidenceError(f"unknown A/B review mode: {mode}")
    capture = frame.get("capture") or {}
    width = int(capture.get("width", 0))
    height = int(capture.get("height", 0))
    rows = frame.get("rows") or []
    indexes = [row.get("review_frame_index") for row in rows if isinstance(row, dict)]
    if (
        len(indexes) != len(rows)
        or not indexes
        or any(type(index) is not int or index < 0 for index in indexes)
    ):
        raise EvidenceError("frame receipt has no complete review-frame index chain")
    unique_indexes = sorted(set(indexes))
    expression = "+".join(f"eq(n\\,{index})" for index in unique_indexes)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise EvidenceError("ffmpeg is required to bind review movies to GPU captures")
    process = subprocess.Popen(
        [
            ffmpeg, "-v", "error", "-i", str(regular_file(media_path, "A/B review media")),
            "-map", "0:v:0", "-vf", f"select={expression}", "-fps_mode", "passthrough",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    frame_bytes = width * height * 3
    decoded: dict[int, bytes] = {}
    for index in unique_indexes:
        payload = _read_exact(process.stdout, frame_bytes)
        if len(payload) != frame_bytes:
            process.kill()
            process.wait()
            raise EvidenceError(f"review movie is missing boundary frame {index}")
        decoded[index] = payload
    if process.stdout.read(1):
        process.kill()
        process.wait()
        raise EvidenceError("review movie emitted surplus selected boundary frames")
    stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
    if process.wait():
        raise EvidenceError(f"ffmpeg cannot extract review boundary frames: {stderr}")

    anchors = []
    for row in rows:
        index = row["review_frame_index"]
        source = local_artifact(frame_path, row.get(mode), f"{row.get('sample_id')} {mode} frame")
        payload = decoded[index]
        measured = _raw_frame_psnr(source, payload, width, height)
        if measured < REVIEW_ANCHOR_PSNR_FLOOR:
            raise EvidenceError(
                f"{mode} review frame {index} is only {measured:.2f} dB from its Metal capture"
            )
        anchors.append(
            {
                "sample_id": row["sample_id"],
                "frame_index": index,
                "review_second": row["review_second"],
                "source_frame_sha256": sha256(source),
                "decoded_rgb_sha256": hashlib.sha256(payload).hexdigest(),
                "psnr_db": round(measured, 9),
            }
        )
    return anchors


def _load_frame_receipt(
    frame_path: Path,
    *,
    sample_path: Path,
    sample: dict[str, Any],
    expected: dict[str, Any],
    schema_root: Path = ROOT,
) -> tuple[dict[str, Any], list[str]]:
    try:
        frame = read_json(frame_path, "production score-to-motion frame receipt")
    except EvidenceError as exc:
        return {}, [str(exc)]
    schema_error = _schema_error(
        frame,
        schema_root / "docs/evidence/score-to-motion-frames-production.schema.json",
        "production frame receipt",
    )
    if schema_error:
        return frame, [schema_error]
    errors = _context_errors(frame, expected, "frame receipt")
    sample_errors, referenced_sample = _artifact_errors(frame_path, frame.get("sample_receipt"), "frame sample receipt")
    errors.extend(sample_errors)
    if referenced_sample is not None and referenced_sample != sample_path.resolve(strict=True):
        errors.append("frame receipt names a different production sample receipt")

    sheet_errors, sheet_path = _artifact_errors(frame_path, frame.get("contact_sheet"), "contact sheet")
    errors.extend(sheet_errors)
    if sheet_path is not None:
        try:
            from PIL import Image
            with Image.open(sheet_path) as sheet:
                if sheet.width < 1 or sheet.height < 1:
                    errors.append("contact sheet is empty")
        except (ImportError, OSError) as exc:
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
        with_errors, with_path = _artifact_errors(frame_path, row.get("with_score"), f"{sample_id} with-score frame")
        control_errors, control_path = _artifact_errors(frame_path, row.get("control"), f"{sample_id} control frame")
        errors.extend(with_errors)
        errors.extend(control_errors)
        if with_path is None or control_path is None:
            continue
        resolved_rows.append((with_path, control_path))
        try:
            measured = _image_psnr(with_path, control_path, width, height)
        except EvidenceError as exc:
            errors.append(f"{sample_id}: {exc}")
        else:
            if abs(measured - float(row.get("psnr_db", -1))) > 1e-9:
                errors.append(f"{sample_id} PSNR is stale")
            if measured >= 60:
                errors.append(f"{sample_id} has no observable pixel difference")

    determinism = frame.get("determinism") or {}
    first_errors, first = _artifact_errors(frame_path, determinism.get("first"), "determinism first frame")
    repeat_errors, repeat = _artifact_errors(frame_path, determinism.get("repeat"), "determinism repeat frame")
    errors.extend(first_errors)
    errors.extend(repeat_errors)
    if first is not None and repeat is not None:
        if first.read_bytes() != repeat.read_bytes():
            errors.append("production boundary-frame renderer is not deterministic")
        if resolved_rows and first != resolved_rows[0][0]:
            errors.append("determinism first frame is not the first with-score boundary frame")
    if sample.get("rows") and determinism.get("absolute_second") != sample["rows"][0]["absolute_second"]:
        errors.append("determinism check does not own the first exact sample time")
    return frame, errors


def _media_errors(
    receipt_path: Path,
    receipt: dict[str, Any],
    *,
    expected: dict[str, Any],
    frame_path: Path | None,
    frame: dict[str, Any],
) -> list[str]:
    errors = []
    duration = float(expected["span"]["duration_seconds"])
    expected_video_frames = round(duration * 30)
    master = expected["audio_master"]
    identities: dict[str, tuple[dict[str, Any], Path]] = {}
    for name in ("with_score", "control"):
        reference = (receipt.get("review_media") or {}).get(name)
        artifact_errors, path = _artifact_errors(receipt_path, reference, f"{name} review media")
        errors.extend(artifact_errors)
        if path is None or not isinstance(reference, dict):
            continue
        try:
            probed = ffprobe_media(path)
        except EvidenceError as exc:
            errors.append(str(exc))
            continue
        for field in (
            "sha256", "bytes", "duration_seconds", "fps", "width", "height", "video_frames",
            "video_streams", "audio_streams", "audio_pcm_sha256", "audio_frames",
            "audio_sample_rate", "audio_channels", "video_framehash_sha256",
            "decoded_video_frames",
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
        else:
            try:
                anchors = review_frame_anchors(path, frame_path=frame_path, frame=frame, mode=name)
            except EvidenceError as exc:
                errors.append(str(exc))
            else:
                if reference.get("anchors") != anchors:
                    errors.append(f"{name} review-media frame anchors are stale")
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
) -> list[str]:
    """Return every reason a receipt cannot satisfy the production machine gate."""
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return [f"production A/B receipt is absent: {receipt_path}"]
    try:
        receipt = read_json(receipt_path, "production A/B receipt")
    except EvidenceError as exc:
        return [str(exc)]
    if _historical_fixture(receipt):
        return ["historical fixture evidence cannot satisfy the production A/B gate"]
    schema_error = _schema_error(
        receipt,
        root / "docs/evidence/score-to-motion-production.schema.json",
        "production A/B receipt",
    )
    if schema_error:
        return [schema_error]

    errors: list[str] = []
    frame_ref_errors, frame_path = _artifact_errors(receipt_path, receipt.get("frame_receipt"), "frame receipt")
    errors.extend(frame_ref_errors)
    sample_ref_errors, sample_path = _artifact_errors(receipt_path, receipt.get("sample_receipt"), "sample receipt")
    errors.extend(sample_ref_errors)
    if expected is None:
        tier = PRODUCTION_TIER
        if frame_path is not None:
            try:
                tier = read_json(frame_path, "production frame receipt").get("capture", {}).get("tier", tier)
            except EvidenceError as exc:
                errors.append(str(exc))
        try:
            audio_path = repository_file(root, receipt["audio_render_receipt"]["path"], "audio-render receipt")
            expected = current_context(audio_path, tier=tier, root=root, require_clean=require_clean)
        except (EvidenceError, KeyError, TypeError) as exc:
            return [*errors, str(exc)]
    errors.extend(_context_errors(receipt, expected, "production A/B receipt"))

    sample: dict[str, Any] = {}
    if sample_path is not None:
        sample, sample_errors = _load_sample_receipt(
            sample_path,
            expected=expected,
            schema_root=root,
            recompute_rows=recompute_samples,
        )
        errors.extend(sample_errors)
    frame: dict[str, Any] = {}
    if frame_path is not None and sample_path is not None and sample:
        frame, frame_errors = _load_frame_receipt(
            frame_path,
            sample_path=sample_path,
            sample=sample,
            expected=expected,
            schema_root=root,
        )
        errors.extend(frame_errors)
    errors.extend(
        _media_errors(
            receipt_path,
            receipt,
            expected=expected,
            frame_path=frame_path,
            frame=frame,
        )
    )
    if receipt.get("human_review") != {"status": "not-attested"}:
        errors.append("machine evidence must not claim human artistic acceptance")
    return errors


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
        paths.add(local_artifact(receipt_path, receipt["review_media"][name], f"{name} review media"))
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


def _capture_query(*, tier: str, width: int, height: int, with_score: bool) -> dict[str, str]:
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
    return query


def _capture_boundary_payloads(
    rows: list[dict[str, Any]],
    *,
    tier: str,
    width: int,
    height: int,
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
                page.goto(f"{base}/film.html?{urlencode(_capture_query(tier=tier, width=width, height=height, with_score=with_score))}", wait_until="load")
                page.wait_for_function("() => window.danseFilmReady === true", timeout=300_000)
                renderer = str(page.gl_renderer)
                page.evaluate(CAPTURE_JS)
                for index, row in enumerate(rows):
                    rendered = page.evaluate("(t) => window.danseFilm.renderAt(t)", row["review_second"])
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
            page.goto(f"{base}/film.html?{urlencode(_capture_query(tier=tier, width=width, height=height, with_score=True))}", wait_until="load")
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
        capture_rows, tier=tier, width=width, height=height
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
    root: Path = ROOT,
) -> dict[str, Any]:
    """Finalize only already-measured machine artifacts; never mint acceptance."""
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise EvidenceError("production A/B receipt destination is unsafe")
    destination.parent.mkdir(parents=True, exist_ok=True)
    base = destination.parent.resolve(strict=True)
    for path in (sample_path, frame_path, with_score, control):
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

    duration = float(context["span"]["duration_seconds"])
    expected_video_frames = round(duration * 30)
    media = {}
    for name, path in (("with_score", with_score), ("control", control)):
        probe = ffprobe_media(path)
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
        media[name] = {
            "path": path.relative_to(base).as_posix(),
            "mode": name,
            **probe,
            "anchors": review_frame_anchors(
                path,
                frame_path=frame_path,
                frame=frame,
                mode=name,
            ),
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
