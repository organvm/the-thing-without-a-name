#!/usr/bin/env python3
"""Validate Danse's layered music rights/provenance register.

The declared JSON Schema is enforced by a dependency-free validator for every
keyword the register uses. Semantic checks additionally resolve tracked paths,
verify SHA-256 bytes, and keep composition status from being mistaken for
edition, MIDI, performance, recording, or sample clearance.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import subprocess
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import yaml

from record_recording_custody import (
    CANONICAL_REPOSITORY_INPUTS,
    CANONICAL_STEMS,
    hydrated_receipt_errors,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTER = ROOT / "music" / "repertoire.yaml"
DEFAULT_SCHEMA = ROOT / "music" / "repertoire.schema.json"
RECORDING_CUSTODY_SCHEMA = ROOT / "music" / "recording-custody.schema.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LAYERS = ("composition", "edition", "arrangement_midi", "performance", "recording", "samples")
RIGHTS_STATUSES = {
    "absent",
    "fixture-only",
    "project-authored",
    "public-domain",
    "licensed",
    "restricted",
    "unverified",
    "pending-render",
    "not-applicable",
}
SELECTABLE = {
    "composition": {"project-authored", "public-domain", "licensed"},
    "edition": {"not-applicable", "project-authored", "public-domain", "licensed"},
    "arrangement_midi": {"project-authored", "licensed"},
    "performance": {"project-authored", "licensed"},
    # Selection is an artistic decision, not a false recording receipt.  A
    # selected score may wait on its deterministic render; delivery rejects
    # pending-render until a digest-bound audio receipt replaces it.
    "recording": {"project-authored", "licensed", "pending-render"},
    "samples": {"none", "project-authored", "licensed"},
}
VISUAL_CHANNELS = {"divergence", "spread", "azimuth", "elevation", "projK", "turnover"}


def _schema_type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float) and math.isfinite(float(value)),
        "boolean": type(value) is bool,
        "null": value is None,
    }.get(expected, False)


def _resolve_local_ref(schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError(f"unsupported non-local schema reference {reference!r}")
    node: Any = schema
    for raw in reference[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or key not in node:
            raise ValueError(f"unresolved schema reference {reference!r}")
        node = node[key]
    if not isinstance(node, dict):
        raise ValueError(f"schema reference {reference!r} is not an object")
    return node


def _schema_errors(
    value: Any,
    rule: dict[str, Any],
    schema: dict[str, Any],
    location: str,
) -> list[str]:
    """Validate the Draft 2020-12 keyword subset used by repertoire.schema.json."""
    if "$ref" in rule:
        try:
            target = _resolve_local_ref(schema, rule["$ref"])
        except ValueError as exc:
            return [f"schema document: {exc}"]
        return _schema_errors(value, target, schema, location)

    errors: list[str] = []
    for child in rule.get("allOf", []):
        errors.extend(_schema_errors(value, child, schema, location))
    alternatives = rule.get("anyOf")
    if alternatives and not any(not _schema_errors(value, child, schema, location) for child in alternatives):
        errors.append(f"{location}: does not match any allowed schema")

    if "const" in rule and value != rule["const"]:
        errors.append(f"{location}: must equal {rule['const']!r}")
    if "enum" in rule and value not in rule["enum"]:
        errors.append(f"{location}: must be one of {rule['enum']!r}")

    declared_types = rule.get("type")
    if declared_types is not None:
        allowed = [declared_types] if isinstance(declared_types, str) else declared_types
        if not isinstance(allowed, list) or not any(_schema_type_matches(value, name) for name in allowed):
            errors.append(f"{location}: must have JSON type {' or '.join(map(str, allowed))}")
            return errors

    if isinstance(value, dict):
        properties = rule.get("properties", {})
        required = rule.get("required", [])
        for name in value:
            if not isinstance(name, str):
                errors.append(f"{location}: object property names must be strings; got {name!r}")
        for name in required:
            if name not in value:
                errors.append(f"{location}.{name}: is required")
        for name, child in properties.items():
            if name in value:
                errors.extend(_schema_errors(value[name], child, schema, f"{location}.{name}"))
        if rule.get("additionalProperties") is False:
            unknown = [name for name in value if isinstance(name, str) and name not in properties]
            for name in sorted(unknown):
                errors.append(f"{location}.{name}: additional property is not allowed")

    if isinstance(value, list):
        minimum = rule.get("minItems")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{location}: must contain at least {minimum} item(s)")
        maximum = rule.get("maxItems")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(f"{location}: must contain at most {maximum} item(s)")
        child = rule.get("items")
        if isinstance(child, dict):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item, child, schema, f"{location}[{index}]"))

    if isinstance(value, str):
        minimum = rule.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{location}: must contain at least {minimum} character(s)")
        pattern = rule.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{location}: does not match {pattern!r}")

    if type(value) in (int, float) and not isinstance(value, bool):
        if "minimum" in rule and value < rule["minimum"]:
            errors.append(f"{location}: must be at least {rule['minimum']}")
        if "maximum" in rule and value > rule["maximum"]:
            errors.append(f"{location}: must be at most {rule['maximum']}")
        if "exclusiveMinimum" in rule and value <= rule["exclusiveMinimum"]:
            errors.append(f"{location}: must be greater than {rule['exclusiveMinimum']}")

    return errors


def validate_schema_instance(
    register: Any,
    path: Path = DEFAULT_SCHEMA,
) -> list[str]:
    return validate_json_instance(register, path, "register")


def validate_json_instance(value: Any, path: Path, label: str) -> list[str]:
    try:
        schema = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"schema document: {exc}"]
    if not isinstance(schema, dict):
        return ["schema document: root must be an object"]
    return _schema_errors(value, schema, schema, label)


def validate_recording_custody_schema(
    receipt: Any,
    path: Path = RECORDING_CUSTODY_SCHEMA,
) -> list[str]:
    try:
        schema = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"recording custody schema: {exc}"]
    if not isinstance(schema, dict):
        return ["recording custody schema: root must be an object"]
    errors = _schema_errors(receipt, schema, schema, "recording custody receipt")
    if not isinstance(receipt, dict):
        return errors
    recorded_on = receipt.get("recorded_on")
    if isinstance(recorded_on, str):
        try:
            dt.date.fromisoformat(recorded_on)
        except ValueError:
            errors.append("recording custody receipt.recorded_on: must be a valid calendar date")
    master = receipt.get("master")
    verification = receipt.get("verification")
    if isinstance(master, dict) and isinstance(verification, dict):
        if verification.get("repeat_master_sha256") != master.get("sha256"):
            errors.append(
                "recording custody receipt.verification.repeat_master_sha256: "
                "must equal the final master digest"
            )
        probes = verification.get("seek_probes")
        if isinstance(probes, list):
            for index, probe in enumerate(probes):
                if isinstance(probe, dict) and probe.get("sha256") != probe.get("repeat_sha256"):
                    errors.append(
                        f"recording custody receipt.verification.seek_probes[{index}]: "
                        "repeat digest must equal the first digest"
                    )
    stems = receipt.get("stems")
    artifacts = [receipt.get("pre_normalized_master"), master]
    if isinstance(stems, list):
        artifacts.extend(stems)
        stem_ids = [row.get("id") for row in stems if isinstance(row, dict) and isinstance(row.get("id"), str)]
        if len(stem_ids) != len(set(stem_ids)):
            errors.append("recording custody receipt.stems: ids must be unique")
        if len(stem_ids) == len(stems) and tuple(stem_ids) != CANONICAL_STEMS:
            errors.append("recording custody receipt.stems: ids must equal the canonical competition mix order")
    paths = [row.get("path") for row in artifacts if isinstance(row, dict) and isinstance(row.get("path"), str)]
    if len(paths) != len(set(paths)):
        errors.append("recording custody receipt: audio output paths must be unique")
    shapes = [
        (row.get("frames"), row.get("sample_rate"), row.get("channels"), row.get("sample_format"))
        for row in artifacts
        if isinstance(row, dict)
        and type(row.get("frames")) is int
        and type(row.get("sample_rate")) is int
        and type(row.get("channels")) is int
        and isinstance(row.get("sample_format"), str)
    ]
    if len(shapes) == len(artifacts) and len(set(shapes)) != 1:
        errors.append("recording custody receipt: all audio outputs must share one exact PCM shape")
    return errors


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_register(path: Path = DEFAULT_REGISTER) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else {}


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def validate_document(
    register: dict[str, Any],
    *,
    root: Path = ROOT,
    check_derived: bool = True,
    require_hydrated: bool = False,
) -> list[str]:
    errors = validate_schema_instance(register)

    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        listed = None
        errors.append(f"repository sources: cannot query Git index: {exc}")
    if listed is not None and listed.returncode != 0:
        errors.append(
            "repository sources: cannot query Git index: "
            + listed.stderr.decode("utf-8", errors="replace").strip()
        )
        tracked: set[str] | None = None
    elif listed is None:
        tracked = None
    else:
        tracked = {
            item.decode("utf-8", errors="surrogateescape")
            for item in listed.stdout.split(b"\0")
            if item
        }

    def error(location: str, message: str) -> None:
        errors.append(f"{location}: {message}")

    def mapping(value: Any, location: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            error(location, "must be a mapping")
            return {}
        return value

    def evidence(layer: dict[str, Any], location: str) -> None:
        rows = layer.get("evidence")
        if not isinstance(rows, list) or not rows:
            error(f"{location}.evidence", "must contain at least one evidence record")
            return
        for index, row in enumerate(rows):
            row = mapping(row, f"{location}.evidence[{index}]")
            if not isinstance(row.get("kind"), str) or not row["kind"].strip():
                error(f"{location}.evidence[{index}].kind", "must be non-empty")
            if not isinstance(row.get("citation"), str) or not row["citation"].strip():
                error(f"{location}.evidence[{index}].citation", "must be non-empty")
            if row.get("source") is not None:
                source(row.get("source"), f"{location}.evidence[{index}].source", required=False)

    def source(
        value: Any,
        location: str,
        *,
        required: bool,
        allow_hydrated: bool = False,
        allow_derived: bool = False,
    ) -> tuple[str, str] | None:
        if value is None:
            if required:
                error(location, "is required")
            return None
        row = mapping(value, location)
        relative = row.get("path")
        digest = row.get("sha256")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            error(f"{location}.path", "must be a non-empty repository-relative path")
            return None
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            error(f"{location}.sha256", "must be a lowercase SHA-256 digest")
            return None
        normalized = Path(relative).as_posix()
        custody = row.get("custody")
        if custody in {"hydrated-local", "hydrated-derived"}:
            if custody == "hydrated-local" and not allow_hydrated:
                error(f"{location}.custody", "hydrated-local is not allowed for this provenance layer")
                return relative, digest
            if custody == "hydrated-derived" and not allow_derived:
                error(f"{location}.custody", "hydrated-derived is allowed only for a rendered recording")
                return relative, digest
            relative_path = Path(relative)
            work_root = (root / ".work").resolve()
            if (
                not normalized.startswith(".work/")
                or any(part in {"", ".", ".."} for part in relative_path.parts)
                or not _inside(work_root, (root / relative_path).resolve())
            ):
                error(f"{location}.path", f"{custody} bytes must live below .work/")
            if tracked is not None and normalized in tracked:
                error(f"{location}.path", f"{custody} bytes must remain untracked")
            if custody == "hydrated-local":
                if not isinstance(row.get("source_url"), str) or not row["source_url"].strip():
                    error(f"{location}.source_url", "must identify the hydration source")
                notice = row.get("license_notice")
                source(notice, f"{location}.license_notice", required=True)
            else:
                source(row.get("receipt"), f"{location}.receipt", required=True)
            candidate = root / relative
            if not candidate.exists():
                if require_hydrated:
                    error(f"{location}.path", f"required hydrated bytes are absent: {relative}")
                return relative, digest
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                error(f"{location}.path", f"does not resolve to hydrated source bytes: {relative}")
                return relative, digest
            if candidate.is_symlink() or not candidate.is_file() or not _inside(root.resolve(), resolved):
                error(f"{location}.path", "must be a regular file inside the repository")
                return relative, digest
            actual = sha256(candidate)
            if actual != digest:
                error(f"{location}.sha256", f"declares {digest}, actual {actual}")
            return relative, digest
        if custody is not None:
            error(f"{location}.custody", f"unknown custody {custody!r}")
        if tracked is None or normalized not in tracked:
            error(f"{location}.path", "must be tracked by Git and available in a clean checkout")
            return relative, digest
        candidate = root / relative
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            error(f"{location}.path", f"does not resolve to a tracked source: {relative}")
            return relative, digest
        if candidate.is_symlink() or not candidate.is_file() or not _inside(root.resolve(), resolved):
            error(f"{location}.path", "must be a regular file inside the repository")
            return relative, digest
        actual = sha256(candidate)
        if actual != digest:
            error(f"{location}.sha256", f"declares {digest}, actual {actual}")
        return relative, digest

    if register.get("schema") != "danse.music.repertoire.v1":
        error("schema", "must be danse.music.repertoire.v1")
    gate = mapping(register.get("artistic_gate"), "artistic_gate")
    if gate.get("status") not in {"pending", "accepted", "rejected"}:
        error("artistic_gate.status", "must be pending, accepted, or rejected")
    if not isinstance(gate.get("authority"), str) or not gate["authority"].strip():
        error("artistic_gate.authority", "must name the human authority")
    if not isinstance(gate.get("note"), str) or not gate["note"].strip():
        error("artistic_gate.note", "must explain the gate")
    if gate.get("status") == "accepted" and not gate.get("evidence"):
        error("artistic_gate.evidence", "is required when the gate is accepted")

    works = register.get("works")
    if not isinstance(works, list) or not works:
        error("works", "must contain at least one work")
        return errors
    seen: set[str] = set()
    for index, candidate in enumerate(works):
        location = f"works[{index}]"
        work = mapping(candidate, location)
        work_id = work.get("id")
        if not isinstance(work_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id):
            error(f"{location}.id", "must be a stable lowercase identifier")
            work_id = f"index-{index}"
        if work_id in seen:
            error(f"{location}.id", "must be unique")
        seen.add(work_id)
        if work.get("role") not in {"fixture", "candidate", "repertoire"}:
            error(f"{location}.role", "must be fixture, candidate, or repertoire")

        selection = mapping(work.get("selection"), f"{location}.selection")
        selection_status = selection.get("status")
        if selection_status not in {"not-selected", "pending", "selected", "rejected"}:
            error(f"{location}.selection.status", "has an unknown status")
        if not isinstance(selection.get("authority"), str) or not selection["authority"].strip():
            error(f"{location}.selection.authority", "must name the human authority")
        if work.get("role") == "fixture" and selection_status == "selected":
            error(f"{location}.selection.status", "a contract fixture cannot be selected as repertoire")

        layer_rows: dict[str, dict[str, Any]] = {}
        for layer_name in LAYERS:
            layer = mapping(work.get(layer_name), f"{location}.{layer_name}")
            layer_rows[layer_name] = layer
            status = layer.get("status")
            allowed = RIGHTS_STATUSES | ({"none"} if layer_name == "samples" else set())
            if status not in allowed:
                error(f"{location}.{layer_name}.status", f"unknown rights status {status!r}")
            if layer_name != "samples" and status == "licensed":
                license_id = layer.get("license")
                if not isinstance(license_id, str) or not license_id.strip():
                    error(f"{location}.{layer_name}.license", "must identify the license for licensed material")
            evidence(layer, f"{location}.{layer_name}")
            sources = layer.get("sources")
            if sources is not None:
                if not isinstance(sources, list) or not sources:
                    error(f"{location}.{layer_name}.sources", "must contain one or more tracked sources")
                else:
                    for source_index, source_value in enumerate(sources):
                        source(
                            source_value,
                            f"{location}.{layer_name}.sources[{source_index}]",
                            required=True,
                        )

        composition = layer_rows["composition"]
        for field in ("title", "composer", "date"):
            if composition.get(field) in (None, ""):
                error(f"{location}.composition.{field}", "is required")
        arrangement = layer_rows["arrangement_midi"]
        performance = layer_rows["performance"]
        arrangement_source = source(
            arrangement.get("source"),
            f"{location}.arrangement_midi.source",
            required=arrangement.get("status") not in {"absent", "unverified"},
        )
        performance_source = source(
            performance.get("source"),
            f"{location}.performance.source",
            required=performance.get("status") not in {"absent", "unverified"},
        )
        for layer_name in ("composition", "edition"):
            layer = layer_rows[layer_name]
            source(layer.get("source"), f"{location}.{layer_name}.source", required=False)
        recording = layer_rows["recording"]
        recording_source = source(
            recording.get("source"),
            f"{location}.recording.source",
            required=recording.get("status") in {"project-authored", "licensed"},
            allow_derived=True,
        )
        recording_source_row = recording.get("source")
        if isinstance(recording_source_row, dict) and recording_source_row.get("custody") == "hydrated-derived":
            if recording.get("status") != "project-authored":
                error(
                    f"{location}.recording.status",
                    "hydrated-derived custody is valid only for a project-authored recording",
                )
            receipt_reference = recording_source_row.get("receipt")
            if isinstance(receipt_reference, dict) and isinstance(receipt_reference.get("path"), str):
                receipt_path = root / receipt_reference["path"]
                try:
                    receipt_document = json.loads(receipt_path.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    error(f"{location}.recording.source.receipt", f"cannot read recording custody receipt: {exc}")
                else:
                    for receipt_error in validate_recording_custody_schema(receipt_document):
                        error(f"{location}.recording.source.receipt", receipt_error)
                    if isinstance(receipt_document, dict):
                        if receipt_document.get("work_id") != work_id:
                            error(
                                f"{location}.recording.source.receipt.work_id",
                                "must identify the repertoire work",
                            )
                        master = receipt_document.get("master")
                        master_identity = (
                            (master.get("path"), master.get("sha256"))
                            if isinstance(master, dict)
                            else None
                        )
                        if recording_source is not None and master_identity != recording_source:
                            error(
                                f"{location}.recording.source",
                                "must equal the master identity in its tracked custody receipt",
                            )
                        receipt_location = f"{location}.recording.source.receipt"
                        for metadata_name in ("generator", "source_schema"):
                            source(
                                receipt_document.get(metadata_name),
                                f"{receipt_location}.{metadata_name}",
                                required=True,
                            )
                        contracts = receipt_document.get("contracts")
                        if isinstance(contracts, dict):
                            for contract_name, expected_path in CANONICAL_REPOSITORY_INPUTS.items():
                                contract_row = contracts.get(contract_name)
                                declared_path = (
                                    contract_row.get("path") if isinstance(contract_row, dict) else None
                                )
                                if declared_path != expected_path:
                                    error(
                                        f"{receipt_location}.contracts.{contract_name}.path",
                                        f"must equal the canonical current artifact {expected_path}",
                                    )
                            contract_identities: dict[str, tuple[str, str] | None] = {}
                            for contract_name, contract_row in contracts.items():
                                if contract_name == "soundfont":
                                    continue
                                contract_identities[contract_name] = source(
                                    contract_row,
                                    f"{receipt_location}.contracts.{contract_name}",
                                    required=True,
                                )

                            derived_rows = work.get("derived_artifacts")
                            derived_by_kind: dict[str, tuple[str, str]] = {}
                            if isinstance(derived_rows, list):
                                for derived_row in derived_rows:
                                    if (
                                        isinstance(derived_row, dict)
                                        and isinstance(derived_row.get("kind"), str)
                                        and isinstance(derived_row.get("path"), str)
                                        and isinstance(derived_row.get("sha256"), str)
                                    ):
                                        derived_by_kind[derived_row["kind"]] = (
                                            derived_row["path"],
                                            derived_row["sha256"],
                                        )
                            expected_contracts = {
                                "midi": arrangement_source,
                                "adaptation": derived_by_kind.get("adaptation-contract"),
                                "toolchain": derived_by_kind.get("audio-toolchain-contract"),
                                "audio_uses": derived_by_kind.get("audio-use-manifest"),
                            }
                            for contract_name, expected_identity in expected_contracts.items():
                                if contract_identities.get(contract_name) != expected_identity:
                                    error(
                                        f"{receipt_location}.contracts.{contract_name}",
                                        "must equal the current repertoire/toolchain source identity",
                                    )

                            parsed_contracts: dict[str, dict[str, Any]] = {}
                            for contract_name in ("score", "choreography", "toolchain", "mix"):
                                identity = contract_identities.get(contract_name)
                                if identity is None:
                                    continue
                                try:
                                    parsed = json.loads((root / identity[0]).read_text())
                                except (OSError, json.JSONDecodeError) as exc:
                                    error(
                                        f"{receipt_location}.contracts.{contract_name}",
                                        f"cannot read current contract: {exc}",
                                    )
                                else:
                                    if isinstance(parsed, dict):
                                        parsed_contracts[contract_name] = parsed

                            toolchain = parsed_contracts.get("toolchain", {})
                            for contract_name in ("mix", "soundfont"):
                                expected_row = toolchain.get(contract_name)
                                receipt_row = contracts.get(contract_name)
                                expected_identity = (
                                    (expected_row.get("path"), expected_row.get("sha256"))
                                    if isinstance(expected_row, dict)
                                    else None
                                )
                                receipt_identity = (
                                    (receipt_row.get("path"), receipt_row.get("sha256"))
                                    if isinstance(receipt_row, dict)
                                    else None
                                )
                                if receipt_identity != expected_identity:
                                    error(
                                        f"{receipt_location}.contracts.{contract_name}",
                                        "must equal the current audio-toolchain source identity",
                                    )

                            score_document = parsed_contracts.get("score", {})
                            score_identity = score_document.get("identity")
                            score_row = contracts.get("score")
                            midi_row = contracts.get("midi")
                            if isinstance(score_identity, dict) and isinstance(score_row, dict):
                                if score_identity.get("work_id") != work_id:
                                    error(f"{receipt_location}.contracts.score", "must identify the repertoire work")
                                midi_digest = midi_row.get("sha256") if isinstance(midi_row, dict) else None
                                if score_identity.get("midi_sha256") != midi_digest:
                                    error(
                                        f"{receipt_location}.contracts.score",
                                        "must bind the current repertoire MIDI",
                                    )
                                if score_identity.get("contract_sha256") != score_row.get("contract_sha256"):
                                    error(f"{receipt_location}.contracts.score", "contract identity is stale")

                            choreography_document = parsed_contracts.get("choreography", {})
                            choreography_identity = choreography_document.get("identity")
                            choreography_row = contracts.get("choreography")
                            if isinstance(choreography_identity, dict) and isinstance(choreography_row, dict):
                                if choreography_identity.get("contract_sha256") != choreography_row.get(
                                    "contract_sha256"
                                ):
                                    error(f"{receipt_location}.contracts.choreography", "contract identity is stale")
                                if choreography_identity.get("score_contract_sha256") != (
                                    score_row.get("contract_sha256") if isinstance(score_row, dict) else None
                                ):
                                    error(
                                        f"{receipt_location}.contracts.choreography",
                                        "must bind the custody receipt score contract",
                                    )

                            executables = receipt_document.get("executables")
                            if isinstance(executables, dict):
                                for executable_name in ("fluidsynth", "ffmpeg"):
                                    expected = toolchain.get(executable_name)
                                    declared = executables.get(executable_name)
                                    expected_identity = (
                                        (expected.get("executable_sha256"), expected.get("version"))
                                        if isinstance(expected, dict)
                                        else None
                                    )
                                    declared_identity = (
                                        (declared.get("sha256"), declared.get("version"))
                                        if isinstance(declared, dict)
                                        else None
                                    )
                                    if declared_identity != expected_identity:
                                        error(
                                            f"{receipt_location}.executables.{executable_name}",
                                            "must equal the current pinned executable identity",
                                        )

                            mix_document = parsed_contracts.get("mix", {})
                            mix_master = mix_document.get("master")
                            mix_normalization = (
                                mix_master.get("normalization") if isinstance(mix_master, dict) else None
                            )
                            custody_normalization = receipt_document.get("normalization")
                            if isinstance(mix_normalization, dict) and isinstance(custody_normalization, dict):
                                expected_targets = {
                                    "integrated_lufs": mix_normalization.get("target_lufs"),
                                    "tolerance_lu": mix_normalization.get("tolerance_lu"),
                                    "target_true_peak_dbtp": mix_normalization.get("target_true_peak_dbtp"),
                                    "max_true_peak_dbtp": mix_normalization.get("max_true_peak_dbtp"),
                                    "lra_lu": mix_normalization.get("target_lra_lu"),
                                }
                                if custody_normalization.get("method") != mix_normalization.get("method"):
                                    error(f"{receipt_location}.normalization.method", "must equal the current mix")
                                if custody_normalization.get("targets") != expected_targets:
                                    error(f"{receipt_location}.normalization.targets", "must equal the current mix")

                            score_time = score_document.get("time")
                            duration = score_time.get("duration_seconds") if isinstance(score_time, dict) else None
                            if type(duration) in (int, float):
                                expected_frames = int(
                                    (Decimal(str(duration)) * 48_000).to_integral_value(rounding=ROUND_HALF_UP)
                                )
                                audio_rows = [receipt_document.get("pre_normalized_master"), master]
                                stems = receipt_document.get("stems")
                                if isinstance(stems, list):
                                    audio_rows.extend(stems)
                                for audio_index, audio_row in enumerate(audio_rows):
                                    if isinstance(audio_row, dict) and audio_row.get("frames") != expected_frames:
                                        error(
                                            f"{receipt_location}.audio[{audio_index}].frames",
                                            f"must equal the current score duration ({expected_frames} frames)",
                                        )
                            sample_items = layer_rows["samples"].get("items")
                            sample_identities: set[tuple[Any, Any]] = set()
                            if isinstance(sample_items, list):
                                for sample_item in sample_items:
                                    sample_source = (
                                        sample_item.get("source") if isinstance(sample_item, dict) else None
                                    )
                                    if isinstance(sample_source, dict):
                                        sample_path = sample_source.get("path")
                                        sample_digest = sample_source.get("sha256")
                                        if isinstance(sample_path, str) and isinstance(sample_digest, str):
                                            sample_identities.add((sample_path, sample_digest))
                            soundfont_row = contracts.get("soundfont")
                            soundfont_identity = (
                                (soundfont_row.get("path"), soundfont_row.get("sha256"))
                                if isinstance(soundfont_row, dict)
                                else None
                            )
                            if soundfont_identity not in sample_identities:
                                error(
                                    f"{receipt_location}.contracts.soundfont",
                                    "must identify a current repertoire sample source",
                                )
                        for receipt_error in hydrated_receipt_errors(
                            receipt_document,
                            root=root,
                            require_hydrated=require_hydrated,
                        ):
                            error(f"{location}.recording.source.receipt", receipt_error)
        derived_recording = (
            isinstance(recording_source_row, dict)
            and recording_source_row.get("custody") == "hydrated-derived"
        )
        render_contract_identity = source(
            recording.get("render_contract"),
            f"{location}.recording.render_contract",
            required=recording.get("status") == "pending-render" or derived_recording,
        )
        if render_contract_identity is not None:
            canonical_render_contract = "music/audio-render.schema.json"
            expected_render_contract = (
                canonical_render_contract,
                sha256(root / canonical_render_contract),
            )
            if render_contract_identity != expected_render_contract:
                error(
                    f"{location}.recording.render_contract",
                    "must equal the canonical deterministic audio-render contract",
                )

        samples = layer_rows["samples"]
        items = samples.get("items")
        if not isinstance(items, list):
            error(f"{location}.samples.items", "must be a list")
        else:
            if samples.get("status") == "none" and items:
                error(f"{location}.samples.items", "must be empty when sample status is none")
            if samples.get("status") != "none" and not items:
                error(f"{location}.samples.items", "must identify source bytes when sample status is not none")
            for item_index, item_value in enumerate(items):
                item = mapping(item_value, f"{location}.samples.items[{item_index}]")
                source(
                    item.get("source"),
                    f"{location}.samples.items[{item_index}].source",
                    required=True,
                    allow_hydrated=True,
                )
                if not item.get("license"):
                    error(f"{location}.samples.items[{item_index}].license", "is required")

        score = mapping(work.get("score"), f"{location}.score")
        midi_source = source(score.get("source_midi"), f"{location}.score.source_midi", required=True)
        if arrangement_source and midi_source and arrangement_source != midi_source:
            error(f"{location}.score.source_midi", "must identify the exact arrangement/MIDI bytes")
        if performance_source and midi_source and performance_source != midi_source:
            error(f"{location}.performance.source", "must identify the exact performed MIDI bytes")
        dynamics_source = mapping(score.get("dynamics_source"), f"{location}.score.dynamics_source")
        if type(dynamics_source.get("track")) is not int or dynamics_source["track"] < 0:
            error(f"{location}.score.dynamics_source.track", "must be a non-negative MIDI track index")
        if type(dynamics_source.get("channel")) is not int or not 0 <= dynamics_source["channel"] <= 15:
            error(f"{location}.score.dynamics_source.channel", "must be a MIDI channel from 0 through 15")
        bindings = score.get("cue_bindings")
        if not isinstance(bindings, dict):
            error(f"{location}.score.cue_bindings", "must be a mapping")
        else:
            for cue_id, binding_value in bindings.items():
                binding = mapping(binding_value, f"{location}.score.cue_bindings.{cue_id}")
                if not isinstance(binding.get("window_beats"), (int, float)) or binding["window_beats"] <= 0:
                    error(f"{location}.score.cue_bindings.{cue_id}.window_beats", "must be positive")
                visual = mapping(binding.get("visual"), f"{location}.score.cue_bindings.{cue_id}.visual")
                unknown = set((visual.get("channel_offsets") or {})) - VISUAL_CHANNELS
                if unknown:
                    error(
                        f"{location}.score.cue_bindings.{cue_id}.visual.channel_offsets",
                        f"unknown channel(s): {', '.join(sorted(unknown))}",
                    )

        roles = work.get("dramatic_roles")
        if not isinstance(roles, list) or not roles or any(not isinstance(role, str) or not role for role in roles):
            error(f"{location}.dramatic_roles", "must name one or more program movements")
        elif len(set(roles)) != len(roles):
            error(f"{location}.dramatic_roles", "must not repeat a movement")

        recording_status = layer_rows["recording"].get("status")
        if composition.get("status") == "public-domain" and recording_status in {"restricted", "unverified"}:
            error(
                f"{location}.recording.status",
                "public-domain composition status does not clear a restricted or unverified recording",
            )
        if selection_status == "selected":
            if gate.get("status") != "accepted" or not selection.get("evidence"):
                error(f"{location}.selection", "selected repertoire requires the accepted human gate and evidence")
            for layer_name, permitted in SELECTABLE.items():
                status = layer_rows[layer_name].get("status")
                if status not in permitted:
                    error(
                        f"{location}.{layer_name}.status",
                        f"selected repertoire requires one of {', '.join(sorted(permitted))}; got {status!r}",
                    )

        derived = work.get("derived_artifacts")
        if not isinstance(derived, list):
            error(f"{location}.derived_artifacts", "must be a list")
        elif check_derived:
            for artifact_index, artifact_value in enumerate(derived):
                artifact = mapping(artifact_value, f"{location}.derived_artifacts[{artifact_index}]")
                source(
                    {"path": artifact.get("path"), "sha256": artifact.get("sha256")},
                    f"{location}.derived_artifacts[{artifact_index}]",
                    required=True,
                )

    return errors


def validate_schema_document(path: Path = DEFAULT_SCHEMA) -> list[str]:
    try:
        schema = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"schema document: {exc}"]
    errors = []
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        errors.append("schema document: must declare JSON Schema draft 2020-12")
    if schema.get("properties", {}).get("schema", {}).get("const") != "danse.music.repertoire.v1":
        errors.append("schema document: does not bind danse.music.repertoire.v1")
    return errors


def display_path(path: Path) -> str:
    """Prefer a repository-relative diagnostic without rejecting external paths."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(resolved)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("register", nargs="?", type=Path, default=DEFAULT_REGISTER)
    parser.add_argument("--allow-stale-derived", action="store_true", help="skip derived artifact byte checks")
    parser.add_argument("--require-hydrated", action="store_true", help="require and hash all hydrated-local bytes")
    args = parser.parse_args()
    try:
        register = load_register(args.register)
    except (OSError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    errors = [
        *validate_schema_document(),
        *validate_document(
            register,
            check_derived=not args.allow_stale_derived,
            require_hydrated=args.require_hydrated,
        ),
    ]
    if errors:
        for row in errors:
            print(f"FAIL: {row}")
        return 1
    print(
        f"ok: {display_path(args.register)} "
        f"({len(register['works'])} work(s); artistic gate {register['artistic_gate']['status']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
