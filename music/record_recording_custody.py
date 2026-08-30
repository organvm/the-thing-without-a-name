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
import tempfile
import wave
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIO_RECEIPT = ROOT / ".work/music/competition/audio-render.json"
DEFAULT_OUTPUT = ROOT / "rights/evidence/delibes-recording-custody.json"
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
    if stem:
        stem_id = row.get("id")
        if not isinstance(stem_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]*", stem_id) is None:
            raise ValueError(f"{label}.id must be a stable lowercase identifier")
        result = {"id": stem_id, **result}
    return result


def checked_contract(row: Any, label: str) -> dict[str, Any]:
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
    if contract is not None:
        if not isinstance(contract, str) or SHA256.fullmatch(contract) is None:
            raise ValueError(f"audio receipt input {label}.contract_sha256 must be a SHA-256 digest")
        result["contract_sha256"] = contract
    return result


def build_receipt(
    audio_receipt_path: Path,
    *,
    work_id: str,
    recorded_on: str,
    root: Path = ROOT,
) -> dict[str, Any]:
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
    if not isinstance(audio, dict) or audio.get("schema") != "danse.audio.render.v1":
        raise ValueError("audio render receipt must be danse.audio.render.v1")
    if audio.get("profile") != "competition-classical":
        raise ValueError("audio render receipt must use the competition-classical profile")
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
    if not isinstance(stems_value, list) or len(stems_value) != 7:
        raise ValueError("audio render receipt must inventory exactly seven stems")
    stems = [checked_output(root, row, f"stems[{index}]", stem=True) for index, row in enumerate(stems_value)]
    stem_ids = [row["id"] for row in stems]
    if len(set(stem_ids)) != len(stem_ids):
        raise ValueError("audio render receipt repeats a stem id")
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
        "pre_normalized_master": pre_normalized_master,
        "master": master,
        "stems": stems,
        "contracts": {key: checked_contract(inputs.get(key), key) for key in REQUIRED_INPUTS},
        "verification": {
            "repeat_master_sha256": repeat_master,
            **{key: True for key in REQUIRED_VERIFICATION},
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
