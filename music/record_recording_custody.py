#!/usr/bin/env python3
"""Emit a public-safe receipt for ignored deterministic recording bytes.

The audio renderer correctly keeps stems and masters below ``.work/``. This
receipt is the durable bridge back into the tracked repertoire register: it
binds those ignored bytes and their exact render contract without claiming
music clearance, final-cut approval, upload, or submission.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import wave
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIO_RECEIPT = ROOT / ".work/music/competition/audio-render.json"
DEFAULT_OUTPUT = ROOT / "rights/evidence/delibes-recording-custody.json"
AUDIO_RENDER_SCHEMA = ROOT / "music/audio-render.schema.json"
CANONICAL_STEMS = (
    "violin-i",
    "violin-ii",
    "viola",
    "cello",
    "contrabass",
    "triangle",
    "timpani",
)
REQUIRED_INPUTS = (
    "score",
    "choreography",
    "midi",
    "adaptation",
    "toolchain",
    "mix",
    "audio_uses",
    "soundfont",
)
REQUIRED_VERIFICATION = (
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
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def work_path(root: Path, relative: Any, label: str) -> Path:
    if not isinstance(relative, str) or not relative.startswith(".work/") or "\\" in relative:
        raise ValueError(f"{label}.path must be a canonical .work/ path")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or posix.as_posix() != relative or any(part in {"", ".", ".."} for part in posix.parts):
        raise ValueError(f"{label}.path must be a canonical .work/ path")
    candidate = root.joinpath(*posix.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to((root / ".work").resolve())
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label}.path does not resolve below .work/: {relative}") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label}.path must be a non-symlink regular file")
    return candidate


def checked_output(root: Path, row: Any, label: str, *, stem: bool = False) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"{label} must be a mapping")
    candidate = work_path(root, row.get("path"), label)
    declared = row.get("sha256")
    actual = sha256(candidate)
    if not isinstance(declared, str) or declared != actual:
        raise ValueError(f"{label}.sha256 declares {declared!r}, actual {actual}")
    result = {
        "path": row["path"],
        "sha256": actual,
        "bytes": candidate.stat().st_size,
        "frames": row.get("frames"),
        "sample_rate": row.get("sample_rate"),
        "channels": row.get("channels"),
    }
    for key in ("frames", "sample_rate", "channels"):
        if type(result[key]) is not int or result[key] <= 0:
            raise ValueError(f"{label}.{key} must be a positive integer")
    try:
        with wave.open(str(candidate), "rb") as handle:
            if handle.getsampwidth() != 2 or handle.getcomptype() != "NONE":
                raise ValueError(f"{label}.path must be uncompressed signed 16-bit PCM WAV")
            actual_audio = {
                "frames": handle.getnframes(),
                "sample_rate": handle.getframerate(),
                "channels": handle.getnchannels(),
            }
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError(f"{label}.path must be a readable WAV file") from exc
    for key, actual_value in actual_audio.items():
        if result[key] != actual_value:
            raise ValueError(f"{label}.{key} declares {result[key]}, actual {actual_value}")
    result["sample_format"] = "signed-16-bit-little-endian-pcm"
    sound = str(root / "sound")
    if sound not in sys.path:
        sys.path.insert(0, sound)
    from render_music import inspect_wav

    measured = inspect_wav(candidate, sample_rate=result["sample_rate"], frames=result["frames"])
    for key in ("duration_seconds", "peak_sample", "rms_sample", "non_silent"):
        if row.get(key) != measured[key]:
            raise ValueError(f"{label}.{key} declares {row.get(key)!r}, actual {measured[key]!r}")
        result[key] = measured[key]
    if "polyphonic_frames" in row:
        polyphonic = row.get("polyphonic_frames")
        if type(polyphonic) is not int or polyphonic <= 0:
            raise ValueError(f"{label}.polyphonic_frames must be a positive integer")
        result["polyphonic_frames"] = polyphonic
    if stem:
        stem_id = row.get("id")
        if not isinstance(stem_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]*", stem_id) is None:
            raise ValueError(f"{label}.id must be a stable lowercase identifier")
        result = {"id": stem_id, **result}
    return result


def checked_contract(row: Any, label: str, *, require_contract: bool = False) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"audio receipt input {label} must be a mapping")
    relative = row.get("path")
    digest = row.get("sha256")
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or PurePosixPath(relative).is_absolute()
        or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
    ):
        raise ValueError(f"audio receipt input {label}.path must be canonical and repository-relative")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise ValueError(f"audio receipt input {label}.sha256 must be a SHA-256 digest")
    result = {"path": relative, "sha256": digest}
    contract = row.get("contract_sha256")
    if require_contract and contract is None:
        raise ValueError(f"audio receipt input {label}.contract_sha256 is required")
    if contract is not None:
        if not isinstance(contract, str) or SHA256.fullmatch(contract) is None:
            raise ValueError(f"audio receipt input {label}.contract_sha256 must be a SHA-256 digest")
        result["contract_sha256"] = contract
    return result


def _input_path(root: Path, row: dict[str, Any], label: str, *, absolute: bool = False) -> Path:
    value = row.get("path")
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"audio receipt input {label}.path is invalid")
    if absolute:
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ValueError(f"audio receipt input {label}.path must be absolute")
    else:
        posix = PurePosixPath(value)
        if posix.is_absolute() or posix.as_posix() != value or any(part in {"", ".", ".."} for part in posix.parts):
            raise ValueError(f"audio receipt input {label}.path must be canonical and repository-relative")
        candidate = root.joinpath(*posix.parts)
        try:
            candidate.resolve(strict=True).relative_to(root.resolve())
        except (OSError, ValueError) as exc:
            raise ValueError(f"audio receipt input {label}.path must resolve inside the repository") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"audio receipt input {label}.path must be a non-symlink regular file")
    declared = row.get("sha256")
    actual = sha256(candidate)
    if declared != actual:
        raise ValueError(f"audio receipt input {label}.sha256 declares {declared!r}, actual {actual}")
    return candidate


def validate_production_input_graph(audio: dict[str, Any], *, root: Path = ROOT) -> tuple[str, ...]:
    """Re-run the authoritative render preflight against every receipt input."""
    if root.resolve() != ROOT.resolve():
        raise ValueError("production input validation requires the canonical repository root")
    inputs = audio["inputs"]
    paths = {
        name: _input_path(root, inputs[name], name)
        for name in REQUIRED_INPUTS
    }
    paths["fluidsynth_executable"] = _input_path(
        root,
        inputs["fluidsynth_executable"],
        "fluidsynth_executable",
        absolute=True,
    )
    paths["ffmpeg_executable"] = _input_path(
        root,
        inputs["ffmpeg_executable"],
        "ffmpeg_executable",
        absolute=True,
    )

    sound = str(root / "sound")
    if sound not in sys.path:
        sys.path.insert(0, sound)
    from render_music import contract_identity, validate_inputs

    contracts = validate_inputs(
        argparse.Namespace(
            score=paths["score"],
            choreography=paths["choreography"],
            midi=paths["midi"],
            adaptation=paths["adaptation"],
            toolchain=paths["toolchain"],
            mix=paths["mix"],
            audio_uses=paths["audio_uses"],
            profile=audio["profile"],
            fluidsynth=paths["fluidsynth_executable"],
            ffmpeg=paths["ffmpeg_executable"],
            soundfont=paths["soundfont"],
        )
    )
    if inputs["score"]["contract_sha256"] != contract_identity(contracts["score"], "score"):
        raise ValueError("audio receipt score contract identity is stale")
    if inputs["choreography"]["contract_sha256"] != contract_identity(
        contracts["choreography"],
        "choreography",
    ):
        raise ValueError("audio receipt choreography contract identity is stale")
    if inputs["score"]["duration_seconds"] != contracts["score"]["time"]["duration_seconds"]:
        raise ValueError("audio receipt score duration is stale")
    expected_stems = tuple(row["id"] for row in contracts["mix"]["stems"])
    if expected_stems != CANONICAL_STEMS:
        raise ValueError("authoritative competition mix has an unexpected stem contract")
    return expected_stems


def build_receipt(
    audio_receipt_path: Path,
    *,
    work_id: str,
    recorded_on: str,
    root: Path = ROOT,
) -> dict[str, Any]:
    if not audio_receipt_path.is_absolute():
        audio_receipt_path = root / audio_receipt_path
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None:
        raise ValueError("work id must be a stable lowercase identifier")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", recorded_on) is None:
        raise ValueError("recorded-on must be an explicit YYYY-MM-DD date")
    try:
        dt.date.fromisoformat(recorded_on)
    except ValueError as exc:
        raise ValueError("recorded-on must be a valid calendar date") from exc
    try:
        audio = json.loads(audio_receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"audio render receipt: {exc}") from exc
    from validate_repertoire import validate_json_instance

    schema_errors = validate_json_instance(audio, AUDIO_RENDER_SCHEMA, "audio render receipt")
    if schema_errors:
        raise ValueError("audio render receipt schema: " + "; ".join(schema_errors))
    expected_stems = validate_production_input_graph(audio, root=root)
    verification = audio.get("verification")
    if not isinstance(verification, dict) or any(verification.get(key) is not True for key in REQUIRED_VERIFICATION):
        raise ValueError("audio render receipt has an incomplete or false verification gate")
    inputs = audio.get("inputs")
    outputs = audio.get("outputs")
    if not isinstance(inputs, dict) or not isinstance(outputs, dict):
        raise ValueError("audio render receipt inputs and outputs must be mappings")
    master = checked_output(root, outputs.get("master"), "master")
    pre_normalized_master = checked_output(
        root,
        outputs.get("pre_normalized_master"),
        "pre_normalized_master",
    )
    stems_value = outputs.get("stems")
    if not isinstance(stems_value, list) or len(stems_value) != len(expected_stems):
        raise ValueError("audio render receipt must inventory exactly seven stems")
    stems = [checked_output(root, row, f"stems[{index}]", stem=True) for index, row in enumerate(stems_value)]
    stem_ids = [row["id"] for row in stems]
    if tuple(stem_ids) != expected_stems:
        raise ValueError("audio render receipt stem order differs from the canonical competition mix")
    outputs_by_path = [pre_normalized_master, master, *stems]
    output_paths = [row["path"] for row in outputs_by_path]
    if len(set(output_paths)) != len(output_paths):
        raise ValueError("audio render receipt repeats an output path")
    audio_shapes = {(row["frames"], row["sample_rate"], row["channels"]) for row in outputs_by_path}
    if len(audio_shapes) != 1:
        raise ValueError("audio render receipt outputs do not share one exact audio shape")
    repeat_master = verification.get("repeat_master_sha256")
    if repeat_master != master["sha256"]:
        raise ValueError("audio render receipt repeat master does not equal the final master")
    from render_music import pcm_slice_hash

    master_path = root / master["path"]
    for index, probe in enumerate(verification["seek_probes"]):
        actual_probe = pcm_slice_hash(master_path, probe["start_frame"], probe["frames"])
        if probe["sha256"] != actual_probe or probe["repeat_sha256"] != actual_probe:
            raise ValueError(f"audio render receipt seek probe {index} does not match the final master")
    audio_path = work_path(root, audio_receipt_path.relative_to(root).as_posix(), "audio_render")
    return {
        "schema": "danse.music.recording-custody.v1",
        "status": "custody-only",
        "profile": "competition-classical",
        "work_id": work_id,
        "recorded_on": recorded_on,
        "audio_render": {
            "path": audio_path.relative_to(root).as_posix(),
            "sha256": sha256(audio_path),
            "bytes": audio_path.stat().st_size,
        },
        "generator": {
            "path": "music/record_recording_custody.py",
            "sha256": sha256(Path(__file__)),
        },
        "source_schema": {
            "path": "music/audio-render.schema.json",
            "sha256": sha256(AUDIO_RENDER_SCHEMA),
        },
        "pre_normalized_master": pre_normalized_master,
        "master": master,
        "stems": stems,
        "contracts": {
            key: checked_contract(
                inputs.get(key),
                key,
                require_contract=key in {"score", "choreography"},
            )
            for key in REQUIRED_INPUTS
        },
        "executables": {
            name.removesuffix("_executable"): {
                "sha256": inputs[name]["sha256"],
                "version": inputs[name]["version"],
            }
            for name in ("fluidsynth_executable", "ffmpeg_executable")
        },
        "normalization": {
            "method": audio["normalization"]["method"],
            "targets": audio["normalization"]["targets"],
            "output": audio["normalization"]["output"],
        },
        "verification": {
            "repeat_master_sha256": repeat_master,
            **{key: True for key in REQUIRED_VERIFICATION},
            "seek_probes": audio["verification"]["seek_probes"],
        },
        "clearance": {
            "gate": "music-cleared",
            "state": "pending",
            "note": (
                "This receipt binds deterministic recording custody only; it does not approve "
                "music rights, credit, final cut, upload, or submission."
            ),
        },
    }


def hydrated_receipt_errors(
    receipt: dict[str, Any],
    *,
    root: Path = ROOT,
    require_hydrated: bool = False,
) -> list[str]:
    errors: list[str] = []
    audio = receipt.get("audio_render") if isinstance(receipt, dict) else None
    relative = audio.get("path") if isinstance(audio, dict) else None
    if not isinstance(relative, str):
        return ["recording custody receipt.audio_render.path is required"]
    candidate = root / relative
    if not candidate.exists():
        if require_hydrated:
            errors.append(f"recording custody receipt requires hydrated audio render bytes: {relative}")
        return errors
    try:
        rebuilt = build_receipt(
            candidate,
            work_id=receipt.get("work_id", ""),
            recorded_on=receipt.get("recorded_on", ""),
            root=root,
        )
    except (OSError, ValueError, TypeError) as exc:
        return [f"recording custody receipt: {exc}"]
    if rebuilt != receipt:
        errors.append("recording custody receipt does not equal the hydrated audio-render graph")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-receipt", type=Path, default=DEFAULT_AUDIO_RECEIPT)
    parser.add_argument("--work-id", default="delibes-screendance-suite")
    parser.add_argument("--recorded-on", required=True, help="explicit YYYY-MM-DD custody date")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        document = build_receipt(
            args.audio_receipt,
            work_id=args.work_id,
            recorded_on=args.recorded_on,
        )
        from validate_repertoire import validate_recording_custody_schema

        schema_errors = validate_recording_custody_schema(document)
        if schema_errors:
            raise ValueError("; ".join(schema_errors))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(document, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
            "w",
            dir=args.out.parent,
            prefix=f".{args.out.name}.",
            delete=False,
        ) as pending:
            pending.write(payload)
            pending_path = Path(pending.name)
        os.replace(pending_path, args.out)
    except (OSError, ValueError, TypeError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"ok: {args.out} ({sha256(args.out)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
