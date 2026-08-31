#!/usr/bin/env python3
"""Render the committed Delibes arrangement on a portable CI toolchain.

This is the submission-recovery path, not the canonical exhibition receipt.
It preserves the bound MIDI, soundfont, seven-stem mix, duration and loudness
contract while recording the actual FluidSynth and ffmpeg executables used by
the cloud runner.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from argparse import Namespace
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOUND = ROOT / "sound"
sys.path.insert(0, str(SOUND))

from render_music import (  # noqa: E402
    loudness_gate,
    render_once,
    require_hash,
    sha256,
)


def document(path: Path, schema: str) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise ValueError(f"{path}: expected {schema}")
    return value


def require_bound(reference: dict, selected: Path, label: str) -> Path:
    if not isinstance(reference, dict):
        raise ValueError(f"{label}: incomplete source binding")
    bound = require_hash(reference, label)
    if bound.resolve() != selected.resolve():
        raise ValueError(f"{label}: path differs from the committed binding")
    return bound


def resolve_executable(path: Path, label: str) -> Path:
    """Return the exact regular file that subprocesses will execute."""
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"missing {label} executable: {path}") from exc
    if not resolved.is_file():
        raise ValueError(f"{label} executable is not a regular file: {resolved}")
    return resolved


def repository_head() -> str:
    """Bind the portable wrapper and its contracts to one exact source tree."""
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = (head.stdout or "").strip().lower()
    if head.returncode or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise ValueError("portable audio requires an exact Git commit identity")
    status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode:
        raise ValueError("cannot verify the portable audio source worktree")
    if (status.stdout or "").strip():
        raise ValueError("portable audio requires a clean exact Git worktree")
    return commit


def snapshot_files(paths: dict[str, Path]) -> dict[str, tuple[Path, str]]:
    """Capture exact non-symlink bytes used by one portable render."""
    snapshot: dict[str, tuple[Path, str]] = {}
    for label, path in paths.items():
        if path.is_symlink():
            raise ValueError(f"{label} must be a non-symlink regular file")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"{label} is missing") from exc
        if not resolved.is_file():
            raise ValueError(f"{label} must be a regular file")
        snapshot[label] = (resolved, sha256(resolved))
    return snapshot


def snapshot_digest(snapshot: dict[str, tuple[Path, str]], label: str) -> str:
    return snapshot[label][1]


def revalidate_snapshot(
    snapshot: dict[str, tuple[Path, str]],
    expected_head: str,
) -> None:
    """Reject any source, tool, executable, HEAD, or cleanliness drift."""
    for label, (path, expected_digest) in snapshot.items():
        if path.is_symlink():
            raise ValueError(f"{label} changed to a symlink during the portable audio render")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"{label} disappeared during the portable audio render") from exc
        if resolved != path or not resolved.is_file():
            raise ValueError(f"{label} changed file identity during the portable audio render")
        if sha256(resolved) != expected_digest:
            raise ValueError(f"{label} bytes changed during the portable audio render")
    if repository_head() != expected_head:
        raise ValueError("repository HEAD changed during the portable audio render")


def competition_profile(
    uses: dict,
    mix: dict,
    *,
    toolchain: dict,
    midi_path: Path,
    soundfont_path: Path,
) -> str:
    """Validate the fixed package-eligible source and stem custody profile."""
    profile_id = uses.get("competition_profile")
    profiles = uses.get("profiles")
    profile = profiles.get(profile_id) if isinstance(profiles, dict) else None
    if not isinstance(profile_id, str) or not isinstance(profile, dict):
        raise ValueError("audio uses has no competition profile")
    if profile.get("package_eligible") is not True:
        raise ValueError(f"audio use profile {profile_id!r} is not package-eligible")

    forbidden = profile.get("forbidden_source_kinds")
    if not isinstance(forbidden, list) or not all(isinstance(kind, str) for kind in forbidden):
        raise ValueError("competition audio forbidden source kinds are malformed")
    forbidden_kinds = set(forbidden)

    soundfont_reference = toolchain.get("soundfont")
    if not isinstance(soundfont_reference, dict):
        raise ValueError("toolchain soundfont binding is required")
    soundfont_notice = soundfont_reference.get("license_notice")
    if not isinstance(soundfont_notice, dict):
        raise ValueError("toolchain soundfont license notice is required")
    notice_path = require_hash(soundfont_notice, "toolchain soundfont license notice")

    expected = {
        midi_path.resolve(): {
            "kind": "project-authored-midi",
            "reference": toolchain.get("midi"),
            "notice": None,
        },
        soundfont_path.resolve(): {
            "kind": "licensed-soundfont",
            "reference": soundfont_reference,
            "notice": (soundfont_notice, notice_path),
        },
    }
    declared_sources = profile.get("declared_sources")
    if not isinstance(declared_sources, list):
        raise ValueError("competition audio profile must declare its sources")
    seen: set[Path] = set()
    for index, source in enumerate(declared_sources):
        if not isinstance(source, dict):
            raise ValueError(f"competition audio source {index} is malformed")
        source_id = source.get("id")
        label = f"competition audio source {source_id or index}"
        kind = source.get("kind")
        if not isinstance(kind, str):
            raise ValueError(f"{label}: source kind is required")
        if kind in forbidden_kinds:
            raise ValueError(f"{label}: source kind {kind!r} is forbidden")
        source_path = require_hash(source, label)
        resolved = source_path.resolve()
        contract = expected.get(resolved)
        if contract is None:
            raise ValueError(f"{label}: source is not used by the portable competition render")
        reference = contract["reference"]
        if (
            not isinstance(reference, dict)
            or source.get("path") != reference.get("path")
            or source.get("sha256") != reference.get("sha256")
        ):
            raise ValueError(f"{label}: source differs from the toolchain binding")
        if kind != contract["kind"]:
            raise ValueError(f"{label}: expected source kind {contract['kind']!r}")
        if resolved in seen:
            raise ValueError(f"{label}: duplicate source binding")
        seen.add(resolved)

        declared_notice = source.get("license_notice")
        expected_notice = contract["notice"]
        if expected_notice is None:
            if declared_notice is not None:
                if not isinstance(declared_notice, dict):
                    raise ValueError(f"{label}: license notice is malformed")
                require_hash(declared_notice, f"{label} license notice")
        else:
            notice_reference, expected_notice_path = expected_notice
            if not isinstance(declared_notice, dict):
                raise ValueError(f"{label}: license notice is required")
            declared_notice_path = require_bound(
                declared_notice,
                expected_notice_path,
                f"{label} license notice",
            )
            if declared_notice != notice_reference or declared_notice_path != notice_path:
                raise ValueError(f"{label}: license notice differs from the toolchain binding")

    if seen != set(expected):
        raise ValueError("competition audio profile does not bind every portable render source")

    stems = mix.get("stems")
    required_stems = profile.get("required_stems")
    if (
        not isinstance(stems, list)
        or not all(isinstance(row, dict) for row in stems)
        or not isinstance(required_stems, list)
        or not all(isinstance(stem_id, str) for stem_id in required_stems)
        or [row.get("id") for row in stems] != required_stems
    ):
        raise ValueError("mix stem order must equal the competition audio-use contract")
    return profile_id


def version(executable: Path, *arguments: str) -> str:
    result = subprocess.run(
        [str(executable), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    line = (result.stdout + result.stderr).strip().splitlines()
    if result.returncode != 0 or not line:
        raise ValueError(f"cannot identify {executable}")
    return line[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--soundfont", type=Path, required=True)
    parser.add_argument("--fluidsynth", type=Path, default=shutil.which("fluidsynth"))
    parser.add_argument("--ffmpeg", type=Path, default=shutil.which("ffmpeg"))
    args = parser.parse_args()

    score_path = ROOT / "music" / "score.json"
    midi_path = ROOT / "music" / "delibes-screendance-suite.mid"
    adaptation_path = ROOT / "music" / "adaptation.json"
    mix_path = ROOT / "music" / "delibes-mix.json"
    toolchain_path = ROOT / "music" / "audio-toolchain.json"
    choreography_path = ROOT / "render" / "choreography.json"
    audio_uses_path = ROOT / "sound" / "audio-uses.json"
    renderer_path = ROOT / "sound" / "render_music.py"
    portable_renderer_path = Path(__file__).resolve()

    try:
        if args.fluidsynth is None or args.ffmpeg is None:
            raise ValueError("fluidsynth and ffmpeg are required")
        args.fluidsynth = resolve_executable(args.fluidsynth, "FluidSynth")
        args.ffmpeg = resolve_executable(args.ffmpeg, "ffmpeg")

        score = document(score_path, "danse.music.score.v1")
        adaptation = document(adaptation_path, "danse.music.adaptation.v1")
        mix = document(mix_path, "danse.audio.mix.v1")
        toolchain = document(toolchain_path, "danse.audio.toolchain.v1")
        choreography = document(choreography_path, "danse.choreography.v1")
        audio_uses = document(audio_uses_path, "danse.audio.uses.v1")

        require_bound(toolchain["midi"], midi_path, "MIDI")
        require_bound(toolchain["adaptation"], adaptation_path, "adaptation")
        require_bound(toolchain["mix"], mix_path, "mix")
        soundfont_path = require_bound(toolchain["soundfont"], args.soundfont, "soundfont")
        require_bound(toolchain["renderer"], renderer_path, "renderer")
        profile_id = competition_profile(
            audio_uses,
            mix,
            toolchain=toolchain,
            midi_path=midi_path,
            soundfont_path=soundfont_path,
        )
        soundfont_notice_path = require_hash(
            toolchain["soundfont"]["license_notice"],
            "toolchain soundfont license notice",
        )
        render_snapshot = snapshot_files(
            {
                "portable renderer": portable_renderer_path,
                "canonical renderer": renderer_path,
                "score": score_path,
                "choreography": choreography_path,
                "MIDI": midi_path,
                "adaptation": adaptation_path,
                "mix": mix_path,
                "toolchain": toolchain_path,
                "audio uses": audio_uses_path,
                "soundfont": soundfont_path,
                "soundfont license notice": soundfont_notice_path,
                "FluidSynth executable": args.fluidsynth,
                "ffmpeg executable": args.ffmpeg,
            }
        )
        source_head = repository_head()
        revalidate_snapshot(render_snapshot, source_head)

        midi_digest = snapshot_digest(render_snapshot, "MIDI")
        if score.get("identity", {}).get("midi_sha256") != midi_digest:
            raise ValueError("score does not bind the selected MIDI")
        if adaptation.get("output", {}).get("sha256") != midi_digest:
            raise ValueError("adaptation does not bind the selected MIDI")

        duration = Decimal(str(score["time"]["duration_seconds"]))
        if abs(float(duration) - float(adaptation["output"]["duration_seconds"])) > 1e-6:
            raise ValueError("score and adaptation durations differ")
        sample_rate = int(mix["sample_rate"])
        frames = int((duration * sample_rate).to_integral_value(rounding=ROUND_HALF_UP))

        ffmpeg_line = version(args.ffmpeg, "-version")
        fluidsynth_line = version(args.fluidsynth, "--version")
        ffmpeg_contract = {
            "version": ffmpeg_line,
            "executable_sha256": snapshot_digest(render_snapshot, "ffmpeg executable"),
        }
        contracts = {
            "mix": mix,
            "fluidsynth": toolchain["fluidsynth"],
            "ffmpeg": ffmpeg_contract,
        }
        runtime_args = Namespace(
            midi=midi_path,
            fluidsynth=args.fluidsynth,
            soundfont=soundfont_path,
            ffmpeg=args.ffmpeg,
        )
        outputs = render_once(
            args.out,
            args=runtime_args,
            contracts=contracts,
            frames=frames,
            sample_rate=sample_rate,
        )
        loudness_ok, peak_ok = loudness_gate(
            outputs["normalization"]["output"],
            mix["master"]["normalization"],
        )
        stems_non_silent = all(row["non_silent"] for row in outputs["stems"])
        polyphonic = outputs["master"]["polyphonic_frames"] > 0
        if (
            not outputs["master"]["non_silent"]
            or not stems_non_silent
            or not polyphonic
            or not loudness_ok
            or not peak_ok
            or outputs["master"]["frames"] != frames
        ):
            raise ValueError("portable master failed frame-count/silence/loudness/true-peak checks")
        revalidate_snapshot(render_snapshot, source_head)

        receipt = {
            "schema": "danse.submission.portable-audio.v1",
            "purpose": "ScreenDance Miami 2027 deadline screener",
            "canonical_exhibition_receipt": False,
            "profile": profile_id,
            "repository_head": source_head,
            "inputs": {
                "toolchain": {
                    "path": toolchain_path.relative_to(ROOT).as_posix(),
                    "sha256": snapshot_digest(render_snapshot, "toolchain"),
                },
                "portable_renderer": {
                    "path": portable_renderer_path.relative_to(ROOT).as_posix(),
                    "sha256": snapshot_digest(render_snapshot, "portable renderer"),
                },
                "renderer": {
                    "path": renderer_path.relative_to(ROOT).as_posix(),
                    "sha256": snapshot_digest(render_snapshot, "canonical renderer"),
                },
                "audio_uses": {
                    "path": audio_uses_path.relative_to(ROOT).as_posix(),
                    "sha256": snapshot_digest(render_snapshot, "audio uses"),
                },
                "score": {
                    "path": score_path.relative_to(ROOT).as_posix(),
                    "sha256": snapshot_digest(render_snapshot, "score"),
                },
                "choreography": {
                    "path": choreography_path.relative_to(ROOT).as_posix(),
                    "sha256": snapshot_digest(render_snapshot, "choreography"),
                    "identity": choreography.get("identity"),
                },
                "midi": {"path": midi_path.relative_to(ROOT).as_posix(), "sha256": midi_digest},
                "adaptation": {
                    "path": adaptation_path.relative_to(ROOT).as_posix(),
                    "sha256": snapshot_digest(render_snapshot, "adaptation"),
                },
                "mix": {
                    "path": mix_path.relative_to(ROOT).as_posix(),
                    "sha256": snapshot_digest(render_snapshot, "mix"),
                },
                "soundfont": {
                    "path": soundfont_path.relative_to(ROOT).as_posix(),
                    "sha256": snapshot_digest(render_snapshot, "soundfont"),
                },
                "soundfont_license_notice": {
                    "path": soundfont_notice_path.relative_to(ROOT).as_posix(),
                    "sha256": snapshot_digest(render_snapshot, "soundfont license notice"),
                },
                "fluidsynth_executable": {
                    "path": str(args.fluidsynth),
                    "sha256": snapshot_digest(render_snapshot, "FluidSynth executable"),
                },
                "ffmpeg_executable": {
                    "path": str(args.ffmpeg),
                    "sha256": snapshot_digest(render_snapshot, "ffmpeg executable"),
                },
            },
            "runtime": {
                "python": sys.version.split()[0],
                "fluidsynth": fluidsynth_line,
                "fluidsynth_path": str(args.fluidsynth),
                "fluidsynth_sha256": snapshot_digest(render_snapshot, "FluidSynth executable"),
                "ffmpeg": ffmpeg_line,
                "ffmpeg_path": str(args.ffmpeg),
                "ffmpeg_sha256": snapshot_digest(render_snapshot, "ffmpeg executable"),
            },
            "outputs": outputs,
            "verification": {
                "duration_matches_score": outputs["master"]["frames"] == frames,
                "non_silent": outputs["master"]["non_silent"],
                "stems_non_silent": stems_non_silent,
                "polyphonic": polyphonic,
                "loudness_in_target": loudness_ok,
                "true_peak_in_target": peak_ok,
            },
        }
        args.out.mkdir(parents=True, exist_ok=True)
        receipt_path = args.out / "portable-audio-receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"READY: {args.out / 'delibes-master.wav'}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
