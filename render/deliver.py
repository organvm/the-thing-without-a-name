#!/usr/bin/env python3
"""Every deliverable the call asks for, from one command. Idempotent.

The river is a pure `f(seed, t)` and the captures in `program.json` are presets
for RECORDING the river starting at a given `--start` offset, so most of this is
not rendering — it is SELECTING from the recorded river. That is the whole
leverage of the spine, and it shows up here as arithmetic:

    passage           RENDERED. 4K ProRes 422 HQ (one whole passage at 4K),
                      the primary submission recording.
    midnight-moment   sliced from the passage recording. ProRes is all-intra,
                      so every frame is a keyframe and a cut is frame-exact with
                      no re-encode at all — Times Square gets literally the film's
                      own frames.
    screener          the passage recording, scaled to 1080p.
    trailer           sliced, then scaled to 1080p.
    reel              RENDERED. The one capture preset that cannot be derived,
                      because 1080x1920 is a vertical aspect and `cover`
                      projection therefore chooses a different field of view.
    stills            six one-frame renders at distinct seeds, named by seed.

SOUND IS SLICED, NEVER RE-SCORED. The passage accepts only the deterministic
FluidSynth master whose receipt binds the selected score, choreography, adapted
MIDI, soundfont, toolchain, mix, stems, and exact duration. Every derived capture
is cut from those same master bytes, so a moment sounds the same in every crop of
the film that contains it.

    render/deliver.py                 # everything
    render/deliver.py --only stills
    render/deliver.py --force reel    # re-make one that already exists
    render/deliver.py --out <scratch-render-root> --package <package-root>
    render/deliver.py --preflight      # same dependency plan, no writes or rendering
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import functools
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import wave
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path, PurePosixPath

import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None

HERE = Path(__file__).resolve().parent
DANSE = HERE.parent
PROGRAM = HERE / "program.json"
DEFAULT_OUT = HERE / "out"
OUT = DEFAULT_OUT
PACKAGE = DEFAULT_OUT / "package"
SCORE = DANSE / "sound" / "score.py"
RENDER = HERE / "render.py"
REGISTER = DANSE / "submission" / "screendance-2027.yaml"
RIGHTS_REGISTER = DANSE / "rights" / "register.json"
MUSIC_REPERTOIRE = DANSE / "music" / "repertoire.yaml"
MUSIC_SCORE = DANSE / "music" / "score.json"
CHOREOGRAPHY = DANSE / "render" / "choreography.json"
ADAPTATION = DANSE / "music" / "adaptation.json"
AUDIO_TOOLCHAIN = DANSE / "music" / "audio-toolchain.json"
AUDIO_MIX = DANSE / "music" / "delibes-mix.json"
AUDIO_USES = DANSE / "sound" / "audio-uses.json"
AUDIO_RENDER_SCHEMA = DANSE / "music" / "audio-render.schema.json"
AUDIO_RENDER_RECEIPT = DANSE / ".work" / "music" / "competition" / "audio-render.json"
AUDIO_MASTER = DANSE / ".work" / "music" / "competition" / "delibes-master.wav"
SCORE_MOTION_EVIDENCE = (
    DANSE
    / ".work"
    / "evidence"
    / "score-to-motion-production"
    / "score-to-motion-production.json"
)
MUSIC_CREDIT = (
    "Music by Léo Delibes. Source arrangements by Paul De Bra, adapted and "
    "re-orchestrated for Danse under CC BY 4.0. Changes include instrumentation, "
    "sequencing, cue markers, and mix."
)
RAW = DANSE / "pipeline" / ".work" / "raw"
BANK = DANSE / "sound" / "bank" / "bank.json"
sys.path.insert(0, str(DANSE / "sound"))
sys.path.insert(0, str(DANSE / "pipeline"))
from bank_contract import audit_bank  # noqa: E402
from corpus_contract import authorize_render_tier  # noqa: E402
from music_score import canonical_sha256  # noqa: E402

# Captures that are sub-spans or scaled versions of the primary 4K `passage` capture,
# so they can be cut/scaled from it. `copy` means stream-copy (no re-encode at all).
DERIVED = {
    "midnight-moment": {"suffix": ".mov", "mode": "copy", "audio": "pcm_s24le"},
    "trailer": {"suffix": ".mp4", "mode": "scale", "audio": "aac"},
    "screener": {"suffix": ".mp4", "mode": "scale", "audio": "aac"},
}

# Six moments, chosen to span the arc rather than to flatter one cut: the
# composite intact, the composite coming apart, the engine at full stride twice,
# a body that never existed, and a reseed.
STILL_FRACTIONS = (0.08, 0.22, 0.38, 0.54, 0.70, 0.88)

SELECTORS = ("master", "derived", "reel", "stills", "origin", "text")
FORCE_ITEMS = (*SELECTORS, *DERIVED)
REEL_ITEM = "reel.mp4"
AUDIO_ITEMS = {
    "master.mov",
    "midnight-moment.mov",
    "trailer.mp4",
    "screener.mp4",
    REEL_ITEM,
}
SCORE_SOURCE_ITEM = "provenance/passage-score.wav"
AUDIO_RENDER_SOURCE_ITEM = "provenance/audio-render.json"
PRODUCTION_RECEIPT = "provenance/production.json"
PRODUCER_RECEIPTS = "provenance/producer-receipts"
SCORE_MOTION_EVIDENCE_DIR = "provenance/score-to-motion"
SCORE_MOTION_EVIDENCE_ITEM = f"{SCORE_MOTION_EVIDENCE_DIR}/score-to-motion-production.json"
SCORE_MOTION_RECEIPT_MAX_BYTES = 4 << 20
PACKAGE_MANIFEST_MAX_BYTES = 4 << 20
PRODUCER_RECEIPT_MAX_BYTES = 4 << 20
PRODUCER_RECEIPT_TOTAL_MAX_BYTES = 16 << 20
PRODUCER_CONCAT_SEGMENT_MAX_COUNT = 256
PRODUCTION_RECEIPT_MAX_BYTES = 4 << 20
PASSAGE_SELECTORS = {"master", "derived", "reel", "stills"}
FIXED_WINDOW_ITEMS = {"midnight-moment.mov", "trailer.mp4", REEL_ITEM}


def sh(cmd: list, **kw) -> subprocess.CompletedProcess:
    rendered = [str(c) for c in cmd]
    try:
        return subprocess.run(rendered, capture_output=True, text=True, **kw)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(rendered, 127, stdout="", stderr=str(exc))


def ffmpeg(args: list) -> None:
    done = sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args])
    if done.returncode != 0:
        raise SystemExit(f"ffmpeg failed:\n{' '.join(str(a) for a in args)}\n{done.stderr.strip()}")


def probe(path: Path) -> dict | None:
    if not path.is_file() or shutil.which("ffprobe") is None:
        return None
    done = sh(
        # fmt: off
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate,channels",
            "-of",
            "json",
            path,
        ]
        # fmt: on
    )
    if done.returncode != 0:
        return None
    raw = json.loads(done.stdout)
    out = {"seconds": float(raw["format"]["duration"]), "bytes": int(raw["format"]["size"])}
    for s in raw.get("streams", []):
        if s["codec_type"] == "video" and "width" not in out:
            num, den = s["r_frame_rate"].split("/")
            out |= {"width": s["width"], "height": s["height"], "fps": round(int(num) / max(int(den), 1), 3)}
            out["vcodec"] = s["codec_name"]
        elif s["codec_type"] == "audio" and "acodec" not in out:
            out |= {"acodec": s["codec_name"], "channels": s.get("channels")}
    return out


def probe_required(path: Path) -> dict | None:
    """Probe media without mistaking a missing tool for an invalid artifact."""
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is required for media delivery; run deliver.py --preflight")
    return probe(path)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bounded_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
    description: str,
) -> tuple[bytes, str]:
    """Read one stable regular-file snapshot without following its final link."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular file")
        if before.st_size > max_bytes:
            raise SystemExit(f"{description} exceeds its safe byte limit: {path.name}")
        chunks = []
        copied = 0
        while copied <= max_bytes:
            block = os.read(descriptor, min(1 << 20, max_bytes + 1 - copied))
            if not block:
                break
            chunks.append(block)
            copied += len(block)
        if copied > max_bytes:
            raise SystemExit(f"{description} exceeds its safe byte limit: {path.name}")
        after = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
    except SystemExit:
        raise
    except OSError as exc:
        raise SystemExit(f"{description} is missing or unsafe: {path.name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields) or (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
    ) != (named.st_dev, named.st_ino, named.st_mode, named.st_size):
        raise SystemExit(f"{description} changed while being read: {path.name}")
    payload = b"".join(chunks)
    if len(payload) != after.st_size:
        raise SystemExit(f"{description} changed while being read: {path.name}")
    return payload, hashlib.sha256(payload).hexdigest()


def bounded_regular_bytes_at(
    directory_fd: int,
    name: str,
    *,
    max_bytes: int,
    description: str,
) -> tuple[bytes, str]:
    """Read one stable regular-file snapshot relative to a pinned directory."""
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise SystemExit(f"{description} has an unsafe descriptor-relative name")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular file")
        if before.st_size > max_bytes:
            raise SystemExit(f"{description} exceeds its safe byte limit: {name}")
        chunks = []
        copied = 0
        while copied <= max_bytes:
            block = os.read(descriptor, min(1 << 20, max_bytes + 1 - copied))
            if not block:
                break
            chunks.append(block)
            copied += len(block)
        if copied > max_bytes:
            raise SystemExit(f"{description} exceeds its safe byte limit: {name}")
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except SystemExit:
        raise
    except OSError as exc:
        raise SystemExit(f"{description} is missing or unsafe: {name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields) or (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
    ) != (named.st_dev, named.st_ino, named.st_mode, named.st_size):
        raise SystemExit(f"{description} changed while being read: {name}")
    payload = b"".join(chunks)
    if len(payload) != after.st_size:
        raise SystemExit(f"{description} changed while being read: {name}")
    return payload, hashlib.sha256(payload).hexdigest()


def atomic_rename_noreplace(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    src_dir_fd: int | None = None,
    dst_dir_fd: int | None = None,
) -> None:
    """Atomically rename one entry while refusing to replace a winner.

    The package transaction cannot safely emulate this with a stat followed by
    ``os.replace``: another publisher can win between those two syscalls.  The
    delivery rail already requires POSIX locking, and its supported production
    hosts expose the corresponding kernel no-replace rename primitive.
    """
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    source_directory = -100 if src_dir_fd is None else src_dir_fd
    destination_directory = -100 if dst_dir_fd is None else dst_dir_fd
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        flag = 1  # RENAME_NOREPLACE
    elif sys.platform == "darwin":  # pragma: no cover - exercised on capture hosts
        rename = getattr(libc, "renameatx_np", None)
        flag = 0x00000004  # RENAME_EXCL
    else:  # pragma: no cover - fail closed on unsupported POSIX variants
        rename = None
        flag = 0
    if rename is None:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace rename is unavailable on this host",
            os.fspath(destination),
        )
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    if rename(
        source_directory,
        source_bytes,
        destination_directory,
        destination_bytes,
        flag,
    ) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fspath(destination))


def write_new_regular_bytes_at(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int = 0o644,
) -> str:
    """Create one descriptor-relative staging file without replacing an entry."""
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise OSError(errno.EINVAL, "unsafe descriptor-relative staging name", name)
    if not isinstance(payload, bytes):
        raise TypeError("staged payload must be bytes")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, mode, dir_fd=directory_fd)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written < 1:
                raise OSError(errno.EIO, "short write while staging package receipt")
            offset += written
        os.fchmod(descriptor, mode)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != len(payload)
            or stat.S_IMODE(info.st_mode) != mode
        ):
            raise OSError(errno.EIO, "unsafe staged package receipt")
    finally:
        os.close(descriptor)
    return hashlib.sha256(payload).hexdigest()


