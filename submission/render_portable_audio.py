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
    load_json,
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


def require_bound(reference: dict, selected: Path, label: str) -> None:
    relative = reference.get("path")
    expected = reference.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise ValueError(f"{label}: incomplete source binding")
    bound = (ROOT / relative).resolve()
    if bound != selected.resolve():
        raise ValueError(f"{label}: path differs from the committed binding")
    if sha256(selected) != expected:
        raise ValueError(f"{label}: digest differs from the committed binding")


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


def repository_identity() -> dict:
    """Return the clean tracked commit/tree that owns the portable producer."""

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(f"git {' '.join(arguments)} failed")
        return result.stdout.strip()

    dirty = git("status", "--porcelain=v1", "--untracked-files=no")
    if dirty:
        raise ValueError("tracked worktree is dirty; portable evidence requires reviewed source")
    return {
        "head": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
    }


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

    try:
        if args.fluidsynth is None or args.ffmpeg is None:
            raise ValueError("fluidsynth and ffmpeg are required")
        for executable in (args.fluidsynth, args.ffmpeg):
            if not executable.is_file():
                raise ValueError(f"missing executable: {executable}")

        score = document(score_path, "danse.music.score.v1")
        adaptation = document(adaptation_path, "danse.music.adaptation.v1")
        mix = document(mix_path, "danse.audio.mix.v1")
        toolchain = document(toolchain_path, "danse.audio.toolchain.v1")
        choreography = document(choreography_path, "danse.choreography.v1")
        audio_uses = load_json(audio_uses_path, "danse.audio.uses.v1")
        profile_id = audio_uses.get("competition_profile")
        profiles = audio_uses.get("profiles")
        profile = profiles.get(profile_id) if isinstance(profiles, dict) else None
        if not isinstance(profile, dict) or profile.get("package_eligible") is not True:
            raise ValueError(f"audio use profile {profile_id!r} is not package-eligible")

        require_bound(toolchain["midi"], midi_path, "MIDI")
        require_bound(toolchain["adaptation"], adaptation_path, "adaptation")
        require_bound(toolchain["mix"], mix_path, "mix")
        require_bound(toolchain["soundfont"], args.soundfont, "soundfont")
        require_bound(toolchain["renderer"], renderer_path, "renderer")

        declared_sources = profile.get("declared_sources")
        if not isinstance(declared_sources, list):
            raise ValueError("competition audio profile must declare its sources")
        forbidden = set(profile.get("forbidden_source_kinds") or [])
        for index, source in enumerate(declared_sources):
            if not isinstance(source, dict):
                raise ValueError(f"audio source {index} is malformed")
            if source.get("kind") in forbidden:
                raise ValueError(f"audio source {source.get('id')} is forbidden")
            require_hash(source, f"audio source {source.get('id')}")
            if isinstance(source.get("license_notice"), dict):
                require_hash(
                    source["license_notice"],
                    f"audio source {source.get('id')}.license_notice",
                )
        stems = mix.get("stems")
        required_stems = profile.get("required_stems")
        if not isinstance(stems, list) or [row.get("id") for row in stems] != required_stems:
            raise ValueError("mix stem order differs from the competition audio profile")

        midi_digest = sha256(midi_path)
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
            "executable_sha256": sha256(args.ffmpeg),
        }
        contracts = {
            "mix": mix,
            "fluidsynth": toolchain["fluidsynth"],
            "ffmpeg": ffmpeg_contract,
        }
        runtime_args = Namespace(
            midi=midi_path,
            fluidsynth=args.fluidsynth,
            soundfont=args.soundfont,
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
            raise ValueError("portable master failed silence/loudness/true-peak checks")

        receipt = {
            "schema": "danse.submission.portable-audio.v1",
            "purpose": "ScreenDance Miami 2027 deadline screener",
            "canonical_exhibition_receipt": False,
            "repository": repository_identity(),
            "inputs": {
                "toolchain": {
                    "path": toolchain_path.relative_to(ROOT).as_posix(),
                    "sha256": sha256(toolchain_path),
                },
                "audio_uses": {
                    "path": audio_uses_path.relative_to(ROOT).as_posix(),
                    "sha256": sha256(audio_uses_path),
                    "profile": profile_id,
                },
                "renderer": {
                    "path": renderer_path.relative_to(ROOT).as_posix(),
                    "sha256": sha256(renderer_path),
                },
                "score": {"path": score_path.relative_to(ROOT).as_posix(), "sha256": sha256(score_path)},
                "choreography": {
                    "path": choreography_path.relative_to(ROOT).as_posix(),
                    "sha256": sha256(choreography_path),
                    "identity": choreography.get("identity"),
                },
                "midi": {"path": midi_path.relative_to(ROOT).as_posix(), "sha256": midi_digest},
                "adaptation": {
                    "path": adaptation_path.relative_to(ROOT).as_posix(),
                    "sha256": sha256(adaptation_path),
                },
                "mix": {"path": mix_path.relative_to(ROOT).as_posix(), "sha256": sha256(mix_path)},
                "soundfont": {"path": str(args.soundfont), "sha256": sha256(args.soundfont)},
            },
            "runtime": {
                "python": sys.version.split()[0],
                "fluidsynth": fluidsynth_line,
                "fluidsynth_sha256": sha256(args.fluidsynth),
                "ffmpeg": ffmpeg_line,
                "ffmpeg_sha256": sha256(args.ffmpeg),
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
