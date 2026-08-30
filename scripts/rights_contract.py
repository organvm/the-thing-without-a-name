#!/usr/bin/env python3
"""Strict, redacted, phase-aware rights contract for Danse.

The tracked register is an inventory and a gate, never a substitute for a
release, signature, repertoire decision, legal review, or artist attestation.
Private evidence stays outside Git. Only a public-safe receipt with an exact
digest may satisfy a tracked gate.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parent.parent
REGISTER = ROOT / "rights" / "register.json"
SCHEMA = ROOT / "rights" / "register.schema.json"
SUBMISSION = ROOT / "submission" / "screendance-2027.yaml"
PHASES = ("draft", "public", "package", "uploaded", "submitted", "release")
PHASE_SCOPES = {
    "draft": (),
    "public": ("public",),
    "package": ("package",),
    "uploaded": ("package", "uploaded"),
    "submitted": ("package", "uploaded", "submitted"),
    "release": ("public", "package", "release"),
}
MAX_JSON_BYTES = 8 << 20
EXPECTED_CATEGORIES = {
    "performer",
    "photograph",
    "video",
    "design",
    "pictured-object",
    "archive",
    "font",
    "texture",
    "recording",
    "still",
    "music",
    "software",
    "text",
    "installation-evidence",
    "other-third-party",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
SAFE_TIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(
    r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"
    r"|(?<![\w])\+(?:\d[ .()/-]*){7,14}\d(?!\d)"
)
PRIVATE_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:[\\/]Users[\\/]|file://|(?:^|\s)~[\\/])")
ABSOLUTE_PATH = re.compile(
    r"(?:(?:^|(?<=[\s(\[{=;,:'\"<>]))/(?!/)[^\s'\"<>]+"
    r"|(?:^|(?<=[\s(\[{=;,'\"<>]))//[^/\s]+/[^\s'\"<>]+"
    r"|(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s'\"<>]+)"
)
PRIVATE_PREFIXES = (".git/", ".work/", ".worktrees/", "pipeline/.work/", "sound/bank/")
SENSITIVE_KEYS = {
    "address",
    "credential",
    "credentials",
    "email",
    "local_path",
    "password",
    "phone",
    "private_path",
    "secret",
    "signature",
    "token",
}
RIGHTS_MEDIA_SUFFIXES = {
    ".avif",
    ".flac",
    ".gif",
    ".mov",
    ".mp4",
    ".mp3",
    ".mxf",
    ".m4v",
    ".m4a",
    ".jpg",
    ".jpeg",
    ".png",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
    ".webp",
    ".aif",
    ".aiff",
}
PRODUCTION_RECEIPT = "provenance/production.json"
PRODUCER_RECEIPTS = "provenance/producer-receipts"
DELIBES_CUSTODY_PATH = "rights/evidence/delibes-source-license-custody.json"
DELIBES_CUSTODY_SHA256 = "92f5df76904b11cc543055c12d1f2e426e78130c5ff9006a06b6ff1f947ed545"
MUSESCORE_GENERAL_NOTICE_PATH = "music/licenses/MuseScore_General_License.md"
MUSESCORE_GENERAL_NOTICE_SHA256 = "9486e6baeb3eb274cd3f0e131cf1bc50ac4b57f548791b1b6678def7988f19d4"
MUSESCORE_GENERAL_SF3_SHA256 = "5b85b6c2c61d10b2b91cddd41efcce7b25cd31c8271d511c73afafbef20b6fa3"
ADAPTED_DELIBES_MIDI_PATH = "music/delibes-screendance-suite.mid"
ADAPTED_DELIBES_MIDI_SHA256 = "a42b36415e6b41f63778e19b6b171b34c65eeca3c862c22eb0f80ee67980f199"
DELIBES_ADAPTATION_PATH = "music/adaptation.json"
DELIBES_ADAPTATION_SHA256 = "19ee6f43b45f18b7737483d158c9363f6788b156c7380f0b8ebb29b4b9f72d21"
AUDIO_USES_PATH = "sound/audio-uses.json"
AUDIO_USES_SHA256 = "d312487481ca456c4f26357b18fef7f0f58b5d8ae4a3dc5e687895d9c98f53e4"
AUDIO_TOOLCHAIN_PATH = "music/audio-toolchain.json"
AUDIO_MIX_PATH = "music/delibes-mix.json"
COMPETITION_AUDIO_RENDER_RECEIPT = ROOT / ".work" / "music" / "competition" / "audio-render.json"
COMPETITION_AUDIO_PROFILE = "competition-classical"
HYBRID_AUDIO_PROFILE = "hybrid-apartment"
COMPETITION_SOURCE_IDS = ("delibes-chamber-midi", "musescore-general-sf3")
COMPETITION_STEM_IDS = (
    "violin-i",
    "violin-ii",
    "viola",
    "cello",
    "contrabass",
    "triangle",
    "timpani",
)
AUDIO_IDENTITY_HASH_FIELDS = (
    "audio_uses_sha256",
    "score_file_sha256",
    "score_contract_sha256",
    "choreography_file_sha256",
    "choreography_contract_sha256",
    "midi_sha256",
    "adaptation_sha256",
    "toolchain_sha256",
    "mix_sha256",
    "soundfont_sha256",
    "audio_render_receipt_sha256",
    "master_sha256",
)
AUDIO_SOUND_FIELDS = (
    "profile",
    *AUDIO_IDENTITY_HASH_FIELDS,
    "sources",
    "stems",
    "credit",
)
COMPETITION_AUDIO_REQUIREMENTS = (
    ("delibes-public-domain-compositions", "competition-composition"),
    ("paul-de-bra-source-arrangements", "competition-arrangement"),
    ("adapted-delibes-midi", "competition-midi"),
    ("musescore-general-soundfont", "competition-soundfont"),
    ("selected-music", "score-audio"),
)
HYBRID_AUDIO_REQUIREMENT = ("room-source-recordings", "hybrid-apartment-grains")
REQUIRED_DELIBES_CREDIT = (
    "Music by Léo Delibes. Source arrangements by Paul De Bra, adapted and "
    "re-orchestrated for Danse under CC BY 4.0. Changes include instrumentation, "
    "sequencing, cue markers, and mix."
)
DELIBES_SOURCE_FILES = (
    (
        "music/sources/Valse-Lente-Delibes.mscz",
        "76e183b57c7f035a319bf5a7c5691d61c8a5f3af61f9d1b860047f7b28e6dc70",
    ),
    (
        "music/sources/Valse-Coppelia.mscz",
        "86e1eaad1e99fcf3f275af9c59cde94580d9f9bfb10f7d366053a398295001c7",
    ),
)


class RightsError(ValueError):
    """The rights register or a bound artifact violates its contract."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _submission_zone(submission: dict[str, Any]) -> tuple[str, ZoneInfo]:
    """Resolve the one named shipping zone declared by the submission register."""
    deadline = submission.get("deadline")
    opportunity = submission.get("opportunity_snapshot")
    timezone_name = opportunity.get("timezone") if isinstance(opportunity, dict) else None
    if not isinstance(timezone_name, str) or not timezone_name:
        raise RightsError("submission deadline has no canonical named timezone")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise RightsError("submission deadline names an unavailable timezone") from exc
    try:
        hard_wall = datetime.fromisoformat(deadline["hard_wall"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RightsError("submission deadline has no valid hard wall") from exc
    if hard_wall.tzinfo is None:
        raise RightsError("submission deadline hard wall has no timezone offset")
    local_wall = hard_wall.astimezone(zone)
    if local_wall.replace(tzinfo=None) != hard_wall.replace(tzinfo=None):
        raise RightsError("submission hard wall does not agree with its named timezone")
    return timezone_name, zone


def project_zone() -> tuple[str, ZoneInfo]:
    """Load the shipping zone from the canonical submission register."""
    return _submission_zone(load_yaml(SUBMISSION, "submission register", expose_path=False))


def project_today() -> date:
    """Return the project shipping date independently of the host timezone."""
    _, zone = project_zone()
    return datetime.now(zone).date()


def _stable_file_measure(
    path: Path,
    label: str,
    *,
    capture: bool = False,
) -> tuple[str, int, bytes | None]:
    """Hash one regular-file generation and optionally retain those exact bytes."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RightsError(f"{label} cannot be read as one stable regular file") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RightsError(f"{label} is not a regular file")
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if capture else None
        total = 0
        while True:
            block = os.read(descriptor, 1 << 20)
            if not block:
                break
            total += len(block)
            if capture and total > MAX_JSON_BYTES:
                raise RightsError(f"{label} exceeds the bounded JSON size")
            digest.update(block)
            if chunks is not None:
                chunks.append(block)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity or total != after.st_size:
            raise RightsError(f"{label} changed while it was being read")
        payload = b"".join(chunks) if chunks is not None else None
        return digest.hexdigest(), total, payload
    except OSError as exc:
        raise RightsError(f"{label} changed while it was being read") from exc
    finally:
        os.close(descriptor)


def value_sha256(value: Any) -> str:
    """Hash a canonical public-safe value without depending on source formatting."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def same_strings(value: object, expected: list[str] | tuple[str, ...]) -> bool:
    """Compare an external sequence without sorting attacker-controlled types."""
    return (
        isinstance(value, list)
        and all(isinstance(item, str) for item in value)
        and sorted(value) == sorted(expected)
    )


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RightsError(f"JSON contains duplicate key {key!r}")
        value[key] = item
    return value


def _parse_json_bytes(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicate_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RightsError(f"cannot read {label}: invalid or unreadable JSON") from exc
    if not isinstance(value, dict):
        raise RightsError(f"{label} must be a JSON object")
    return value


def load_json(path: Path, label: str, *, expose_path: bool = True) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        detail = f" at {path}: {exc}" if expose_path else ": invalid or unreadable JSON"
        raise RightsError(f"cannot read {label}{detail}") from exc
    return _parse_json_bytes(payload, label)


def load_yaml(path: Path, label: str, *, expose_path: bool = True) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader) or {}
    except (OSError, yaml.YAMLError) as exc:
        detail = f" at {path}: {exc}" if expose_path else ": invalid or unreadable YAML"
        raise RightsError(f"cannot read {label}{detail}") from exc
    if not isinstance(value, dict):
        raise RightsError(f"{label} must be a mapping")
    return value


def approved_credit_contract(
    document: dict[str, Any],
    gate_id: str,
) -> list[dict[str, Any]]:
    """Return the exact asset labels whose publication one gate approves."""
    assets = {
        asset.get("id"): asset
        for asset in document.get("assets", [])
        if isinstance(asset, dict) and isinstance(asset.get("id"), str)
    }
    approved: list[dict[str, Any]] = []
    for rule in document.get("credit_rules", []):
        if not isinstance(rule, dict) or rule.get("gate") != gate_id:
            continue
        asset_id = rule.get("asset")
        asset = assets.get(asset_id)
        credit = asset.get("public_credit") if isinstance(asset, dict) else None
        approved.append(
            {
                "asset_id": asset_id,
                "label": credit.get("label") if isinstance(credit, dict) else None,
            }
        )
    return sorted(approved, key=lambda row: str(row["asset_id"]))


def validate_gate_decision_receipt(
    path: Path,
    gate: dict[str, Any],
    approved_credits: list[dict[str, Any]] | None = None,
) -> tuple[Any, list[str]]:
    """Validate one public-safe receipt as the typed decision for exactly one gate."""
    label = f"human gate {gate['id']} decision receipt"
    try:
        receipt = load_json(path, label, expose_path=False)
    except RightsError as exc:
        return None, [str(exc)]
    expected_keys = {
        "schema",
        "gate_id",
        "authority",
        "decision",
        "required_for",
        "approved_credits",
    }
    errors: list[str] = []
    if set(receipt) != expected_keys:
        errors.append(f"{label} has fields outside the typed decision contract")
    if receipt.get("schema") != "danse.rights.decision.v2":
        errors.append(f"{label} has the wrong schema")
    if receipt.get("gate_id") != gate["id"]:
        errors.append(f"{label} names a different gate")
    if receipt.get("authority") != gate["authority"]:
        errors.append(f"{label} names a different authority")
    required_for = receipt.get("required_for")
    if not same_strings(required_for, gate["required_for"]):
        errors.append(f"{label} has different phase scope")
    expected_credits = approved_credits or []
    receipt_credits = receipt.get("approved_credits")
    if (
        not isinstance(receipt_credits, list)
        or any(
            not isinstance(row, dict) or set(row) != {"asset_id", "label"}
            for row in receipt_credits
        )
        or receipt_credits != expected_credits
    ):
        errors.append(f"{label} does not bind the exact approved credit wording")
    decision = receipt.get("decision")
    attestation = gate["attestation"]
    if attestation is None:
        valid_decision = decision is True
    elif attestation["kind"] == "boolean":
        valid_decision = decision is True
    else:
        valid_decision = isinstance(decision, str) and decision in attestation["values"]
    if not valid_decision:
        errors.append(f"{label} has no registered affirmative decision")
    return decision if not errors else None, errors


def validate_use_decision_receipt(
    path: Path,
    asset: dict[str, Any],
    use: dict[str, Any],
) -> list[str]:
    """Require clearance evidence to bind one exact asset, use, and granted scope."""
    label = f"asset use {asset['id']}/{use['id']} decision receipt"
    try:
        receipt = load_json(path, label, expose_path=False)
    except RightsError as exc:
        return [str(exc)]
    expected_keys = {
        "schema",
        "asset_id",
        "use_id",
        "authority",
        "decision",
        "medium",
        "required_for",
        "territory",
        "term",
        "expires",
        "promotion",
        "archive",
    }
    errors: list[str] = []
    if set(receipt) != expected_keys:
        errors.append(f"{label} has fields outside the typed use-decision contract")
    exact = {
        "schema": "danse.rights.use-decision.v1",
        "asset_id": asset["id"],
        "use_id": use["id"],
        "authority": asset["rights_holder"],
        "decision": "cleared",
        "medium": use["medium"],
        "territory": use["territory"],
        "term": use["term"],
        "expires": use["expires"],
        "promotion": use["promotion"],
        "archive": use["archive"],
    }
    for key, expected in exact.items():
        if receipt.get(key) != expected:
            errors.append(f"{label} has a different {key.replace('_', ' ')}")
    if not same_strings(receipt.get("required_for"), use["required_for"]):
        errors.append(f"{label} has different phase scope")
    return errors


def validate_media_clearance_receipt(
    path: Path,
    media_id: str,
    rule: dict[str, Any],
    source: dict[str, Any],
    clearance: dict[str, Any],
) -> list[str]:
    """Bind one clearance decision to one exact staged release artifact."""
    label = f"release media {media_id} typed clearance receipt"
    try:
        receipt = load_json(path, label, expose_path=False)
    except RightsError as exc:
        return [str(exc)]
    expected_keys = {
        "schema",
        "media_id",
        "destination",
        "sha256",
        "bytes",
        "authority",
        "decision",
        "required_for",
    }
    errors: list[str] = []
    if set(receipt) != expected_keys:
        errors.append(f"{label} has fields outside the typed media-clearance contract")
    exact = {
        "schema": "danse.rights.media-clearance.v1",
        "media_id": media_id,
        "destination": rule["destination"],
        "sha256": source.get("sha256"),
        "bytes": source.get("bytes"),
        "authority": clearance.get("owner"),
        "decision": "cleared",
    }
    for key, expected in exact.items():
        if receipt.get(key) != expected:
            errors.append(f"{label} has a different {key.replace('_', ' ')}")
    if not same_strings(receipt.get("required_for"), rule["required_for"]):
        errors.append(f"{label} has different phase scope")
    return errors


def safe_relative(value: object, label: str, *, expose_value: bool = True) -> str:
    if not isinstance(value, str) or not value:
        raise RightsError(f"{label} must be a non-empty relative path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or "\\" in value
        or value != pure.as_posix()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        detail = f": {value!r}" if expose_value else ""
        raise RightsError(f"{label} is not a safe portable relative path{detail}")
    relative = pure.as_posix()
    if relative in {prefix.rstrip("/") for prefix in PRIVATE_PREFIXES} or relative.startswith(PRIVATE_PREFIXES):
        raise RightsError(f"{label} points into private or generated custody: {relative!r}")
    return relative


def regular_file(root: Path, relative: object, label: str, *, expose_value: bool = True) -> Path:
    relative = safe_relative(relative, label, expose_value=expose_value)
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise RightsError(f"repository root must be a regular directory: {root}")
    root = root.resolve()
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            detail = f": {relative!r}" if expose_value else ""
            raise RightsError(f"{label} traverses a symlink{detail}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        detail = f": {relative!r}" if expose_value else ""
        raise RightsError(f"{label} is missing or outside the repository{detail}") from exc
    if not resolved.is_file():
        detail = f": {relative!r}" if expose_value else ""
        raise RightsError(f"{label} is not a regular file{detail}")
    return resolved


def tracked_paths(root: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise RightsError(f"cannot query the Git source inventory: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RightsError(f"cannot query the Git source inventory: {detail or f'exit {result.returncode}'}")
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


def verify_record(root: Path, record: dict[str, Any], label: str, tracked: set[str]) -> Path:
    relative = safe_relative(record.get("path"), f"{label} path")
    if relative not in tracked:
        raise RightsError(f"{label} path is not tracked by Git: {relative!r}")
    path = regular_file(root, relative, f"{label} path")
    expected = record.get("sha256")
    if not isinstance(expected, str) or not HEX64.fullmatch(expected):
        raise RightsError(f"{label} has no valid lowercase SHA-256")
    actual = sha256(path)
    if actual != expected:
        raise RightsError(f"{label} digest mismatch for {relative}: expected {expected}, got {actual}")
    return path


def _source_records(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for name, binding in document.get("bindings", {}).items():
        if isinstance(binding, dict) and isinstance(binding.get("source"), dict):
            yield f"binding {name}", binding["source"]
    for index, binding in enumerate(document.get("package_text", [])):
        if isinstance(binding, dict) and isinstance(binding.get("source"), dict):
            yield f"package_text[{index}] source", binding["source"]
    for index, gate in enumerate(document.get("human_gates", [])):
        evidence = gate.get("evidence") if isinstance(gate, dict) else None
        if isinstance(evidence, dict):
            yield f"human_gates[{index}] evidence", evidence
    for asset_index, asset in enumerate(document.get("assets", [])):
        if not isinstance(asset, dict):
            continue
        license_row = asset.get("license")
        if isinstance(license_row, dict) and isinstance(license_row.get("evidence"), dict):
            yield f"assets[{asset_index}] license evidence", license_row["evidence"]
        private = asset.get("private_evidence")
        if isinstance(private, dict) and isinstance(private.get("receipt"), dict):
            yield f"assets[{asset_index}] private evidence receipt", private["receipt"]
        for source_index, source in enumerate(asset.get("provenance", [])):
            if isinstance(source, dict):
                yield f"assets[{asset_index}] provenance[{source_index}]", source
        for use_index, use in enumerate(asset.get("uses", [])):
            evidence = use.get("evidence") if isinstance(use, dict) else None
            if isinstance(evidence, dict):
                yield f"assets[{asset_index}] uses[{use_index}] evidence", evidence


def _strings(value: Any) -> Iterator[tuple[str, str]]:
    def walk(item: Any, location: str) -> Iterator[tuple[str, str]]:
        if isinstance(item, str):
            yield location, item
        elif isinstance(item, dict):
            for key, child in item.items():
                yield from walk(child, f"{location}.{key}" if location else str(key))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                yield from walk(child, f"{location}[{index}]")

    yield from walk(value, "")


def _keys(value: Any, location: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = f"{location}.{key}" if location else str(key)
            yield child_location, key
            yield from _keys(child, child_location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _keys(child, f"{location}[{index}]")


def _schema_errors(document: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        return [f"rights schema is invalid: {exc.message}"]
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    rendered = []
    for error in errors:
        location = ".".join(str(part) for part in error.absolute_path) or "register"
        rendered.append(f"{location}: {error.message}")
    return rendered


@functools.cache
def _pages_contract() -> Any:
    """Load the canonical Pages allowlist without adding a second corpus census."""
    path = ROOT / "scripts" / "build-pages.py"
    spec = importlib.util.spec_from_file_location("danse_pages_source_contract", path)
    if spec is None or spec.loader is None:
        raise RightsError("cannot load the canonical Pages source contract")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, ValueError, AttributeError) as exc:
        raise RightsError("cannot load the canonical Pages source contract") from exc
    return module


def public_corpus_identity(root: Path, tracked: set[str]) -> dict[str, Any]:
    """Hash every corpus byte selected by the actual Pages allowlist."""
    try:
        relative_paths = sorted(_pages_contract().corpus_files(root))
    except (OSError, ValueError, AttributeError, RuntimeError) as exc:
        raise RightsError("cannot resolve the public corpus derivative inventory") from exc
    untracked = set(relative_paths) - tracked
    if untracked:
        raise RightsError(
            f"public corpus derivative inventory contains {len(untracked)} untracked file(s)"
        )
    records: list[dict[str, Any]] = []
    for relative in relative_paths:
        path = regular_file(root, relative, "public corpus derivative", expose_value=False)
        digest, size, _ = _stable_file_measure(path, "public corpus derivative")
        records.append({"path": relative, "bytes": size, "sha256": digest})
    return {
        "files": len(records),
        "sha256": value_sha256(records),
    }


def _submission_assertion_contracts(
    document: dict[str, Any],
    submission: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Map every canonical manual/choice assertion to one phase-owning gate."""
    gates = {
        gate["attestation"]["key"]: gate
        for gate in document["human_gates"]
        if gate["attestation"] is not None
    }
    contracts: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    seen: set[str] = set()
    for section in ("requirements", "approvals", "terms"):
        rows = submission.get(section)
        if not isinstance(rows, list):
            errors.append(f"submission {section} has no assertion inventory")
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("check") not in {"manual", "choice"}:
                continue
            assertion_id = row.get("id")
            phase = row.get("phase")
            if not isinstance(assertion_id, str) or not SAFE_ID.fullmatch(assertion_id):
                errors.append(f"submission {section} contains a malformed assertion")
                continue
            if assertion_id in seen:
                errors.append(f"submission assertion is duplicated: {assertion_id}")
                continue
            seen.add(assertion_id)
            if row["check"] == "choice":
                values = row.get("values", row.get("choices"))
                contract = {"kind": "choice", "values": values}
                if (
                    not isinstance(values, list)
                    or len(values) < 2
                    or not all(isinstance(value, str) and value for value in values)
                ):
                    errors.append(f"submission assertion {assertion_id} has invalid choices")
                    continue
            else:
                contract = {"kind": "boolean", "values": [True]}
            contracts[assertion_id] = contract
            gate = gates.get(assertion_id)
            if gate is None:
                errors.append(f"submission assertion {assertion_id} has no registered human gate")
                continue
            if phase not in gate["required_for"]:
                errors.append(
                    f"submission assertion {assertion_id} is not owned by its canonical {phase} phase"
                )
            attestation = gate["attestation"]
            if attestation["kind"] != contract["kind"] or {
                json.dumps(value, sort_keys=True) for value in attestation["values"]
            } != {json.dumps(value, sort_keys=True) for value in contract["values"]}:
                errors.append(f"attestation contract disagrees for registered gate {assertion_id}")
    return contracts, errors


def _validate_bindings(
    root: Path,
    document: dict[str, Any],
    verified: dict[str, Path],
    tracked: set[str],
) -> list[str]:
    errors: list[str] = []
    bindings = document["bindings"]

    try:
        corpus = load_json(verified["binding corpus"], "corpus binding")
        declared = bindings["corpus"]
        frames = corpus.get("frames")
        if corpus.get("schema") != declared["schema"]:
            errors.append("binding corpus schema disagrees with corpus/manifest.json")
        if not isinstance(frames, list) or len(frames) != declared["frames"]:
            errors.append(f"binding corpus frame count is not {declared['frames']}")
        elif len({row.get("id") for row in frames if isinstance(row, dict)}) != len(frames):
            errors.append("binding corpus frame ids are not unique")
        public_identity = public_corpus_identity(root, tracked)
        if public_identity["files"] != declared["public_files"]:
            errors.append("binding corpus public derivative file count has drifted")
        if public_identity["sha256"] != declared["public_tree_sha256"]:
            errors.append("binding corpus public derivative tree digest has drifted")
    except RightsError as exc:
        errors.append(str(exc))

    try:
        music = load_yaml(verified["binding music"], "music binding")
        declared = bindings["music"]
        works = music.get("works")
        gate = music.get("artistic_gate") if isinstance(music.get("artistic_gate"), dict) else {}
        if music.get("schema") != declared["schema"]:
            errors.append("binding music schema disagrees with music/repertoire.yaml")
        if not isinstance(works, list) or len(works) != declared["works"]:
            errors.append(f"binding music work count is not {declared['works']}")
        if gate.get("status") != declared["artistic_gate"]:
            errors.append("binding music artistic gate has drifted")
    except RightsError as exc:
        errors.append(str(exc))

    try:
        vendor_path = verified["binding pose_vendor"]
        vendor = load_json(vendor_path, "pose vendor binding")
        declared = bindings["pose_vendor"]
        files = vendor.get("files")
        if vendor.get("schema") != declared["schema"]:
            errors.append("binding pose_vendor schema disagrees with its manifest")
        if not isinstance(files, list) or len(files) != declared["files"]:
            errors.append(f"binding pose_vendor file count is not {declared['files']}")
            files = []
        package = vendor.get("package") if isinstance(vendor.get("package"), dict) else {}
        model = vendor.get("model") if isinstance(vendor.get("model"), dict) else {}
        if package.get("license") != declared["package_license"]:
            errors.append("binding pose_vendor package license has drifted")
        if model.get("license") != declared["model_license"]:
            errors.append("binding pose_vendor model license has drifted")
        vendor_relative = PurePosixPath(bindings["pose_vendor"]["source"]["path"]).parent
        seen: set[str] = set()
        for index, row in enumerate(files):
            if not isinstance(row, dict):
                errors.append(f"pose vendor files[{index}] is not a record")
                continue
            try:
                leaf = safe_relative(row.get("path"), f"pose vendor files[{index}] path")
                combined = (vendor_relative / leaf).as_posix()
                if combined in seen:
                    errors.append(f"pose vendor file is duplicated: {combined}")
                    continue
                seen.add(combined)
                if combined not in tracked:
                    errors.append(f"pose vendor file is not tracked: {combined}")
                    continue
                file_path = regular_file(root, combined, f"pose vendor files[{index}]")
                if row.get("sha256") != sha256(file_path):
                    errors.append(f"pose vendor file digest mismatch: {combined}")
                if row.get("bytes") != file_path.stat().st_size:
                    errors.append(f"pose vendor file byte count mismatch: {combined}")
            except RightsError as exc:
                errors.append(str(exc))
        if "interaction/vendor/mediapipe/Apache-2.0.txt" not in seen:
            errors.append("pose vendor bundle does not retain Apache-2.0.txt")
    except RightsError as exc:
        errors.append(str(exc))

    try:
        submission = load_yaml(verified["binding submission"], "submission binding")
        declared = bindings["submission"]
        if submission.get("schema") != declared["schema"]:
            errors.append("binding submission schema disagrees with its register")
        _submission_zone(submission)
        terms = {
            row.get("id")
            for row in submission.get("terms", [])
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        missing = sorted(set(declared["required_terms"]) - terms)
        if missing:
            errors.append(f"binding submission is missing published terms: {', '.join(missing)}")
        _, assertion_errors = _submission_assertion_contracts(document, submission)
        errors.extend(assertion_errors)
    except RightsError as exc:
        errors.append(str(exc))
    return errors


def _validate_delibes_custody(
    root: Path,
    document: dict[str, Any],
    tracked: set[str],
) -> list[str]:
    """Authenticate selected-music custody without turning it into clearance."""
    errors: list[str] = []
    declared_records = [
        record
        for _, record in _source_records(document)
        if record.get("path") == DELIBES_CUSTODY_PATH
    ]
    if not declared_records:
        return ["selected Delibes sources have no digest-bound custody evidence"]
    if any(record.get("sha256") != DELIBES_CUSTODY_SHA256 for record in declared_records):
        errors.append("selected Delibes custody evidence is declared with a stale digest")
    try:
        if DELIBES_CUSTODY_PATH not in tracked:
            raise RightsError("selected Delibes custody evidence is not tracked by Git")
        evidence_path = regular_file(root, DELIBES_CUSTODY_PATH, "selected Delibes custody evidence")
        if sha256(evidence_path) != DELIBES_CUSTODY_SHA256:
            raise RightsError("selected Delibes custody evidence bytes have drifted")
        evidence = load_json(evidence_path, "selected Delibes custody evidence")
    except RightsError as exc:
        return [*errors, str(exc)]

    expected_compositions = [
        {
            "work_id": "sylvia-valse-lente",
            "composer": "Léo Delibes",
            "title": "Valse lente from Sylvia",
            "rights_status": "public-domain-composition",
            "library_of_congress_url": "https://www.loc.gov/item/2023848130/",
        },
        {
            "work_id": "coppelia-valse",
            "composer": "Léo Delibes",
            "title": "Valse from Coppélia",
            "rights_status": "public-domain-composition",
            "library_of_congress_url": "https://www.loc.gov/item/2023855939/",
        },
    ]
    expected_arrangements = [
        {
            "work_id": "sylvia-valse-lente",
            "arranger": "Paul De Bra",
            "path": DELIBES_SOURCE_FILES[0][0],
            "sha256": DELIBES_SOURCE_FILES[0][1],
            "source_page_url": "https://www.de-bra.nl/arrangements.html",
            "imslp_url": "https://imslp.org/wiki/Sylvia_%28Delibes%2C_L%C3%A9o%29",
            "imslp_file_ids": [1001719, 1001726],
            "publisher_year": 2025,
            "declared_instrumentation": "4 accordions, bass, and triangle",
            "license": {
                "spdx": "CC-BY-4.0",
                "url": "https://creativecommons.org/licenses/by/4.0/",
            },
        },
        {
            "work_id": "coppelia-valse",
            "arranger": "Paul De Bra",
            "path": DELIBES_SOURCE_FILES[1][0],
            "sha256": DELIBES_SOURCE_FILES[1][1],
            "source_page_url": "https://www.de-bra.nl/arrangements.html",
            "imslp_url": "https://imslp.org/wiki/Copp%C3%A9lia_%28Delibes%2C_L%C3%A9o%29",
            "imslp_file_ids": [818172],
            "publisher_year": 2022,
            "reference_duration": "3:23",
            "license": {
                "spdx": "CC-BY-4.0",
                "url": "https://creativecommons.org/licenses/by/4.0/",
            },
        },
    ]
    expected_soundfont = {
        "name": "MuseScore_General.sf3",
        "license": "MIT",
        "documentation_url": "https://musescore.org/en/handbook/3/soundfonts-and-sfz-files",
        "source_url": "https://ftp.osuosl.org/pub/musescore/soundfont/MuseScore_General/MuseScore_General.sf3",
        "notice": {
            "path": MUSESCORE_GENERAL_NOTICE_PATH,
            "sha256": MUSESCORE_GENERAL_NOTICE_SHA256,
        },
        "soundfont_sha256": MUSESCORE_GENERAL_SF3_SHA256,
    }
    expected_clearance = {
        "gate": "music-cleared",
        "state": "pending",
        "note": (
            "This receipt records source and license custody only. It does not approve the "
            "adapted MIDI, rendered audio, credit, final cut, or submission."
        ),
    }
    if set(evidence) != {
        "schema",
        "status",
        "recorded_on",
        "composition_evidence",
        "source_arrangements",
        "soundfont",
        "required_credit",
        "clearance",
    }:
        errors.append("selected Delibes custody evidence has fields outside its typed contract")
    if evidence.get("schema") != "danse.music-rights-custody.v1":
        errors.append("selected Delibes custody evidence has the wrong schema")
    if evidence.get("status") != "custody-only" or evidence.get("recorded_on") != "2026-08-26":
        errors.append("selected Delibes custody evidence overstates or misdates its status")
    if evidence.get("composition_evidence") != expected_compositions:
        errors.append("selected Delibes composition evidence has drifted")
    if evidence.get("source_arrangements") != expected_arrangements:
        errors.append("selected Paul De Bra source or CC BY 4.0 evidence has drifted")
    if evidence.get("soundfont") != expected_soundfont:
        errors.append("MuseScore_General soundfont license custody has drifted")
    if evidence.get("required_credit") != REQUIRED_DELIBES_CREDIT:
        errors.append("selected Delibes required credit has drifted")
    if evidence.get("clearance") != expected_clearance:
        errors.append("selected Delibes custody evidence falsely changes the clearance gate")

    for relative, expected_digest in (
        *DELIBES_SOURCE_FILES,
        (MUSESCORE_GENERAL_NOTICE_PATH, MUSESCORE_GENERAL_NOTICE_SHA256),
    ):
        try:
            if relative not in tracked:
                raise RightsError(f"selected music custody source is not tracked by Git: {relative}")
            source = regular_file(root, relative, "selected music custody source")
            if sha256(source) != expected_digest:
                raise RightsError(f"selected music custody source digest drifted: {relative}")
        except RightsError as exc:
            errors.append(str(exc))

    assets = {asset["id"]: asset for asset in document.get("assets", [])}
    components = {
        "delibes-public-domain-compositions": ("public-domain-with-provenance", None),
        "paul-de-bra-source-arrangements": ("licensed", "CC-BY-4.0"),
        "adapted-delibes-midi": ("owned", None),
        "musescore-general-soundfont": ("licensed", "MIT"),
    }
    for asset_id, (disposition, spdx) in components.items():
        asset = assets.get(asset_id)
        if asset is None:
            errors.append(f"selected music custody is missing component asset {asset_id}")
            continue
        if asset.get("disposition") != disposition:
            errors.append(f"selected music component {asset_id} has an invalid disposition")
        license_row = asset.get("license")
        if spdx is None and license_row is not None:
            errors.append(f"selected music component {asset_id} invents a license layer")
        if spdx is not None and (
            not isinstance(license_row, dict) or license_row.get("spdx") != spdx
        ):
            errors.append(f"selected music component {asset_id} has the wrong license")

    arrangement = assets.get("paul-de-bra-source-arrangements")
    if arrangement is not None:
        paths = {record.get("path") for record in arrangement.get("provenance", [])}
        expected_paths = {DELIBES_CUSTODY_PATH, *(path for path, _ in DELIBES_SOURCE_FILES)}
        if paths != expected_paths:
            errors.append("Paul De Bra arrangement asset does not bind both exact sources")
    soundfont = assets.get("musescore-general-soundfont")
    if soundfont is not None:
        license_row = soundfont.get("license") or {}
        if (license_row.get("evidence") or {}).get("path") != MUSESCORE_GENERAL_NOTICE_PATH:
            errors.append("MuseScore_General asset does not retain its exact license notice")
    selected = assets.get("selected-music")
    if selected is None:
        errors.append("selected Delibes suite has no integrated music asset")
    elif (selected.get("public_credit") or {}).get("label") != REQUIRED_DELIBES_CREDIT:
        errors.append("selected Delibes suite does not carry the exact required credit")
    return errors


def _validate_audio_use_profiles(
    root: Path,
    document: dict[str, Any],
    tracked: set[str],
) -> list[str]:
    """Bind competition audio to classical sources and quarantine private grains."""
    errors: list[str] = []
    records = [
        record
        for _, record in _source_records(document)
        if record.get("path") == AUDIO_USES_PATH
    ]
    if not records:
        return ["rights register does not bind the canonical audio-use profiles"]
    if any(record.get("sha256") != AUDIO_USES_SHA256 for record in records):
        errors.append("canonical audio-use profile is declared with a stale digest")
    try:
        if AUDIO_USES_PATH not in tracked:
            raise RightsError("canonical audio-use profile is not tracked by Git")
        profile_path = regular_file(root, AUDIO_USES_PATH, "canonical audio-use profile")
        if sha256(profile_path) != AUDIO_USES_SHA256:
            raise RightsError("canonical audio-use profile bytes have drifted")
        audio_uses = load_json(profile_path, "canonical audio-use profile")
    except RightsError as exc:
        return [*errors, str(exc)]

    if set(audio_uses) != {"schema", "competition_profile", "profiles"}:
        errors.append("canonical audio-use profile has fields outside its typed contract")
    if audio_uses.get("schema") != "danse.audio.uses.v1":
        errors.append("canonical audio-use profile has the wrong schema")
    if audio_uses.get("competition_profile") != COMPETITION_AUDIO_PROFILE:
        errors.append("canonical audio-use profile selects the wrong competition profile")
    profiles = audio_uses.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != {
        COMPETITION_AUDIO_PROFILE,
        HYBRID_AUDIO_PROFILE,
    }:
        errors.append("canonical audio-use profile census has drifted")
        profiles = {}

    competition = profiles.get(COMPETITION_AUDIO_PROFILE)
    expected_competition_sources = [
        {
            "id": "delibes-chamber-midi",
            "kind": "project-authored-midi",
            "path": ADAPTED_DELIBES_MIDI_PATH,
            "sha256": ADAPTED_DELIBES_MIDI_SHA256,
        },
        {
            "id": "musescore-general-sf3",
            "kind": "licensed-soundfont",
            "path": ".work/music/MuseScore_General.sf3",
            "sha256": MUSESCORE_GENERAL_SF3_SHA256,
            "custody": "hydrated-local",
            "license": "MIT",
            "source_url": "https://ftp.osuosl.org/pub/musescore/soundfont/MuseScore_General/MuseScore_General.sf3",
            "license_notice": {
                "path": MUSESCORE_GENERAL_NOTICE_PATH,
                "sha256": MUSESCORE_GENERAL_NOTICE_SHA256,
            },
        },
    ]
    expected_stems = list(COMPETITION_STEM_IDS)
    if not isinstance(competition, dict) or set(competition) != {
        "package_eligible",
        "description",
        "declared_sources",
        "required_stems",
        "forbidden_source_kinds",
    }:
        errors.append("competition-classical audio profile has an unknown shape")
        competition = {}
    if competition.get("package_eligible") is not True:
        errors.append("competition-classical audio profile is not package eligible")
    if competition.get("declared_sources") != expected_competition_sources:
        errors.append("competition-classical sources do not match exact MIDI and soundfont custody")
    if competition.get("required_stems") != expected_stems:
        errors.append("competition-classical required stem census has drifted")
    if competition.get("forbidden_source_kinds") != [
        "private-grain-bank",
        "daw-instrument",
        "unverified-download",
    ]:
        errors.append("competition-classical forbidden source kinds have drifted")

    hybrid = profiles.get(HYBRID_AUDIO_PROFILE)
    expected_hybrid_source = {
        "id": "apartment-grain-bank",
        "kind": "private-grain-bank",
        "path": "sound/sources.json",
        "custody": "ignored-private-optional",
    }
    if not isinstance(hybrid, dict) or set(hybrid) != {
        "package_eligible",
        "description",
        "inherits",
        "declared_sources",
        "required_stems",
        "forbidden_source_kinds",
    }:
        errors.append("hybrid-apartment audio profile has an unknown shape")
        hybrid = {}
    if hybrid.get("package_eligible") is not False:
        errors.append("hybrid-apartment audio profile must remain package ineligible")
    if hybrid.get("inherits") != COMPETITION_AUDIO_PROFILE:
        errors.append("hybrid-apartment audio profile no longer inherits competition custody")
    if hybrid.get("declared_sources") != [expected_hybrid_source]:
        errors.append("hybrid-apartment source census has drifted")
    if hybrid.get("required_stems") != [] or hybrid.get("forbidden_source_kinds") != []:
        errors.append("hybrid-apartment overlay contract has drifted")
    competition_kinds = {
        source.get("kind")
        for source in competition.get("declared_sources", [])
        if isinstance(source, dict)
    }
    forbidden = set(competition.get("forbidden_source_kinds", []))
    if competition_kinds & forbidden or "private-grain-bank" in competition_kinds:
        errors.append("competition-classical profile admits a forbidden private or unverified source")

    for relative, expected_digest in (
        (ADAPTED_DELIBES_MIDI_PATH, ADAPTED_DELIBES_MIDI_SHA256),
        (DELIBES_ADAPTATION_PATH, DELIBES_ADAPTATION_SHA256),
    ):
        try:
            if relative not in tracked:
                raise RightsError(f"competition audio source is not tracked by Git: {relative}")
            source = regular_file(root, relative, "competition audio source")
            if sha256(source) != expected_digest:
                raise RightsError(f"competition audio source digest drifted: {relative}")
        except RightsError as exc:
            errors.append(str(exc))

    try:
        adaptation = load_json(
            regular_file(root, DELIBES_ADAPTATION_PATH, "competition adaptation receipt"),
            "competition adaptation receipt",
        )
    except RightsError as exc:
        errors.append(str(exc))
        adaptation = {}
    adaptation_sources = adaptation.get("sources")
    expected_adaptation_sources = {
        source_id: (path, digest)
        for source_id, (path, digest) in zip(
            ("sylvia-valse-lente", "coppelia-valse"),
            DELIBES_SOURCE_FILES,
            strict=True,
        )
    }
    if not isinstance(adaptation_sources, list) or len(adaptation_sources) != 2:
        errors.append("competition adaptation does not bind both licensed source arrangements")
    else:
        for source in adaptation_sources:
            source_id = source.get("id") if isinstance(source, dict) else None
            expected = expected_adaptation_sources.get(source_id)
            if (
                expected is None
                or source.get("path") != expected[0]
                or source.get("sha256") != expected[1]
                or source.get("arranger") != "Paul De Bra"
                or source.get("license") != "CC BY 4.0"
                or source.get("license_url")
                != "https://creativecommons.org/licenses/by/4.0/"
            ):
                errors.append("competition adaptation loses an exact CC BY 4.0 source identity")
                break
    if adaptation.get("credit") != REQUIRED_DELIBES_CREDIT:
        errors.append("competition adaptation does not carry the exact approved credit wording")
    output = adaptation.get("output")
    if not isinstance(output, dict) or (
        output.get("path") != ADAPTED_DELIBES_MIDI_PATH
        or output.get("sha256") != ADAPTED_DELIBES_MIDI_SHA256
        or output.get("timing_mode") != "native-tempo"
    ):
        errors.append("competition adaptation output identity or native timing has drifted")

    try:
        if AUDIO_TOOLCHAIN_PATH not in tracked:
            raise RightsError("competition audio toolchain is not tracked by Git")
        toolchain = load_json(
            regular_file(root, AUDIO_TOOLCHAIN_PATH, "competition audio toolchain"),
            "competition audio toolchain",
        )
    except RightsError as exc:
        errors.append(str(exc))
        toolchain = {}
    toolchain_soundfont = toolchain.get("soundfont")
    if not isinstance(toolchain_soundfont, dict) or (
        toolchain_soundfont.get("path") != ".work/music/MuseScore_General.sf3"
        or toolchain_soundfont.get("sha256") != MUSESCORE_GENERAL_SF3_SHA256
        or toolchain_soundfont.get("license") != "MIT"
        or toolchain_soundfont.get("license_notice")
        != {
            "path": MUSESCORE_GENERAL_NOTICE_PATH,
            "sha256": MUSESCORE_GENERAL_NOTICE_SHA256,
        }
    ):
        errors.append("competition audio toolchain loses the exact soundfont notice identity")
    if toolchain.get("midi") != {
        "path": ADAPTED_DELIBES_MIDI_PATH,
        "sha256": ADAPTED_DELIBES_MIDI_SHA256,
    } or toolchain.get("adaptation") != {
        "path": DELIBES_ADAPTATION_PATH,
        "sha256": DELIBES_ADAPTATION_SHA256,
    }:
        errors.append("competition audio toolchain loses the exact MIDI/adaptation identity")
    fluidsynth = toolchain.get("fluidsynth")
    if not isinstance(fluidsynth, dict) or (
        fluidsynth.get("version") != "2.6.0"
        or not isinstance(fluidsynth.get("executable_sha256"), str)
        or not HEX64.fullmatch(fluidsynth["executable_sha256"])
    ):
        errors.append("competition audio toolchain does not pin FluidSynth 2.6.0 exactly")

    assets = {asset["id"]: asset for asset in document.get("assets", [])}
    adapted = assets.get("adapted-delibes-midi")
    if adapted is not None:
        paths = {record.get("path") for record in adapted.get("provenance", [])}
        if not {
            ADAPTED_DELIBES_MIDI_PATH,
            DELIBES_ADAPTATION_PATH,
            AUDIO_USES_PATH,
            DELIBES_CUSTODY_PATH,
        } <= paths:
            errors.append("adapted Delibes MIDI asset lacks exact profile and adaptation provenance")
    room = assets.get("room-source-recordings")
    if room is None:
        errors.append("hybrid apartment grain source has no rights asset")
    else:
        uses = room.get("uses")
        if not isinstance(uses, list) or len(uses) != 1 or uses[0].get("id") != HYBRID_AUDIO_REQUIREMENT[1]:
            errors.append("hybrid apartment grain source has the wrong use identity")
        elif uses[0].get("required_for") != []:
            errors.append("hybrid apartment grain source leaks into global shipping phases")

    audio_assets = {asset for asset, _ in (*COMPETITION_AUDIO_REQUIREMENTS, HYBRID_AUDIO_REQUIREMENT)}

    def audio_requirements(rule: dict[str, Any]) -> list[tuple[str, str]]:
        return [
            (row.get("asset"), row.get("use"))
            for row in rule.get("requirements", [])
            if row.get("asset") in audio_assets
        ]

    package_rules = {rule["id"]: rule for rule in document.get("package_rules", [])}
    for rule_id in (
        "moving-image",
        "score-source",
        "audio-render-receipt",
        "score-motion-evidence",
    ):
        rule = package_rules.get(rule_id)
        if rule is not None and audio_requirements(rule) != list(COMPETITION_AUDIO_REQUIREMENTS):
            errors.append(f"package rule {rule_id} does not require the exact competition audio rights layers")
    release_rules = {rule["media_id"]: rule for rule in document.get("release_rules", [])}
    for media_id in ("accessible-trailer", "score-driven-master"):
        rule = release_rules.get(media_id)
        if rule is not None and audio_requirements(rule) != list(COMPETITION_AUDIO_REQUIREMENTS):
            errors.append(f"release rule {media_id} does not require the exact competition audio rights layers")
    return errors


def validate_document(
    document: dict[str, Any],
    *,
    root: Path = ROOT,
    schema: dict[str, Any] | None = None,
    enforce_tracked: bool = True,
) -> list[str]:
    """Validate schema, redaction, exact sources, inventory, and rule graph."""
    errors: list[str] = []
    if schema is None:
        try:
            schema = load_json(SCHEMA, "rights schema")
        except RightsError as exc:
            return [str(exc)]
    errors.extend(_schema_errors(document, schema))
    for location, value in _strings(document):
        if PRIVATE_PATH.search(value) or ABSOLUTE_PATH.search(value):
            errors.append(f"{location}: contains a private or machine-local path")
        if EMAIL.search(value):
            errors.append(f"{location}: contains an email address")
        if PHONE.search(value):
            errors.append(f"{location}: contains a phone number")
    for location, key in _keys(document):
        if key.lower() in SENSITIVE_KEYS:
            errors.append(f"{location}: prohibited sensitive field {key!r}")
    if errors:
        return errors

    try:
        tracked = tracked_paths(root) if enforce_tracked else {
            record["path"] for _, record in _source_records(document)
        }
    except RightsError as exc:
        return [*errors, str(exc)]

    verified: dict[str, Path] = {}
    path_digests: dict[str, str] = {}
    for label, record in _source_records(document):
        path = record.get("path")
        digest = record.get("sha256")
        if isinstance(path, str) and isinstance(digest, str):
            previous = path_digests.setdefault(path, digest)
            if previous != digest:
                errors.append(f"{label}: {path} is declared with conflicting digests")
                continue
        try:
            verified[label] = verify_record(root, record, label, tracked)
        except RightsError as exc:
            errors.append(str(exc))

    required_binding_labels = {f"binding {name}" for name in ("corpus", "music", "pose_vendor", "submission")}
    if required_binding_labels <= verified.keys():
        errors.extend(_validate_bindings(root, document, verified, tracked))
    errors.extend(_validate_delibes_custody(root, document, tracked))
    errors.extend(_validate_audio_use_profiles(root, document, tracked))

    gate_rows = document["human_gates"]
    gate_ids = [row["id"] for row in gate_rows]
    if len(gate_ids) != len(set(gate_ids)):
        errors.append("human gate ids must be unique")
    gates_by_id = {gate["id"]: gate for gate in gate_rows}
    gate_decisions: dict[str, Any] = {}
    attestation_keys: set[str] = set()
    for gate_index, gate in enumerate(gate_rows):
        evidence = gate["evidence"]
        if gate["state"] == "satisfied" and evidence is None:
            errors.append(f"human gate {gate['id']} is satisfied without a redacted evidence receipt")
        if gate["state"] != "satisfied" and evidence is not None:
            errors.append(f"human gate {gate['id']} is {gate['state']} but carries completion evidence")
        if gate["state"] == "satisfied" and evidence is not None:
            evidence_path = verified.get(f"human_gates[{gate_index}] evidence")
            if evidence_path is not None:
                decision, decision_errors = validate_gate_decision_receipt(
                    evidence_path,
                    gate,
                    approved_credit_contract(document, gate["id"]),
                )
                errors.extend(decision_errors)
                if not decision_errors:
                    gate_decisions[gate["id"]] = decision
        attestation = gate["attestation"]
        if attestation is not None:
            key = attestation["key"]
            if key in attestation_keys:
                errors.append(f"attestation key is reused: {key}")
            attestation_keys.add(key)
            values = attestation["values"]
            if attestation["kind"] == "boolean" and values != [True]:
                errors.append(f"human gate {gate['id']} boolean attestation must accept only true")
            if attestation["kind"] == "choice" and (
                len(values) < 2 or not all(isinstance(value, str) and value for value in values)
            ):
                errors.append(f"human gate {gate['id']} choice attestation needs at least two named values")

    asset_rows = document["assets"]
    assessed_on = date.fromisoformat(document["assessment"]["date"])
    asset_ids = [row["id"] for row in asset_rows]
    if len(asset_ids) != len(set(asset_ids)):
        errors.append("asset ids must be unique")
    categories = {row["category"] for row in asset_rows}
    if categories != EXPECTED_CATEGORIES:
        missing = sorted(EXPECTED_CATEGORIES - categories)
        extra = sorted(categories - EXPECTED_CATEGORIES)
        errors.append(f"asset category census is incomplete (missing={missing}, extra={extra})")

    assets: dict[str, dict[str, Any]] = {row["id"]: row for row in asset_rows}
    vendor_asset = assets.get("mediapipe-pose-runtime")
    if vendor_asset is None:
        errors.append("the exact MediaPipe vendor bundle has no asset disposition")
    else:
        vendor_license = vendor_asset["license"] or {}
        declared_vendor = document["bindings"]["pose_vendor"]
        if not (
            vendor_license.get("spdx")
            == declared_vendor["package_license"]
            == declared_vendor["model_license"]
        ):
            errors.append("the MediaPipe asset license disagrees with the exact package/model binding")
    uses: dict[tuple[str, str], dict[str, Any]] = {}
    for asset_index, asset in enumerate(asset_rows):
        asset_id = asset["id"]
        disposition = asset["disposition"]
        license_row = asset["license"]
        blocker = asset["blocker"]
        if disposition in {"owned", "licensed"} and not asset["rights_holder"]:
            errors.append(f"asset {asset_id} with disposition {disposition} must name a rights holder")
        if disposition == "licensed" and license_row is None:
            errors.append(f"licensed asset {asset_id} has no license")
        if disposition != "licensed" and license_row is not None:
            errors.append(f"non-licensed asset {asset_id} carries a license record")
        if disposition in {"owned", "licensed", "public-domain-with-provenance"} and not asset["provenance"]:
            errors.append(f"asset {asset_id} has no public-safe provenance")
        if disposition == "blocked" and not blocker:
            errors.append(f"blocked asset {asset_id} does not state its blocker")
        if disposition == "excluded" and blocker is not None:
            errors.append(f"excluded asset {asset_id} must not carry an unresolved blocker")

        credit = asset["public_credit"]
        if credit["state"] == "approved" and not credit["label"]:
            errors.append(f"asset {asset_id} has an approved blank public credit")
        private = asset["private_evidence"]
        if private["state"] == "verified" and private["receipt"] is None:
            errors.append(f"asset {asset_id} claims verified private evidence without a redacted receipt")
        if private["state"] != "verified" and private["receipt"] is not None:
            errors.append(f"asset {asset_id} carries a private-evidence receipt while {private['state']}")
        if private["state"] == "not-required" and private["custodian"] is not None:
            errors.append(f"asset {asset_id} does not require private evidence but names a custodian")

        local_use_ids: set[str] = set()
        for use_index, use in enumerate(asset["uses"]):
            use_id = use["id"]
            key = (asset_id, use_id)
            if use_id in local_use_ids:
                errors.append(f"asset {asset_id} repeats use id {use_id}")
            local_use_ids.add(use_id)
            uses[key] = use
            status = use["status"]
            conditional = use.get("conditional_exclusion")
            if conditional is not None:
                gate = gates_by_id.get(conditional["gate"])
                attestation = gate.get("attestation") if gate is not None else None
                if (
                    gate is None
                    or attestation is None
                    or attestation["kind"] != "choice"
                    or conditional["value"] not in attestation["values"]
                ):
                    errors.append(f"asset {asset_id} use {use_id} has an invalid conditional exclusion")
                elif not set(use["required_for"]) <= set(gate["required_for"]):
                    errors.append(f"asset {asset_id} use {use_id} exclusion gate has insufficient phase scope")
                if status != "blocked" or use["evidence"] is not None:
                    errors.append(f"asset {asset_id} use {use_id} conditional exclusion must remain blocked")
            if status == "cleared":
                if disposition not in {"owned", "licensed", "public-domain-with-provenance"}:
                    errors.append(f"asset {asset_id} use {use_id} is cleared from disposition {disposition}")
                if use["evidence"] is None:
                    errors.append(f"asset {asset_id} use {use_id} is cleared without evidence")
                else:
                    evidence_path = verified.get(
                        f"assets[{asset_index}] uses[{use_index}] evidence"
                    )
                    if evidence_path is not None:
                        errors.extend(validate_use_decision_receipt(evidence_path, asset, use))
                if use["territory"] == "pending" or use["term"] == "pending":
                    errors.append(f"asset {asset_id} use {use_id} is cleared with unsettled territory or term")
                if use["promotion"] == "pending" or use["archive"] == "pending":
                    errors.append(f"asset {asset_id} use {use_id} is cleared with unsettled promotion or archive scope")
                if use["medium"] in {"press", "festival-promotion"} and use["promotion"] != "allowed":
                    errors.append(f"asset {asset_id} use {use_id} cannot serve promotion")
                if use["medium"] == "festival-archive" and use["archive"] not in {"allowed", "opt-out"}:
                    errors.append(f"asset {asset_id} use {use_id} has no archive choice")
            elif use["evidence"] is not None:
                errors.append(f"asset {asset_id} use {use_id} is {status} but carries completion evidence")
            if disposition == "blocked" and status == "cleared":
                errors.append(f"blocked asset {asset_id} has cleared use {use_id}")
            if disposition == "excluded" and status != "excluded":
                errors.append(f"excluded asset {asset_id} has non-excluded use {use_id}")
            if use["term"] == "fixed" and use["expires"] is None:
                errors.append(f"asset {asset_id} use {use_id} has a fixed term without expiry")
            if use["term"] == "fixed" and use["expires"] is not None:
                if date.fromisoformat(use["expires"]) < assessed_on:
                    errors.append(f"asset {asset_id} use {use_id} expired before the assessment date")
            if use["term"] != "fixed" and use["expires"] is not None:
                errors.append(f"asset {asset_id} use {use_id} has an expiry outside a fixed term")

    dancer = assets.get("dancer-performance-likeness")
    dancer_gate = gates_by_id.get("dancer-release-and-credit")
    if dancer is None:
        errors.append("the performer inventory has no dancer performance/likeness disposition")
    elif dancer_gate is None:
        errors.append("the dancer performance/likeness disposition has no human gate")
    elif dancer_gate["state"] != "satisfied":
        if dancer["rights_holder"] is not None:
            errors.append("the dancer must remain unnamed until the dancer gate has a redacted receipt")
        if dancer["public_credit"]["label"] is not None:
            errors.append("the dancer public credit must remain withheld until approved")
    else:
        if dancer["private_evidence"]["state"] != "verified":
            errors.append("a satisfied dancer gate requires verified private evidence with a redacted receipt")
        if dancer["public_credit"]["state"] == "pending":
            errors.append("a satisfied dancer gate must record the approved or not-required credit disposition")

    package_text_ids: set[str] = set()
    destinations: set[str] = set()
    for row in document["package_text"]:
        if row["id"] in package_text_ids:
            errors.append(f"package text id is duplicated: {row['id']}")
        package_text_ids.add(row["id"])
        if row["destination"] in destinations:
            errors.append(f"package text destination is duplicated: {row['destination']}")
        destinations.add(row["destination"])
        if row["gate"] not in gate_ids:
            errors.append(f"package text {row['id']} names unknown gate {row['gate']}")

    package_rule_ids: set[str] = set()
    for rule in document["package_rules"]:
        if rule["id"] in package_rule_ids:
            errors.append(f"package rule id is duplicated: {rule['id']}")
        package_rule_ids.add(rule["id"])
        try:
            expression = re.compile(rule["pattern"])
            if expression.fullmatch(""):
                errors.append(f"package rule {rule['id']} matches an empty path")
        except re.error as exc:
            errors.append(f"package rule {rule['id']} has invalid regex: {exc}")
        for requirement in rule["requirements"]:
            key = (requirement["asset"], requirement["use"])
            if key not in uses:
                errors.append(f"package rule {rule['id']} names unknown asset/use {key[0]}/{key[1]}")
    for required_rule in (
        "moving-image",
        "origin-still",
        "score-source",
        "audio-render-receipt",
        "score-motion-evidence",
    ):
        if required_rule not in package_rule_ids:
            errors.append(f"required package rule is missing: {required_rule}")

    release_media_ids: set[str] = set()
    release_destinations: set[str] = set()
    for rule in document["release_rules"]:
        if rule["media_id"] in release_media_ids:
            errors.append(f"release media rule is duplicated: {rule['media_id']}")
        release_media_ids.add(rule["media_id"])
        try:
            destination = safe_relative(
                rule["destination"],
                f"release rule {rule['media_id']} destination",
                expose_value=False,
            )
        except RightsError as exc:
            errors.append(str(exc))
            destination = rule["destination"]
        if isinstance(destination, str) and not destination.startswith("media/assets/"):
            errors.append(f"release rule {rule['media_id']} destination is outside media/assets")
        if destination in release_destinations:
            errors.append(f"release destination is duplicated: {destination}")
        release_destinations.add(destination)
        for requirement in rule["requirements"]:
            key = (requirement["asset"], requirement["use"])
            if key not in uses:
                errors.append(f"release rule {rule['media_id']} names unknown asset/use {key[0]}/{key[1]}")

    credit_ids: set[str] = set()
    for rule in document["credit_rules"]:
        if rule["credit_id"] in credit_ids:
            errors.append(f"release credit rule is duplicated: {rule['credit_id']}")
        credit_ids.add(rule["credit_id"])
        if rule["asset"] not in assets:
            errors.append(f"credit rule {rule['credit_id']} names unknown asset {rule['asset']}")
        if rule["gate"] not in gate_ids:
            errors.append(f"credit rule {rule['credit_id']} names unknown gate {rule['gate']}")
        gate = gates_by_id.get(rule["gate"])
        asset = assets.get(rule["asset"])
        if gate is not None and gate["state"] == "satisfied" and asset is not None:
            credit = asset["public_credit"]
            if credit["state"] != "approved" or not credit["label"]:
                errors.append(
                    f"satisfied gate {gate['id']} has no approved wording for asset {asset['id']}"
                )

    if document["status"] == "cleared":
        pending_gates = [gate["id"] for gate in gate_rows if gate["state"] != "satisfied"]
        blocked_uses = [
            f"{asset}/{use}"
            for (asset, use), row in uses.items()
            if row["status"] == "blocked"
            and not (
                row.get("conditional_exclusion")
                and gate_decisions.get(row["conditional_exclusion"]["gate"])
                == row["conditional_exclusion"]["value"]
            )
        ]
        if pending_gates or blocked_uses:
            errors.append("register status cannot be cleared while gates or uses remain blocked")
    return errors


def load_register(
    register_path: Path = REGISTER,
    schema_path: Path = SCHEMA,
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    tracked = tracked_paths(root)
    for path, label in ((register_path, "rights register"), (schema_path, "rights schema")):
        if path.is_symlink() or not path.is_file():
            raise RightsError(f"{label} must be a regular tracked file")
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError as exc:
            raise RightsError(f"{label} must stay inside the canonical repository") from exc
        if relative not in tracked:
            raise RightsError(f"{label} must be tracked by Git")
    document = load_json(register_path, "rights register")
    schema = load_json(schema_path, "rights schema")
    errors = validate_document(document, root=root, schema=schema)
    if errors:
        raise RightsError("rights register:\n  - " + "\n  - ".join(errors))
    return document


def _asset_use_index(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (asset["id"], use["id"]): use
        for asset in document["assets"]
        for use in asset["uses"]
    }


def _requirement_blockers(
    requirements: list[dict[str, str]],
    uses: dict[tuple[str, str], dict[str, Any]],
    label: str,
    validation_date: date,
) -> list[str]:
    blockers: list[str] = []
    for requirement in requirements:
        asset_id, use_id = requirement["asset"], requirement["use"]
        use = uses.get((asset_id, use_id))
        if use is None:
            blockers.append(f"{label} names unknown asset/use {asset_id}/{use_id}")
            continue
        if use["status"] != "cleared":
            blockers.append(f"{label} requires {asset_id}/{use_id}, which is {use['status']}: {use['note']}")
        elif use["term"] == "fixed":
            expires = use.get("expires")
            if not isinstance(expires, str):
                blockers.append(f"{label} requires {asset_id}/{use_id}, whose fixed permission has no expiry")
                continue
            try:
                expired = date.fromisoformat(expires) < validation_date
            except ValueError:
                blockers.append(f"{label} requires {asset_id}/{use_id}, whose fixed permission has invalid expiry")
                continue
            if expired:
                blockers.append(
                    f"{label} requires {asset_id}/{use_id}, whose fixed permission expired before "
                    "the validation date"
                )
    return blockers


def _external_root(path: Path, label: str) -> Path:
    path = path.absolute()
    if path.is_symlink() or not path.is_dir():
        raise RightsError(f"{label} must be an existing regular directory")
    return path.resolve()


def _external_file(root: Path, relative: str, label: str) -> Path:
    relative = safe_relative(relative, label, expose_value=False)
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise RightsError(f"{label} traverses a symlink")
    try:
        path = candidate.resolve(strict=True)
        path.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise RightsError(f"{label} is missing or outside the package") from exc
    if not path.is_file():
        raise RightsError(f"{label} is not a regular file")
    return path


def _load_attestation_with_identity(
    package: Path | None,
) -> tuple[dict[str, Any], list[str], dict[str, Any] | None]:
    if package is None:
        return {}, [], None
    blockers: list[str] = []
    try:
        root = _external_root(package, "package")
        path = _external_file(root, "attest.yaml", "package attestation")
        digest, size, payload = _stable_file_measure(
            path,
            "package attestation",
            capture=True,
        )
        if payload is None:
            raise RightsError("package attestation bytes could not be retained")
        try:
            value = yaml.load(
                payload.decode("utf-8"),
                Loader=_UniqueKeySafeLoader,
            ) or {}
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise RightsError("cannot read package attestation: invalid or unreadable YAML") from exc
        if not isinstance(value, dict):
            raise RightsError("package attestation must be a mapping")
    except RightsError as exc:
        blockers.append(str(exc))
        return {}, blockers, None
    return value, blockers, {"sha256": digest, "bytes": size}


def load_attestation(package: Path | None) -> tuple[dict[str, Any], list[str]]:
    value, blockers, _ = _load_attestation_with_identity(package)
    return value, blockers


def validate_attestation(
    document: dict[str, Any],
    attestation: dict[str, Any],
    *,
    root: Path = ROOT,
) -> list[str]:
    """Reject unknown or ill-typed package assertions without echoing their values."""
    blockers: list[str] = []
    contracts = {
        gate["attestation"]["key"]: gate["attestation"]
        for gate in document["human_gates"]
        if gate["attestation"] is not None
    }
    try:
        submission_path = regular_file(
            root,
            document["bindings"]["submission"]["source"]["path"],
            "submission binding",
        )
        submission = load_yaml(submission_path, "submission binding")
    except RightsError as exc:
        return [str(exc)]
    _, assertion_errors = _submission_assertion_contracts(document, submission)
    blockers.extend(assertion_errors)

    unknown = [key for key in attestation if not isinstance(key, str) or key not in contracts]
    if unknown:
        blockers.append(f"package attestation contains {len(unknown)} unknown key(s)")
    for key, record in contracts.items():
        if key not in attestation or attestation[key] is None:
            continue
        value = attestation[key]
        if record["kind"] == "boolean":
            if type(value) is not bool:
                blockers.append(f"package attestation {key} must be boolean or null")
        elif not isinstance(value, str) or value not in record["values"]:
            blockers.append(f"package attestation {key} must be one registered choice or null")
    return blockers


def gate_satisfied(gate: dict[str, Any], attestation: dict[str, Any], *, allow_attestation: bool) -> bool:
    if gate["state"] == "satisfied":
        return True
    if gate["state"] == "rejected":
        return False
    record = gate["attestation"]
    if not allow_attestation or record is None:
        return False
    value = attestation.get(record["key"])
    if record["kind"] == "boolean":
        # bool is a subclass of int in Python: membership alone would let the
        # YAML integer ``1`` satisfy a human-authored ``true`` gate.
        return value is True and any(candidate is True for candidate in record["values"])
    return isinstance(value, str) and value in record["values"]


def gate_decision(
    document: dict[str, Any],
    gate: dict[str, Any],
    attestation: dict[str, Any],
    *,
    allow_attestation: bool,
    root: Path,
) -> Any:
    """Return the verified durable or phase-owned decision for one gate."""
    if gate["state"] == "satisfied" and gate["evidence"] is not None:
        try:
            path = regular_file(root, gate["evidence"]["path"], f"human gate {gate['id']} evidence")
        except RightsError:
            return None
        decision, errors = validate_gate_decision_receipt(
            path,
            gate,
            approved_credit_contract(document, gate["id"]),
        )
        return None if errors else decision
    record = gate["attestation"]
    if not allow_attestation or record is None:
        return None
    value = attestation.get(record["key"])
    return value if gate_satisfied(gate, attestation, allow_attestation=True) else None


def use_is_conditionally_excluded(
    document: dict[str, Any],
    use: dict[str, Any],
    gates: dict[str, dict[str, Any]],
    attestation: dict[str, Any],
    *,
    allow_attestation: bool,
    root: Path,
) -> bool:
    conditional = use.get("conditional_exclusion")
    if not isinstance(conditional, dict):
        return False
    gate = gates.get(conditional.get("gate"))
    if gate is None:
        return False
    return gate_decision(
        document,
        gate,
        attestation,
        allow_attestation=allow_attestation,
        root=root,
    ) == conditional.get("value")


def attestation_identity(document: dict[str, Any], attestation: dict[str, Any]) -> dict[str, Any]:
    """Return only registered, type-valid assertions for a redacted receipt."""
    contracts = {
        gate["attestation"]["key"]: gate["attestation"]
        for gate in document["human_gates"]
        if gate["attestation"] is not None
    }
    values: dict[str, bool | str | None] = {}
    for key in sorted(contracts):
        value = attestation.get(key)
        record = contracts[key]
        if value is None:
            values[key] = None
        elif record["kind"] == "boolean" and type(value) is bool:
            values[key] = value
        elif record["kind"] == "choice" and isinstance(value, str) and value in record["values"]:
            values[key] = value
        else:
            # Invalid external values are never reflected into a public receipt.
            values[key] = None
    return {"sha256": value_sha256(values), "values": values}


@functools.lru_cache(maxsize=1)
def _delivery_contract() -> Any:
    """Load the package builder once without depending on caller sys.path state."""
    try:
        path = ROOT / "render" / "deliver.py"
        spec = importlib.util.spec_from_file_location("danse_delivery_source_contract", path)
        if spec is None or spec.loader is None:
            raise ImportError("delivery source contract has no loader")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except (ImportError, OSError, ValueError, AttributeError) as exc:
        raise RightsError("cannot load the canonical delivery source contract") from exc
    return module


def expected_delivery_source_sha256(tier: str) -> str:
    """Query the package builder's canonical source identity for one safe tier."""
    if not SAFE_TIER.fullmatch(tier):
        raise RightsError("package manifest corpus tier is invalid")
    try:
        return _delivery_contract().delivery_source_sha256(tier)
    except (OSError, ValueError, AttributeError) as exc:
        raise RightsError("cannot compute the canonical delivery source identity") from exc


def expected_renderer_source_sha256(tier: str) -> str:
    """Query the exact visual source identity required in segment receipts."""
    if not SAFE_TIER.fullmatch(tier):
        raise RightsError("package manifest corpus tier is invalid")
    try:
        return _delivery_contract().renderer_source_sha256(tier)
    except (OSError, ValueError, AttributeError, SystemExit) as exc:
        raise RightsError("cannot compute the canonical renderer source identity") from exc


def _audio_identity_blockers(identity: object, label: str) -> list[str]:
    """Validate the one typed competition sound identity copied across a package."""
    if not isinstance(identity, dict):
        return [f"{label} has no typed competition sound identity"]
    blockers: list[str] = []
    if set(identity) != set(AUDIO_SOUND_FIELDS):
        blockers.append(f"{label} has fields outside the competition sound contract")
    if identity.get("profile") != COMPETITION_AUDIO_PROFILE:
        blockers.append(f"{label} does not select the package-eligible competition-classical profile")
    for field in AUDIO_IDENTITY_HASH_FIELDS:
        value = identity.get(field)
        if not isinstance(value, str) or not HEX64.fullmatch(value):
            blockers.append(f"{label} has no exact {field} identity")
    fixed_hashes = {
        "audio_uses_sha256": AUDIO_USES_SHA256,
        "midi_sha256": ADAPTED_DELIBES_MIDI_SHA256,
        "adaptation_sha256": DELIBES_ADAPTATION_SHA256,
        "soundfont_sha256": MUSESCORE_GENERAL_SF3_SHA256,
    }
    for field, expected in fixed_hashes.items():
        if identity.get(field) != expected:
            blockers.append(f"{label} has a stale or substituted {field} identity")
    if identity.get("sources") != list(COMPETITION_SOURCE_IDS):
        blockers.append(f"{label} does not name the exact competition-classical sources")
    stems = identity.get("stems")
    if not isinstance(stems, list) or len(stems) != len(COMPETITION_STEM_IDS):
        blockers.append(f"{label} has no exact competition stem identity")
    else:
        for index, (stem, expected_id) in enumerate(zip(stems, COMPETITION_STEM_IDS, strict=True)):
            if (
                not isinstance(stem, dict)
                or set(stem) != {"id", "sha256"}
                or stem.get("id") != expected_id
                or not isinstance(stem.get("sha256"), str)
                or not HEX64.fullmatch(stem["sha256"])
            ):
                blockers.append(f"{label} has a malformed or reordered stem at index {index}")
                break
    if identity.get("credit") != REQUIRED_DELIBES_CREDIT:
        blockers.append(f"{label} does not carry the exact required Delibes credit")
    return blockers


def current_audio_identity(span: dict[str, Any]) -> dict[str, Any]:
    """Return the exact deterministic competition-audio identity for one passage."""
    try:
        identity = _delivery_contract().competition_audio_provenance(span)
    except (OSError, ValueError, AttributeError, SystemExit) as exc:
        raise RightsError(
            "cannot verify package audio against the deterministic competition render receipt"
        ) from exc
    blockers = _audio_identity_blockers(identity, "canonical competition audio")
    if blockers:
        raise RightsError(blockers[0])
    return {field: identity[field] for field in AUDIO_SOUND_FIELDS}


def _package_inventory(package_root: Path) -> tuple[set[str], list[str]]:
    """Inventory every regular package path without following directory links."""
    paths: set[str] = set()
    blockers: list[str] = []

    def walk_error(_: OSError) -> None:
        blockers.append("package could not be completely inventoried")

    for directory, dirnames, filenames in os.walk(
        package_root,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        base = Path(directory)
        descend: list[str] = []
        for name in sorted(dirnames):
            candidate = base / name
            if candidate.is_symlink():
                blockers.append("package contains a symlink directory")
            elif not candidate.is_dir():
                blockers.append("package contains a non-directory entry")
            else:
                descend.append(name)
        dirnames[:] = descend
        for name in sorted(filenames):
            candidate = base / name
            if candidate.is_symlink():
                blockers.append("package contains a symlink file")
            elif not candidate.is_file():
                blockers.append("package contains a non-regular file")
            else:
                paths.add(candidate.relative_to(package_root).as_posix())
    return paths, blockers


def _required_package_blockers(
    submission: dict[str, Any],
    item_rule_ids: dict[str, str],
) -> list[str]:
    """Require the exact artifact census declared by the submission register."""
    package = submission.get("package")
    if not isinstance(package, dict):
        return ["canonical submission register has no package contract"]
    blockers: list[str] = []
    try:
        delivery = _delivery_contract()
        audio_items = delivery.AUDIO_ITEMS
        required_items = {}
        for section_name in ("master", "screener"):
            matches = sorted(
                name
                for name in audio_items
                if PurePosixPath(name).stem == section_name
            )
            if len(matches) != 1:
                raise RightsError(
                    f"canonical delivery contract has no unique {section_name} destination"
                )
            required_items[section_name] = (matches[0], "moving-image")
        score_destination = safe_relative(
            delivery.SCORE_SOURCE_ITEM,
            "canonical package score source destination",
            expose_value=False,
        )
        audio = package.get("audio")
        if not isinstance(audio, dict):
            raise RightsError("canonical package audio contract is missing")
        audio_render_destination = safe_relative(
            audio.get("audio_render_receipt"),
            "canonical package audio-render receipt destination",
            expose_value=False,
        )
    except (RightsError, AttributeError, TypeError):
        return ["canonical delivery contract cannot resolve required package destinations"]
    moving_image_required = False
    for section_name, (destination, rule_id) in required_items.items():
        section = package.get(section_name)
        if not isinstance(section, dict):
            blockers.append(f"canonical package {section_name} contract is missing")
            continue
        if section.get("required") is not True:
            continue
        moving_image_required = True
        if item_rule_ids.get(destination) != rule_id:
            blockers.append(
                f"package is missing required {section_name.replace('_', ' ')} artifact {destination}"
            )

    if moving_image_required:
        if item_rule_ids.get(score_destination) != "score-source":
            blockers.append(
                f"package is missing required score source artifact {score_destination}"
            )
        if item_rule_ids.get(audio_render_destination) != "audio-render-receipt":
            blockers.append(
                "package is missing required audio-render receipt artifact "
                f"{audio_render_destination}"
            )

    origin = package.get("origin_still")
    if not isinstance(origin, dict):
        blockers.append("canonical package origin_still contract is missing")
    elif origin.get("required") is True:
        filename = origin.get("filename")
        if (
            not isinstance(filename, str)
            or PurePosixPath(filename).name != filename
            or not filename
        ):
            blockers.append("canonical package origin still filename is invalid")
        else:
            destination = f"stills/{filename}"
            if item_rule_ids.get(destination) != "origin-still":
                blockers.append(
                    f"package is missing required origin still artifact {destination}"
                )

    stills = package.get("stills")
    if not isinstance(stills, dict):
        blockers.append("canonical package stills contract is missing")
    elif stills.get("required") is True:
        count_min = stills.get("count_min")
        generated = sum(1 for rule_id in item_rule_ids.values() if rule_id == "generated-still")
        if type(count_min) is not int or count_min < 1:
            blockers.append("canonical package stills minimum is invalid")
        elif generated < count_min:
            blockers.append(
                f"package has {generated} generated still(s); canonical submission requires {count_min}"
            )
    return blockers


def _package_production_blockers(
    package_root: Path,
    manifest: dict[str, Any],
    item_records: dict[str, dict[str, Any]],
    item_rule_ids: dict[str, str],
    audio_identity: dict[str, Any] | None,
) -> list[str]:
    """Authenticate rendered package outputs through immutable producer receipts."""
    blockers: list[str] = []
    rendered_names = {
        name
        for name, rule_id in item_rule_ids.items()
        if rule_id in {"moving-image", "generated-still", "score-source"}
    }
    if not rendered_names:
        return blockers
    reference = manifest.get("production")
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        return ["package manifest has no typed production receipt"]
    if reference.get("path") != PRODUCTION_RECEIPT:
        return ["package manifest production receipt is not at its canonical path"]
    try:
        production_path = _external_file(
            package_root,
            PRODUCTION_RECEIPT,
            "package production receipt",
        )
        production_digest, _, production_payload = _stable_file_measure(
            production_path,
            "package production receipt",
            capture=True,
        )
        if reference.get("sha256") != production_digest or production_payload is None:
            raise RightsError("package production receipt digest is missing or stale")
        production = _parse_json_bytes(production_payload, "package production receipt")
    except RightsError as exc:
        return [str(exc)]
    if set(production) != {
        "schema",
        "source_tree_sha256",
        "repository_head",
        "passage",
        "sound",
        "producers",
        "outputs",
    }:
        blockers.append("package production receipt has fields outside its typed contract")
    if production.get("schema") != "danse.delivery.production.v1":
        blockers.append("package production receipt has the wrong schema")
    source_tree = manifest.get("source_tree_sha256")
    if production.get("source_tree_sha256") != source_tree:
        blockers.append("package production receipt names a different delivery source tree")
    repository_head = manifest.get("repository_head")
    if production.get("repository_head") != repository_head:
        blockers.append("package production receipt names a different repository head")
    if production.get("sound") != manifest.get("sound"):
        blockers.append("package production receipt does not copy the manifest sound identity")
    tier = manifest.get("corpus_tier")
    try:
        renderer_source_tree = expected_renderer_source_sha256(tier)
    except RightsError as exc:
        blockers.append(str(exc))
        renderer_source_tree = None
    try:
        delivery_contract = _delivery_contract()
        reel_item = delivery_contract.REEL_ITEM
        if (
            not isinstance(reel_item, str)
            or reel_item not in delivery_contract.AUDIO_ITEMS
        ):
            raise RightsError("canonical delivery contract has no reel destination")
    except (AttributeError, OSError, RightsError):
        blockers.append("canonical delivery contract has no reel destination")
        reel_item = None
    passage_keys = {
        "seed",
        "passage_seed",
        "passage",
        "start",
        "t0",
        "t1",
        "duration",
        "corpus_tier",
    }
    passage = production.get("passage")
    if not isinstance(passage, dict) or set(passage) != passage_keys:
        blockers.append("package production receipt has a malformed passage identity")
    elif any(passage.get(key) != manifest.get(key) for key in passage_keys):
        blockers.append("package production receipt names a different passage")

    producer_rows = production.get("producers")
    if not isinstance(producer_rows, list) or not producer_rows:
        blockers.append("package production receipt has no producers")
        producer_rows = []
    producers: dict[str, dict[str, Any]] = {}
    producer_receipts: dict[str, tuple[dict[str, Any], str, Path]] = {}
    producer_paths: set[str] = set()
    for index, producer in enumerate(producer_rows):
        if not isinstance(producer, dict) or set(producer) != {
            "id",
            "kind",
            "receipt",
            "output_sha256",
            "components",
        }:
            blockers.append(f"package producer {index} has fields outside its typed contract")
            continue
        producer_id = producer.get("id")
        if (
            not isinstance(producer_id, str)
            or not SAFE_ID.fullmatch(producer_id)
            or producer_id in producers
        ):
            blockers.append(f"package producer {index} has an invalid or repeated identity")
            continue
        producers[producer_id] = producer
        kind = producer.get("kind")
        if kind not in {"render-segment", "render-concat", "score"}:
            blockers.append(f"package producer {producer_id} has an unknown kind")
        components = producer.get("components")
        if (
            not isinstance(components, list)
            or not all(isinstance(value, str) and SAFE_ID.fullmatch(value) for value in components)
            or len(components) != len(set(components))
        ):
            blockers.append(f"package producer {producer_id} has malformed components")
        receipt = producer.get("receipt")
        if not isinstance(receipt, dict) or set(receipt) != {"path", "sha256"}:
            blockers.append(f"package producer {producer_id} has no exact receipt identity")
            continue
        try:
            relative = safe_relative(
                receipt.get("path"),
                f"package producer {producer_id} receipt",
                expose_value=False,
            )
            pure = PurePosixPath(relative)
            if pure.parent.as_posix() != PRODUCER_RECEIPTS or pure.suffix != ".json":
                raise RightsError(f"package producer {producer_id} receipt is outside its boundary")
            if relative in producer_paths:
                raise RightsError(f"package producer {producer_id} reuses another producer receipt")
            producer_paths.add(relative)
            path = _external_file(
                package_root,
                relative,
                f"package producer {producer_id} receipt",
            )
            digest, _, payload = _stable_file_measure(
                path,
                f"package producer {producer_id} receipt",
                capture=True,
            )
            if receipt.get("sha256") != digest or payload is None:
                raise RightsError(f"package producer {producer_id} receipt digest is stale")
            producer_receipts[producer_id] = (
                _parse_json_bytes(payload, f"package producer {producer_id} receipt"),
                digest,
                path,
            )
        except RightsError as exc:
            blockers.append(str(exc))

    receipt_root = package_root / PRODUCER_RECEIPTS
    try:
        if receipt_root.is_symlink() or not receipt_root.is_dir():
            raise RightsError("package producer-receipt boundary is not a regular directory")
        inventoried_receipts = set()
        for path in receipt_root.iterdir():
            if path.is_symlink() or not path.is_file():
                raise RightsError("package producer-receipt boundary contains an unsafe entry")
            inventoried_receipts.add(path.relative_to(package_root).as_posix())
        if inventoried_receipts != producer_paths:
            blockers.append("package producer-receipt inventory does not match its typed producers")
    except RightsError as exc:
        blockers.append(str(exc))
    except OSError:
        blockers.append("package producer-receipt boundary could not be inventoried")

    for producer_id, producer in producers.items():
        got = producer_receipts.get(producer_id)
        if got is None:
            continue
        receipt, _, _ = got
        kind = producer.get("kind")
        components = producer.get("components")
        output_sha256 = producer.get("output_sha256")
        if not isinstance(output_sha256, str) or not HEX64.fullmatch(output_sha256):
            blockers.append(f"package producer {producer_id} has no exact output digest")
        if kind == "render-segment":
            inputs = receipt.get("inputs") if isinstance(receipt.get("inputs"), dict) else {}
            if receipt.get("schema") != "danse.render.segment.v1":
                blockers.append(f"package producer {producer_id} is not a render-segment receipt")
            if components:
                blockers.append(f"package render segment {producer_id} must not have components")
            if (
                renderer_source_tree is None
                or inputs.get("source_tree_sha256") != renderer_source_tree
            ):
                blockers.append(f"package render segment {producer_id} names a different source tree")
            if inputs.get("tier") != manifest.get("corpus_tier"):
                blockers.append(f"package render segment {producer_id} names a different corpus tier")
            if receipt.get("file_sha256") != output_sha256:
                blockers.append(f"package render segment {producer_id} output digest is stale")
        elif kind == "render-concat":
            segments = receipt.get("segments")
            if receipt.get("schema") != "danse.render.concat.v1" or not isinstance(
                segments, list
            ):
                blockers.append(f"package producer {producer_id} is not a render-concat receipt")
                segments = []
            component_digests = [
                producer_receipts[value][1]
                for value in components or []
                if value in producer_receipts
            ]
            declared_digests = [
                row.get("receipt_sha256")
                for row in segments
                if isinstance(row, dict)
            ]
            if (
                len(component_digests) != len(components or [])
                or declared_digests != component_digests
                or any(
                    producers.get(value, {}).get("kind") != "render-segment"
                    for value in components or []
                )
            ):
                blockers.append(f"package render concat {producer_id} has an invalid segment chain")
            if receipt.get("file_sha256") != output_sha256:
                blockers.append(f"package render concat {producer_id} output digest is stale")
        elif kind == "score":
            expected_fields = {
                "schema",
                "sha256",
                "t0",
                "t1",
                "duration",
                *AUDIO_SOUND_FIELDS,
            }
            if set(receipt) != expected_fields:
                blockers.append(
                    f"package score producer {producer_id} has fields outside its typed contract"
                )
            if receipt.get("schema") != "danse.score.receipt.v2":
                blockers.append(f"package producer {producer_id} is not a score receipt")
            if components:
                blockers.append(f"package score producer {producer_id} must not have components")
            if receipt.get("sha256") != output_sha256:
                blockers.append(f"package score producer {producer_id} output digest is stale")
            if (
                receipt.get("t0") != manifest.get("t0")
                or receipt.get("t1") != manifest.get("t1")
                or receipt.get("duration") != manifest.get("duration")
            ):
                blockers.append(f"package score producer {producer_id} names a different passage")
            receipt_sound = {field: receipt.get(field) for field in AUDIO_SOUND_FIELDS}
            blockers.extend(
                _audio_identity_blockers(
                    receipt_sound,
                    f"package score producer {producer_id}",
                )
            )
            manifest_sound = manifest.get("sound")
            if receipt_sound != manifest_sound:
                blockers.append(
                    f"package score producer {producer_id} does not copy the manifest sound identity"
                )
            if audio_identity is not None and receipt_sound != audio_identity:
                blockers.append(
                    f"package score producer {producer_id} names a different competition audio render"
                )
            if receipt.get("sha256") != receipt_sound.get("master_sha256"):
                blockers.append(
                    f"package score producer {producer_id} master digest differs from its WAV"
                )

    output_rows = production.get("outputs")
    if not isinstance(output_rows, list):
        blockers.append("package production receipt has no output inventory")
        output_rows = []
    outputs: dict[str, dict[str, Any]] = {}
    directly_used_producers: set[str] = set()
    score_output_sha = next(
        (
            item_records[name].get("sha256")
            for name, rule_id in item_rule_ids.items()
            if rule_id == "score-source"
        ),
        None,
    )

    def canonical_capture_start(capture: str) -> float | None:
        if capture == "passage":
            return manifest.get("t0")
        try:
            start = float(manifest.get("start"))
            return _delivery_contract().query_capture_span(capture, start=start)["t0"]
        except (KeyError, OSError, TypeError, ValueError, SystemExit):
            blockers.append(f"cannot resolve canonical {capture} capture start")
            return None

    def validate_render_invocation(producer_id: str, capture: str) -> None:
        producer = producers.get(producer_id)
        if producer is None:
            return
        component_ids = (
            producer.get("components")
            if producer.get("kind") == "render-concat"
            else [producer_id]
        )
        if not isinstance(component_ids, list):
            return
        expected_start = canonical_capture_start(capture)
        expected = {
            "window": capture,
            "tier": tier,
            "seed": None,
            "stream": 0,
            "codec": "h264" if capture == "reel" else "prores",
            "width": None,
            "height": None,
            "fps": None,
            "segment_frames": 600,
        }
        for component_id in component_ids or []:
            got = producer_receipts.get(component_id)
            if got is None:
                continue
            receipt = got[0]
            inputs = receipt.get("inputs") if isinstance(receipt.get("inputs"), dict) else {}
            mismatch = any(inputs.get(key) != value for key, value in expected.items())
            try:
                start_mismatch = expected_start is None or abs(
                    float(inputs.get("start")) - float(expected_start)
                ) > 1e-9
            except (TypeError, ValueError):
                start_mismatch = True
            if mismatch or start_mismatch:
                blockers.append(
                    f"package render producer {component_id} does not match the canonical {capture} invocation"
                )

    def validate_still_invocation(producer_id: str, name: str) -> None:
        got = producer_receipts.get(producer_id)
        if got is None:
            return
        receipt = got[0]
        inputs = receipt.get("inputs") if isinstance(receipt.get("inputs"), dict) else {}
        try:
            seed = int(Path(name).stem.removeprefix("seed-"), 0)
        except ValueError:
            seed = None
        try:
            expected_start = (
                _delivery_contract()
                .query_capture_span(
                    "passage",
                    seed=seed,
                    start=float(manifest.get("t0")),
                )["t0"]
                if seed is not None
                else None
            )
        except (KeyError, OSError, TypeError, ValueError, SystemExit):
            expected_start = None
        expected = {
            "window": "passage",
            "tier": tier,
            "seed": seed,
            "stream": 0,
            "codec": "prores",
            "width": None,
            "height": None,
            "fps": None,
            "segment_frames": 1,
        }
        try:
            start_mismatch = expected_start is None or abs(
                float(inputs.get("start")) - float(expected_start)
            ) > 1e-9
        except (TypeError, ValueError):
            start_mismatch = True
        if seed is None or any(
            inputs.get(key) != value for key, value in expected.items()
        ) or receipt.get("frames") != 1 or start_mismatch:
            blockers.append(
                f"package render producer {producer_id} does not match generated still {name}"
            )

    for index, output in enumerate(output_rows):
        if not isinstance(output, dict) or set(output) != {
            "name",
            "bytes",
            "sha256",
            "producers",
        }:
            blockers.append(f"package production output {index} has fields outside its typed contract")
            continue
        name = output.get("name")
        if not isinstance(name, str) or name in outputs:
            blockers.append(f"package production output {index} has an invalid or repeated identity")
            continue
        outputs[name] = output
        item = item_records.get(name)
        if item is None or output.get("sha256") != item.get("sha256") or output.get(
            "bytes"
        ) != item.get("bytes"):
            blockers.append(f"package production output {name} does not bind its manifested bytes")
        producer_ids = output.get("producers")
        if (
            not isinstance(producer_ids, list)
            or not producer_ids
            or not all(isinstance(value, str) and value in producers for value in producer_ids)
            or len(producer_ids) != len(set(producer_ids))
        ):
            blockers.append(f"package production output {name} has invalid producers")
            continue
        directly_used_producers.update(producer_ids)
        kinds = [producers[value]["kind"] for value in producer_ids]
        rule_id = item_rule_ids.get(name)
        if rule_id == "moving-image":
            if sorted(kinds) != ["render-concat", "score"]:
                blockers.append(f"package moving image {name} lacks exact render and score producers")
            else:
                render_id = producer_ids[kinds.index("render-concat")]
                score_id = producer_ids[kinds.index("score")]
                validate_render_invocation(
                    render_id,
                    "reel" if name == reel_item else "passage",
                )
                if producers[score_id].get("output_sha256") != score_output_sha:
                    blockers.append(f"package moving image {name} names a different score producer")
        elif rule_id == "generated-still":
            if kinds != ["render-segment"]:
                blockers.append(f"package generated still {name} lacks its exact render producer")
            else:
                validate_still_invocation(producer_ids[0], name)
        elif rule_id == "score-source":
            if kinds != ["score"]:
                blockers.append(f"package score source {name} lacks its exact score producer")
            elif producers[producer_ids[0]].get("output_sha256") != output.get("sha256"):
                blockers.append(f"package score source {name} names a different score producer")

    missing_outputs = sorted(rendered_names - outputs.keys())
    extra_outputs = sorted(outputs.keys() - rendered_names)
    if missing_outputs:
        blockers.append(
            f"package production receipt is missing {len(missing_outputs)} rendered output(s)"
        )
    if extra_outputs:
        blockers.append(
            f"package production receipt names {len(extra_outputs)} non-rendered output(s)"
        )

    reachable_producers = set(directly_used_producers)
    frontier = list(reachable_producers)
    while frontier:
        producer_id = frontier.pop()
        producer = producers.get(producer_id)
        for component in (producer or {}).get("components") or []:
            if component in producers and component not in reachable_producers:
                reachable_producers.add(component)
                frontier.append(component)
    if set(producers) != reachable_producers:
        blockers.append("package production receipt contains an unreferenced producer")

    try:
        final_digest, _, _ = _stable_file_measure(
            production_path,
            "package production receipt",
        )
        if final_digest != production_digest:
            blockers.append("package production receipt changed during validation")
    except RightsError:
        blockers.append("package production receipt changed during validation")
    for producer_id, (_, digest, path) in producer_receipts.items():
        try:
            final_digest, _, _ = _stable_file_measure(
                path,
                f"package producer {producer_id} receipt",
            )
            if final_digest != digest:
                blockers.append("package producer receipt changed during validation")
        except RightsError:
            blockers.append("package producer receipt changed during validation")
    return blockers


def validate_package(
    document: dict[str, Any],
    package: Path,
    *,
    root: Path = ROOT,
    as_of: date | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    blockers: list[str] = []
    validation_date = as_of or project_today()
    uses = _asset_use_index(document)
    try:
        package_root = _external_root(package, "package")
        manifest_path = _external_file(package_root, "manifest.json", "package manifest")
        manifest_digest, _, manifest_payload = _stable_file_measure(
            manifest_path,
            "package manifest",
            capture=True,
        )
        if manifest_payload is None:
            raise RightsError("package manifest bytes could not be retained")
        manifest = _parse_json_bytes(manifest_payload, "package manifest")
    except RightsError as exc:
        return [str(exc)], None

    package_schema = manifest.get("schema")
    if package_schema != "danse.delivery.manifest.v1":
        blockers.append("package manifest schema is not danse.delivery.manifest.v1")
    tier = manifest.get("corpus_tier")
    if not isinstance(tier, str) or not SAFE_TIER.fullmatch(tier):
        blockers.append("package manifest has no valid corpus tier")
    if not isinstance(manifest.get("source_tree_sha256"), str) or not HEX64.fullmatch(manifest["source_tree_sha256"]):
        blockers.append("package manifest has no exact source-tree SHA-256")
    elif isinstance(tier, str) and SAFE_TIER.fullmatch(tier):
        try:
            expected_source_tree = expected_delivery_source_sha256(tier)
            if manifest["source_tree_sha256"] != expected_source_tree:
                blockers.append("package manifest source-tree SHA-256 does not match the canonical delivery tree")
        except RightsError as exc:
            blockers.append(str(exc))
    repository_head = manifest.get("repository_head")
    if not isinstance(repository_head, str) or not GIT_OID.fullmatch(repository_head):
        blockers.append("package manifest has no exact repository head")
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        blockers.append("package manifest has no items")
        items = []

    rules: list[tuple[dict[str, Any], re.Pattern[str]]] = []
    rule_ids = {rule["id"] for rule in document["package_rules"]}
    for rule in document["package_rules"]:
        try:
            rules.append((rule, re.compile(rule["pattern"])))
        except re.error:
            blockers.append(f"rights register package rule {rule['id']} has an invalid regex")
    for required_rule in (
        "moving-image",
        "origin-still",
        "score-source",
        "audio-render-receipt",
        "score-motion-evidence",
    ):
        if required_rule not in rule_ids:
            blockers.append(f"rights register is missing required package rule {required_rule}")
    item_names: set[str] = set()
    item_records: dict[str, dict[str, Any]] = {}
    item_rule_ids: dict[str, str] = {}
    verified_items: list[tuple[str, dict[str, Any]]] = []
    submission = load_yaml(regular_file(root, document["bindings"]["submission"]["source"]["path"], "submission binding"), "submission binding")
    expected_origin = (((submission.get("package") or {}).get("origin_still") or {}).get("source_sha256"))
    initial_package_paths, initial_inventory_blockers = _package_inventory(package_root)
    blockers.extend(initial_inventory_blockers)

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            blockers.append(f"package manifest item {index} is not a record")
            continue
        try:
            name = safe_relative(
                item.get("name"),
                f"package manifest item {index} name",
                expose_value=False,
            )
        except RightsError as exc:
            blockers.append(str(exc))
            continue
        if name in item_names:
            blockers.append(f"package manifest repeats item identity at index {index}")
            continue
        item_names.add(name)
        item_records[name] = item
        matched = [rule for rule, expression in rules if expression.fullmatch(name)]
        public_label = name if len(matched) == 1 else f"manifest item {index}"
        try:
            path = _external_file(package_root, name, f"package {public_label}")
            actual_digest, actual_bytes, _ = _stable_file_measure(path, f"package {public_label}")
            expected_digest = item.get("sha256")
            item_is_exact = True
            if not isinstance(expected_digest, str) or not HEX64.fullmatch(expected_digest):
                blockers.append(f"package {public_label} has no valid SHA-256")
                item_is_exact = False
            elif actual_digest != expected_digest:
                blockers.append(f"package {public_label} digest does not match its manifest")
                item_is_exact = False
            if item.get("bytes") != actual_bytes:
                blockers.append(f"package {public_label} byte count does not match its manifest")
                item_is_exact = False
            if item_is_exact:
                verified_items.append((name, item))
        except RightsError as exc:
            blockers.append(str(exc))

        if len(matched) != 1:
            blockers.append(f"package manifest item {index} matches {len(matched)} rights rules; exactly one is required")
            rule_id = None
        else:
            blockers.extend(
                _requirement_blockers(
                    matched[0]["requirements"],
                    uses,
                    f"package item {name}",
                    validation_date,
                )
            )
            rule_id = matched[0]["id"]
            item_rule_ids[name] = rule_id

        if rule_id == "origin-still" and (
            item.get("source_sha256") != expected_origin or item.get("copy_mode") != "byte-identical"
        ):
            blockers.append("package origin still is not bound byte-identically to its registered source")

    blockers.extend(_required_package_blockers(submission, item_rule_ids))

    moving_items = [
        (name, item_records[name])
        for name, rule_id in item_rule_ids.items()
        if rule_id == "moving-image"
    ]
    score_items = [
        (name, item_records[name])
        for name, rule_id in item_rule_ids.items()
        if rule_id == "score-source"
    ]
    audio_render_items = [
        (name, item_records[name])
        for name, rule_id in item_rule_ids.items()
        if rule_id == "audio-render-receipt"
    ]
    timed_audio_items = [*moving_items, *score_items]
    audio_identity: dict[str, Any] | None = None
    if moving_items:
        if len(score_items) != 1:
            blockers.append("package moving images require exactly one manifested score source")
            score_digest = None
        else:
            score_digest = score_items[0][1].get("sha256")
        try:
            audio_identity = current_audio_identity(
                {**manifest, "river_seed": int(str(manifest.get("seed", "-1")), 0)}
            )
        except RightsError as exc:
            blockers.append(str(exc))
            audio_identity = None
        manifest_sound = manifest.get("sound")
        blockers.extend(_audio_identity_blockers(manifest_sound, "package manifest"))
        if audio_identity is not None and manifest_sound != audio_identity:
            blockers.append("package manifest sound does not bind the canonical competition audio render")
        if isinstance(manifest_sound, dict) and (
            score_digest is None or manifest_sound.get("master_sha256") != score_digest
        ):
            blockers.append("package manifest sound does not bind the manifested score source")
        for name, item in timed_audio_items:
            sound = item.get("sound")
            if not isinstance(sound, dict):
                blockers.append(f"package audio item {name} has no competition sound identity")
                continue
            if sound != manifest_sound:
                blockers.append(f"package audio item {name} does not copy the manifest sound identity")
        expected_audio_render = (
            (((submission.get("package") or {}).get("audio") or {}).get("audio_render_receipt"))
        )
        if len(audio_render_items) != 1:
            blockers.append("package moving images require exactly one manifested audio-render receipt")
        else:
            audio_render_name, audio_render_item = audio_render_items[0]
            if audio_render_name != expected_audio_render:
                blockers.append("package audio-render receipt is not at its canonical destination")
            receipt_digest = audio_render_item.get("sha256")
            if not isinstance(manifest_sound, dict) or (
                receipt_digest != manifest_sound.get("audio_render_receipt_sha256")
            ):
                blockers.append(
                    "package audio-render receipt does not bind the manifest sound identity"
                )

    blockers.extend(
        _package_production_blockers(
            package_root,
            manifest,
            item_records,
            item_rule_ids,
            audio_identity,
        )
    )

    for relative in initial_package_paths:
        if Path(relative).suffix.lower() in RIGHTS_MEDIA_SUFFIXES and relative not in item_names:
            blockers.append("rights-bearing package media is absent from the manifest")

    for binding in document["package_text"]:
        destination = binding["destination"]
        item = item_records.get(destination)
        if item is None:
            blockers.append(f"package text {binding['id']} is absent from the manifest")
        try:
            staged = _external_file(package_root, destination, f"package text {binding['id']}")
            expected_digest = binding["source"]["sha256"]
            expected_source = regular_file(
                root,
                binding["source"]["path"],
                f"package text {binding['id']} source",
            )
            expected_bytes = expected_source.stat().st_size
            if sha256(staged) != expected_digest:
                blockers.append(f"package text {binding['id']} does not match its tracked source")
            if item is not None and (
                item.get("sha256") != expected_digest or item.get("bytes") != expected_bytes
            ):
                blockers.append(f"package text {binding['id']} manifest identity is stale")
        except RightsError as exc:
            blockers.append(str(exc))

    for name, item in verified_items:
        try:
            path = _external_file(package_root, name, "package manifest item recheck")
            actual_digest, actual_bytes, _ = _stable_file_measure(path, "package manifest item recheck")
            if item.get("sha256") != actual_digest or item.get("bytes") != actual_bytes:
                blockers.append("package manifest item changed during package validation")
        except RightsError:
            blockers.append("package manifest item changed during package validation")
    try:
        final_manifest_digest, _, _ = _stable_file_measure(manifest_path, "package manifest")
        if final_manifest_digest != manifest_digest:
            blockers.append("package manifest changed during validation")
    except RightsError:
        blockers.append("package manifest changed during validation")
    final_package_paths, final_inventory_blockers = _package_inventory(package_root)
    blockers.extend(final_inventory_blockers)
    if (
        final_package_paths != initial_package_paths
        or final_inventory_blockers != initial_inventory_blockers
    ):
        blockers.append("package inventory changed during validation")

    identity = {
        "schema": package_schema if package_schema == "danse.delivery.manifest.v1" else None,
        "sha256": manifest_digest,
        "items": len(items),
    }
    return blockers, identity


def _verify_release_source(
    root: Path,
    source: object,
    label: str,
    *,
    tracked: set[str],
    require_tracked: bool,
    require_artifact: bool = False,
) -> list[str]:
    if not isinstance(source, dict):
        return [f"{label} has no source record"]
    try:
        relative = safe_relative(source.get("path"), f"{label} source", expose_value=False)
    except RightsError as exc:
        return [str(exc)]
    if require_artifact:
        try:
            destination = safe_relative(
                source.get("destination"),
                f"{label} destination",
                expose_value=False,
            )
        except RightsError as exc:
            return [str(exc)]
        if destination != relative:
            return [f"{label} source is not the exact staged release destination"]
        if not relative.startswith("media/assets/"):
            return [f"{label} source is outside the release media boundary"]
    if require_tracked and relative not in tracked:
        return [f"{label} source is not tracked public-safe evidence"]
    try:
        path = regular_file(root, relative, f"{label} source", expose_value=False)
    except RightsError as exc:
        return [str(exc)]
    expected = source.get("sha256")
    try:
        actual_digest, actual_bytes, _ = _stable_file_measure(path, f"{label} source")
    except RightsError as exc:
        return [str(exc)]
    if not isinstance(expected, str) or not HEX64.fullmatch(expected) or actual_digest != expected:
        return [f"{label} source digest is missing or stale"]
    if require_artifact and (
        type(source.get("bytes")) is not int
        or source["bytes"] < 0
        or source["bytes"] != actual_bytes
    ):
        return [f"{label} source byte count is missing or stale"]
    return []


def _release_boundary_inventory(root: Path) -> tuple[set[str], list[str]]:
    """Inventory every regular file staged beneath the public release boundary."""
    boundary = root.absolute() / "media" / "assets"
    if boundary.is_symlink():
        return set(), ["release media boundary must not be a symlink"]
    if not boundary.exists():
        return set(), ["release media boundary is missing"]
    if not boundary.is_dir():
        return set(), ["release media boundary is not a directory"]

    paths: set[str] = set()
    blockers: list[str] = []

    def walk_error(_: OSError) -> None:
        blockers.append("release media boundary could not be inventoried")

    for directory, dirnames, filenames in os.walk(
        boundary,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        parent = Path(directory)
        descend: list[str] = []
        for name in sorted(dirnames):
            candidate = parent / name
            if candidate.is_symlink():
                blockers.append("release media boundary contains a symlink directory")
            else:
                descend.append(name)
        dirnames[:] = descend
        for name in sorted(filenames):
            candidate = parent / name
            if candidate.is_symlink():
                blockers.append("release media boundary contains a symlink file")
            elif not candidate.is_file():
                blockers.append("release media boundary contains a non-regular file")
            else:
                paths.add(candidate.relative_to(root.absolute()).as_posix())
    return paths, blockers


def _release_manifest_shape_errors(
    manifest: dict[str, Any],
    root: Path,
) -> list[str]:
    """Enforce either the compact interchange shape or the full closed release schema."""
    blockers: list[str] = []
    compact_top = {"schema", "release_id", "status", "media", "credits", "gates"}
    full_top = {
        "schema",
        "release_id",
        "version",
        "status",
        "opportunity_snapshot",
        "identity",
        "copy",
        "installation",
        "accessibility",
        "press",
        "claims",
        "credits",
        "media",
        "products",
        "gates",
    }
    top_keys = set(manifest)
    if top_keys == compact_top:
        full = False
    elif top_keys == full_top:
        full = True
        try:
            schema_path = regular_file(
                root,
                "release/manifest.schema.json",
                "full release manifest schema",
                expose_value=False,
            )
            schema = load_json(schema_path, "full release manifest schema", expose_path=False)
            jsonschema.Draft202012Validator.check_schema(schema)
            schema_errors = sorted(
                jsonschema.Draft202012Validator(
                    schema,
                    format_checker=jsonschema.FormatChecker(),
                ).iter_errors(manifest),
                key=lambda error: [str(part) for part in error.absolute_path],
            )
            for error in schema_errors:
                location = ".".join(str(part) for part in error.absolute_path) or "manifest"
                blockers.append(
                    f"full release manifest violates its closed schema at {location}"
                )
        except (RightsError, jsonschema.SchemaError):
            blockers.append("full release manifest closed schema is missing or invalid")
    else:
        full = False
        blockers.append("release manifest has fields outside a closed top-level schema")

    evidence_keys = {"path", "sha256", "summary"}

    def evidence_shape(value: Any, label: str, *, nullable: bool = False) -> None:
        if nullable and value is None:
            return
        if not isinstance(value, dict) or set(value) != evidence_keys:
            blockers.append(f"{label} has fields outside the closed evidence schema")

    media_keys = (
        {"id", "kind", "label", "required_for", "status", "source", "clearance", "alt_text"}
        if full
        else {"id", "required_for", "status", "source", "clearance"}
    )
    media = manifest.get("media")
    if isinstance(media, list):
        for index, row in enumerate(media):
            label = f"release media[{index}]"
            if not isinstance(row, dict) or set(row) != media_keys:
                blockers.append(f"{label} has fields outside the closed media schema")
                continue
            source = row.get("source")
            if source is not None and (
                not isinstance(source, dict)
                or set(source) != {"path", "destination", "sha256", "bytes"}
            ):
                blockers.append(f"{label} source has fields outside the closed source schema")
            clearance = row.get("clearance")
            if not isinstance(clearance, dict) or set(clearance) not in (
                {"status"},
                {"status", "owner", "evidence"},
            ):
                blockers.append(
                    f"{label} clearance has fields outside the closed clearance schema"
                )
            elif set(clearance) == {"status", "owner", "evidence"}:
                evidence_shape(clearance.get("evidence"), f"{label} clearance", nullable=True)

    credit_keys = (
        {"id", "role", "name", "status", "note", "evidence"}
        if full
        else {"id", "name", "status", "evidence"}
    )
    credit_rows = manifest.get("credits")
    if isinstance(credit_rows, list):
        for index, row in enumerate(credit_rows):
            label = f"release credit[{index}]"
            if not isinstance(row, dict) or set(row) != credit_keys:
                blockers.append(f"{label} has fields outside the closed credit schema")
                continue
            evidence_shape(row.get("evidence"), label, nullable=True)

    gate_keys = (
        {"id", "owner", "issue", "required_for", "state", "action", "evidence"}
        if full
        else {"id", "required_for", "state", "evidence"}
    )
    gates = manifest.get("gates")
    if isinstance(gates, list):
        for index, row in enumerate(gates):
            label = f"release gate[{index}]"
            if not isinstance(row, dict) or set(row) != gate_keys:
                blockers.append(f"{label} has fields outside the closed gate schema")
                continue
            evidence_shape(row.get("evidence"), label, nullable=True)
    return blockers


def _release_manifest_redaction_errors(manifest: dict[str, Any]) -> list[str]:
    """Apply the tracked register's public-safe redaction boundary to a release."""
    blockers: list[str] = []
    for location, value in _strings(manifest):
        if PRIVATE_PATH.search(value) or ABSOLUTE_PATH.search(value):
            blockers.append(f"release manifest {location}: contains a machine-local path")
        if EMAIL.search(value):
            blockers.append(f"release manifest {location}: contains an email address")
        if PHONE.search(value):
            blockers.append(f"release manifest {location}: contains a phone number")
    for location, key in _keys(manifest):
        if key.lower() in SENSITIVE_KEYS:
            blockers.append(f"release manifest {location}: contains a sensitive field")
    return blockers


def validate_release_manifest(
    document: dict[str, Any],
    release_manifest: Path,
    phase: str,
    *,
    root: Path = ROOT,
    register_path: Path = REGISTER,
    as_of: date | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    blockers: list[str] = []
    validation_date = as_of or project_today()
    try:
        tracked = tracked_paths(root)
    except RightsError as exc:
        return [str(exc)], None
    try:
        manifest_digest, _, manifest_payload = _stable_file_measure(
            release_manifest,
            "release manifest",
            capture=True,
        )
        if manifest_payload is None:
            raise RightsError("release manifest bytes could not be retained")
        manifest = _parse_json_bytes(manifest_payload, "release manifest")
    except RightsError as exc:
        return [str(exc)], None
    blockers.extend(_release_manifest_shape_errors(manifest, root))
    blockers.extend(_release_manifest_redaction_errors(manifest))
    release_schema = manifest.get("schema")
    if release_schema != "danse.release.v1":
        blockers.append("release manifest schema is not danse.release.v1")
    release_id = manifest.get("release_id")
    if not isinstance(release_id, str) or not SAFE_ID.fullmatch(release_id):
        blockers.append("release manifest has an invalid release identifier")
        release_id = None
    required_status = {"public-approved", "released"} if phase == "public" else {"released"}
    release_status = manifest.get("status")
    if not isinstance(release_status, str) or release_status not in required_status:
        blockers.append(f"release manifest status is not valid for {phase}")

    uses = _asset_use_index(document)
    release_rules = {row["media_id"]: row for row in document["release_rules"]}
    media_rows = manifest.get("media")
    if not isinstance(media_rows, list):
        blockers.append("release manifest has no media inventory")
        media_rows = []
    media_ids: set[str] = set()
    manifested_destinations: set[str] = set()
    verified_media: list[tuple[dict[str, Any], str]] = []
    for row in media_rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not SAFE_ID.fullmatch(row["id"])
        ):
            blockers.append("release manifest contains malformed media")
            continue
        media_id = row["id"]
        if media_id in media_ids:
            blockers.append(f"release manifest repeats media id {media_id}")
            continue
        media_ids.add(media_id)
        rule = release_rules.get(media_id)
        if rule is None:
            blockers.append(f"release media {media_id} has no rights rule")
            continue
        declared_phases = row.get("required_for")
        if not same_strings(declared_phases, rule["required_for"]):
            blockers.append(f"release media {media_id} phase scope disagrees with its rights rule")
        if phase not in rule["required_for"]:
            continue
        source = row.get("source")
        if not isinstance(source, dict) or (
            source.get("path") != rule["destination"]
            or source.get("destination") != rule["destination"]
        ):
            blockers.append(f"release media {media_id} does not use its canonical destination")
        else:
            manifested_destinations.add(rule["destination"])
            media_label = f"release media {media_id}"
            source_blockers = _verify_release_source(
                root,
                source,
                media_label,
                tracked=tracked,
                require_tracked=False,
                require_artifact=True,
            )
            blockers.extend(source_blockers)
            if not source_blockers:
                verified_media.append((source, media_label))
        blockers.extend(
            _requirement_blockers(
                rule["requirements"],
                uses,
                f"release media {media_id}",
                validation_date,
            )
        )
        if row.get("status") != "ready":
            blockers.append(f"release media {media_id} is not ready")
        clearance = row.get("clearance") if isinstance(row.get("clearance"), dict) else {}
        if clearance.get("status") != "cleared":
            blockers.append(f"release media {media_id} clearance is not cleared")
        clearance_evidence = clearance.get("evidence")
        clearance_blockers = _verify_release_source(
            root,
            clearance_evidence,
            f"release media {media_id} clearance",
            tracked=tracked,
            require_tracked=True,
        )
        blockers.extend(clearance_blockers)
        if (
            not clearance_blockers
            and clearance.get("status") == "cleared"
            and isinstance(source, dict)
            and isinstance(clearance_evidence, dict)
        ):
            try:
                clearance_path = regular_file(
                    root,
                    clearance_evidence.get("path"),
                    f"release media {media_id} clearance",
                    expose_value=False,
                )
                blockers.extend(
                    validate_media_clearance_receipt(
                        clearance_path,
                        media_id,
                        rule,
                        source,
                        clearance,
                    )
                )
            except RightsError as exc:
                blockers.append(str(exc))
    missing_media = sorted(set(release_rules) - media_ids)
    if missing_media:
        blockers.append(f"release manifest is missing rights-ruled media: {', '.join(missing_media)}")
    staged_destinations, boundary_blockers = _release_boundary_inventory(root)
    blockers.extend(boundary_blockers)
    unmanifested = staged_destinations - manifested_destinations
    if unmanifested:
        blockers.append(
            f"release media boundary contains {len(unmanifested)} artifact(s) not listed in the release manifest"
        )

    credit_rules = {row["credit_id"]: row for row in document["credit_rules"]}
    credit_rows = manifest.get("credits")
    if not isinstance(credit_rows, list):
        blockers.append("release manifest has no credit inventory")
        credit_rows = []
    credit_ids: set[str] = set()
    gates = {gate["id"]: gate for gate in document["human_gates"]}
    assets = {asset["id"]: asset for asset in document["assets"]}
    for row in credit_rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not SAFE_ID.fullmatch(row["id"])
        ):
            blockers.append("release manifest contains malformed credit")
            continue
        credit_id = row["id"]
        if credit_id in credit_ids:
            blockers.append(f"release manifest repeats credit id {credit_id}")
            continue
        credit_ids.add(credit_id)
        rule = credit_rules.get(credit_id)
        if rule is None:
            blockers.append(f"release credit {credit_id} has no rights rule")
            continue
        if row.get("status") != "cleared" or not row.get("name"):
            blockers.append(f"release credit {credit_id} is not cleared and named")
        blockers.extend(
            _verify_release_source(
                root,
                row.get("evidence"),
                f"release credit {credit_id}",
                tracked=tracked,
                require_tracked=True,
            )
        )
        gate = gates.get(rule["gate"])
        asset = assets.get(rule["asset"])
        if gate is None:
            blockers.append(f"release credit {credit_id} names unknown gate {rule['gate']}")
        elif gate["state"] != "satisfied":
            blockers.append(f"release credit {credit_id} depends on pending gate {rule['gate']}")
        if asset is None:
            blockers.append(f"release credit {credit_id} names unknown asset {rule['asset']}")
        elif asset["public_credit"]["state"] != "approved":
            blockers.append(f"release credit {credit_id} depends on unapproved asset credit {rule['asset']}")
        elif row.get("name") != asset["public_credit"]["label"]:
            blockers.append(f"release credit {credit_id} does not match its approved attribution")
    missing_credits = sorted(set(credit_rules) - credit_ids)
    if missing_credits:
        blockers.append(f"release manifest is missing rights-ruled credits: {', '.join(missing_credits)}")

    release_gates: dict[str, dict[str, Any]] = {}
    gate_rows = manifest.get("gates")
    if not isinstance(gate_rows, list):
        blockers.append("release manifest has no gate inventory")
        gate_rows = []
    for row in gate_rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not SAFE_ID.fullmatch(row["id"])
        ):
            blockers.append("release manifest contains malformed gate")
            continue
        gate_id = row["id"]
        if gate_id in release_gates:
            blockers.append(f"release manifest repeats gate id {gate_id}")
            continue
        release_gates[gate_id] = row
    rights_gate = release_gates.get("rights-register")
    if not rights_gate or rights_gate.get("state") != "satisfied":
        blockers.append("release manifest rights-register gate is not satisfied")
    elif (
        not same_strings(rights_gate.get("required_for"), ["public", "release"])
    ):
        blockers.append("release manifest rights-register gate must govern public and release phases")
    else:
        evidence = rights_gate.get("evidence")
        expected_path = register_path.resolve()
        expected_digest = sha256(register_path)
        if not isinstance(evidence, dict):
            blockers.append("release manifest rights-register gate has no evidence")
        else:
            try:
                evidence_path = regular_file(
                    root,
                    evidence.get("path"),
                    "release rights-register evidence",
                    expose_value=False,
                )
                if evidence_path != expected_path or evidence.get("sha256") != expected_digest:
                    blockers.append("release manifest does not bind this exact rights register")
            except RightsError as exc:
                blockers.append(str(exc))

    final_destinations, final_boundary_blockers = _release_boundary_inventory(root)
    if (
        not boundary_blockers
        and (final_boundary_blockers or final_destinations != staged_destinations)
    ):
        blockers.append("release media boundary changed during validation")
    for source, label in verified_media:
        if _verify_release_source(
            root,
            source,
            label,
            tracked=tracked,
            require_tracked=False,
            require_artifact=True,
        ):
            blockers.append(f"{label} changed during release validation")
    try:
        final_manifest_digest, _, _ = _stable_file_measure(release_manifest, "release manifest")
        if final_manifest_digest != manifest_digest:
            blockers.append("release manifest changed during validation")
    except RightsError:
        blockers.append("release manifest changed during validation")

    identity = {
        "schema": release_schema if release_schema == "danse.release.v1" else None,
        "sha256": manifest_digest,
        "release_id": release_id,
    }
    return blockers, identity


def phase_blockers(
    document: dict[str, Any],
    phase: str,
    *,
    package: Path | None = None,
    release_manifest: Path | None = None,
    root: Path = ROOT,
    register_path: Path = REGISTER,
    as_of: date | None = None,
) -> tuple[list[str], dict[str, Any]]:
    if phase not in PHASES:
        raise RightsError(f"unknown rights phase {phase!r}")
    inputs: dict[str, Any] = {}
    if phase == "draft":
        return [], inputs

    timezone_name, zone = project_zone()
    validation_date = as_of or datetime.now(zone).date()
    inputs["validation_date"] = validation_date.isoformat()
    inputs["validation_timezone"] = timezone_name
    scopes = set(PHASE_SCOPES[phase])
    blockers: list[str] = []
    if document["status"] == "draft":
        blockers.append(f"rights register status is draft; {phase} requires reviewed evidence")
    if phase == "release" and document["status"] != "cleared":
        blockers.append(f"rights register status is {document['status']}; release requires cleared")

    attestation, attestation_blockers, attestation_file = _load_attestation_with_identity(
        package
    )
    if scopes & {"package", "uploaded", "submitted"}:
        blockers.extend(attestation_blockers)
        if package is not None:
            inputs["attestation"] = attestation_identity(document, attestation)
        if not attestation_blockers:
            blockers.extend(validate_attestation(document, attestation, root=root))
    allow_attestation = phase in {"package", "uploaded", "submitted"}
    gates = {gate["id"]: gate for gate in document["human_gates"]}
    for gate in document["human_gates"]:
        if scopes.intersection(gate["required_for"]) and not gate_satisfied(
            gate, attestation, allow_attestation=allow_attestation
        ):
            blockers.append(f"human gate {gate['id']} is {gate['state']}: {gate['note']}")

    for asset in document["assets"]:
        for use in asset["uses"]:
            if not scopes.intersection(use["required_for"]):
                continue
            if use_is_conditionally_excluded(
                document,
                use,
                gates,
                attestation,
                allow_attestation=allow_attestation,
                root=root,
            ):
                continue
            if use["status"] == "cleared" and use["term"] == "fixed":
                expires = use.get("expires")
                if not isinstance(expires, str):
                    blockers.append(
                        f"asset use {asset['id']}/{use['id']} fixed permission has no expiry"
                    )
                    continue
                try:
                    expired = date.fromisoformat(expires) < validation_date
                except ValueError:
                    blockers.append(
                        f"asset use {asset['id']}/{use['id']} fixed permission has invalid expiry"
                    )
                    continue
                if expired:
                    blockers.append(
                        f"asset use {asset['id']}/{use['id']} fixed permission expired before "
                        f"the {phase} validation date"
                    )
                    continue
            if use["status"] == "cleared":
                continue
            if use["status"] == "excluded" and asset["disposition"] == "excluded":
                continue
            blockers.append(f"asset use {asset['id']}/{use['id']} is {use['status']}: {use['note']}")

    if "package" in scopes:
        if package is None:
            blockers.append(f"{phase} requires --package with an exact delivery manifest")
        else:
            package_blockers, identity = validate_package(
                document,
                package,
                root=root,
                as_of=validation_date,
            )
            blockers.extend(package_blockers)
            if identity is not None:
                inputs["package_manifest"] = identity

    if phase in {"public", "release"}:
        if release_manifest is None:
            blockers.append(f"{phase} requires --release-manifest with an exact release inventory")
        else:
            release_blockers, identity = validate_release_manifest(
                document,
                release_manifest,
                phase,
                root=root,
                register_path=register_path,
                as_of=validation_date,
            )
            blockers.extend(release_blockers)
            if identity is not None:
                inputs["release_manifest"] = identity
    if package is not None and scopes & {"package", "uploaded", "submitted"}:
        final_attestation, final_attestation_blockers, final_attestation_file = (
            _load_attestation_with_identity(package)
        )
        if (
            final_attestation_blockers != attestation_blockers
            or final_attestation_file != attestation_file
            or final_attestation != attestation
        ):
            blockers.append("package attestation changed during phase validation")
    return sorted(set(blockers)), inputs


def build_receipt(
    document: dict[str, Any],
    register_path: Path,
    schema_path: Path,
    phase: str,
    blockers: list[str],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": "danse.rights.receipt.v1",
        "phase": phase,
        "status": "ready" if not blockers else "blocked",
        "register": {
            "schema": document["schema"],
            "sha256": sha256(register_path),
            "schema_sha256": sha256(schema_path),
            "assessment_date": document["assessment"]["date"],
            "status": document["status"],
        },
        "inventory": {
            "assets": len(document["assets"]),
            "human_gates": len(document["human_gates"]),
            "package_rules": len(document["package_rules"]),
            "release_rules": len(document["release_rules"]),
        },
        "inputs": inputs,
        "blockers": blockers,
    }


def _resolved_output(path: Path, label: str) -> Path:
    try:
        return path.absolute().resolve(strict=False)
    except OSError as exc:
        raise RightsError(f"{label} cannot be resolved safely") from exc


def _inside(candidate: Path, boundary: Path) -> bool:
    try:
        candidate.relative_to(boundary)
    except ValueError:
        return False
    return True


def validate_receipt_destination(
    document: dict[str, Any],
    receipt_path: Path,
    *,
    phase: str,
    register_path: Path,
    schema_path: Path,
    package: Path | None,
    release_manifest: Path | None,
    root: Path = ROOT,
) -> None:
    """Keep receipt output disjoint from every validated source and artifact."""
    destination = _resolved_output(receipt_path, "receipt destination")
    exact_inputs = {
        _resolved_output(register_path, "rights register"),
        _resolved_output(schema_path, "rights schema"),
    }
    if release_manifest is not None:
        exact_inputs.add(_resolved_output(release_manifest, "release manifest"))
    for _, source in _source_records(document):
        try:
            relative = safe_relative(source.get("path"), "validated source", expose_value=False)
        except RightsError:
            continue
        exact_inputs.add(_resolved_output(root / relative, "validated source"))
    if destination in exact_inputs:
        raise RightsError("receipt destination overlaps a validated input")

    boundaries: list[Path] = []
    if package is not None:
        boundaries.append(_resolved_output(package, "package"))
    if phase in {"public", "release"}:
        boundaries.append(_resolved_output(root / "media" / "assets", "release media boundary"))
    if any(_inside(destination, boundary) for boundary in boundaries):
        raise RightsError("receipt destination overlaps a validated artifact boundary")


def validate_all(
    *,
    register_path: Path = REGISTER,
    schema_path: Path = SCHEMA,
    phase: str = "draft",
    package: Path | None = None,
    release_manifest: Path | None = None,
    receipt_path: Path | None = None,
    root: Path = ROOT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    document = load_register(register_path, schema_path, root=root)
    if receipt_path is not None:
        validate_receipt_destination(
            document,
            receipt_path,
            phase=phase,
            register_path=register_path,
            schema_path=schema_path,
            package=package,
            release_manifest=release_manifest,
            root=root,
        )
    blockers, inputs = phase_blockers(
        document,
        phase,
        package=package,
        release_manifest=release_manifest,
        root=root,
        register_path=register_path,
    )
    return document, build_receipt(document, register_path, schema_path, phase, blockers, inputs)


def canonical_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", type=Path, default=REGISTER)
    parser.add_argument("--schema", type=Path, default=SCHEMA)
    parser.add_argument("--phase", choices=PHASES, default="draft")
    parser.add_argument("--package", type=Path)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--receipt", type=Path, help="write a deterministic, redacted validation receipt")
    parser.add_argument("--json", action="store_true", help="print the complete receipt as JSON")
    args = parser.parse_args(argv)

    try:
        _, receipt = validate_all(
            register_path=args.register,
            schema_path=args.schema,
            phase=args.phase,
            package=args.package,
            release_manifest=args.release_manifest,
            receipt_path=args.receipt,
        )
    except RightsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(canonical_json(receipt), encoding="utf-8")
    if args.json:
        print(canonical_json(receipt), end="")
    else:
        print("Danse rights and attribution")
        print(f"  register  {receipt['register']['sha256']}")
        print(
            f"  inventory {receipt['inventory']['assets']} assets · "
            f"{receipt['inventory']['human_gates']} human gates · "
            f"{receipt['inventory']['package_rules']} package rules · "
            f"{receipt['inventory']['release_rules']} release rules"
        )
        print(f"  phase     {args.phase} — {receipt['status'].upper()}")
        for blocker in receipt["blockers"]:
            print(f"  [BLOCK] {blocker}")
        if not receipt["blockers"]:
            print("  [ok] exact public-safe sources, redaction, inventory, and rule graph validate")
    return 0 if receipt["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