def repository_state() -> dict:
    """Return the producing commit only when Git can describe this checkout."""
    head = sh(["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=DANSE)
    if head.returncode != 0:
        raise SystemExit("delivery requires a Git commit identity")
    if not isinstance(head.stdout, str):
        raise SystemExit("delivery received no Git commit identity")
    commit = head.stdout.strip().lower()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise SystemExit("delivery received an invalid Git commit identity")
    status = sh(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=DANSE,
    )
    if status.returncode != 0:
        raise SystemExit("delivery cannot verify the repository worktree")
    if not isinstance(status.stdout, str):
        raise SystemExit("delivery received no repository worktree status")
    changes = [line for line in status.stdout.splitlines() if line.strip()]
    return {
        "head": commit,
        "clean": not changes,
        "changes": changes,
    }


def require_clean_repository() -> dict:
    """Refuse to mint package identities for bytes not owned by the named commit."""
    state = repository_state()
    if not state["clean"]:
        sample = ", ".join(line[:160] for line in state["changes"][:5])
        extra = len(state["changes"]) - 5
        suffix = f" (+{extra} more)" if extra > 0 else ""
        raise SystemExit(
            "production package delivery requires a clean tracked/untracked worktree; "
            f"commit or remove repository changes first: {sample}{suffix}"
        )
    return state


def regular_json(path: Path, schema: str) -> dict:
    """Load one known contract without accepting a symlink or non-object JSON."""
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"required {schema} contract is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read {schema} contract at {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise SystemExit(f"{path} is not a {schema} contract")
    return value


def repository_file(relative: object, label: str) -> Path:
    """Resolve one regular repository file without accepting traversal or links."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise SystemExit(f"{label} has no safe repository-relative path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or "." in pure.parts or ".." in pure.parts:
        raise SystemExit(f"{label} escapes the repository")
    current = DANSE
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise SystemExit(f"{label} traverses a symlink")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(DANSE.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{label} is missing or outside the repository") from exc
    if not resolved.is_file():
        raise SystemExit(f"{label} is not a regular file")
    return resolved


def contract_sha256(value: dict, label: str) -> str:
    identity = value.get("identity")
    declared = identity.get("contract_sha256") if isinstance(identity, dict) else None
    if not isinstance(declared, str) or not re.fullmatch(r"[0-9a-f]{64}", declared):
        raise SystemExit(f"{label} has no contract_sha256")
    source = {
        **value,
        "identity": {key: row for key, row in identity.items() if key != "contract_sha256"},
    }
    if canonical_sha256(source) != declared:
        raise SystemExit(f"{label} contract_sha256 does not match its content")
    return declared


def competition_audio_provenance(span: dict) -> dict:
    """Authenticate every producer identity behind the fixed competition master."""
    score = regular_json(MUSIC_SCORE, "danse.music.score.v1")
    choreography = regular_json(CHOREOGRAPHY, "danse.choreography.v1")
    adaptation = regular_json(ADAPTATION, "danse.music.adaptation.v1")
    toolchain = regular_json(AUDIO_TOOLCHAIN, "danse.audio.toolchain.v1")
    mix = regular_json(AUDIO_MIX, "danse.audio.mix.v1")
    uses = regular_json(AUDIO_USES, "danse.audio.uses.v1")
    receipt = regular_json(AUDIO_RENDER_RECEIPT, "danse.audio.render.v1")

    try:
        repertoire = yaml.safe_load(MUSIC_REPERTOIRE.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"cannot read selected repertoire: {exc}") from exc
    works = repertoire.get("works") if isinstance(repertoire, dict) else None
    selected = [
        work
        for work in works or []
        if isinstance(work, dict) and work.get("selection", {}).get("status") == "selected"
    ]
    gate = repertoire.get("artistic_gate") if isinstance(repertoire, dict) else None
    if (
        not isinstance(gate, dict)
        or gate.get("status") != "accepted"
        or len(selected) != 1
        or selected[0].get("id") != "delibes-screendance-suite"
        or selected[0].get("role") != "repertoire"
    ):
        raise SystemExit("competition delivery rejects fixture or pending artistic repertoire")
    if score.get("identity", {}).get("work_id") != selected[0]["id"]:
        raise SystemExit("music score does not bind the selected repertoire work")
    selected_midi = selected[0].get("score", {}).get("source_midi")
    if (
        not isinstance(selected_midi, dict)
        or selected_midi.get("sha256") != score.get("identity", {}).get("midi_sha256")
        or selected_midi.get("sha256") != adaptation.get("output", {}).get("sha256")
    ):
        raise SystemExit("selected repertoire, score, and adapted MIDI identities differ")
    if score.get("release_status") != "production-selected":
        raise SystemExit("competition delivery requires a production-selected score")
    if score.get("time", {}).get("passage_mapping") != "native-tempo":
        raise SystemExit("competition delivery rejects affine score timing")
    duration = score.get("time", {}).get("duration_seconds")
    if type(duration) not in (int, float) or abs(float(duration) - float(span["duration"])) > 1e-6:
        raise SystemExit("competition score duration does not match the selected passage")
    if (
        abs(float(span["t0"])) > 1e-9
        or int(span.get("passage", -1)) != 0
        or int(span.get("river_seed", -1)) != 20170620
    ):
        raise SystemExit("competition delivery is locked to river seed 20170620, passage 0, score time 0")

    adaptation_sources = adaptation.get("sources")
    if not isinstance(adaptation_sources, list) or len(adaptation_sources) != 2:
        raise SystemExit("Delibes adaptation must bind exactly two arrangement sources")
    for row in adaptation_sources:
        relative = row.get("path") if isinstance(row, dict) else None
        expected = row.get("sha256") if isinstance(row, dict) else None
        if (
            not isinstance(relative, str)
            or not isinstance(expected, str)
            or row.get("license") != "CC BY 4.0"
            or row.get("license_url") != "https://creativecommons.org/licenses/by/4.0/"
        ):
            raise SystemExit("Delibes adaptation source/license identity is incomplete")
        source_path = repository_file(relative, "Delibes arrangement source")
        if digest(source_path) != expected:
            raise SystemExit(f"Delibes arrangement source is missing or stale: {relative}")

    profile_id = uses.get("competition_profile")
    profiles = uses.get("profiles")
    profile = profiles.get(profile_id) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict) or profile.get("package_eligible") is not True:
        raise SystemExit("competition audio profile is absent or package-ineligible")
    sources = profile.get("declared_sources")
    required_stems = profile.get("required_stems")
    forbidden = set(profile.get("forbidden_source_kinds") or [])
    if not isinstance(sources, list) or not isinstance(required_stems, list):
        raise SystemExit("competition audio profile has no typed sources/stems")
    if any(not isinstance(row, dict) or row.get("kind") in forbidden for row in sources):
        raise SystemExit("competition audio profile contains an undeclared or forbidden source kind")
    for row in sources:
        relative = row.get("path")
        expected = row.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise SystemExit("competition audio source has no exact path/hash")
        source_path = repository_file(relative, "competition audio source")
        if digest(source_path) != expected:
            raise SystemExit(f"competition audio source is missing or stale: {relative}")
        notice = row.get("license_notice")
        if notice is not None:
            notice_path = (
                repository_file(notice.get("path"), "competition audio license notice")
                if isinstance(notice, dict)
                else None
            )
            notice_sha = notice.get("sha256") if isinstance(notice, dict) else None
            if (
                notice_path is None
                or not isinstance(notice_sha, str)
                or digest(notice_path) != notice_sha
            ):
                raise SystemExit(f"competition audio source has a missing or stale license notice: {relative}")

    toolchain_files = {
        "midi": repository_file(adaptation["output"]["path"], "adapted MIDI"),
        "adaptation": ADAPTATION,
        "mix": AUDIO_MIX,
        "renderer": DANSE / "sound" / "render_music.py",
    }
    for name, expected_path in toolchain_files.items():
        row = toolchain.get(name)
        if (
            not isinstance(row, dict)
            or row.get("path") != expected_path.relative_to(DANSE).as_posix()
            or row.get("sha256") != digest(expected_path)
        ):
            raise SystemExit(f"audio toolchain {name} identity is missing or stale")
    soundfont = toolchain.get("soundfont")
    soundfont_notice = soundfont.get("license_notice") if isinstance(soundfont, dict) else None
    soundfont_path = (
        repository_file(soundfont.get("path"), "pinned soundfont")
        if isinstance(soundfont, dict)
        else None
    )
    soundfont_notice_path = (
        repository_file(soundfont_notice.get("path"), "soundfont license notice")
        if isinstance(soundfont_notice, dict)
        else None
    )
    if (
        not isinstance(soundfont, dict)
        or soundfont.get("path") != ".work/music/MuseScore_General.sf3"
        or soundfont_path is None
        or soundfont.get("sha256") != digest(soundfont_path)
        or soundfont.get("license") != "MIT"
        or not isinstance(soundfont_notice, dict)
        or soundfont_notice.get("path") != "music/licenses/MuseScore_General_License.md"
        or soundfont_notice_path is None
        or soundfont_notice.get("sha256") != digest(soundfont_notice_path)
    ):
        raise SystemExit("audio toolchain soundfont/license identity is missing or stale")

    expected_inputs = {
        "score": (MUSIC_SCORE, contract_sha256(score, "music score")),
        "choreography": (CHOREOGRAPHY, contract_sha256(choreography, "choreography")),
        "midi": (repository_file(adaptation["output"]["path"], "adapted MIDI"), None),
        "adaptation": (ADAPTATION, None),
        "toolchain": (AUDIO_TOOLCHAIN, None),
        "mix": (AUDIO_MIX, None),
        "audio_uses": (AUDIO_USES, None),
        "soundfont": (repository_file(toolchain["soundfont"]["path"], "pinned soundfont"), None),
    }
    inputs = receipt.get("inputs")
    if not isinstance(inputs, dict):
        raise SystemExit("audio render receipt has no inputs")
    for name, (path, contract) in expected_inputs.items():
        row = inputs.get(name)
        if (
            not isinstance(row, dict)
            or row.get("path") != path.relative_to(DANSE).as_posix()
            or row.get("sha256") != digest(path)
            or (contract is not None and row.get("contract_sha256") != contract)
        ):
            raise SystemExit(f"audio render receipt {name} identity is missing or stale")

    fluidsynth = inputs.get("fluidsynth_executable")
    pinned_fluidsynth = toolchain.get("fluidsynth")
    if (
        not isinstance(fluidsynth, dict)
        or not isinstance(pinned_fluidsynth, dict)
        or fluidsynth.get("version") != "2.6.0"
        or fluidsynth.get("sha256") != pinned_fluidsynth.get("executable_sha256")
    ):
        raise SystemExit("audio render receipt does not bind the pinned FluidSynth executable")
    executable = Path(str(fluidsynth.get("path", "")))
    if executable.is_symlink() or not executable.is_file() or digest(executable) != fluidsynth["sha256"]:
        raise SystemExit("pinned FluidSynth executable is missing or changed")

    ffmpeg_input = inputs.get("ffmpeg_executable")
    pinned_ffmpeg = toolchain.get("ffmpeg")
    if (
        not isinstance(ffmpeg_input, dict)
        or not isinstance(pinned_ffmpeg, dict)
        or ffmpeg_input.get("version") != "9.0.1"
        or ffmpeg_input.get("sha256") != pinned_ffmpeg.get("executable_sha256")
    ):
        raise SystemExit("audio render receipt does not bind the pinned ffmpeg executable")
    ffmpeg_executable = Path(str(ffmpeg_input.get("path", "")))
    if (
        ffmpeg_executable.is_symlink()
        or not ffmpeg_executable.is_file()
        or digest(ffmpeg_executable) != ffmpeg_input["sha256"]
    ):
        raise SystemExit("pinned ffmpeg executable is missing or changed")

    verification = receipt.get("verification")
    required_checks = (
        "deterministic",
        "non_silent",
        "stems_non_silent",
        "polyphonic",
        "normalization_deterministic",
        "loudness_in_target",
        "true_peak_in_target",
        "duration_matches_score",
        "seek_safe",
    )
    if not isinstance(verification, dict) or not all(verification.get(name) is True for name in required_checks):
        raise SystemExit("audio render receipt has not passed every deterministic render check")
    normalization = receipt.get("normalization")
    settings = mix.get("master", {}).get("normalization")
    output_loudness = normalization.get("output") if isinstance(normalization, dict) else None
    expected_targets = {
        "integrated_lufs": -16.0,
        "tolerance_lu": 0.5,
        "target_true_peak_dbtp": -1.1,
        "max_true_peak_dbtp": -1.0,
        "lra_lu": 11.0,
    }
    if (
        not isinstance(settings, dict)
        or settings.get("method") != "ffmpeg-loudnorm-two-pass"
        or pinned_ffmpeg.get("settings") != settings
        or not isinstance(normalization, dict)
        or normalization.get("schema") != "danse.audio.normalization.v1"
        or normalization.get("method") != settings["method"]
        or normalization.get("limiter") != "ffmpeg-loudnorm-dynamic-true-peak"
        or normalization.get("normalization_type") != "dynamic"
        or normalization.get("targets") != expected_targets
        or normalization.get("ffmpeg")
        != {"version": "9.0.1", "executable_sha256": ffmpeg_input["sha256"]}
        or not isinstance(output_loudness, dict)
    ):
        raise SystemExit("audio render receipt has no exact pinned loudness-normalization identity")
    try:
        measured_lufs = float(output_loudness["integrated_lufs"])
        measured_true_peak = float(output_loudness["true_peak_dbtp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit("audio render receipt has no numeric loudness measurement") from exc
    if abs(measured_lufs - expected_targets["integrated_lufs"]) > expected_targets["tolerance_lu"]:
        raise SystemExit("competition audio master loudness is outside the package target")
    if measured_true_peak > expected_targets["max_true_peak_dbtp"]:
        raise SystemExit("competition audio master true peak exceeds the package ceiling")
    outputs = receipt.get("outputs")
    master = outputs.get("master") if isinstance(outputs, dict) else None
    stems = outputs.get("stems") if isinstance(outputs, dict) else None
    if receipt.get("profile") != profile_id or not isinstance(master, dict) or not isinstance(stems, list):
        raise SystemExit("audio render receipt has no exact master/stem outputs")
    if master.get("path") != AUDIO_MASTER.relative_to(DANSE).as_posix():
        raise SystemExit("audio render receipt points at the wrong competition master")
    if AUDIO_MASTER.is_symlink() or not AUDIO_MASTER.is_file() or master.get("sha256") != digest(AUDIO_MASTER):
        raise SystemExit("competition audio master is missing or stale")
    sample_rate = int(mix.get("sample_rate", 0))
    expected_frames = int(
        (Decimal(str(duration)) * sample_rate).to_integral_value(rounding=ROUND_HALF_UP)
    )
    sample_grid_duration = expected_frames / sample_rate
    if (
        master.get("frames") != expected_frames
        or master.get("sample_rate") != sample_rate
        or master.get("channels") != 2
        or abs(float(master.get("duration_seconds", -1)) - sample_grid_duration) > 1e-9
        or abs(sample_grid_duration - float(duration)) > (0.5 / sample_rate + 1e-9)
    ):
        raise SystemExit("competition audio master duration/format drifted")
    try:
        with wave.open(str(AUDIO_MASTER), "rb") as reader:
            actual_master = (
                reader.getnframes(),
                reader.getframerate(),
                reader.getnchannels(),
                reader.getsampwidth(),
            )
    except (OSError, wave.Error) as exc:
        raise SystemExit(f"competition audio master is not canonical PCM WAV: {exc}") from exc
    if actual_master != (expected_frames, sample_rate, 2, 2):
        raise SystemExit("competition audio master bytes disagree with the receipt format")
    by_stem = {row.get("id"): row for row in stems if isinstance(row, dict)}
    if len(stems) != len(required_stems) or list(by_stem) != required_stems:
        raise SystemExit("audio render receipt stem order differs from the competition profile")
    for stem_id in required_stems:
        row = by_stem[stem_id]
        path = repository_file(row.get("path"), f"competition audio stem {stem_id}")
        if (
            row.get("sha256") != digest(path)
            or row.get("frames") != expected_frames
            or row.get("sample_rate") != sample_rate
            or row.get("channels") != 2
            or row.get("non_silent") is not True
        ):
            raise SystemExit(f"competition audio stem is missing or stale: {stem_id}")

    credit = adaptation.get("credit")
    if credit != MUSIC_CREDIT:
        raise SystemExit("competition music adaptation does not carry the required exact credit")
    return {
        "profile": profile_id,
        "audio_uses_sha256": digest(AUDIO_USES),
        "score_file_sha256": digest(MUSIC_SCORE),
        "score_contract_sha256": expected_inputs["score"][1],
        "choreography_file_sha256": digest(CHOREOGRAPHY),
        "choreography_contract_sha256": expected_inputs["choreography"][1],
        "midi_sha256": inputs["midi"]["sha256"],
        "adaptation_sha256": inputs["adaptation"]["sha256"],
        "toolchain_sha256": inputs["toolchain"]["sha256"],
        "mix_sha256": inputs["mix"]["sha256"],
        "soundfont_sha256": inputs["soundfont"]["sha256"],
        "audio_render_receipt_sha256": digest(AUDIO_RENDER_RECEIPT),
        "master_sha256": master["sha256"],
        "sources": [row["id"] for row in sources],
        "stems": [{"id": stem_id, "sha256": by_stem[stem_id]["sha256"]} for stem_id in required_stems],
        "credit": credit,
    }


@functools.lru_cache(maxsize=None)
def delivery_source_sha256(tier: str) -> str:
    """Identity of every tracked or derived byte that can change a package artifact."""
    roots = [
        DANSE / "film.html",
        DANSE / "arrival.js",
        PROGRAM,
        DANSE / "submission" / "screendance-2027.yaml",
        DANSE / "rights" / "register.json",
        HERE / "deliver.py",
        HERE / "render.py",
        HERE / "browser.py",
        DANSE / "pipeline/corpus_contract.py",
        DANSE / "corpus/manifest.json",
        DANSE / "corpus/room.webp",
        DANSE / "corpus/score-2017.json",
        DANSE / "corpus/manifest.local.json",
        DANSE / "corpus" / "tier-receipts" / f"{tier}.json",
        DANSE / "music/compile_score.py",
        DANSE / "music/repertoire.yaml",
        DANSE / "music/repertoire.schema.json",
        DANSE / "music/score.json",
        DANSE / "music/score.schema.json",
        DANSE / "music/adapt_delibes.py",
        DANSE / "music/adaptation.json",
        DANSE / "music/audio-render.schema.json",
        DANSE / "music/audio-toolchain.json",
        DANSE / "music/delibes-mix.json",
        DANSE / "music/delibes-screendance-suite.mid",
        DANSE / "music/licenses/CC-BY-4.0-NOTICE.md",
        DANSE / "music/licenses/MuseScore_General_License.md",
        DANSE / "music/sources/Valse-Lente-Delibes.mscz",
        DANSE / "music/sources/Valse-Coppelia.mscz",
        DANSE / "render/choreography.json",
        DANSE / "render/choreography.schema.json",
        DANSE / "sound/audio-uses.json",
        DANSE / "sound/choreography.py",
        DANSE / "sound/control.mjs",
        DANSE / "sound/render_music.py",
    ]
    roots.extend(sorted((DANSE / "engine").glob("*.js")))
    for kind in ("plates", "mattes"):
        roots.extend(sorted((DANSE / "corpus" / kind / tier).glob("*.webp")))
    h = hashlib.sha256()
    for path in roots:
        if path.is_file():
            h.update(str(path.relative_to(DANSE)).encode())
            h.update(bytes.fromhex(digest(path)))
    return h.hexdigest()


@functools.lru_cache(maxsize=None)
def renderer_source_sha256(tier: str) -> str:
    """Canonical visual source identity embedded in offline segment receipts."""
    spec = importlib.util.spec_from_file_location("danse_delivery_renderer_contract", RENDER)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load the canonical renderer source contract")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Args:
        score = MUSIC_SCORE.relative_to(DANSE).as_posix()
        choreography = CHOREOGRAPHY.relative_to(DANSE).as_posix()

    args = Args()
    args.tier = tier
    return module.source_tree_sha256(args)


def captures(program: dict) -> dict:
    return {k: v for k, v in program.get("captures", {}).items() if isinstance(v, dict)}


def hexseed(seed: int) -> str:
    return f"0x{seed:X}"


@functools.lru_cache(maxsize=None)
def _capture_span_items(capture_name: str, seed: int | None = None, start: float = 0.0) -> tuple:
    """Cache the immutable representation of one control-track query."""
    cmd = [
        "node",
        str(DANSE / "sound" / "control.mjs"),
        "--window",
        capture_name,
        "--from",
        str(start),
        "--rate",
        "0",
        "--score",
        MUSIC_SCORE.relative_to(DANSE).as_posix(),
        "--choreography",
        CHOREOGRAPHY.relative_to(DANSE).as_posix(),
    ]
    if seed is not None:
        cmd += ["--seed", str(seed)]
    done = sh(cmd)
    if done.returncode != 0:
        raise SystemExit(f"failed to query capture span for {capture_name}:\n{done.stderr.strip()}")
    data = json.loads(done.stdout)
    return tuple(
        {
            "t0": data["t0"],
            "t1": data["t1"],
            "duration": data["duration"],
            "seed": data["passageSeed"],
            "river_seed": data["seed"],
            "passage": data["passage"],
            "capture": data["capture"],
            "origin": data.get("origin"),
        }.items()
    )


def query_capture_span(capture_name: str, seed: int | None = None, start: float = 0.0) -> dict:
    """Return a fresh mapping while reusing the pure control-track subprocess."""
    return dict(_capture_span_items(capture_name, seed, start))


def hydrated_work_root() -> Path:
    """Honor the same external private-work mount as export and origin delivery."""
    configured = os.environ.get("DANSE_WORK")
    return Path(configured).expanduser() if configured else RAW.parent


def registered_origin() -> Path:
    """The submission register is the sole owner of the source photograph."""
    register = yaml.safe_load(REGISTER.read_text()) or {}
    spec = (register.get("package") or {}).get("origin_still") or {}
    filename = spec.get("source_filename")
    if not filename:
        raise SystemExit(f"{REGISTER} does not declare package.origin_still.source_filename")
    if spec.get("copy_mode") != "byte-identical":
        raise SystemExit(f"{REGISTER} must declare package.origin_still.copy_mode: byte-identical")
    source_sha256 = spec.get("source_sha256")
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256):
        raise SystemExit(f"{REGISTER} must declare package.origin_still.source_sha256")
    return hydrated_work_root() / "raw" / filename


def registered_origin_source_sha256() -> str:
    """The previously approved byte identity of the source photograph."""
    register = yaml.safe_load(REGISTER.read_text()) or {}
    source_sha256 = (((register.get("package") or {}).get("origin_still") or {}).get("source_sha256"))
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256):
        raise SystemExit(f"{REGISTER} must declare package.origin_still.source_sha256")
    return source_sha256.lower()


def registered_audio_sources() -> list[str]:
    """The only recordings a delivery score may claim, from the register."""
    register = yaml.safe_load(REGISTER.read_text()) or {}
    return list((((register.get("package") or {}).get("audio") or {}).get("source_recordings") or []))


def registered_audio_source_digests() -> dict[str, str]:
    register = yaml.safe_load(REGISTER.read_text()) or {}
    audio = ((register.get("package") or {}).get("audio") or {})
    declared = audio.get("source_sha256") or {}
    return {name: declared.get(name, "") for name in audio.get("source_recordings") or []}


def bank_provenance() -> dict | None:
    """Current usable grain-bank identity, bound to the registered sources."""
    audit = audit_bank(BANK, registered_audio_source_digests())
    if not audit.valid or audit.fingerprint is None:
        return None
    return {"bank_fingerprint": audit.fingerprint, "sources": list(audit.sources)}


def score_receipt_path(score: Path) -> Path:
    return score.with_suffix(".json")


def score_provenance(score: Path, span: dict) -> dict | None:
    """Provenance bound to the exact cached score bytes and absolute span."""
    receipt = score_receipt_path(score)
    if not score.is_file() or not receipt.is_file():
        return None
    try:
        data = json.loads(receipt.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") == "danse.score.receipt.v2":
        try:
            provenance = competition_audio_provenance(span)
            valid = (
                data.get("sha256") == digest(score) == provenance["master_sha256"]
                and abs(float(data.get("t0", -1)) - span["t0"]) < 1e-9
                and abs(float(data.get("duration", -1)) - span["duration"]) < 1e-6
                and all(data.get(key) == value for key, value in provenance.items())
            )
        except (OSError, SystemExit, KeyError, TypeError, ValueError):
            return None
        return provenance if valid else None
    # Historical apartment-grain receipts are not eligible to enter the fixed
    # classical competition package, even when their old local bank still exists.
    return None


def write_score_receipt(score: Path, span: dict, provenance: dict) -> None:
    payload = {
        "schema": "danse.score.receipt.v2",
        "sha256": digest(score),
        "t0": span["t0"],
        "t1": span["t1"],
        "duration": span["duration"],
        **provenance,
    }
    score_receipt_path(score).write_text(json.dumps(payload, indent=2) + "\n")


def _production_targets(manifest: dict) -> dict[str, dict]:
    """Rendered outputs that require an independent producer-receipt chain."""
    targets = {}
    for item in manifest.get("items") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = item["name"]
        if (
            name in AUDIO_ITEMS
            or name == SCORE_SOURCE_ITEM
            or re.fullmatch(r"stills/seed-0x[0-9A-Fa-f]{4,}\.(?:jpg|jpeg|png)", name)
        ):
            targets[name] = item
    return targets


def _passage_identity(manifest: dict) -> dict:
    keys = (
        "seed",
        "passage_seed",
        "passage",
        "start",
        "t0",
        "t1",
        "duration",
        "corpus_tier",
    )
    missing = [key for key in keys if key not in manifest]
    if missing:
        raise SystemExit("rendered package has no complete passage identity")
    return {key: manifest[key] for key in keys}


def _prior_production_matches(
    package: Path,
    manifest: dict,
    previous: dict,
    *,
    provenance_fd: int | None = None,
) -> dict | None:
    reference = previous.get("production")
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        return None
    if reference.get("path") != PRODUCTION_RECEIPT:
        return None
    try:
        if provenance_fd is None:
            path = package / PRODUCTION_RECEIPT
            if path.is_symlink() or not path.is_file():
                return None
            document, production_sha = bounded_regular_bytes(
                path,
                max_bytes=PRODUCTION_RECEIPT_MAX_BYTES,
                description="package production receipt",
            )
        else:
            document, production_sha = bounded_regular_bytes_at(
                provenance_fd,
                Path(PRODUCTION_RECEIPT).name,
                max_bytes=PRODUCTION_RECEIPT_MAX_BYTES,
                description="package production receipt",
            )
    except SystemExit:
        return None
    try:
        production = json.loads(document)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if production_sha != reference.get("sha256"):
        return None
    if not isinstance(production, dict):
        return None
    if (
        production.get("schema") != "danse.delivery.production.v1"
        or production.get("repository_head") != manifest.get("repository_head")
        or production.get("source_tree_sha256") != manifest.get("source_tree_sha256")
        or production.get("passage") != _passage_identity(manifest)
        or production.get("sound") != manifest.get("sound")
    ):
        return None
    outputs = {
        row.get("name"): row
        for row in production.get("outputs") or []
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    targets = _production_targets(manifest)
    if set(outputs) != set(targets):
        return None
    for name, item in targets.items():
        output = outputs[name]
        if output.get("sha256") != item.get("sha256") or output.get("bytes") != item.get(
            "bytes"
        ):
            return None
    return reference


def _read_producer_receipt(
    path: Path,
    expected_schema: str,
    *,
    max_bytes: int = PRODUCER_RECEIPT_MAX_BYTES,
) -> tuple[dict, str, int]:
    if type(max_bytes) is not int or max_bytes < 1:
        raise SystemExit("producer receipt graph exceeds its aggregate byte limit")
    try:
        document, receipt_sha = bounded_regular_bytes(
            path,
            max_bytes=min(max_bytes, PRODUCER_RECEIPT_MAX_BYTES),
            description="producer receipt",
        )
    except SystemExit as exc:
        if (
            max_bytes < PRODUCER_RECEIPT_MAX_BYTES
            and "exceeds its safe byte limit" in str(exc)
        ):
            raise SystemExit(
                "producer receipt graph exceeds its aggregate byte limit"
            ) from exc
        raise
    try:
        value = json.loads(document)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SystemExit(f"producer receipt is invalid: {path.name}") from exc
    if not isinstance(value, dict) or value.get("schema") != expected_schema:
        raise SystemExit(f"producer receipt has the wrong schema: {path.name}")
    return value, receipt_sha, len(document)


def write_production_receipt(
    package: Path,
    render_root: Path,
    manifest: dict,
    previous: dict,
    *,
    publish_manifest: bool = False,
    previous_manifest_sha256: str | None = None,
) -> dict | None:
    """Bind package bytes to their producers and optionally commit the manifest.

    Direct callers retain the historical graph-only behavior. Delivery uses
    ``publish_manifest`` so the manifest rename is the final commit point for
    the graph it references.
    """
    targets = _production_targets(manifest)
    if not targets and not publish_manifest:
        return None
    if previous_manifest_sha256 is not None and (
        not isinstance(previous_manifest_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", previous_manifest_sha256)
    ):
        raise SystemExit("prior package manifest has no exact byte identity")

    passage = None
    repository_head = None
    source_tree = None
    if targets:
        passage = _passage_identity(manifest)
        repository_head = manifest.get("repository_head")
        if not isinstance(repository_head, str) or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", repository_head
        ):
            raise SystemExit("rendered package has no exact repository-head identity")
        source_tree = manifest.get("source_tree_sha256")
        if not isinstance(source_tree, str) or not re.fullmatch(r"[0-9a-f]{64}", source_tree):
            raise SystemExit("rendered package has no complete source-tree identity")

    package = package.absolute()
    if package.is_symlink() or (package.exists() and not package.is_dir()):
        raise SystemExit("package root is not a regular directory")
    package.mkdir(parents=True, exist_ok=True)
    if package.is_symlink() or not package.is_dir():
        raise SystemExit("package root is not a regular directory")

    manifest_name = "manifest.json"
    provenance_name = Path(PRODUCTION_RECEIPT).parts[0]
    production_name = Path(PRODUCTION_RECEIPT).name
    receipt_root = package / PRODUCER_RECEIPTS
    receipt_root_name = Path(PRODUCER_RECEIPTS).name
    common_flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | common_flags | getattr(os, "O_DIRECTORY", 0)
    with contextlib.ExitStack() as stack:
        if fcntl is None:
            raise SystemExit(
                "package producer receipt publication requires POSIX advisory locking"
            )
        try:
            if not package.name:
                raise OSError("package root cannot be a filesystem root")
            stage_parent_fd = os.open(package.parent, directory_flags)
            stack.callback(os.close, stage_parent_fd)
            opened_stage_parent = os.fstat(stage_parent_fd)
            named_stage_parent = os.stat(package.parent, follow_symlinks=False)
            directory_fd = os.open(package.name, directory_flags, dir_fd=stage_parent_fd)
            stack.callback(os.close, directory_fd)
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            stack.callback(fcntl.flock, directory_fd, fcntl.LOCK_UN)
            opened_package = os.fstat(directory_fd)
            named_package = os.stat(
                package.name,
                dir_fd=stage_parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise SystemExit("package root cannot be locked for receipt publication") from exc
        if (
            not stat.S_ISDIR(opened_stage_parent.st_mode)
            or (
                opened_stage_parent.st_dev,
                opened_stage_parent.st_ino,
                opened_stage_parent.st_mode,
            )
            != (
                named_stage_parent.st_dev,
                named_stage_parent.st_ino,
                named_stage_parent.st_mode,
            )
            or not stat.S_ISDIR(opened_package.st_mode)
            or (opened_package.st_dev, opened_package.st_ino, opened_package.st_mode)
            != (named_package.st_dev, named_package.st_ino, named_package.st_mode)
        ):
            raise SystemExit("package root changed while being locked")

        def entry_stat(directory: int, name: str) -> os.stat_result | None:
            try:
                return os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return None

        def edge_identity(value: os.stat_result) -> tuple[int, int, int]:
            return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))

        package_identity = edge_identity(opened_package)
        stage_parent_identity = edge_identity(opened_stage_parent)

        def require_stage_parent_edge() -> None:
            try:
                named = os.stat(package.parent, follow_symlinks=False)
            except OSError as exc:
                raise OSError("package parent changed during receipt publication") from exc
            if not stat.S_ISDIR(named.st_mode) or edge_identity(named) != stage_parent_identity:
                raise OSError("package parent changed during receipt publication")

        def require_package_edge() -> None:
            require_stage_parent_edge()
            try:
                named = os.stat(
                    package.name,
                    dir_fd=stage_parent_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise OSError("package root changed during receipt publication") from exc
            if not stat.S_ISDIR(named.st_mode) or edge_identity(named) != package_identity:
                raise OSError("package root changed during receipt publication")

        if publish_manifest:
            manifest_info = entry_stat(directory_fd, manifest_name)
            if manifest_info is not None and not stat.S_ISREG(manifest_info.st_mode):
                raise SystemExit("package manifest destination is not a regular file")
            live_manifest_sha = (
                bounded_regular_bytes_at(
                    directory_fd,
                    manifest_name,
                    max_bytes=PACKAGE_MANIFEST_MAX_BYTES,
                    description="package manifest",
                )[1]
                if manifest_info is not None
                else None
            )
            if live_manifest_sha != previous_manifest_sha256:
                raise SystemExit("package manifest changed after it was read")

        provenance_fd = None
        provenance_identity = None
        if targets:
            provenance_info = entry_stat(directory_fd, provenance_name)
            if provenance_info is None:
                try:
                    os.mkdir(provenance_name, mode=0o755, dir_fd=directory_fd)
                except OSError as exc:
                    raise SystemExit(
                        "package production receipt parent cannot be created safely"
                    ) from exc
                provenance_info = entry_stat(directory_fd, provenance_name)
            if provenance_info is None or not stat.S_ISDIR(provenance_info.st_mode):
                raise SystemExit("package production receipt parent is not a regular directory")
            try:
                provenance_fd = os.open(
                    provenance_name,
                    directory_flags,
                    dir_fd=directory_fd,
                )
                stack.callback(os.close, provenance_fd)
                opened_provenance = os.fstat(provenance_fd)
                named_provenance = os.stat(
                    provenance_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise SystemExit(
                    "package production receipt parent cannot be descriptor-pinned"
                ) from exc
            if (
                not stat.S_ISDIR(opened_provenance.st_mode)
                or edge_identity(opened_provenance) != edge_identity(named_provenance)
            ):
                raise SystemExit("package production receipt parent changed while being opened")
            provenance_identity = edge_identity(opened_provenance)

            def require_provenance_edge() -> None:
                assert provenance_fd is not None and provenance_identity is not None
                require_package_edge()
                try:
                    named = os.stat(
                        provenance_name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise OSError(
                        "package provenance boundary changed during receipt publication"
                    ) from exc
                if not stat.S_ISDIR(named.st_mode) or edge_identity(named) != provenance_identity:
                    raise OSError(
                        "package provenance boundary changed during receipt publication"
                    )

            production_info = entry_stat(provenance_fd, production_name)
            if production_info is not None and not stat.S_ISREG(production_info.st_mode):
                raise SystemExit("package production receipt destination is not a regular file")

        prior = None
        if targets:
            assert provenance_fd is not None
            receipt_info = entry_stat(provenance_fd, receipt_root_name)
            if receipt_info is not None and not stat.S_ISDIR(receipt_info.st_mode):
                raise SystemExit("package producer-receipt boundary is not a regular directory")
            if receipt_info is not None:
                try:
                    receipt_fd = os.open(
                        receipt_root_name,
                        directory_flags,
                        dir_fd=provenance_fd,
                    )
                except OSError as exc:
                    raise SystemExit(
                        "package producer-receipt boundary cannot be descriptor-pinned"
                    ) from exc
                try:
                    for old_name in os.listdir(receipt_fd):
                        old_info = entry_stat(receipt_fd, old_name)
                        if old_info is None or not stat.S_ISREG(old_info.st_mode):
                            raise SystemExit(
                                "package producer-receipt boundary contains an unsafe entry"
                            )
                finally:
                    os.close(receipt_fd)

            previous_reference = previous.get("production")
            valid_previous_reference = (
                isinstance(previous_reference, dict)
                and set(previous_reference) == {"path", "sha256"}
                and previous_reference.get("path") == PRODUCTION_RECEIPT
                and isinstance(previous_reference.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", previous_reference["sha256"])
            )
            if valid_previous_reference and production_info is not None:
                live_production_sha = bounded_regular_bytes_at(
                    provenance_fd,
                    production_name,
                    max_bytes=PRODUCTION_RECEIPT_MAX_BYTES,
                    description="package production receipt",
                )[1]
                if live_production_sha != previous_reference["sha256"]:
                    raise SystemExit("package production receipt changed after its manifest was read")
            elif not valid_previous_reference and production_info is not None:
                raise SystemExit("package production receipt has no stable prior manifest reference")
            prior = _prior_production_matches(
                package,
                manifest,
                previous,
                provenance_fd=provenance_fd,
            )
            if prior is not None and not publish_manifest:
                return prior

        if package_identity == stage_parent_identity:
            raise SystemExit("package transaction boundary must be outside the package surface")
        stage_name = ""
        for _ in range(128):
            candidate = f".danse-package-transaction-{secrets.token_hex(12)}"
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=stage_parent_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise SystemExit(
                    "package transaction boundary cannot be created safely"
                ) from exc
            stage_name = candidate
            break
        if not stage_name:
            raise SystemExit("package transaction boundary name could not be allocated")
        stage_root = package.parent / stage_name
        try:
            stage_fd = os.open(stage_name, directory_flags, dir_fd=stage_parent_fd)
            stack.callback(os.close, stage_fd)
            opened_stage = os.fstat(stage_fd)
            named_stage = os.stat(
                stage_name,
                dir_fd=stage_parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            try:
                os.rmdir(stage_name, dir_fd=stage_parent_fd)
            except OSError:
                pass
            raise SystemExit("package transaction boundary cannot be descriptor-pinned") from exc
        if (
            not stat.S_ISDIR(opened_stage.st_mode)
            or edge_identity(opened_stage) != edge_identity(named_stage)
            or opened_stage.st_dev != opened_package.st_dev
        ):
            if (
                stat.S_ISDIR(named_stage.st_mode)
                and edge_identity(opened_stage) == edge_identity(named_stage)
            ):
                try:
                    os.rmdir(stage_name, dir_fd=stage_parent_fd)
                except OSError:
                    pass
            raise SystemExit("package transaction boundary changed while being opened")
        stage_identity = edge_identity(opened_stage)

        moves: list[dict[str, object]] = []

        def inode_at(directory: int, name: str) -> tuple[int, int, int] | None:
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return None
            return edge_identity(info)

        def move(
            source_directory: int,
            source: str,
            destination_directory: int,
            destination: str,
            rollback_directory: int,
            rollback: str,
            label: str,
        ) -> None:
            identity = inode_at(source_directory, source)
            if identity is None:
                raise OSError(f"{label} source disappeared before publication")
            record = {
                "source_directory": source_directory,
                "source": source,
                "destination_directory": destination_directory,
                "destination": destination,
                "rollback_directory": rollback_directory,
                "rollback": rollback,
                "identity": identity,
                "label": label,
            }
            moves.append(record)
            try:
                atomic_rename_noreplace(
                    source,
                    destination,
                    src_dir_fd=source_directory,
                    dst_dir_fd=destination_directory,
                )
            finally:
                # State is derived from the inode after the syscall, so an
                # interrupt delivered after rename cannot hide a completed move.
                record["moved"] = (
                    inode_at(destination_directory, destination) == identity
                    and inode_at(source_directory, source) != identity
                )

        def stage_edge_is_original() -> bool:
            info = entry_stat(stage_parent_fd, stage_name)
            return (
                info is not None
                and stat.S_ISDIR(info.st_mode)
                and edge_identity(info) == stage_identity
            )

        def recovery_notice() -> str:
            if stage_edge_is_original():
                return f"recovery preserved at {stage_root}"
            return (
                "recovery preserved in displaced originally opened "
                f"stage inode {opened_stage.st_dev}:{opened_stage.st_ino}; "
                f"unrelated named entry preserved at {stage_root}"
            )

        def clear_directory(directory: int) -> None:
            for name in os.listdir(directory):
                if not stage_edge_is_original():
                    raise OSError("transaction boundary changed during cleanup")
                info = entry_stat(directory, name)
                if info is None:
                    raise OSError("transaction entry disappeared during cleanup")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, directory_flags, dir_fd=directory)
                    try:
                        opened_child = os.fstat(child)
                        if edge_identity(opened_child) != edge_identity(info):
                            raise OSError("transaction directory changed during cleanup")
                        clear_directory(child)
                    finally:
                        os.close(child)
                    current = entry_stat(directory, name)
                    if current is None or edge_identity(current) != edge_identity(info):
                        raise OSError("transaction directory changed during cleanup")
                    if not stage_edge_is_original():
                        raise OSError("transaction boundary changed during cleanup")
                    os.rmdir(name, dir_fd=directory)
                else:
                    current = entry_stat(directory, name)
                    if current is None or edge_identity(current) != edge_identity(info):
                        raise OSError("transaction entry changed during cleanup")
                    if not stage_edge_is_original():
                        raise OSError("transaction boundary changed during cleanup")
                    os.unlink(name, dir_fd=directory)

        def cleanup_transaction(context: str) -> None:
            if not stage_edge_is_original():
                raise SystemExit(f"{context}; {recovery_notice()}")
            try:
                clear_directory(stage_fd)
                if not stage_edge_is_original():
                    raise OSError("transaction boundary changed during cleanup")
                os.rmdir(stage_name, dir_fd=stage_parent_fd)
            except BaseException as cleanup_exc:
                raise SystemExit(f"{context}; {recovery_notice()}") from cleanup_exc

        reference: dict | None = prior
        try:
            staged_receipt_root_name = "producer-receipts.new"
            staged_production_name = "production.new.json"
            staged_manifest_name = "manifest.new.json"
            expected_receipts: dict[str, str] = {}
            expected_production_sha = None
            staged_receipt_fd = None

            if targets and prior is None:
                # Authenticate and stage the complete replacement graph before
                # touching the currently receipted producer set.
                os.mkdir(staged_receipt_root_name, mode=0o755, dir_fd=stage_fd)
                staged_receipt_fd = os.open(
                    staged_receipt_root_name,
                    directory_flags,
                    dir_fd=stage_fd,
                )
                stack.callback(os.close, staged_receipt_fd)
                opened_staged_receipts = os.fstat(staged_receipt_fd)
                named_staged_receipts = os.stat(
                    staged_receipt_root_name,
                    dir_fd=stage_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(opened_staged_receipts.st_mode)
                    or edge_identity(opened_staged_receipts)
                    != edge_identity(named_staged_receipts)
                    or stat.S_IMODE(opened_staged_receipts.st_mode) != 0o755
                ):
                    raise OSError("staged producer receipt boundary is unsafe")
                producers: dict[str, dict] = {}
                producer_receipt_bytes = 0

                def add_receipt(receipt_path: Path, kind: str) -> str:
                    nonlocal producer_receipt_bytes
                    schema = {
                        "render-segment": "danse.render.segment.v1",
                        "render-concat": "danse.render.concat.v1",
                        "score": "danse.score.receipt.v2",
                    }[kind]
                    remaining_receipt_bytes = (
                        PRODUCER_RECEIPT_TOTAL_MAX_BYTES - producer_receipt_bytes
                    )
                    value, receipt_sha, receipt_bytes = _read_producer_receipt(
                        receipt_path,
                        schema,
                        max_bytes=remaining_receipt_bytes,
                    )
                    producer_receipt_bytes += receipt_bytes
                    if kind == "render-segment":
                        inputs = (
                            value.get("inputs")
                            if isinstance(value.get("inputs"), dict)
                            else {}
                        )
                        if (
                            inputs.get("source_tree_sha256")
                            != renderer_source_sha256(manifest["corpus_tier"])
                            or inputs.get("tier") != manifest["corpus_tier"]
                        ):
                            raise SystemExit(
                                "render segment receipt source identity is stale: "
                                f"{receipt_path.name}"
                            )
                    producer_id = f"{kind}-{receipt_sha[:20]}"
                    if producer_id in producers:
                        if producers[producer_id]["receipt"]["sha256"] != receipt_sha:
                            raise SystemExit("producer receipt identity prefix collision")
                        return producer_id
                    components: list[str] = []
                    if kind == "render-concat":
                        segments = value.get("segments")
                        if not isinstance(segments, list) or not segments:
                            raise SystemExit(
                                "render concat receipt has no segment chain: "
                                f"{receipt_path.name}"
                            )
                        if len(segments) > PRODUCER_CONCAT_SEGMENT_MAX_COUNT:
                            raise SystemExit(
                                "render concat receipt exceeds its segment-count limit: "
                                f"{receipt_path.name}"
                            )
                        segment_names = []
                        for segment in segments:
                            name = segment.get("name") if isinstance(segment, dict) else None
                            if not isinstance(name, str) or Path(name).name != name:
                                raise SystemExit(
                                    "render concat receipt has an unsafe segment: "
                                    f"{receipt_path.name}"
                                )
                            segment_names.append(name)
                        if len(set(segment_names)) != len(segment_names):
                            raise SystemExit(
                                "render concat receipt repeats a segment: "
                                f"{receipt_path.name}"
                            )
                        for segment, name in zip(segments, segment_names, strict=True):
                            segment_path = receipt_path.parent / f"{name}.receipt.json"
                            component = add_receipt(segment_path, "render-segment")
                            if producers[component]["receipt"]["sha256"] != segment.get(
                                "receipt_sha256"
                            ):
                                raise SystemExit(
                                    "render concat receipt segment digest is stale: "
                                    f"{receipt_path.name}"
                                )
                            components.append(component)
                    output_sha = (
                        value.get("sha256")
                        if kind == "score"
                        else value.get("file_sha256")
                    )
                    if not isinstance(output_sha, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", output_sha
                    ):
                        raise SystemExit(
                            f"producer receipt has no output digest: {receipt_path.name}"
                        )
                    canonical_destination = receipt_root / f"{producer_id}.json"
                    document, copied_sha = bounded_regular_bytes(
                        receipt_path,
                        max_bytes=min(
                            receipt_bytes,
                            PRODUCER_RECEIPT_MAX_BYTES,
                        ),
                        description="producer receipt",
                    )
                    if copied_sha != receipt_sha or len(document) != receipt_bytes:
                        raise SystemExit(
                            f"producer receipt copy changed bytes: {receipt_path.name}"
                        )
                    assert staged_receipt_fd is not None
                    staged_sha = write_new_regular_bytes_at(
                        staged_receipt_fd,
                        canonical_destination.name,
                        document,
                    )
                    if staged_sha != receipt_sha:
                        raise SystemExit(
                            f"producer receipt copy changed bytes: {receipt_path.name}"
                        )
                    expected_receipts[canonical_destination.name] = receipt_sha
                    producers[producer_id] = {
                        "id": producer_id,
                        "kind": kind,
                        "receipt": {
                            "path": canonical_destination.relative_to(package).as_posix(),
                            "sha256": receipt_sha,
                        },
                        "output_sha256": output_sha,
                        "components": components,
                    }
                    return producer_id

                score_id = None
                if SCORE_SOURCE_ITEM in targets or any(
                    name in AUDIO_ITEMS for name in targets
                ):
                    score_id = add_receipt(
                        score_receipt_path(render_root / "passage-score.wav"),
                        "score",
                    )
                picture_id = None
                if any(name in AUDIO_ITEMS - {REEL_ITEM} for name in targets):
                    picture_id = add_receipt(
                        render_root / "passage-default.mov.receipt.json",
                        "render-concat",
                    )
                reel_id = None
                if REEL_ITEM in targets:
                    reel_id = add_receipt(
                        render_root / "reel-provenance/reel-default.mp4.receipt.json",
                        "render-concat",
                    )

                outputs = []
                for name, item in sorted(targets.items()):
                    if name == SCORE_SOURCE_ITEM:
                        producer_ids = [score_id]
                    elif name == REEL_ITEM:
                        producer_ids = [reel_id, score_id]
                    elif name in AUDIO_ITEMS:
                        producer_ids = [picture_id, score_id]
                    else:
                        seed = int(Path(name).stem.removeprefix("seed-"), 0)
                        matches = sorted(
                            render_root.glob(f"passage-{seed}-seg-*.mov.receipt.json")
                        )
                        if len(matches) != 1:
                            raise SystemExit(
                                f"generated still has no unique render receipt: {name}"
                            )
                        producer_ids = [add_receipt(matches[0], "render-segment")]
                    if any(value is None for value in producer_ids):
                        raise SystemExit(
                            f"rendered package output has incomplete producer evidence: {name}"
                        )
                    outputs.append(
                        {
                            "name": name,
                            "bytes": item["bytes"],
                            "sha256": item["sha256"],
                            "producers": producer_ids,
                        }
                    )

                production = {
                    "schema": "danse.delivery.production.v1",
                    "repository_head": repository_head,
                    "source_tree_sha256": source_tree,
                    "passage": passage,
                    "sound": manifest.get("sound"),
                    "producers": [producers[key] for key in sorted(producers)],
                    "outputs": outputs,
                }
                production_bytes = (json.dumps(production, indent=2) + "\n").encode()
                if len(production_bytes) > PRODUCTION_RECEIPT_MAX_BYTES:
                    raise SystemExit("package production receipt exceeds its safe byte limit")
                expected_production_sha = write_new_regular_bytes_at(
                    stage_fd,
                    staged_production_name,
                    production_bytes,
                )
                reference = {
                    "path": PRODUCTION_RECEIPT,
                    "sha256": expected_production_sha,
                }
            elif targets and prior is not None:
                assert provenance_fd is not None
                production_bytes, expected_production_sha = bounded_regular_bytes_at(
                    provenance_fd,
                    production_name,
                    max_bytes=PRODUCTION_RECEIPT_MAX_BYTES,
                    description="reused package production receipt",
                )
                if expected_production_sha != prior["sha256"]:
                    raise OSError("reused production receipt changed while being staged")
                staged_sha = write_new_regular_bytes_at(
                    stage_fd,
                    staged_production_name,
                    production_bytes,
                )
                if staged_sha != expected_production_sha:
                    raise OSError("reused production receipt changed while being staged")

            expected_manifest_sha = None
            if publish_manifest:
                if reference is not None:
                    manifest["production"] = reference
                else:
                    manifest.pop("production", None)
                manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
                if len(manifest_bytes) > PACKAGE_MANIFEST_MAX_BYTES:
                    raise SystemExit("package manifest exceeds its safe byte limit")
                expected_manifest_sha = write_new_regular_bytes_at(
                    stage_fd,
                    staged_manifest_name,
                    manifest_bytes,
                )

            old_manifest = "manifest.old.json"
            old_production = "production.old.json"
            old_receipts = "producer-receipts.old"
            failed_manifest = "manifest.failed.json"
            failed_production = "production.failed.json"
            failed_receipts = "producer-receipts.failed"

            def validate_published_production_graph() -> None:
                assert provenance_fd is not None
                require_provenance_edge()
                production_info = entry_stat(provenance_fd, production_name)
                if (
                    production_info is None
                    or not stat.S_ISREG(production_info.st_mode)
                    or stat.S_IMODE(production_info.st_mode) != 0o644
                    or bounded_regular_bytes_at(
                        provenance_fd,
                        production_name,
                        max_bytes=PRODUCTION_RECEIPT_MAX_BYTES,
                        description="published package production receipt",
                    )[1]
                    != expected_production_sha
                ):
                    raise OSError("published package production receipt changed bytes")
                if prior is not None:
                    return
                receipt_info = entry_stat(provenance_fd, receipt_root_name)
                if receipt_info is None or not stat.S_ISDIR(receipt_info.st_mode):
                    raise OSError("published package producer receipt boundary is unsafe")
                try:
                    receipt_fd = os.open(
                        receipt_root_name,
                        directory_flags,
                        dir_fd=provenance_fd,
                    )
                except OSError as receipt_exc:
                    raise OSError(
                        "published package producer receipt boundary is unsafe"
                    ) from receipt_exc
                try:
                    opened_receipts = os.fstat(receipt_fd)
                    if (
                        edge_identity(opened_receipts) != edge_identity(receipt_info)
                        or stat.S_IMODE(opened_receipts.st_mode) != 0o755
                    ):
                        raise OSError(
                            "published package producer receipt boundary is unsafe"
                        )
                    published_receipts = {}
                    for candidate_name in os.listdir(receipt_fd):
                        candidate_info = entry_stat(receipt_fd, candidate_name)
                        if (
                            candidate_info is None
                            or not stat.S_ISREG(candidate_info.st_mode)
                            or stat.S_IMODE(candidate_info.st_mode) != 0o644
                        ):
                            raise OSError(
                                "published package producer receipt graph is unsafe"
                            )
                        published_receipts[candidate_name] = bounded_regular_bytes_at(
                            receipt_fd,
                            candidate_name,
                            max_bytes=PRODUCER_RECEIPT_MAX_BYTES,
                            description="published package producer receipt",
                        )[1]
                finally:
                    os.close(receipt_fd)
                if published_receipts != expected_receipts:
                    raise OSError("published package producer receipt graph changed bytes")

            require_package_edge()
            if targets:
                require_provenance_edge()
            if publish_manifest:
                manifest_info = entry_stat(directory_fd, manifest_name)
                live_manifest_sha = (
                    bounded_regular_bytes_at(
                        directory_fd,
                        manifest_name,
                        max_bytes=PACKAGE_MANIFEST_MAX_BYTES,
                        description="package manifest",
                    )[1]
                    if manifest_info is not None and stat.S_ISREG(manifest_info.st_mode)
                    else None
                )
                if live_manifest_sha != previous_manifest_sha256:
                    raise OSError("package manifest changed before publication")
            if targets and prior is not None:
                assert provenance_fd is not None
                live_production_sha = bounded_regular_bytes_at(
                    provenance_fd,
                    production_name,
                    max_bytes=PRODUCTION_RECEIPT_MAX_BYTES,
                    description="package production receipt",
                )[1]
                if live_production_sha != prior["sha256"]:
                    raise OSError("reused production receipt changed before manifest commit")
            if publish_manifest and entry_stat(directory_fd, manifest_name) is not None:
                move(
                    directory_fd,
                    manifest_name,
                    stage_fd,
                    old_manifest,
                    directory_fd,
                    manifest_name,
                    "prior package manifest",
                )
            if targets:
                assert provenance_fd is not None
                if entry_stat(provenance_fd, production_name) is not None:
                    move(
                        provenance_fd,
                        production_name,
                        stage_fd,
                        old_production,
                        provenance_fd,
                        production_name,
                        "prior production receipt",
                    )
                if prior is None and entry_stat(provenance_fd, receipt_root_name) is not None:
                    move(
                        provenance_fd,
                        receipt_root_name,
                        stage_fd,
                        old_receipts,
                        provenance_fd,
                        receipt_root_name,
                        "prior producer receipts",
                    )
                if prior is None:
                    move(
                        stage_fd,
                        staged_receipt_root_name,
                        provenance_fd,
                        receipt_root_name,
                        stage_fd,
                        failed_receipts,
                        "new producer receipts",
                    )
                move(
                    stage_fd,
                    staged_production_name,
                    provenance_fd,
                    production_name,
                    stage_fd,
                    failed_production,
                    "new production receipt",
                )
                # Authenticate the graph after every publication rename and
                # before its manifest is allowed to become the commit point.
                validate_published_production_graph()
            if publish_manifest:
                # The named provenance edge is part of the graph the manifest
                # is about to publish.  A rename through the pinned descriptor
                # may be safe while a concurrent path swap would make the
                # manifest point somewhere else, so reject that split view.
                require_package_edge()
                if targets:
                    require_provenance_edge()
                move(
                    stage_fd,
                    staged_manifest_name,
                    directory_fd,
                    manifest_name,
                    stage_fd,
                    failed_manifest,
                    "new package manifest",
                )
            require_package_edge()
            if targets:
                # Revalidate after the final manifest publication as well.  A
                # mutation in this interval rolls the manifest and graph back
                # through their transaction-owned inodes.
                validate_published_production_graph()
            if publish_manifest:
                manifest_info = entry_stat(directory_fd, manifest_name)
                if (
                    manifest_info is None
                    or not stat.S_ISREG(manifest_info.st_mode)
                    or stat.S_IMODE(manifest_info.st_mode) != 0o644
                    or bounded_regular_bytes_at(
                        directory_fd,
                        manifest_name,
                        max_bytes=PACKAGE_MANIFEST_MAX_BYTES,
                        description="published package manifest",
                    )[1]
                    != expected_manifest_sha
                ):
                    raise OSError("published package manifest changed bytes")
        except BaseException as exc:
            rollback_errors = []
            for record in reversed(moves):
                source_directory = record["source_directory"]
                source = record["source"]
                destination_directory = record["destination_directory"]
                destination = record["destination"]
                rollback_directory = record["rollback_directory"]
                rollback = record["rollback"]
                identity = record["identity"]
                label = record["label"]
                source_identity = inode_at(source_directory, source)
                destination_identity = inode_at(destination_directory, destination)
                if source_identity == identity and destination_identity != identity:
                    continue
                if destination_identity != identity or source_identity == identity:
                    rollback_errors.append(f"{label}: publication state is ambiguous")
                    continue
                restored = False
                try:
                    try:
                        atomic_rename_noreplace(
                            destination,
                            rollback,
                            src_dir_fd=destination_directory,
                            dst_dir_fd=rollback_directory,
                        )
                    finally:
                        restored = (
                            inode_at(rollback_directory, rollback) == identity
                            and inode_at(destination_directory, destination) != identity
                        )
                except BaseException as rollback_exc:
                    if not restored:
                        rollback_errors.append(f"{label}: {rollback_exc}")
                else:
                    if not restored:
                        rollback_errors.append(f"{label}: rollback did not restore its inode")
            if rollback_errors:
                detail = "; ".join(rollback_errors)
                raise SystemExit(
                    "package receipt transaction failed; rollback failed: "
                    f"{detail}; {recovery_notice()}"
                ) from exc
            cleanup_transaction("package receipt transaction failed after rollback")
            if isinstance(exc, OSError):
                raise SystemExit("package producer receipt replacement failed") from exc
            raise
        else:
            cleanup_transaction("package receipt transaction committed")
            return reference


@functools.lru_cache(maxsize=1)
def score_motion_contract():
    """Load the portable A/B authenticator without making delivery import scripts."""
    path = DANSE / "scripts" / "score_motion_production.py"
    spec = importlib.util.spec_from_file_location("danse_delivery_score_motion_contract", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load the production score-to-motion evidence contract")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_score_motion_directory(package: Path, directory: Path) -> Path:
    """Create one evidence directory without traversing a package symlink."""
    package = package.absolute()
    directory = directory.absolute()
    if package.is_symlink() or not package.is_dir():
        raise SystemExit("package score-to-motion evidence root is not a regular directory")
    try:
        relative = directory.relative_to(package)
    except ValueError as exc:
        raise SystemExit("package score-to-motion evidence destination escapes the package") from exc
    current = package
    for part in relative.parts:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise SystemExit("package score-to-motion evidence destination is unsafe")
        current.mkdir(exist_ok=True)
    return current


def prior_score_motion_evidence(
    package: Path,
    previous: dict,
    items: dict[str, dict],
) -> dict | None:
    """Retain one prior evidence reference only from stable in-package bytes."""
    reference = previous.get("score_motion_evidence")
    if (
        not isinstance(reference, dict)
        or set(reference) != {"path", "sha256"}
        or reference.get("path") != SCORE_MOTION_EVIDENCE_ITEM
        or not isinstance(reference.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"])
    ):
        return None
    item = items.get(SCORE_MOTION_EVIDENCE_ITEM)
    if not isinstance(item, dict):
        return None

    common_flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | common_flags | getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    directory_edges: list[tuple[int, str, os.stat_result]] = []
    try:
        root_fd = os.open(package, directory_flags)
        descriptors.append(root_fd)
        root_info = os.fstat(root_fd)
        named_root = os.stat(package, follow_symlinks=False)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or (root_info.st_dev, root_info.st_ino) != (named_root.st_dev, named_root.st_ino)
        ):
            return None

        parent_fd = root_fd
        parts = PurePosixPath(SCORE_MOTION_EVIDENCE_ITEM).parts
        for part in parts[:-1]:
            child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            descriptors.append(child_fd)
            child_info = os.fstat(child_fd)
            if not stat.S_ISDIR(child_info.st_mode):
                return None
            directory_edges.append((parent_fd, part, child_info))
            parent_fd = child_fd

        receipt_fd = os.open(parts[-1], os.O_RDONLY | common_flags, dir_fd=parent_fd)
        descriptors.append(receipt_fd)
        before = os.fstat(receipt_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > SCORE_MOTION_RECEIPT_MAX_BYTES
        ):
            return None
        receipt_digest = hashlib.sha256()
        copied = 0
        while True:
            block = os.read(
                receipt_fd,
                min(1 << 20, SCORE_MOTION_RECEIPT_MAX_BYTES + 1 - copied),
            )
            if not block:
                break
            receipt_digest.update(block)
            copied += len(block)
            if copied > SCORE_MOTION_RECEIPT_MAX_BYTES:
                return None
        after = os.fstat(receipt_fd)

        def identity(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
        if identity(before) != identity(after) or copied != after.st_size:
            return None
        named_receipt = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if identity(after) != identity(named_receipt):
            return None
        for ancestor_fd, name, opened in directory_edges:
            named = os.stat(name, dir_fd=ancestor_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino, opened.st_mode) != (
                named.st_dev,
                named.st_ino,
                named.st_mode,
            ):
                return None
        digest_value = receipt_digest.hexdigest()
        if (
            digest_value != reference["sha256"]
            or item.get("sha256") != digest_value
            or item.get("bytes") != copied
        ):
            return None
        return dict(reference)
    except OSError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def prune_score_motion_evidence(destination_root: Path, expected: set[str]) -> None:
    """Make the owned evidence subtree equal the current graph, never a union."""
    expected_directories = {
        parent.as_posix()
        for relative in expected
        for parent in PurePosixPath(relative).parents
        if parent != PurePosixPath(".")
    }
    try:
        surface = sorted(
            destination_root.rglob("*"),
            key=lambda candidate: (len(candidate.parts), candidate.as_posix()),
            reverse=True,
        )
    except OSError as exc:
        raise SystemExit("package score-to-motion evidence boundary cannot be inventoried") from exc
    for candidate in surface:
        relative = candidate.relative_to(destination_root).as_posix()
        if candidate.is_symlink():
            raise SystemExit("package score-to-motion evidence boundary contains a symlink")
        if candidate.is_file():
            if relative not in expected:
                candidate.unlink()
        elif candidate.is_dir():
            if relative not in expected_directories:
                try:
                    candidate.rmdir()
                except OSError as exc:
                    raise SystemExit(
                        "package score-to-motion evidence boundary contains an unsafe entry"
                    ) from exc
        else:
            raise SystemExit("package score-to-motion evidence boundary contains an unsafe entry")


def require_score_motion_evidence_census(destination_root: Path, expected: set[str]) -> None:
    """Authenticate the final owned subtree after every atomic replacement."""
    expected_directories = {
        parent.as_posix()
        for relative in expected
        for parent in PurePosixPath(relative).parents
        if parent != PurePosixPath(".")
    }
    files: set[str] = set()
    directories: set[str] = set()
    try:
        surface = destination_root.rglob("*")
        for candidate in surface:
            relative = candidate.relative_to(destination_root).as_posix()
            if candidate.is_symlink():
                raise SystemExit("package score-to-motion evidence boundary contains a symlink")
            if candidate.is_file():
                files.add(relative)
            elif candidate.is_dir():
                directories.add(relative)
            else:
                raise SystemExit("package score-to-motion evidence boundary contains an unsafe entry")
    except OSError as exc:
        raise SystemExit("package score-to-motion evidence boundary cannot be inventoried") from exc
    if files != expected or directories != expected_directories:
        raise SystemExit("package score-to-motion evidence census is not exact")


def atomic_stage_score_motion_file(package: Path, source: Path, destination: Path) -> None:
    """Replace one evidence path without following links or rewriting a shared inode."""
    safe_score_motion_directory(package, destination.parent)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise SystemExit("package score-to-motion evidence destination is unsafe")

    common_flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | common_flags | getattr(os, "O_DIRECTORY", 0)
    directory_fd = source_fd = temporary_fd = result_fd = None
    temporary_name: str | None = None
    replaced = False
    try:
        directory_fd = os.open(destination.parent, directory_flags)
        opened_directory = os.fstat(directory_fd)
        safe_score_motion_directory(package, destination.parent)
        named_directory = os.stat(destination.parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened_directory.st_mode)
            or (opened_directory.st_dev, opened_directory.st_ino)
            != (named_directory.st_dev, named_directory.st_ino)
        ):
            raise SystemExit("package score-to-motion evidence destination changed during staging")

        source_fd = os.open(source, os.O_RDONLY | common_flags)
        source_before = os.fstat(source_fd)
        if not stat.S_ISREG(source_before.st_mode):
            raise SystemExit("score-to-motion evidence source is not a regular file")
        for _ in range(16):
            candidate = f".{destination.name}.stage-{os.getpid()}-{os.urandom(8).hex()}"
            try:
                temporary_fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | common_flags,
                    (stat.S_IMODE(source_before.st_mode) & 0o777) | 0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_fd is None or temporary_name is None:
            raise SystemExit("cannot reserve a score-to-motion evidence staging file")

        source_digest = hashlib.sha256()
        copied = 0
        while True:
            block = os.read(source_fd, 1 << 20)
            if not block:
                break
            source_digest.update(block)
            copied += len(block)
            remaining = memoryview(block)
            while remaining:
                written = os.write(temporary_fd, remaining)
                if written <= 0:
                    raise OSError("short score-to-motion evidence write")
                remaining = remaining[written:]
        source_after = os.fstat(source_fd)
        source_before_identity = (
            source_before.st_dev,
            source_before.st_ino,
            source_before.st_mode,
            source_before.st_size,
            source_before.st_mtime_ns,
            source_before.st_ctime_ns,
        )
        source_after_identity = (
            source_after.st_dev,
            source_after.st_ino,
            source_after.st_mode,
            source_after.st_size,
            source_after.st_mtime_ns,
            source_after.st_ctime_ns,
        )
        if (
            source_before_identity != source_after_identity
            or copied != source_after.st_size
        ):
            raise SystemExit("score-to-motion evidence source changed during staging")
        os.close(temporary_fd)
        temporary_fd = None
        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        replaced = True

        result_fd = os.open(destination.name, os.O_RDONLY | common_flags, dir_fd=directory_fd)
        result_info = os.fstat(result_fd)
        if not stat.S_ISREG(result_info.st_mode):
            raise SystemExit("package score-to-motion evidence destination is unsafe")
        result_digest = hashlib.sha256()
        result_bytes = 0
        while True:
            block = os.read(result_fd, 1 << 20)
            if not block:
                break
            result_digest.update(block)
            result_bytes += len(block)
        if result_bytes != copied or result_digest.digest() != source_digest.digest():
            raise SystemExit("package score-to-motion evidence copy changed bytes")

        safe_score_motion_directory(package, destination.parent)
        named_directory = os.stat(destination.parent, follow_symlinks=False)
        named_result = os.stat(destination.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            (opened_directory.st_dev, opened_directory.st_ino)
            != (named_directory.st_dev, named_directory.st_ino)
            or (result_info.st_dev, result_info.st_ino)
            != (named_result.st_dev, named_result.st_ino)
        ):
            raise SystemExit("package score-to-motion evidence destination changed during staging")
    except OSError as exc:
        raise SystemExit("package score-to-motion evidence could not be copied safely") from exc
    finally:
        for descriptor in (result_fd, temporary_fd, source_fd):
            if descriptor is not None:
                os.close(descriptor)
        if directory_fd is not None:
            if temporary_name is not None and not replaced:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            os.close(directory_fd)


def stage_score_motion_evidence(
    package: Path,
    span: dict,
    repository_head: str,
) -> tuple[dict | None, list[Path]]:
    """Copy only a complete, current evidence graph; absence stays a later gate."""
    destination_root = safe_score_motion_directory(
        package,
        package / SCORE_MOTION_EVIDENCE_DIR,
    )
    if not SCORE_MOTION_EVIDENCE.is_file():
        prune_score_motion_evidence(destination_root, set())
        destination_root.rmdir()
        return None, []
    contract = score_motion_contract()
    errors = contract.production_receipt_errors(SCORE_MOTION_EVIDENCE)
    if errors:
        raise SystemExit("production score-to-motion evidence is stale: " + "; ".join(errors[:6]))
    receipt = json.loads(SCORE_MOTION_EVIDENCE.read_text(encoding="utf-8"))
    expected_span = {
        "river_seed": 20170620,
        "stream": 0,
        "passage": span["passage"],
        "t0": span["t0"],
        "t1": span["t1"],
        "duration_seconds": span["duration"],
    }
    receipt_span = receipt.get("span")
    comparable_span = isinstance(receipt_span, dict) and set(receipt_span) == set(expected_span)
    if comparable_span:
        comparable_span = all(
            receipt_span[key] == expected_span[key]
            for key in expected_span
            if key != "duration_seconds"
        )
    receipt_duration = receipt_span.get("duration_seconds") if isinstance(receipt_span, dict) else None
    if (
        not isinstance(receipt_duration, (int, float))
        or isinstance(receipt_duration, bool)
        or not math.isfinite(receipt_duration)
        or abs(receipt_duration - expected_span["duration_seconds"]) > 1e-6
    ):
        comparable_span = False
    if receipt.get("repository_head") != repository_head or not comparable_span:
        raise SystemExit("production score-to-motion evidence belongs to a different package span or Git HEAD")
    source_root = SCORE_MOTION_EVIDENCE.parent.resolve(strict=True)
    sources = contract.evidence_artifact_paths(SCORE_MOTION_EVIDENCE)
    expected = {source.relative_to(source_root).as_posix() for source in sources}
    prune_score_motion_evidence(destination_root, expected)
    staged = []
    for source in sources:
        relative = source.relative_to(source_root)
        destination = destination_root / relative
        atomic_stage_score_motion_file(package, source, destination)
        staged.append(destination)
    prune_score_motion_evidence(destination_root, expected)
    require_score_motion_evidence_census(destination_root, expected)
    receipt_copy = package / SCORE_MOTION_EVIDENCE_ITEM
    staged_errors = contract.production_receipt_errors(receipt_copy)
    if staged_errors:
        raise SystemExit(
            "staged production score-to-motion evidence is inconsistent: "
            + "; ".join(staged_errors[:6])
        )
    return {"path": SCORE_MOTION_EVIDENCE_ITEM, "sha256": digest(receipt_copy)}, staged


def capture_root(root: Path, span: dict, start: float) -> Path:
    """Keep restartable intermediates for different absolute spans disjoint."""
    offset = f"{start:.3f}".rstrip("0").rstrip(".").replace("-", "m").replace(".", "p") or "0"
    return root / f"passage-{span['seed']:08X}-from-{offset}"


def retained_capture_root(root: Path, manifest: dict) -> Path:
    """Recover the restart root for passage media retained by a partial update."""
    passage_seed = manifest.get("passage_seed")
    t0 = manifest.get("t0")
    if not isinstance(passage_seed, str) or not re.fullmatch(
        r"0x[0-9A-Fa-f]{1,8}",
        passage_seed,
    ):
        raise SystemExit("retained package has no valid passage-seed capture identity")
    if (
        not isinstance(t0, (int, float))
        or isinstance(t0, bool)
        or not math.isfinite(t0)
        or t0 < 0
    ):
        raise SystemExit("retained package has no valid capture start identity")
    return capture_root(root, {"seed": int(passage_seed, 16)}, float(t0))


def is_forced(force: set[str], name: str, group: str | None = None) -> bool:
    return name in force or (group is not None and group in force)


def recognized_package_media(package: Path) -> list[Path]:
    """Known delivery media that cannot be adopted without a manifest."""
    paths = [
        package / name
        for name in sorted(AUDIO_ITEMS | {SCORE_SOURCE_ITEM, AUDIO_RENDER_SOURCE_ITEM})
    ]
    stills = package / "stills"
    if stills.is_dir():
        paths.extend(stills.glob("seed-0x*.*"))
        paths.append(stills / "origin-2017.jpg")
    return sorted({path for path in paths if path.is_file()})


def regular_directory_slot(path: Path) -> bool:
    """True when a delivery directory is absent or a real directory."""
    return not path.is_symlink() and (not path.exists() or path.is_dir())


def package_provenance_matches(
    package: Path,
    span: dict,
    start: float | None = None,
    source_tree_sha256: str | None = None,
    repository_head: str | None = None,
) -> bool:
    manifest = package / "manifest.json"
    if not manifest.is_file():
        return not recognized_package_media(package)
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    items = data.get("items", [])
    if not isinstance(items, list):
        return False
    item_names = {
        item.get("name") for item in items if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    passage_items = {name for name in item_names if name in AUDIO_ITEMS or name.startswith("stills/seed-")}
    if not passage_items:
        unmanifested_passage_media = [
            path
            for path in recognized_package_media(package)
            if path.name != "origin-2017.jpg"
        ]
        return not unmanifested_passage_media
    try:
        passage_matches = (
            data.get("passage_seed") == hexseed(span["seed"])
            and data.get("passage") == span["passage"]
            and abs(float(data.get("t0", -1)) - span["t0"]) < 1e-9
            and abs(float(data.get("t1", -1)) - span["t1"]) < 1e-9
            and abs(float(data.get("duration", -1)) - span["duration"]) < 1e-3
        )
        start_matches = not (passage_items & FIXED_WINDOW_ITEMS) or (
            start is not None and abs(float(data.get("start", -1)) - start) < 1e-9
        )
        source_matches = source_tree_sha256 is None or data.get("source_tree_sha256") == source_tree_sha256
        repository_matches = repository_head is None or data.get("repository_head") == repository_head
        return passage_matches and start_matches and source_matches and repository_matches
    except (TypeError, ValueError):
        return False


def still_destinations(program: dict, package: Path) -> list[Path]:
    sys.path.insert(0, str(DANSE / "sound"))
    from rng import hash32

    return [
        package / "stills" / f"seed-{hexseed(hash32(program['seed'], 0x57111, i) & 0xFFFFFF)}.jpg"
        for i in range(len(STILL_FRACTIONS))
    ]


def pending(program: dict, only: set[str], force: set[str], package: Path) -> dict:
    """The outputs that would actually be rebuilt for this invocation."""
    score_forced = "master" in force
    derived = {
        name
        for name, spec in DERIVED.items()
        if "derived" in only
        and (
            score_forced
            or is_forced(force, name, "derived")
            or not (package / f"{name}{spec['suffix']}").is_file()
        )
    }
    stills = still_destinations(program, package)
    return {
        "master": "master" in only and (is_forced(force, "master") or not (package / "master.mov").is_file()),
        "derived": derived,
        "reel": "reel" in only
        and (score_forced or is_forced(force, "reel") or not (package / REEL_ITEM).is_file()),
        "stills": "stills" in only and (is_forced(force, "stills") or not all(path.is_file() for path in stills)),
    }


def expand_rebuilt_score_dependents(work: dict, only: set[str]) -> None:
    """A new score invalidates every selected artifact that embeds its bytes."""
    if "master" in only:
        work["master"] = True
    if "derived" in only:
        work["derived"] = set(DERIVED)
    if "reel" in only:
        work["reel"] = True


def capture_span_error(name: str, passage_span: dict, start: float) -> str | None:
    """Explain when a fixed capture would overrun its selected passage."""
    span = query_capture_span(name, start=start)
    if span["t0"] < passage_span["t0"] - 1e-9 or span["t1"] > passage_span["t1"] + 1e-9:
        return (
            f"{name} [{span['t0']:.3f}, {span['t1']:.3f}] does not fit passage "
            f"[{passage_span['t0']:.3f}, {passage_span['t1']:.3f}]"
        )
    return None


def preflight(
    program: dict,
    span: dict | None,
    only: set[str],
    force: set[str],
    tier: str,
    render_root: Path,
    package: Path,
    origin: Path | None,
    start: float = 0.0,
    span_error: str | None = None,
    passage_requested: bool = True,
) -> int:
    """Validate a delivery invocation without creating a directory or rendering."""
    rows: list[tuple[bool, str, str]] = []

    def add(ok: bool, name: str, detail: str) -> None:
        rows.append((ok, name, detail))

    package_root_ok = regular_directory_slot(package)
    add(package_root_ok, "package root", str(package))
    try:
        repository = repository_state()
    except SystemExit as exc:
        repository = None
        repository_detail = str(exc)
    else:
        repository_detail = (
            repository["head"]
            if repository["clean"]
            else f"{repository['head']} · {len(repository['changes'])} repository change(s)"
        )
    add(
        repository is not None and repository["clean"],
        "exact repository head",
        repository_detail,
    )
    add(program.get("schema") == "danse.program.v2", "program", str(program.get("schema")))
    add(
        not passage_requested or program.get("seed") == 20170620,
        "competition river seed",
        str(program.get("seed")),
    )
    add(
        not passage_requested or abs(start) <= 1e-9,
        "competition passage",
        "river seed 20170620 · passage 0 · score time 0"
        if abs(start) <= 1e-9
        else f"production capture rejects --start {start}",
    )
    add(
        not passage_requested or (span is not None and span["duration"] > 0),
        "capture span",
        "not needed for passage-independent outputs"
        if not passage_requested
        else (f"{span['duration']:.3f}s from {span['t0']:.3f}s" if span else span_error or "unavailable"),
    )
    node = shutil.which("node")
    add(not passage_requested or node is not None, "node", node or ("not needed" if not passage_requested else "missing"))
    add(
        not passage_requested
        or (
            package_root_ok
            and span is not None
            and package_provenance_matches(
                package,
                span,
                start,
                delivery_source_sha256(tier),
                repository["head"] if repository and repository["clean"] else None,
            )
        ),
        "package passage provenance",
        "preserved" if not passage_requested else str(package / "manifest.json"),
    )

    work = pending(program, only, force, package)
    span_names = sorted(work["derived"] | ({"reel"} if work["reel"] else set()))
    for name in span_names:
        if span is None:
            add(False, f"{name} fits selected passage", span_error or "capture span unavailable")
            continue
        try:
            error = capture_span_error(name, span, start)
        except SystemExit as exc:
            error = str(exc)
        add(error is None, f"{name} fits selected passage", error or f"within {span['duration']:.3f}s passage")
    need_picture = work["master"] or bool(work["derived"])
    need_score = need_picture or work["reel"]
    need_renderer = work["reel"] or work["stills"]

    picture = render_root / "passage-default.mov"
    picture_info = probe(picture)
    cap = captures(program)["passage"]
    picture_candidate = bool(
        span
        and picture_info
        and abs(picture_info.get("seconds", 0) * cap.get("fps", 30) - span["duration"] * cap.get("fps", 30)) < 2
        and not is_forced(force, "master")
    )
    picture_ready = False
    if need_picture and picture_candidate:
        checked = subprocess.run(
            [
                sys.executable,
                str(RENDER),
                "--capture",
                "passage",
                "--start",
                str(span["t0"]),
                "--tier",
                tier,
                "--codec",
                "prores",
                "--quiet",
                "--score",
                MUSIC_SCORE.relative_to(DANSE).as_posix(),
                "--choreography",
                CHOREOGRAPHY.relative_to(DANSE).as_posix(),
                "--out",
                str(render_root),
                "--check-concat",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        picture_ready = checked.returncode == 0
    need_renderer = need_renderer or (need_picture and not picture_ready)

    score = render_root / "passage-score.wav"
    score_info = probe(score)
    score_ready = bool(
        span
        and score_info
        and abs(score_info.get("seconds", 0) - span["duration"]) < 0.1
        and score_provenance(score, span)
        and not is_forced(force, "master")
    )
    need_audio_render = need_score and not score_ready

    if need_picture or need_score or work["reel"] or work["stills"]:
        for command in ("ffmpeg", "ffprobe"):
            add(shutil.which(command) is not None, command, shutil.which(command) or "missing")

    if only & PASSAGE_SELECTORS:
        tier_ok, tier_detail = authorize_render_tier(DANSE / "corpus", hydrated_work_root(), tier)
        add(tier_ok, f"corpus tier {tier} receipt", tier_detail)

    if need_renderer:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        add(importlib.util.find_spec("playwright") is not None, "Playwright", "Python module")
        add(chrome.is_file(), "Google Chrome", str(chrome))

    if work["stills"]:
        add(importlib.util.find_spec("PIL") is not None, "Python module Pillow", "package still dependency")

    if need_audio_render:
        try:
            provenance = competition_audio_provenance(span) if span else None
        except (SystemExit, OSError, KeyError, TypeError, ValueError) as exc:
            provenance = None
            audio_detail = str(exc)
        else:
            audio_detail = (
                f"{provenance['profile']} · master {provenance['master_sha256'][:16]}… · "
                f"{len(provenance['stems'])} deterministic stems"
                if provenance
                else "capture span unavailable"
            )
        add(provenance is not None, "competition audio receipt", audio_detail)

    if "origin" in only:
        origin_dest = package / "stills" / "origin-2017.jpg"
        origin_slot_ok = (
            package_root_ok
            and regular_directory_slot(origin_dest.parent)
            and not origin_dest.is_symlink()
            and (not origin_dest.exists() or origin_dest.is_file())
        )
        add(origin_slot_ok, "staged origin is a regular file", str(origin_dest))
        need_origin_source = is_forced(force, "origin") or not origin_dest.is_file()
        candidate = origin if need_origin_source else origin_dest
        candidate_exists = (
            origin_slot_ok
            and candidate is not None
            and candidate.is_file()
            and (need_origin_source or not candidate.is_symlink())
        )
        expected_origin = registered_origin_source_sha256()
        add(
            candidate_exists,
            "unaltered origin photograph",
            str(candidate),
        )
        origin_identity_ok = False
        origin_identity_detail = expected_origin
        if candidate_exists:
            try:
                origin_identity_ok = digest(candidate) == expected_origin
            except OSError as exc:
                origin_identity_detail = f"{candidate}: source bytes are unreadable ({exc})"
        add(
            origin_identity_ok,
            "registered origin photograph identity",
            origin_identity_detail,
        )
    if "text" in only:
        text_root = DANSE / "submission" / "text"
        names = {
            "synopsis_short",
            "synopsis_long",
            "artist_statement",
            "bio",
            "technical_note",
            "rights_declaration",
        }
        missing = sorted(name for name in names if not (text_root / f"{name}.txt").is_file())
        add(not missing, "tracked text package", f"{len(names) - len(missing)}/{len(names)} sources")

    print("delivery preflight\n")
    for ok, name, detail in rows:
        print(f"  [{'ok' if ok else 'FAIL':>4}] {name} — {detail}")
    failures = sum(not ok for ok, _, _ in rows)
    print(f"\n{'READY' if not failures else 'NOT READY'} — {failures} failure(s); no files changed")
    return 1 if failures else 0


# ── the expensive half ─────────────────────────────────────────────────────────


def passage_picture(program: dict, tier: str, force: bool, start: float = 0.0) -> Path:
    """Render the primary 4K passage recording, or keep it. `render.py --resume` decides per segment."""
    stem = OUT / "passage-default"
    dest = stem.with_suffix(".mov")
    span = query_capture_span("passage", start=start)
    cap = captures(program)["passage"]
    fps = cap.get("fps", 30)
    want = int(round(span["duration"] * fps))
    render_command = [
        sys.executable,
        str(RENDER),
        "--capture",
        "passage",
        "--start",
        str(span["t0"]),
        "--tier",
        tier,
        "--codec",
        "prores",
        "--quiet",
        "--score",
        MUSIC_SCORE.relative_to(DANSE).as_posix(),
        "--choreography",
        CHOREOGRAPHY.relative_to(DANSE).as_posix(),
        "--out",
        str(OUT),
    ]
    if not force:
        got = probe_required(dest) if dest.is_file() else None
        if got and abs(got["seconds"] * fps - want) < 2:
            checked = subprocess.run([*render_command, "--check-concat"], capture_output=True, text=True, check=False)
            if checked.returncode == 0:
                print(f"  passage picture · kept · {got['width']}×{got['height']} @{got['fps']} · {got['seconds']:.1f}s")
                return dest
    print("  passage picture · rendering (this is the long one)")
    done = subprocess.run(
        [*render_command, "--resume"],
        check=False,
    )
    if done.returncode != 0 or not dest.is_file():
        raise SystemExit("the passage picture would not render")
    return dest


def passage_sound(force: bool, start: float = 0.0) -> tuple[Path, dict, bool]:
    """One score for the passage recording. Every derived capture is cut from it."""
    dest = OUT / "passage-score.wav"
    span = query_capture_span("passage", start=start)
    provenance = competition_audio_provenance(span)
    if not force:
        got = probe_required(dest) if dest.is_file() else None
        cached = score_provenance(dest, span) if got else None
        if got and cached == provenance and abs(got["seconds"] - span["duration"]) < 0.01:
            print(f"  passage score · kept · {got['seconds']:.1f}s")
            return dest, provenance, False
    print("  passage score · accepting verified deterministic competition master")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or (dest.exists() and not dest.is_file()):
        raise SystemExit("passage score cache is not a regular file")
    shutil.copy2(AUDIO_MASTER, dest)
    if not dest.is_file() or digest(dest) != provenance["master_sha256"]:
        raise SystemExit("verified competition master changed while copying into delivery cache")
    write_score_receipt(dest, span, provenance)
    return dest, provenance, True


def mux(video: Path, audio: Path, dest: Path, acodec: str, vcopy: bool = True, vfilter: str | None = None) -> None:
    args = ["-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0"]
    if vcopy:
        args += ["-c:v", "copy"]
    else:
        args += ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if vfilter:
        args += ["-vf", vfilter]
    args += ["-c:a", acodec] + (["-b:a", "320k"] if acodec == "aac" else []) + ["-shortest", dest]
    ffmpeg(args)


def cut_audio(source: Path, t0: float, seconds: float, dest: Path, fade: float = 0.3) -> None:
    """A capture's sound, from the passage score, with edges that do not click."""
    filters = [] if fade <= 0 else [f"afade=t=in:st=0:d={fade}", f"afade=t=out:st={max(0.0, seconds - fade)}:d={fade}"]
    args = ["-ss", t0, "-t", seconds, "-i", source]
    if filters:
        args += ["-af", ",".join(filters)]
    ffmpeg([*args, dest])


# ── deliverables ───────────────────────────────────────────────────────────────


def deliver_passage(picture: Path, sound: Path, force: bool) -> Path:
    dest = PACKAGE / "master.mov"
    if dest.is_file() and not force:
        return dest
    print("  master.mov (4K passage) · muxing")
    mux(picture, sound, dest, "pcm_s24le")
    return dest


def deliver_derived(
    name: str, spec: dict, program: dict, picture: Path, sound: Path, force: bool, start: float = 0.0
) -> Path:
    cap = captures(program)[name]
    span = query_capture_span(name, start=start)
    passage_span = query_capture_span("passage", start=start)
    error = capture_span_error(name, passage_span, start)
    if error:
        raise SystemExit(error)

    rel_t0 = max(0.0, span["t0"] - passage_span["t0"])
    seconds = span["duration"]
    fps = cap.get("fps", 30)
    w_out, h_out = cap.get("w", 1920), cap.get("h", 1080)

    dest = PACKAGE / f"{name}{spec['suffix']}"
    if dest.is_file() and not force:
        return dest
    print(f"  {dest.name} · {'slicing' if spec['mode'] == 'copy' else 'slicing + scaling'} from the passage recording")

    tmp_v = OUT / f".{name}-v{spec['suffix']}"
    tmp_a = OUT / f".{name}-a.wav"
    if spec["mode"] == "copy":
        ffmpeg(["-ss", rel_t0, "-t", seconds, "-i", picture, "-c", "copy", tmp_v])
    else:
        ffmpeg(["-ss", rel_t0, "-t", seconds, "-i", picture, "-c", "copy", OUT / f".{name}-raw.mov"])
        tmp_v = OUT / f".{name}-raw.mov"

    cut_audio(sound, rel_t0, seconds, tmp_a, fade=0.0 if name == "screener" else 0.3)
    scale = None if spec["mode"] == "copy" else f"scale={w_out}:{h_out}:flags=lanczos"
    mux(tmp_v, tmp_a, dest, spec["audio"], vcopy=(spec["mode"] == "copy"), vfilter=scale)
    for junk in (OUT / f".{name}-v{spec['suffix']}", OUT / f".{name}-a.wav", OUT / f".{name}-raw.mov"):
        junk.unlink(missing_ok=True)

    got = probe_required(dest)
    if not got:
        raise SystemExit(f"ffprobe could not inspect {dest.name} after muxing")
    want_frames = int(round(seconds * fps))
    have = int(round(got["seconds"] * got.get("fps", fps)))
    if abs(have - want_frames) > 1:
        raise SystemExit(f"{dest.name} is {have} frames, the capture declares {want_frames} — the slice is wrong")
    print(f"      {got['seconds']:.3f}s · {have} frames (declared {want_frames})")
    return dest


def deliver_reel(program: dict, sound: Path, tier: str, force: bool, start: float = 0.0) -> Path:
    """The one capture preset that must be rendered — vertical aspect is a different field of view."""
    dest = PACKAGE / REEL_ITEM
    if dest.is_file() and not force:
        return dest
    span = query_capture_span("reel", start=start)
    passage_span = query_capture_span("passage", start=start)
    error = capture_span_error("reel", passage_span, start)
    if error:
        raise SystemExit(error)
    rel_t0 = max(0.0, span["t0"] - passage_span["t0"])
    seconds = span["duration"]

    print(f"  {REEL_ITEM} · rendering (vertical is a different field of view, not a crop)")
    with tempfile.TemporaryDirectory(prefix=".reel-", dir=OUT) as render_tmp:
        render_out = Path(render_tmp)
        stem = render_out / "reel-default"
        render_command = [
            sys.executable,
            str(RENDER),
            "--capture",
            "reel",
            "--start",
            str(span["t0"]),
            "--tier",
            tier,
            "--codec",
            "h264",
            "--quiet",
            "--score",
            MUSIC_SCORE.relative_to(DANSE).as_posix(),
            "--choreography",
            CHOREOGRAPHY.relative_to(DANSE).as_posix(),
            "--out",
            str(render_out),
        ]
        done = subprocess.run(render_command, check=False)
        picture = stem.with_suffix(".mp4")
        if done.returncode == 0 and not picture.is_file():
            # A one-part full plan is left at its segment path. Ask the
            # renderer to validate every planned segment and create the
            # canonical output rather than adopting a segment directly.
            done = subprocess.run([*render_command, "--concat"], check=False)
        if done.returncode != 0 or not picture.is_file():
            raise SystemExit("the reel would not render")
        provenance = OUT / "reel-provenance"
        if provenance.is_symlink() or (provenance.exists() and not provenance.is_dir()):
            raise SystemExit("reel provenance boundary is not a regular directory")
        provenance.mkdir(parents=True, exist_ok=True)
        for old in provenance.iterdir():
            if old.is_symlink() or not old.is_file():
                raise SystemExit("reel provenance boundary contains an unsafe entry")
            old.unlink()
        concat_receipt = picture.with_name(picture.name + ".receipt.json")
        concat_value, _, _ = _read_producer_receipt(
            concat_receipt,
            "danse.render.concat.v1",
        )
        shutil.copy2(concat_receipt, provenance / concat_receipt.name)
        for segment in concat_value.get("segments") or []:
            name = segment.get("name") if isinstance(segment, dict) else None
            if not isinstance(name, str) or Path(name).name != name:
                raise SystemExit("reel concat receipt has an unsafe segment")
            receipt = render_out / f"{name}.receipt.json"
            _read_producer_receipt(receipt, "danse.render.segment.v1")
            shutil.copy2(receipt, provenance / receipt.name)
        tmp_a = render_out / "reel-a.wav"
        cut_audio(sound, rel_t0, seconds, tmp_a)
        with tempfile.TemporaryDirectory(prefix=".reel-publish-", dir=PACKAGE) as publish_tmp:
            staged = Path(publish_tmp) / dest.name
            mux(picture, tmp_a, staged, "aac")
            got = probe_required(staged)
            fps = captures(program).get("reel", {}).get("fps", 30)
            want_frames = int(round(seconds * fps))
            have = int(round(got["seconds"] * got.get("fps", fps))) if got else -1
            if not got or abs(have - want_frames) > 1:
                raise SystemExit(
                    f"{REEL_ITEM} is {have} frames, the capture declares {want_frames} — the render is wrong"
                )
            staged.replace(dest)
            print(f"      {got['seconds']:.3f}s · {have} frames (declared {want_frames})")
    return dest


def deliver_stills(program: dict, tier: str, force: bool, start: float = 0.0) -> list[Path]:
    """Six frames, six seeds. The filename IS the provenance — `seed-0x….jpg`
    says this is one of the films, not the film."""
    (PACKAGE / "stills").mkdir(parents=True, exist_ok=True)
    cap = captures(program)["passage"]
    fps = cap.get("fps", 30)
    span = query_capture_span("passage", start=start)
    made = []
    for fraction, dest in zip(STILL_FRACTIONS, still_destinations(program, PACKAGE), strict=True):
        seed = int(dest.stem.removeprefix("seed-"), 0)
        still_span = query_capture_span("passage", seed=seed, start=span["t0"])
        t = still_span["duration"] * fraction
        if dest.is_file() and not force:
            continue
        frame = int(round(t * fps))
        print(f"  {dest.name} · t={t:.0f}s")
        for junk in OUT.glob(f"passage-{seed}*"):
            junk.unlink(missing_ok=True)
        done = subprocess.run(
            # fmt: off
            [
                sys.executable,
                str(RENDER),
                "--capture",
                "passage",
                "--start",
                str(still_span["t0"]),
                "--tier",
                tier,
                "--codec",
                "prores",
                "--seed",
                str(seed),
                "--segment",
                str(frame),
                "--segment-frames",
                "1",
                "--quiet",
                "--score",
                MUSIC_SCORE.relative_to(DANSE).as_posix(),
                "--choreography",
                CHOREOGRAPHY.relative_to(DANSE).as_posix(),
                "--out",
                str(OUT),
            ],
            # fmt: on
            check=False,
        )
        one = OUT / f"passage-{seed}-seg-{frame:03d}.mov"
        if done.returncode != 0 or not one.is_file():
            raise SystemExit(f"still at t={t} would not render")
        ffmpeg(["-i", one, "-frames:v", "1", "-q:v", "2", dest])
        one.unlink(missing_ok=True)
        made.append(dest)
    return made


def deliver_text() -> list[Path]:
    """The written half, from its git-tracked source.

    These live in `submission/text/` and are COPIED here, never authored here:
    the package is a build artifact and gets wiped, and a synopsis is not
    something that should be recoverable only from a directory nobody backs up.
    """
    source = DANSE / "submission" / "text"
    if not source.is_dir():
        print(f"  text · MISSING SOURCE at {source}")
        return []
    dest = PACKAGE / "text"
    dest.mkdir(parents=True, exist_ok=True)
    made = []
    for path in sorted(source.glob("*.txt")):
        shutil.copy2(path, dest / path.name)
        made.append(dest / path.name)
    print(f"  text/ · {len(made)} files · {sum(len(p.read_text().split()) for p in made)} words")
    return made


def deliver_origin(origin: Path, force: bool) -> Path:
    dest = PACKAGE / "stills" / "origin-2017.jpg"
    expected = registered_origin_source_sha256()
    if (
        not regular_directory_slot(PACKAGE)
        or not regular_directory_slot(dest.parent)
        or dest.is_symlink()
        or (dest.exists() and not dest.is_file())
    ):
        raise SystemExit(f"staged origin photograph must be a regular non-symlink file: {dest}")
    if dest.is_file() and not force:
        if digest(dest) != expected:
            raise SystemExit(f"staged origin photograph does not match {REGISTER}; rerun with --force origin")
        # Return a verified reuse so the caller rewrites its manifest item from
        # the canonical register. Exact bytes are sufficient custody to repair
        # a missing or stale package receipt without the private raw mount.
        return dest
    if not origin.is_file():
        raise SystemExit(f"origin photograph source is missing at {origin}")
    actual = digest(origin)
    if actual != expected:
        raise SystemExit(f"registered origin identity mismatch for {origin.name}: {actual}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  origin-2017.jpg · byte-identical copy of {origin.name}")
    shutil.copy2(origin, dest)
    if (
        not regular_directory_slot(PACKAGE)
        or not regular_directory_slot(dest.parent)
        or not dest.is_file()
        or dest.is_symlink()
        or digest(dest) != expected
    ):
        raise SystemExit(f"copied origin photograph does not match registered identity: {dest}")
    return dest


def attestation_template() -> str:
    reg = yaml.safe_load(REGISTER.read_text()) or {}
    requirements = [
        item
        for section in ("requirements", "approvals")
        for item in reg.get(section, [])
        if item.get("check") == "manual"
    ]
    entries = [
        {
            "key": item["id"],
            "phase": item.get("phase", "UNOWNED"),
            "rule": item.get("rule", ""),
            "kind": "boolean",
            "values": [True],
        }
        for item in requirements
        if item.get("id")
    ]
    seen = {entry["key"] for entry in entries}

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("rights register contains a duplicate JSON key")
            value[key] = item
        return value

    try:
        if not RIGHTS_REGISTER.is_file():
            raise OSError("rights register is missing")
        rights = json.loads(RIGHTS_REGISTER.read_text(), object_pairs_hook=unique_object)
        if not isinstance(rights, dict):
            raise ValueError("rights register is not a JSON object")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit("rights register is invalid or unreadable JSON") from exc
    for gate in rights.get("human_gates", []):
        attestation = gate.get("attestation") if isinstance(gate, dict) else None
        if not isinstance(attestation, dict) or attestation.get("key") in seen:
            continue
        key = attestation.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", key):
            continue
        phases = [
            phase
            for phase in (gate.get("required_for") or [])
            if phase in {"public", "package", "uploaded", "submitted", "release"}
        ]
        note = " ".join(str(gate.get("note", "")).split())
        entries.append(
            {
                "key": key,
                "phase": ",".join(phases) if phases else "UNOWNED",
                "rule": note,
                "kind": attestation.get("kind"),
                "values": attestation.get("values") or [],
            }
        )
        seen.add(key)

    lines = [
        "# Human assertions. The package build creates nulls; only a human who",
        "# performed or verified an act may set its value to true.",
        "# check.py and check-rights.py read only the cumulative gates owned by --phase.",
    ]
    for entry in entries:
        choice = f" choose one of {json.dumps(entry['values'])}" if entry["kind"] == "choice" else ""
        lines.append(f"#   {entry['key']:<34} [{entry['phase']}] {entry['rule']}{choice}")
    lines.extend(f"{entry['key']}: null" for entry in entries)
    return "\n".join(lines) + "\n"


def main() -> int:
    global OUT, PACKAGE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", default="film", help="corpus tier for rendered items")
    ap.add_argument(
        "--start",
        type=float,
        default=0.0,
        help="diagnostic/preflight offset; production competition delivery requires 0",
    )
    ap.add_argument("--only", action="append", choices=SELECTORS, help="build one output group (repeatable)")
    ap.add_argument("--force", action="append", choices=FORCE_ITEMS, default=[], help="re-make an existing item")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="root for restartable render intermediates")
    ap.add_argument("--package", type=Path, help="staged package root (default: <out>/package)")
    ap.add_argument("--preflight", action="store_true", help="validate this invocation without rendering or writing")
    args = ap.parse_args()
    if args.start < 0:
        ap.error("--start must be non-negative")

    program = json.loads(PROGRAM.read_text())
    only = set(args.only or SELECTORS)
    force = set(args.force)
    package = args.package or (args.out / "package")
    origin = registered_origin() if "origin" in only else None
    passage_requested = bool(only & PASSAGE_SELECTORS)
    if passage_requested and program.get("seed") != 20170620 and not args.preflight:
        raise SystemExit("production delivery requires canonical river seed 20170620")
    if passage_requested and abs(args.start) > 1e-9 and not args.preflight:
        raise SystemExit("production delivery is locked to river seed 20170620, passage 0, score time 0")
    span_error = None
    span = None
    if passage_requested:
        try:
            span = query_capture_span("passage", start=args.start)
        except SystemExit as exc:
            if not args.preflight:
                raise
            span_error = str(exc)
    render_root = capture_root(args.out, span, span["t0"]) if span else args.out
    if args.preflight:
        return preflight(
            program,
            span,
            only,
            force,
            args.tier,
            render_root,
            package,
            origin,
            args.start,
            span_error,
            passage_requested,
        )
    repository = require_clean_repository()
    if not regular_directory_slot(package):
        raise SystemExit(f"package root must be an absent or regular non-symlink directory: {package}")
    source_tree = delivery_source_sha256(args.tier) if passage_requested else None
    if passage_requested and not package_provenance_matches(
        package,
        span,
        args.start,
        source_tree,
        repository["head"],
    ):
        raise SystemExit(f"{package}/manifest.json belongs to a different passage; choose a fresh --package root")

    work = pending(program, only, force, package)
    selected_fixed_windows = {
        name for name in DERIVED if "derived" in only
    } | ({"reel"} if "reel" in only else set())
    for name in sorted(selected_fixed_windows):
        assert span is not None
        error = capture_span_error(name, span, args.start)
        if error:
            raise SystemExit(error)
    audio_selected = bool(only & {"master", "derived", "reel"})
    media_pending = bool(work["master"] or work["derived"] or work["reel"] or work["stills"])
    if (media_pending or audio_selected) and shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is required for media delivery; run deliver.py --preflight")

    OUT = render_root
    PACKAGE = package
    OUT.mkdir(parents=True, exist_ok=True)
    PACKAGE.mkdir(parents=True, exist_ok=True)

    if span:
        print(
            f"{program['title']} · seed {hexseed(program['seed'])} · passage seed {hexseed(span['seed'])} · "
            f"{span['duration']:.1f}s (start at {args.start:.1f}s)\n"
        )
    else:
        print(f"{program['title']} · passage-independent package update\n")

    score_forced = "master" in force
    sound, sound_provenance, score_rebuilt = (
        passage_sound(score_forced, start=args.start) if audio_selected else (None, None, False)
    )
    if score_rebuilt:
        expand_rebuilt_score_dependents(work, only)
    need_picture = work["master"] or bool(work["derived"])
    picture = passage_picture(program, args.tier, score_forced, start=args.start) if need_picture else None
    made: list[Path] = []
    if audio_selected:
        assert sound is not None and sound_provenance is not None
        score_source = PACKAGE / SCORE_SOURCE_ITEM
        if not regular_directory_slot(score_source.parent) or score_source.is_symlink():
            raise SystemExit("package score provenance must use a regular non-symlink destination")
        score_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sound, score_source)
        if not score_source.is_file() or score_source.is_symlink() or digest(score_source) != digest(sound):
            raise SystemExit("package score provenance copy does not match the rendered score")
        made.append(score_source)
        audio_receipt_source = PACKAGE / AUDIO_RENDER_SOURCE_ITEM
        if not regular_directory_slot(audio_receipt_source.parent) or audio_receipt_source.is_symlink():
            raise SystemExit("package audio-render receipt must use a regular non-symlink destination")
        shutil.copy2(AUDIO_RENDER_RECEIPT, audio_receipt_source)
        if (
            not audio_receipt_source.is_file()
            or audio_receipt_source.is_symlink()
            or digest(audio_receipt_source) != sound_provenance["audio_render_receipt_sha256"]
        ):
            raise SystemExit("package audio-render receipt copy does not match the verified receipt")
        made.append(audio_receipt_source)

    if "master" in only and work["master"]:
        made.append(deliver_passage(picture, sound, score_forced))
    if "derived" in only:
        for name, spec in DERIVED.items():
            if name in work["derived"]:
                made.append(
                    deliver_derived(
                        name,
                        spec,
                        program,
                        picture,
                        sound,
                        score_forced or is_forced(force, name, "derived"),
                        start=args.start,
                    )
                )
    if "reel" in only and work["reel"]:
        made.append(deliver_reel(program, sound, args.tier, score_forced or "reel" in force, start=args.start))
    if "stills" in only:
        made += deliver_stills(program, args.tier, "stills" in force, start=args.start)
    if "text" in only:
        made += deliver_text()
    if "origin" in only:
        assert origin is not None
        got = deliver_origin(origin, "origin" in force)
        if got:
            made.append(got)

    attest = PACKAGE / "attest.yaml"
    if not attest.exists():
        attest.write_text(attestation_template())
        print("  attest.yaml · scaffold written — every line is a human's to set")

    print()
    manifest_path = PACKAGE / "manifest.json"
    if manifest_path.is_symlink() or (
        manifest_path.exists() and not manifest_path.is_file()
    ):
        raise SystemExit("package manifest must be a regular non-symlink file")
    previous_manifest_sha256 = None
    if manifest_path.is_file():
        try:
            previous_manifest_bytes, previous_manifest_sha256 = bounded_regular_bytes(
                manifest_path,
                max_bytes=PACKAGE_MANIFEST_MAX_BYTES,
                description="package manifest",
            )
            previous = json.loads(previous_manifest_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SystemExit("package manifest is invalid or unreadable") from exc
        if not isinstance(previous, dict):
            raise SystemExit("package manifest is not a JSON object")
    else:
        previous = {}
    score_motion_evidence = None
    if span is not None:
        score_motion_evidence, evidence_files = stage_score_motion_evidence(
            PACKAGE,
            span,
            repository["head"],
        )
        made.extend(evidence_files)
    previous_items = {
        item["name"]: item
        for item in previous.get("items", [])
        if isinstance(item, dict) and item.get("name") and (PACKAGE / item["name"]).is_file()
    }
    previous_sound = previous.get("sound") if isinstance(previous.get("sound"), dict) else None
    manifest = {
        "schema": "danse.delivery.manifest.v1",
        "title": program["title"],
        "seed": hexseed(program["seed"]),
        "repository_head": repository["head"],
        "items": [],
    }
    passage_fields = ("passage_seed", "passage", "start", "t0", "t1", "duration")
    if span:
        manifest |= {
            "passage_seed": hexseed(span["seed"]),
            "passage": span["passage"],
            "start": args.start,
            "t0": span["t0"],
            "t1": span["t1"],
            "duration": span["duration"],
            "corpus_tier": args.tier,
            "source_tree_sha256": source_tree,
        }
    else:
        manifest |= {
            key: previous[key]
            for key in (*passage_fields, "corpus_tier", "source_tree_sha256")
            if key in previous
        }
    for path in made:
        if not path.is_file():
            continue
        size = path.stat().st_size
        name = str(path.relative_to(PACKAGE))
        # ffprobe accepts arbitrary text through its `ansi` demuxer and treats
        # still images as one-frame video. Only time-based delivery media belongs
        # in this receipt; text and photographs have their own package predicates.
        info = (probe(path) or {}) if name in AUDIO_ITEMS else {}
        prior = previous_items.get(name) or {}
        item = {"name": name, "bytes": size, "sha256": digest(path), **info}
        if name in AUDIO_ITEMS:
            item_sound = sound_provenance or prior.get("sound") or previous_sound
            if item_sound:
                item["sound"] = item_sound
        elif name in {SCORE_SOURCE_ITEM, AUDIO_RENDER_SOURCE_ITEM} and sound_provenance:
            item["sound"] = sound_provenance
        if name == "stills/origin-2017.jpg":
            assert origin is not None
            item |= {
                "source": origin.name,
                "source_sha256": registered_origin_source_sha256(),
                "copy_mode": "byte-identical",
            }
        previous_items[name] = item
        shape = f"{info.get('width', '?')}×{info.get('height', '?')}"
        rate = f"@{info['fps']}" if "fps" in info else ""
        secs = f"{info['seconds']:.1f}s " if "seconds" in info else ""
        media = f"{secs}{shape} {rate}" if info else ""
        print(f"  {name:<28} {size / 1e6:>8.1f} MB  {media}")
    if sound_provenance:
        for name, item in previous_items.items():
            if name in AUDIO_ITEMS:
                item["sound"] = sound_provenance
    manifest["items"] = [previous_items[name] for name in sorted(previous_items)]
    if sound_provenance:
        manifest["sound"] = sound_provenance
    elif (master_sound := (previous_items.get("master.mov") or {}).get("sound")):
        manifest["sound"] = master_sound
    elif previous_sound:
        manifest["sound"] = previous_sound
    if span is None:
        score_motion_evidence = prior_score_motion_evidence(
            PACKAGE,
            previous,
            previous_items,
        )
    if score_motion_evidence is not None:
        manifest["score_motion_evidence"] = score_motion_evidence
    producer_root = OUT
    if span is None and _production_targets(manifest):
        producer_root = retained_capture_root(args.out, manifest)
    write_production_receipt(
        PACKAGE,
        producer_root,
        manifest,
        previous,
        publish_manifest=True,
        previous_manifest_sha256=previous_manifest_sha256,
    )
    total = sum(i["bytes"] for i in manifest["items"])
    print(f"\n  {len(manifest['items'])} items · {total / 1e9:.2f} GB · {PACKAGE}")
    if shutil.which("python3"):
        print("\nnext: submission/check.py --phase package --package " + str(PACKAGE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
