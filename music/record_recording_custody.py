#!/usr/bin/env python3
"""Emit a public-safe receipt for ignored deterministic recording bytes.

The audio renderer correctly keeps stems and masters below ``.work/``. This
receipt is the durable bridge back into the tracked repertoire register: it
binds those ignored bytes and their exact render contract without claiming
music clearance, final-cut approval, upload, or submission.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import wave
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIO_RECEIPT = ROOT / ".work/music/competition/audio-render.json"
DEFAULT_OUTPUT = ROOT / "rights/evidence/delibes-recording-custody.json"
AUDIO_RENDER_SCHEMA = ROOT / "music/audio-render.schema.json"
DEFAULT_REPERTOIRE = ROOT / "music/repertoire.yaml"
DEFAULT_RIGHTS_REGISTER = ROOT / "rights/register.json"
REPERTOIRE_PATH = "music/repertoire.yaml"
CANONICAL_STEMS = (
    "violin-i",
    "violin-ii",
    "viola",
    "cello",
    "contrabass",
    "triangle",
    "timpani",
)
CANONICAL_REPOSITORY_INPUTS = {
    "score": "music/score.json",
    "choreography": "render/choreography.json",
    "midi": "music/delibes-screendance-suite.mid",
    "adaptation": "music/adaptation.json",
    "toolchain": "music/audio-toolchain.json",
    "mix": "music/delibes-mix.json",
    "audio_uses": "sound/audio-uses.json",
}
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
        if type(polyphonic) is not int or not 0 < polyphonic <= result["frames"]:
            raise ValueError(f"{label}.polyphonic_frames must be positive and no greater than frames")
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


def _render_row_identity(row: Any, label: str, *, stem: bool = False) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"{label} reproduction must be a mapping")
    keys = (
        "sha256",
        "frames",
        "sample_rate",
        "channels",
        "duration_seconds",
        "peak_sample",
        "rms_sample",
        "non_silent",
    )
    identity = {key: row.get(key) for key in keys}
    if stem:
        identity = {"id": row.get("id"), **identity}
    else:
        identity["polyphonic_frames"] = row.get("polyphonic_frames")
    return identity


def _assert_reproduced_outputs(audio: dict[str, Any], rendered: dict[str, Any], label: str) -> None:
    declared = audio.get("outputs")
    if not isinstance(declared, dict):
        raise ValueError("audio render receipt outputs must be a mapping")
    for name in ("pre_normalized_master", "master"):
        expected = _render_row_identity(declared.get(name), f"audio receipt {name}")
        actual = _render_row_identity(rendered.get(name), f"{label} {name}")
        if actual != expected:
            raise ValueError(f"{label} {name} does not equal the source audio receipt")
    declared_stems = declared.get("stems")
    rendered_stems = rendered.get("stems")
    if not isinstance(declared_stems, list) or not isinstance(rendered_stems, list):
        raise ValueError(f"{label} stems must be a list")
    expected_stems = [
        _render_row_identity(row, f"audio receipt stems[{index}]", stem=True)
        for index, row in enumerate(declared_stems)
    ]
    actual_stems = [
        _render_row_identity(row, f"{label} stems[{index}]", stem=True)
        for index, row in enumerate(rendered_stems)
    ]
    if actual_stems != expected_stems:
        raise ValueError(f"{label} stems do not equal the source audio receipt")
    if rendered.get("normalization") != audio.get("normalization"):
        raise ValueError(f"{label} normalization does not equal the source audio receipt")


def authenticate_production_outputs(
    audio: dict[str, Any],
    *,
    args: argparse.Namespace,
    contracts: dict[str, Any],
) -> None:
    """Reproduce the complete render twice and bind its outputs to the source receipt."""
    from render_music import render_once

    sample_rate = int(contracts["mix"]["sample_rate"])
    duration = Decimal(str(contracts["score"]["time"]["duration_seconds"]))
    frames = int((duration * sample_rate).to_integral_value(rounding=ROUND_HALF_UP))

    reproductions: list[dict[str, Any]] = []
    for index in range(2):
        # Keep only one full stem set on disk at a time; a production render is
        # hundreds of megabytes even though the returned identities are small.
        with tempfile.TemporaryDirectory(prefix=f"danse-custody-reproduction-{index + 1}-") as temporary:
            reproduced = render_once(
                Path(temporary),
                args=args,
                contracts=contracts,
                frames=frames,
                sample_rate=sample_rate,
            )
            _assert_reproduced_outputs(audio, reproduced, f"reproduction {index + 1}")
            reproductions.append(reproduced)

    # Each reproduction was compared with the source receipt. This explicit
    # comparison also makes the deterministic requirement legible in errors if
    # the renderer ever changes between consecutive runs.
    first = {
        "pre_normalized_master": _render_row_identity(reproductions[0]["pre_normalized_master"], "first"),
        "master": _render_row_identity(reproductions[0]["master"], "first"),
        "stems": [
            _render_row_identity(row, "first stem", stem=True)
            for row in reproductions[0]["stems"]
        ],
        "normalization": reproductions[0]["normalization"],
    }
    second = {
        "pre_normalized_master": _render_row_identity(reproductions[1]["pre_normalized_master"], "second"),
        "master": _render_row_identity(reproductions[1]["master"], "second"),
        "stems": [
            _render_row_identity(row, "second stem", stem=True)
            for row in reproductions[1]["stems"]
        ],
        "normalization": reproductions[1]["normalization"],
    }
    if first != second:
        raise ValueError("consecutive custody reproductions are not byte-deterministic")


def validate_production_input_graph(audio: dict[str, Any], *, root: Path = ROOT) -> tuple[str, ...]:
    """Re-run the authoritative render preflight against every receipt input."""
    if root.resolve() != ROOT.resolve():
        raise ValueError("production input validation requires the canonical repository root")
    inputs = audio["inputs"]
    for name, expected_path in CANONICAL_REPOSITORY_INPUTS.items():
        if inputs[name].get("path") != expected_path:
            raise ValueError(
                f"audio receipt input {name}.path must equal the canonical current artifact {expected_path}"
            )
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

    args = argparse.Namespace(
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
    contracts = validate_inputs(args)
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
    authenticate_production_outputs(audio, args=args, contracts=contracts)
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
    if pre_normalized_master.get("polyphonic_frames") != master.get("polyphonic_frames"):
        raise ValueError("audio render receipt masters must share one polyphonic frame count")
    repeat_master = verification.get("repeat_master_sha256")
    if repeat_master != master["sha256"]:
        raise ValueError("audio render receipt repeat master does not equal the final master")
    from render_music import pcm_slice_hash

    master_path = root / master["path"]
    for index, probe in enumerate(verification["seek_probes"]):
        if (
            type(probe.get("start_frame")) is not int
            or type(probe.get("frames")) is not int
            or probe["start_frame"] < 0
            or probe["frames"] <= 0
            or probe["start_frame"] + probe["frames"] > master["frames"]
        ):
            raise ValueError(f"audio render receipt seek probe {index} is outside the final master")
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


def _rights_repertoire_sources(document: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = document.get("bindings")
    music = bindings.get("music") if isinstance(bindings, dict) else None
    binding_source = music.get("source") if isinstance(music, dict) else None
    assets = document.get("assets")
    selected = [row for row in assets or [] if isinstance(row, dict) and row.get("id") == "selected-music"]
    provenance = selected[0].get("provenance") if len(selected) == 1 else None
    inventory_sources = [
        row
        for row in provenance or []
        if isinstance(row, dict) and row.get("path") == REPERTOIRE_PATH
    ]
    sources = [binding_source, *inventory_sources]
    if (
        len(selected) != 1
        or len(inventory_sources) != 1
        or len(sources) != 2
        or any(not isinstance(row, dict) or row.get("path") != REPERTOIRE_PATH for row in sources)
    ):
        raise ValueError("rights register must contain exactly the binding and selected-music repertoire identities")
    return [row for row in sources if isinstance(row, dict)]


def transition_recording_custody(
    repertoire: dict[str, Any],
    rights: dict[str, Any],
    receipt: dict[str, Any],
    *,
    receipt_path: str,
    receipt_sha256: str,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """Build the tracked, non-affirming custody transition and all hash rebindings."""
    if (
        not isinstance(receipt_path, str)
        or PurePosixPath(receipt_path).is_absolute()
        or PurePosixPath(receipt_path).as_posix() != receipt_path
        or any(part in {"", ".", ".."} for part in PurePosixPath(receipt_path).parts)
    ):
        raise ValueError("custody receipt path must be canonical and repository-relative")
    if not isinstance(receipt_sha256, str) or SHA256.fullmatch(receipt_sha256) is None:
        raise ValueError("custody receipt identity must be an exact SHA-256 digest")
    from validate_repertoire import validate_recording_custody_schema

    schema_errors = validate_recording_custody_schema(receipt)
    if schema_errors:
        raise ValueError("custody transition receipt is invalid: " + "; ".join(schema_errors))
    if receipt.get("schema") != "danse.music.recording-custody.v1" or receipt.get("status") != "custody-only":
        raise ValueError("custody transition requires a validated public-safe receipt")
    master = receipt.get("master")
    if not isinstance(master, dict) or not isinstance(master.get("path"), str) or not isinstance(master.get("sha256"), str):
        raise ValueError("custody receipt has no exact master identity")

    transitioned = copy.deepcopy(repertoire)
    works = transitioned.get("works")
    matching = [row for row in works or [] if isinstance(row, dict) and row.get("id") == receipt.get("work_id")]
    if len(matching) != 1:
        raise ValueError("custody receipt must identify exactly one repertoire work")
    recording = matching[0].get("recording")
    if not isinstance(recording, dict) or recording.get("status") not in {"pending-render", "project-authored"}:
        raise ValueError("custody transition requires the pending-render recording layer")
    if not isinstance(recording.get("render_contract"), dict):
        raise ValueError("custody transition must retain the deterministic render contract")
    receipt_identity = {"path": receipt_path, "sha256": receipt_sha256}
    expected_source = {
        "path": master["path"],
        "sha256": master["sha256"],
        "custody": "hydrated-derived",
        "receipt": receipt_identity,
    }
    if recording.get("status") == "project-authored" and recording.get("source") != expected_source:
        raise ValueError("custody transition cannot replace a different project-authored recording")
    recording["status"] = "project-authored"
    recording["source"] = expected_source
    evidence = recording.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("recording layer must retain its render-contract evidence")
    custody_rows = [row for row in evidence if isinstance(row, dict) and row.get("kind") == "recording-custody-receipt"]
    custody_evidence = {
        "kind": "recording-custody-receipt",
        "citation": (
            "Deterministic recording custody only; this does not approve music rights, "
            "credit, final cut, upload, or submission."
        ),
        "source": receipt_identity,
    }
    if custody_rows:
        if len(custody_rows) != 1:
            raise ValueError("recording layer has duplicate custody evidence")
        custody_rows[0].clear()
        custody_rows[0].update(custody_evidence)
    else:
        evidence.append(custody_evidence)

    repertoire_payload = yaml.safe_dump(
        transitioned,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    ).encode("utf-8")
    repertoire_digest = hashlib.sha256(repertoire_payload).hexdigest()
    rebound_rights = copy.deepcopy(rights)
    for source in _rights_repertoire_sources(rebound_rights):
        source["sha256"] = repertoire_digest
    rebound = _rights_repertoire_sources(rebound_rights)
    if any(source.get("sha256") != repertoire_digest for source in rebound):
        raise ValueError("rights register repertoire identities did not rebind atomically")
    return transitioned, repertoire_payload, rebound_rights


def _canonical_repository_output(path: Path, label: str) -> tuple[Path, str]:
    if "\\" in str(path) or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must use one canonical repository-relative spelling")
    absolute = path if path.is_absolute() else Path.cwd() / path
    try:
        relative = absolute.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise ValueError(f"{label} must remain inside the repository") from exc
    current = ROOT
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} must not traverse or replace a symlink")
    return absolute, relative


def _atomic_write_many(writes: list[tuple[Path, bytes]]) -> None:
    """Stage every payload first and roll back if any replacement fails."""
    if len({path for path, _ in writes}) != len(writes):
        raise ValueError("custody transition output paths must be distinct")
    snapshots = {
        path: (path.exists(), path.read_bytes() if path.exists() else b"")
        for path, _ in writes
    }
    staged: list[tuple[Path, Path]] = []
    try:
        for path, payload in writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as pending:
                pending.write(payload)
                staged.append((path, Path(pending.name)))
        replaced: list[Path] = []
        try:
            for path, pending_path in staged:
                os.replace(pending_path, path)
                replaced.append(path)
        except OSError:
            for path in reversed(replaced):
                existed, previous = snapshots[path]
                if existed:
                    with tempfile.NamedTemporaryFile(
                        "wb",
                        dir=path.parent,
                        prefix=f".{path.name}.rollback.",
                        delete=False,
                    ) as rollback:
                        rollback.write(previous)
                        rollback_path = Path(rollback.name)
                    os.replace(rollback_path, path)
                else:
                    path.unlink(missing_ok=True)
            raise
    finally:
        for _, pending_path in staged:
            pending_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-receipt", type=Path, default=DEFAULT_AUDIO_RECEIPT)
    parser.add_argument("--work-id", default="delibes-screendance-suite")
    parser.add_argument("--recorded-on", required=True, help="explicit YYYY-MM-DD custody date")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--apply-registers",
        action="store_true",
        help="transition repertoire custody and rebind both rights-register identities",
    )
    parser.add_argument("--repertoire", type=Path, default=DEFAULT_REPERTOIRE)
    parser.add_argument("--rights-register", type=Path, default=DEFAULT_RIGHTS_REGISTER)
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
        payload = json.dumps(document, indent=2) + "\n"
        receipt_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        output_path = args.out if args.out.is_absolute() else Path.cwd() / args.out
        if output_path.is_symlink():
            raise ValueError("custody receipt output must not be a symlink")
        transitioned: tuple[bytes, bytes] | None = None
        if args.apply_registers:
            output_path, relative_receipt = _canonical_repository_output(args.out, "custody receipt output")
            repertoire_path, relative_repertoire = _canonical_repository_output(
                args.repertoire,
                "repertoire register",
            )
            rights_path, relative_rights = _canonical_repository_output(
                args.rights_register,
                "rights register",
            )
            if relative_repertoire != REPERTOIRE_PATH or relative_rights != "rights/register.json":
                raise ValueError("applied custody transition must update the canonical repertoire and rights registers")
            repertoire = yaml.safe_load(args.repertoire.read_text())
            rights = json.loads(args.rights_register.read_text())
            if not isinstance(repertoire, dict) or not isinstance(rights, dict):
                raise ValueError("repertoire and rights registers must be mappings")
            _, repertoire_payload, rebound_rights = transition_recording_custody(
                repertoire,
                rights,
                document,
                receipt_path=relative_receipt,
                receipt_sha256=receipt_digest,
            )
            transitioned = (
                repertoire_payload,
                (json.dumps(rebound_rights, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            )
        writes = [(output_path, payload.encode("utf-8"))]
        if transitioned is not None:
            writes.extend([(repertoire_path, transitioned[0]), (rights_path, transitioned[1])])
        _atomic_write_many(writes)
    except (OSError, ValueError, TypeError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"ok: {args.out} ({sha256(args.out)})")
    if args.apply_registers:
        print("ok: repertoire transitioned and both rights-register identities rebound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
