#!/usr/bin/env python3
"""Strict, phase-aware contract for the Danse public/release artifacts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
from copy import deepcopy
from collections.abc import Iterable, Iterator
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import jsonschema
import rights_contract as _TRUSTED_RIGHTS_CHECKER

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path("release/manifest.json")
SCHEMA = Path("release/manifest.schema.json")
RELEASE_SCHEMA = "danse.release.v1"
RELEASE_GATE_RECEIPT_SCHEMA_PATH = Path("release/gate-receipt.schema.json")
RELEASE_GATE_PROOF_SCHEMA_PATH = Path("release/gate-proof.schema.json")
RELEASE_OWNER_ATTESTATION_SCHEMA_PATH = Path("release/owner-attestation.schema.json")
RELEASE_PROOF_PINS_SCHEMA_PATH = Path("release/proof-pins.schema.json")
RELEASE_PROOF_PINS_PATH = Path("release/evidence/proof-pins.json")
RIGHTS_REGISTER_SCHEMA_PATH = Path("rights/register.schema.json")
EXPECTED_OPPORTUNITY_ID = "omega-20260829"
EXPECTED_OPPORTUNITY_FROZEN_AT = "2026-08-29T22:12:19Z"
EXPECTED_OPPORTUNITY_SHA256 = "c9941a027bd91236f6e48157f332d6ca11f08d9946af2bfc7f029e44bbc67294"
EXPECTED_OPPORTUNITY_RECEIPT_SHA256 = "d53752f1a9232a5af06637b54cb110d8460780dbc8c81910a14bb11d31e8eeae"
EXPECTED_SOURCE_EVIDENCE_SHA256 = "7e9ba1c74f8ac78df116ada8c94d8af4e7d04813f2a3c026693258cd6c974bc8"
LIVE_INTERACTION_EVIDENCE_PATH = "release/evidence/live-interaction-replay-20260804.json"
LIVE_INTERACTION_COMMENT_BODY_SHA256 = "4cc41f9ed353c92c27b172907800b123c7b4e85ef4ba7165ed210133f40952bf"
LIVE_INTERACTION_DEPLOYED_COMMIT = "f19244afbce94015e78b7f746b07d267ed9e67ae"
PROGRESSIVE_CONTROLS_EVIDENCE_PATH = "release/evidence/progressive-controls-replay.json"
PROGRESSIVE_CONTROLS_SCHEMA_PATH = "release/progressive-controls-replay.schema.json"
PROGRESSIVE_CONTROLS_EVIDENCE_SUMMARY = (
    "Exact-head progressive-controls replay only; does not establish final-cut, "
    "rights, package, upload, or filing readiness."
)
PROGRESSIVE_CONTROLS_CHECKS = (
    "exact-head",
    "desktop-layout",
    "mobile-320-layout",
    "mobile-390-layout",
    "zoom-200-layout",
    "touch-targets",
    "keyboard-focus",
    "reduced-motion",
    "receipt-state",
    "shipped-score-audio",
    "map-gating",
    "console-clean",
    "http-clean",
)
RELEASE_GATE_RECEIPT_SCHEMA = "danse.release.gate.v3"
RELEASE_GATE_PROOF_SCHEMA = "danse.release.gate-proof.v1"
RELEASE_OWNER_ATTESTATION_SCHEMA = "danse.release.owner-attestation.v2"
RELEASE_PROOF_PINS_SCHEMA = "danse.release.proof-pins.v1"
PROGRESSIVE_CONTROLS_SCHEMA = "danse.progressive-controls-replay.v2"
RELEASE_REPOSITORY = "organvm/the-thing-without-a-name"
RELEASE_OWNER_NAME = "Anthony J. Padavano"
RELEASE_OWNER_LOGIN = "4444J99"
RELEASE_HIGH_RISK_CLAIMS = (
    "accessibility-approved",
    "archive-choice-made",
    "contact-approved",
    "custody-complete",
    "deployment-complete",
    "final-cut-approved",
    "identity-approved",
    "installation-complete",
    "package-ready",
    "presentation-complete",
    "publication-approved",
    "restore-complete",
    "rights-cleared",
    "stills-cleared",
    "submission-complete",
    "terms-accepted",
    "upload-complete",
)
RELEASE_PROOF_CHECKS = {
    "accessibility-review": (
        "alt-text",
        "captions",
        "reduced-motion",
        "silent-fallback",
        "transcript",
    ),
    "custody-completion": (
        "checksum-parity",
        "clean-restore",
        "independent-copies",
        "material-census",
        "retention",
    ),
    "installation-completion": (
        "calibration",
        "clean-restore",
        "hardware",
        "runtime",
        "venue",
        "wall-plug-recovery",
    ),
    "presentation-lifecycle": (
        "actual-lifecycle",
        "host-receipt",
        "program-evidence",
        "public-url",
    ),
    "restore-completion": (
        "apple-metal",
        "installation",
        "package",
        "portable",
        "source-restore",
    ),
    "rights-validation": (
        "asset-census",
        "credits",
        "included-use-clearance",
        "press-stills",
        "private-evidence",
        "zero-blockers",
    ),
    "submission-package": (
        "delivery-chain",
        "machine-batch",
        "package-manifest",
        "package-receipt-schema",
    ),
    "submission-validation": (
        "apple-metal",
        "delivery",
        "package-phase",
        "portable",
    ),
}
RELEASE_PROOF_ISSUER_KINDS = {
    "accessibility-review": "tool",
    "custody-completion": "tool",
    "installation-completion": "venue",
    "presentation-lifecycle": "host",
    "progressive-controls-replay": "tool",
    "restore-completion": "tool",
    "rights-validation": "tool",
    "submission-package": "tool",
    "submission-validation": "tool",
}
RELEASE_PROOF_GENERATORS = {
    "accessibility-review": "scripts/release_contract.py",
    "custody-completion": "scripts/private_custody.py",
    "installation-completion": "installation/contract.py",
    "presentation-lifecycle": "scripts/release_contract.py",
    "restore-completion": "scripts/private_custody.py",
    "rights-validation": "scripts/rights_contract.py",
    "submission-package": "render/deliver.py",
    "submission-validation": "render/deliver.py",
}
RIGHTS_CLEARANCE_GATES = {
    "rights-register": {
        "rights-declaration-approved",
        "dancer-release-and-credit",
        "pictured-objects-reviewed",
        "music-cleared",
        "mediapipe-attribution-retained",
    },
    "press-stills-clearance": {"press-stills-cleared"},
}
RIGHTS_STILL_MEDIA = {"still", "press", "festival-promotion"}
RIGHTS_FILING_ONLY_MEDIA = {"festival-archive"}
RIGHTS_CLEARANCE_REQUIRED_PHASES = {"public", "package", "release"}
RELEASE_GATE_CONTRACTS: dict[str, dict[str, Any]] = {
    "final-artistic-approval": {
        "issue": 10,
        "owner": "Anthony",
        "proofs": ("owner-attestation",),
        "affirms": ("final-cut-approved",),
        "package": True,
    },
    "final-cut-evidence-gate": {
        "issue": 10,
        "owner": "Issue 10 and Anthony",
        "proofs": ("submission-package", "submission-validation"),
        "affirms": ("package-ready",),
        "package": True,
    },
    "installation-evidence": {
        "issue": 14,
        "owner": "Issue 14, Anthony, and venue",
        "proofs": ("installation-completion",),
        "affirms": ("installation-complete",),
        "package": True,
    },
    "rights-register": {
        "issue": 16,
        "owner": "Issue 16, Anthony, and contributors",
        "proofs": ("rights-validation", "owner-attestation"),
        "affirms": ("rights-cleared",),
        "package": True,
    },
    "press-stills-clearance": {
        "issue": 16,
        "owner": "Issue 16 and Anthony",
        "proofs": ("owner-attestation", "rights-validation"),
        "affirms": ("stills-cleared",),
        "package": True,
    },
    "accessibility-review": {
        "issue": 17,
        "owner": "Issue 17 and Anthony",
        "proofs": ("accessibility-review", "owner-attestation"),
        "affirms": ("accessibility-approved",),
        "package": True,
    },
    "contact-route-approval": {
        "issue": 17,
        "owner": "Anthony",
        "proofs": ("owner-attestation",),
        "affirms": ("contact-approved",),
        "package": False,
    },
    "public-identity-copy-approval": {
        "issue": 17,
        "owner": "Anthony",
        "proofs": ("owner-attestation",),
        "affirms": ("identity-approved",),
        "package": True,
    },
    "live-interaction-replay": {
        "issue": 17,
        "owner": "Issue 17 and Anthony",
        "proofs": (),
        "affirms": (),
        "package": False,
    },
    "progressive-controls-replay": {
        "issue": 17,
        "owner": "Issue 17 and Anthony",
        "proofs": ("progressive-controls-replay",),
        "affirms": (),
        "package": False,
    },
    "publication-approval": {
        "issue": 17,
        "owner": "Anthony",
        "proofs": ("owner-attestation",),
        "affirms": ("publication-approved",),
        "package": True,
    },
    "release-custody": {
        "issue": 12,
        "owner": "Issue 12 and Anthony",
        "proofs": ("custody-completion", "owner-attestation"),
        "affirms": ("custody-complete",),
        "package": True,
    },
    "restore-rehearsal": {
        "issue": 12,
        "owner": "Issue 12 and Anthony",
        "proofs": ("restore-completion", "owner-attestation"),
        "affirms": ("restore-complete",),
        "package": True,
    },
    "actual-presentation": {
        "issue": 12,
        "owner": "Issue 12, Anthony, and host",
        "proofs": ("presentation-lifecycle",),
        "affirms": ("presentation-complete",),
        "package": True,
    },
}
# The schema-closed evidence pin ledger is a review inventory, not a signature or
# trust root. Keeping mutable proof hashes out of validator code avoids an
# impossible receipt-hash/source-commit cycle. Repository governance must still
# supply independent review of each tracked pin change. The ledger is empty in
# draft state.
PHASES = ("draft", "public", "release")
GENERATED_PRODUCT_PATHS = {
    "project-page-copy": "project/index.html",
    "pitch-pdf-copy": "pitch/danse-installation-pitch.pdf",
    "accessibility-copy": "accessibility/accessibility.md",
    "caption-track-copy": "accessibility/captions.en.vtt",
    "transcript-copy": "accessibility/transcript.txt",
    "press-kit-copy": "press/press-kit.md",
    "credits-copy": "press/credits.txt",
}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(
    r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"
    r"|(?<![\w])\+(?:\d[ .()/-]*){7,14}\d(?!\d)"
)
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
APPLE_ANGLE_METAL_RENDERER = re.compile(
    r"\AANGLE \(Apple, ANGLE Metal Renderer: "
    r"Apple M[1-9][0-9]*(?: (?:Pro|Max|Ultra))?, Unspecified Version\)\Z"
)
PRIVATE_PREFIXES = (
    ".git/",
    ".work/",
    ".worktrees/",
    ".limen-workstream/",
)
PRIVATE_PATH_MARKER = re.compile(
    r"(?:^|[\s'\"`(\[])"
    r"(?:/(?!/)[^\s'\"`)]*|//[^/\s]+/[^\s'\"`)]*|"
    r"[A-Za-z]:[\\/][^\s'\"`)]*|\\\\[^\\/\s]+[\\/][^\s'\"`)]*|"
    r"~[\\/][^\s'\"`)]*|file://[^\s'\"`)]*)"
)
PUBLIC_MARKERS = re.compile(
    r"(?:\bTODO\b|\bTBD\b|\bplaceholder\b|\blorem ipsum\b|\bdraft\b|"
    r"\bpending\b|\bprovisional\b|not for publication|awaits? (?:issue|the)|"
    r"require(?:s)? .* approval)",
    re.IGNORECASE,
)


class ReleaseError(ValueError):
    """The release manifest or artifact violates its declared contract."""


def _git_environment() -> dict[str, str]:
    """Run provenance Git without ambient repository/object/config redirects."""
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    }
    # Git for Windows needs SYSTEMROOT; temporary-path variables are harmless
    # process prerequisites. Deliberately do not inherit any other ambient key,
    # especially GIT_DIR, GIT_WORK_TREE, index/object redirects, namespaces, or
    # GIT_CONFIG_COUNT/KEY/VALUE injection.
    for key in ("SYSTEMROOT", "TMPDIR", "TMP", "TEMP"):
        if value := os.environ.get(key):
            environment[key] = value
    return environment


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ReleaseError(f"JSON contains duplicate key {key!r}")
        value[key] = item
    return value


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} must be a JSON object")
    return value


def safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseError(f"{label} must be a non-empty repository-relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or "\\" in value or any(part in {"", ".", ".."} for part in pure.parts):
        raise ReleaseError(f"{label} is not a safe portable relative path: {value!r}")
    relative = pure.as_posix()
    if relative in {prefix.rstrip("/") for prefix in PRIVATE_PREFIXES} or relative.startswith(PRIVATE_PREFIXES):
        raise ReleaseError(f"{label} points into private or generated custody: {relative!r}")
    return relative


def source_file(root: Path, relative: object, label: str) -> Path:
    relative = safe_relative(relative, label)
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ReleaseError(f"repository root must be a regular directory: {root}")
    root = root.resolve()
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ReleaseError(f"{label} traverses a symlink: {relative!r}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ReleaseError(f"{label} is missing or outside the repository: {relative!r}") from exc
    if not resolved.is_file():
        raise ReleaseError(f"{label} is not a regular file: {relative!r}")
    return resolved


def verify_record(root: Path, record: dict[str, Any], label: str) -> Path:
    path = source_file(root, record.get("path"), f"{label} path")
    expected = record.get("sha256")
    if not isinstance(expected, str) or not HEX64.fullmatch(expected):
        raise ReleaseError(f"{label} has no valid SHA-256")
    actual = sha256(path)
    if actual != expected:
        raise ReleaseError(f"{label} digest mismatch for {record.get('path')}: expected {expected}, got {actual}")
    if "bytes" in record:
        expected_bytes = record["bytes"]
        if type(expected_bytes) is not int or expected_bytes < 0 or path.stat().st_size != expected_bytes:
            raise ReleaseError(f"{label} byte count mismatch for {record.get('path')}")
    return path


def strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys(item)


CONTROL_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SENSITIVE_TEXT = re.compile(
    r"\b(?:api[ _-]?key|authorization|credential|password|private[ _-]?key|"
    r"secret|token)\b\s*(?::|=|\bis\b)\s*\S"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
    r"|\bAKIA[0-9A-Z]{16}\b",
    re.IGNORECASE,
)


def _decoded_variants(value: str) -> Iterator[str]:
    """Expose nested percent-encoding before applying public-safety rules."""

    current = value
    for _ in range(10):
        yield current
        decoded = unquote(current)
        if decoded == current:
            return
        current = decoded
    raise ReleaseError("public-safe text exceeds the percent-decoding depth limit")


def validate_public_safe_document(value: Any, label: str) -> None:
    """Reject private, contact, credential, encoded, or control data everywhere."""

    sensitive_key = next(
        (
            key
            for key in keys(value)
            if any(
                decoded.lower() in SENSITIVE_KEYS
                for decoded in _decoded_variants(key)
            )
        ),
        None,
    )
    if sensitive_key:
        raise ReleaseError(f"{label} exposes sensitive field {sensitive_key!r}")
    for raw in strings(value):
        for candidate in _decoded_variants(raw):
            if (
                CONTROL_CHAR.search(candidate)
                or PRIVATE_PATH_MARKER.search(candidate)
                or EMAIL.search(candidate)
                or PHONE.search(candidate)
                or SENSITIVE_TEXT.search(candidate)
            ):
                raise ReleaseError(
                    f"{label} exposes private contact or path data or credentials"
                )


def _git_output(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            env=_git_environment(),
        )
    except OSError as exc:
        raise ReleaseError(
            "release receipt requires an available Git repository"
        ) from exc
    if result.returncode != 0:
        raise ReleaseError(
            "release receipt names an unavailable Git object or relationship"
        )
    return result.stdout.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            env=_git_environment(),
        )
    except OSError as exc:
        raise ReleaseError(
            "release receipt requires an available Git repository"
        ) from exc
    if result.returncode != 0:
        raise ReleaseError(
            "release receipt names an unavailable Git object or relationship"
        )
    return result.stdout


def _reject_git_replace_refs(root: Path) -> None:
    replacements = _git_output(
        root, "for-each-ref", "--format=%(refname)", "refs/replace"
    )
    if replacements:
        raise ReleaseError("release validation rejects Git replacement refs")


def _verify_git_object_integrity(root: Path, *oids: str) -> None:
    """Recompute reachable Git object identities under the repository hash.

    Normal object lookup can consume a loose object from the pathname of a
    different hash.  `git fsck --full` independently walks the declared roots
    and verifies commit, tree and blob identities before a release may rely on
    their contents.
    """

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "fsck",
                "--full",
                "--strict",
                "--no-reflogs",
                "--no-dangling",
                "--no-progress",
                *oids,
            ],
            capture_output=True,
            check=False,
            env=_git_environment(),
        )
    except OSError as exc:
        raise ReleaseError("cannot verify raw Git object integrity") from exc
    if result.returncode != 0:
        raise ReleaseError(
            "release receipt source or evidence fails raw Git object integrity"
        )


def _json_bytes(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_no_duplicate_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} must be a JSON object")
    return value


def _parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
        raise ReleaseError(f"{label} has no canonical UTC time")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ReleaseError(f"{label} has an invalid UTC time") from exc
    return parsed


def git_commit_identity(root: Path, oid: object) -> tuple[str, datetime]:
    """Resolve one real ancestor commit to its exact tree and committer time."""

    if not isinstance(oid, str) or not HEX40.fullmatch(oid):
        raise ReleaseError("release receipt has no exact 40-character repository head")
    _git_output(root, "cat-file", "-e", f"{oid}^{{commit}}")
    tree = _git_output(root, "rev-parse", f"{oid}^{{tree}}")
    checkout_head = _git_output(root, "rev-parse", "HEAD")
    _git_output(root, "merge-base", "--is-ancestor", oid, checkout_head)
    committed = _git_output(root, "show", "-s", "--format=%cI", oid)
    try:
        committed_at = datetime.fromisoformat(committed).astimezone(timezone.utc)
    except ValueError as exc:
        raise ReleaseError(
            "release receipt source commit has no valid committer time"
        ) from exc
    return tree, committed_at


def validate_evidence_only_descendant(
    root: Path, oid: str, allowed_paths: set[str]
) -> None:
    """Require checkout drift from a frozen source to be evidence-only.

    A tracked receipt cannot name the tree that contains its own digest without
    becoming self-referential.  The admissible relation is therefore one real
    ancestor source commit plus a descendant that changes only the release
    manifest and the closed receipt/proof envelope.  Source, runtime, schema,
    media, and product drift all fail closed.
    """

    try:
        source_diff = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                oid,
                "HEAD",
                "--",
            ],
            capture_output=True,
            check=False,
            env=_git_environment(),
        )
    except OSError as exc:
        raise ReleaseError("cannot compare the frozen release source tree") from exc
    if source_diff.returncode != 0:
        raise ReleaseError("cannot compare the frozen release source tree")
    try:
        changed = {
            item.decode("utf-8") for item in source_diff.stdout.split(b"\0") if item
        }
    except UnicodeDecodeError as exc:
        raise ReleaseError("frozen release source diff contains a non-UTF-8 path") from exc
    invalid = sorted(changed - allowed_paths)
    if invalid:
        raise ReleaseError(
            "release receipt source drift is not limited to its exact tracked evidence envelope: "
            + ", ".join(invalid)
        )


def _manifest_without_gate_completion(manifest: dict[str, Any]) -> dict[str, Any]:
    projected = deepcopy(manifest)
    for gate in projected.get("gates", []):
        if isinstance(gate, dict):
            gate.pop("state", None)
            gate.pop("evidence", None)
    return projected


def _validate_frozen_manifest(
    root: Path,
    source_head: str,
    expected_sha256: object,
    current: dict[str, Any],
) -> None:
    payload = _git_bytes(root, "show", f"{source_head}:{MANIFEST.as_posix()}")
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if expected_sha256 != actual_sha256:
        raise ReleaseError(
            "release gate receipt source manifest digest disagrees with its exact head"
        )
    source = _json_bytes(payload, "source release manifest")
    if _manifest_without_gate_completion(source) != _manifest_without_gate_completion(
        current
    ):
        raise ReleaseError(
            "release manifest changed outside gate state and evidence completion"
        )
    source_gates = source.get("gates")
    current_gates = current.get("gates")
    if not isinstance(source_gates, list) or not isinstance(current_gates, list):
        raise ReleaseError("release manifest gate inventory is malformed")
    if len(source_gates) != len(current_gates):
        raise ReleaseError("release manifest gate inventory changed after source freeze")
    for before, after in zip(source_gates, current_gates, strict=True):
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ReleaseError("release manifest gate inventory is malformed")
        if before.get("id") != after.get("id"):
            raise ReleaseError("release manifest gate order changed after source freeze")
        if before.get("state") == "satisfied" and (
            after.get("state") != "satisfied"
            or after.get("evidence") != before.get("evidence")
        ):
            raise ReleaseError(
                f"completed gate {before.get('id')} changed after source freeze"
            )
        if before.get("state") == "pending" and after.get("state") not in {
            "pending",
            "satisfied",
        }:
            raise ReleaseError(
                f"gate {before.get('id')} has an invalid completion transition"
            )


def validate_clean_checkout(root: Path) -> None:
    status = _git_bytes(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if status:
        raise ReleaseError(
            "pinned release validation requires a clean committed checkout"
        )
    ignored = _git_bytes(
        root,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        "--",
        "release",
        "rights",
    )
    if ignored:
        raise ReleaseError(
            "pinned release validation rejects ignored contract or evidence files"
        )


def _verify_commit_record(
    root: Path,
    commit: str,
    relative: str,
    expected_sha256: str,
    label: str,
) -> None:
    payload = _git_bytes(root, "show", f"{commit}:{relative}")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise ReleaseError(f"{label} is not the exact tracked blob at its bound commit")


def _validate_receipt_time(
    value: object,
    label: str,
    committed_at: datetime,
) -> datetime:
    observed_at = _parse_utc(value, label)
    if observed_at < committed_at:
        raise ReleaseError(f"{label} predates its source commit")
    if observed_at > datetime.now(timezone.utc):
        raise ReleaseError(f"{label} is in the future")
    return observed_at


def _current_rights_validation_date() -> date:
    return datetime.now(timezone.utc).astimezone(
        ZoneInfo("America/New_York")
    ).date()


def _tracked_paths(root: Path) -> set[str]:
    payload = _git_output(root, "ls-files", "-z")
    return {item for item in payload.split("\0") if item}


def _validate_subject(
    root: Path,
    subject: object,
    manifest: dict[str, Any],
    contract: dict[str, Any],
    verified_git_objects: set[tuple[str, str]],
) -> tuple[dict[str, Any], datetime]:
    if not isinstance(subject, dict) or set(subject) != {
        "release_id",
        "release_version",
        "repository_head",
        "repository_tree",
        "release_manifest_sha256",
        "package_manifest_sha256",
    }:
        raise ReleaseError("release gate receipt has no exact release subject")
    package_digest = subject.get("package_manifest_sha256")
    if contract["package"]:
        if not isinstance(package_digest, str) or not HEX64.fullmatch(package_digest):
            raise ReleaseError("release gate receipt has no exact package binding")
    elif package_digest is not None:
        raise ReleaseError(
            "release gate receipt invents an out-of-scope package binding"
        )
    if (
        subject.get("release_id") != manifest["release_id"]
        or subject.get("release_version") != manifest["version"]
    ):
        raise ReleaseError("release gate receipt names a different release subject")
    source_head = subject.get("repository_head")
    if not isinstance(source_head, str) or not HEX40.fullmatch(source_head):
        raise ReleaseError("release receipt has no exact 40-character repository head")
    checkout_head = _git_output(root, "rev-parse", "HEAD")
    integrity_key = (source_head, checkout_head)
    if integrity_key not in verified_git_objects:
        _verify_git_object_integrity(root, source_head, checkout_head)
        verified_git_objects.add(integrity_key)
    tree, committed_at = git_commit_identity(root, source_head)
    if subject.get("repository_tree") != tree:
        raise ReleaseError(
            "release gate receipt repository tree disagrees with its head"
        )
    _validate_frozen_manifest(
        root,
        subject["repository_head"],
        subject.get("release_manifest_sha256"),
        manifest,
    )
    return subject, committed_at


def _source_identity(subject: dict[str, Any]) -> dict[str, Any]:
    return {
        key: subject[key]
        for key in (
            "release_id",
            "release_version",
            "repository_head",
            "repository_tree",
            "release_manifest_sha256",
        )
    }


def _expected_package(subject: dict[str, Any]) -> dict[str, Any] | None:
    digest = subject["package_manifest_sha256"]
    if digest is None:
        return None
    return {
        "manifest": "manifest.json",
        "manifest_sha256": digest,
        "repository_head": subject["repository_head"],
        "repository_tree": subject["repository_tree"],
        "release_manifest_sha256": subject["release_manifest_sha256"],
    }


def _owner_scope_sha256(
    root: Path,
    gate: dict[str, Any],
    subject: dict[str, Any],
    manifest: dict[str, Any],
) -> str:
    scope: dict[str, Any] = {
        "gate_id": gate["id"],
        "issue": gate["issue"],
        "claim": RELEASE_GATE_CONTRACTS[gate["id"]]["affirms"][0],
        "subject": subject,
    }
    if gate["id"] in {"rights-register", "press-stills-clearance"}:
        rights_path = source_file(root, "rights/register.json", "rights register")
        scope["rights_register_sha256"] = sha256(rights_path)
    if gate["id"] == "press-stills-clearance":
        scope["still_ids"] = sorted(
            medium["id"]
            for medium in manifest["media"]
            if medium["kind"] in {"still", "social-card"}
        )
    return hashlib.sha256(canonical_json(scope)).hexdigest()


def _owner_approval_body_sha256(
    root: Path,
    gate: dict[str, Any],
    subject: dict[str, Any],
    manifest: dict[str, Any],
) -> str:
    contract = RELEASE_GATE_CONTRACTS[gate["id"]]
    payload = {
        "schema": "danse.release.owner-approval.v1",
        "gate_id": gate["id"],
        "issue": gate["issue"],
        "authority_login": RELEASE_OWNER_LOGIN,
        "decision": {
            "claim": contract["affirms"][0],
            "scope_sha256": _owner_scope_sha256(root, gate, subject, manifest),
        },
        "subject": subject,
        "package": _expected_package(subject),
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _rights_check_digest(kind: str, payload: object) -> str:
    return hashlib.sha256(
        canonical_json({"check": kind, "value": payload})
    ).hexdigest()


def _external_authority_body_sha256(
    gate: dict[str, Any],
    kind: str,
    authority_login: str,
    subject: dict[str, Any],
    checks: list[dict[str, Any]],
) -> str:
    payload = {
        "schema": "danse.release.external-authority-approval.v1",
        "gate_id": gate["id"],
        "issue": gate["issue"],
        "proof_kind": kind,
        "authority_login": authority_login,
        "subject": subject,
        "checks": checks,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _rights_register_inventory(
    root: Path,
    document: dict[str, Any],
    *,
    gate_id: str,
    source_head: str,
    tracked: set[str],
    validation_date: date,
) -> tuple[list[str], list[str], set[str]]:
    """Return the exact cleared use and redacted-receipt inventory.

    This is deliberately narrower than legal review. It ensures a machine proof
    cannot call a still-blocked register "zero blockers" or omit a tracked
    human/use receipt. The separate owner attestation remains mandatory and the
    repository review rail remains responsible for evaluating those decisions.
    """

    _validate_schema(
        document,
        _load_closed_schema(root, RIGHTS_REGISTER_SCHEMA_PATH, "rights register"),
        "rights register",
    )
    if document.get("status") not in {"reviewed", "cleared"}:
        raise ReleaseError("rights validation proof binds an unreviewed register")
    required_gates = RIGHTS_CLEARANCE_GATES.get(gate_id)
    if required_gates is None:
        raise ReleaseError("rights validation proof names an unknown clearance scope")
    validate_public_safe_document(document, "rights register")

    receipt_digests: set[str] = set()
    receipt_paths: set[str] = set()

    def bind_receipt(record: object, label: str) -> None:
        if not isinstance(record, dict):
            raise ReleaseError(f"{label} has no redacted receipt")
        path = verify_record(root, record, label)
        relative = path.relative_to(root).as_posix()
        digest = record.get("sha256")
        if relative not in tracked:
            raise ReleaseError(f"{label} is not tracked by the repository")
        _verify_commit_record(root, source_head, relative, digest, label)
        validate_public_safe_document(load_json(path, label), label)
        if relative in receipt_paths or digest in receipt_digests:
            raise ReleaseError("rights register reuses a redacted decision receipt")
        receipt_paths.add(relative)
        receipt_digests.add(digest)

    gates = {gate.get("id"): gate for gate in document.get("human_gates", [])}
    for required_gate in sorted(required_gates):
        gate = gates.get(required_gate)
        if gate is None:
            raise ReleaseError(
                f"rights clearance scope {gate_id} is missing gate {required_gate}"
            )
        if gate.get("state") != "satisfied":
            raise ReleaseError(
                f"rights register human gate {gate.get('id')} is not satisfied"
            )
        bind_receipt(gate.get("evidence"), f"rights human gate {gate.get('id')}")

    asset_use_ids: list[str] = []
    for asset in document.get("assets", []):
        scoped_uses = [
            use
            for use in asset.get("uses", [])
            if set(use.get("required_for", [])) & RIGHTS_CLEARANCE_REQUIRED_PHASES
            and (
                use.get("medium") in RIGHTS_STILL_MEDIA
                if gate_id == "press-stills-clearance"
                else use.get("medium") not in RIGHTS_STILL_MEDIA
                and use.get("medium") not in RIGHTS_FILING_ONLY_MEDIA
            )
        ]
        if not scoped_uses:
            continue
        asset_id = asset.get("id")
        disposition = asset.get("disposition")
        if disposition == "blocked":
            raise ReleaseError(f"rights register asset {asset_id} remains blocked")
        credit = asset.get("public_credit", {})
        if credit.get("state") == "pending":
            raise ReleaseError(f"rights register asset {asset_id} has pending credit")
        private = asset.get("private_evidence", {})
        if private.get("state") == "verified":
            bind_receipt(
                private.get("receipt"),
                f"rights asset {asset_id} private evidence",
            )
        elif private.get("state") != "not-required" or private.get("receipt") is not None:
            raise ReleaseError(
                f"rights register asset {asset_id} has unresolved private evidence"
            )
        for use in scoped_uses:
            use_id = use.get("id")
            identity = f"{asset_id}/{use_id}"
            asset_use_ids.append(identity)
            status = use.get("status")
            if disposition == "excluded":
                if status != "excluded" or use.get("evidence") is not None:
                    raise ReleaseError(
                        f"rights register excluded use {identity} is inconsistent"
                    )
                continue
            if status != "cleared":
                raise ReleaseError(f"rights register asset use {identity} is not cleared")
            if any(
                use.get(field) == "pending"
                for field in ("territory", "term", "promotion", "archive")
            ):
                raise ReleaseError(
                    f"rights register asset use {identity} has unsettled scope"
                )
            expires = use.get("expires")
            if use.get("term") == "fixed" and expires is None:
                raise ReleaseError(
                    f"rights register asset use {identity} has no fixed-term expiry"
                )
            if expires is not None:
                try:
                    expiry_date = date.fromisoformat(expires)
                except (TypeError, ValueError) as exc:
                    raise ReleaseError(
                        f"rights register asset use {identity} has an invalid expiry"
                    ) from exc
                if expiry_date < validation_date:
                    raise ReleaseError(
                        f"rights register asset use {identity} expired before validation"
                    )
            bind_receipt(use.get("evidence"), f"rights asset use {identity}")

    if len(asset_use_ids) != len(set(asset_use_ids)):
        raise ReleaseError("rights register asset/use inventory is not unique")
    return sorted(asset_use_ids), sorted(receipt_digests), receipt_paths


def _strict_owner_comment_url(value: object, issue: int, comment_id: int) -> str:
    if not isinstance(value, str) or unquote(value) != value or "\\" in value:
        raise ReleaseError("owner attestation comment URL is encoded or malformed")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ReleaseError(
            "owner attestation comment URL has a malformed authority"
        ) from exc
    expected_path = f"/organvm/the-thing-without-a-name/issues/{issue}"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment != f"issuecomment-{comment_id}"
    ):
        raise ReleaseError(
            "owner attestation does not use the immutable GitHub comment form"
        )
    return value


def _load_closed_schema(root: Path, relative: Path, label: str) -> dict[str, Any]:
    schema = load_json(
        source_file(root, relative.as_posix(), f"{label} schema"), f"{label} schema"
    )
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise ReleaseError(f"{label} schema is invalid: {exc.message}") from exc
    return schema


def _load_rights_checker(root: Path, source_head: str):
    """Return the trusted current verifier after checking its frozen identity.

    Historical repository blobs are data identities only.  In particular, a
    receipt-selected source commit must never become executable Python merely
    because its digest was recorded in an evidence document.
    """

    relative = RELEASE_PROOF_GENERATORS["rights-validation"]
    path = source_file(root, relative, "rights checker")
    source_digest = hashlib.sha256(
        _git_bytes(root, "show", f"{source_head}:{relative}")
    ).hexdigest()
    trusted_path = Path(_TRUSTED_RIGHTS_CHECKER.__file__).resolve()
    trusted_digest = sha256(trusted_path)
    if sha256(path) != source_digest:
        raise ReleaseError("rights checker differs from the frozen release source")
    if source_digest != trusted_digest:
        raise ReleaseError(
            "frozen rights checker identity does not match the trusted current verifier"
        )
    return _TRUSTED_RIGHTS_CHECKER


def _validate_schema(document: Any, schema: dict[str, Any], label: str) -> None:
    errors = sorted(
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise ReleaseError(f"{label} schema failure at {location}: {error.message}")


def load_proof_pins(
    root: Path,
) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    """Load the schema-closed review-required pin inventory."""

    path = source_file(root, RELEASE_PROOF_PINS_PATH.as_posix(), "proof pin ledger")
    document = load_json(path, "proof pin ledger")
    _validate_schema(
        document,
        _load_closed_schema(
            root, RELEASE_PROOF_PINS_SCHEMA_PATH, "proof pin ledger"
        ),
        "proof pin ledger",
    )
    if document["schema"] != RELEASE_PROOF_PINS_SCHEMA:
        raise ReleaseError("proof pin ledger has the wrong schema")
    validate_public_safe_document(document, "proof pin ledger")
    records = document["records"]
    ordered = sorted(records, key=lambda item: (item["gate_id"], item["kind"]))
    if records != ordered:
        raise ReleaseError("proof pin ledger records are not canonically ordered")
    pins: dict[tuple[str, str], dict[str, Any]] = {}
    seen_paths: set[str] = set()
    seen_digests: set[str] = set()
    seen_ids: set[str] = set()
    seen_owner_comments: set[tuple[str, int, int]] = set()
    seen_owner_urls: set[str] = set()
    for record in records:
        key = (record["gate_id"], record["kind"])
        receipt = record["receipt"]
        if key in pins:
            raise ReleaseError("proof pin ledger repeats a gate and proof kind")
        if receipt["path"] in seen_paths or receipt["sha256"] in seen_digests:
            raise ReleaseError("proof pin ledger reuses a proof path or digest")
        if record["proof_id"] in seen_ids:
            raise ReleaseError("proof pin ledger reuses a proof identity")
        source = record["source"]
        if source is not None:
            identity = (
                source["repository"],
                source["issue"],
                source["comment_id"],
            )
            if identity in seen_owner_comments or source["comment_url"] in seen_owner_urls:
                raise ReleaseError("proof pin ledger reuses an owner comment identity")
            seen_owner_comments.add(identity)
            seen_owner_urls.add(source["comment_url"])
        pins[key] = record
        seen_paths.add(receipt["path"])
        seen_digests.add(receipt["sha256"])
        seen_ids.add(record["proof_id"])
    return document, pins


def gate_receipt_summary(gate_id: str) -> str:
    if gate_id == "progressive-controls-replay":
        return PROGRESSIVE_CONTROLS_EVIDENCE_SUMMARY
    return f"Typed evidence receipt for gate {gate_id}."


def gate_receipt_path(gate_id: str) -> str:
    return f"release/evidence/{gate_id}-receipt.json"


def gate_proof_path(gate_id: str, kind: str) -> str:
    if kind == "progressive-controls-replay":
        return PROGRESSIVE_CONTROLS_EVIDENCE_PATH
    return f"release/evidence/proofs/{gate_id}-{kind}.json"


def rights_validation_receipt_path(gate_id: str) -> str:
    return f"release/evidence/proofs/{gate_id}-rights-zero-blockers.json"


def _validate_owner_attestation(
    root: Path,
    path: Path,
    gate: dict[str, Any],
    subject: dict[str, Any],
    committed_at: datetime,
    pin: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[str, datetime, set[str], tuple[str, int, int], str, set[str]]:
    label = f"release gate {gate['id']} owner attestation"
    document = load_json(path, label)
    _validate_schema(
        document,
        _load_closed_schema(root, RELEASE_OWNER_ATTESTATION_SCHEMA_PATH, label),
        label,
    )
    if (
        document["gate_id"] != gate["id"]
        or document["issue"] != gate["issue"]
        or document["kind"] != "owner-attestation"
        or document["attestation_id"] != pin["proof_id"]
        or document["subject"] != subject
        or document["package"] != _expected_package(subject)
    ):
        raise ReleaseError(f"{label} belongs to a different gate or release")
    if document["authority"] != {
        "name": RELEASE_OWNER_NAME,
        "github_login": RELEASE_OWNER_LOGIN,
    }:
        raise ReleaseError(f"{label} has no pinned canonical owner")
    contract = RELEASE_GATE_CONTRACTS[gate["id"]]
    if len(contract["affirms"]) != 1 or document["decision"] != {
        "claim": contract["affirms"][0],
        "scope_sha256": _owner_scope_sha256(root, gate, subject, manifest),
    }:
        raise ReleaseError(f"{label} does not bind its exact decision scope")
    source = document["source"]
    if source != pin.get("source"):
        raise ReleaseError(f"{label} does not match its pinned owner-authored source")
    if (
        source["repository"] != RELEASE_REPOSITORY
        or source["issue"] != gate["issue"]
        or source["comment_author"] != RELEASE_OWNER_LOGIN
        or source["comment_body_sha256"]
        != _owner_approval_body_sha256(root, gate, subject, manifest)
    ):
        raise ReleaseError(
            f"{label} does not bind the exact canonical owner approval payload"
        )
    _strict_owner_comment_url(
        source["comment_url"], gate["issue"], source["comment_id"]
    )
    created = _validate_receipt_time(
        source["comment_created_at"], f"{label} source creation", committed_at
    )
    updated = _validate_receipt_time(
        source["comment_updated_at"], f"{label} source update", committed_at
    )
    recorded = _validate_receipt_time(
        document["recorded_at"], f"{label} recording", committed_at
    )
    if not created <= updated <= recorded:
        raise ReleaseError(f"{label} source and recording times are inconsistent")
    validate_public_safe_document(document, label)
    identity = (source["repository"], source["issue"], source["comment_id"])
    return (
        document["attestation_id"],
        recorded,
        {source["comment_body_sha256"]},
        identity,
        source["comment_url"],
        set(),
    )


def _validate_operational_proof(
    root: Path,
    path: Path,
    gate: dict[str, Any],
    kind: str,
    subject: dict[str, Any],
    committed_at: datetime,
    pin: dict[str, Any],
    unsupported_authenticity: set[tuple[str, str]],
) -> tuple[
    str,
    datetime,
    set[str],
    tuple[str, int, int] | None,
    str | None,
    set[str],
]:
    label = f"release gate {gate['id']} {kind} proof"
    document = load_json(path, label)
    _validate_schema(
        document,
        _load_closed_schema(root, RELEASE_GATE_PROOF_SCHEMA_PATH, label),
        label,
    )
    if (
        document["gate_id"] != gate["id"]
        or document["issue"] != gate["issue"]
        or document["kind"] != kind
        or document["proof_id"] != pin["proof_id"]
        or document["subject"] != subject
        or document["package"] != _expected_package(subject)
        or document["issuer"] != pin.get("issuer")
    ):
        raise ReleaseError(f"{label} belongs to a different source, issuer, or release")
    if document["issuer"]["kind"] != RELEASE_PROOF_ISSUER_KINDS[kind]:
        raise ReleaseError(f"{label} uses an authority type that cannot issue this proof")
    expected_checks = RELEASE_PROOF_CHECKS[kind]
    checks = document["checks"]
    if [check["id"] for check in checks] != list(expected_checks):
        raise ReleaseError(f"{label} has a different executable check inventory")
    check_digests = [check["receipt_sha256"] for check in checks]
    if len(check_digests) != len(set(check_digests)):
        raise ReleaseError(f"{label} reuses a source receipt digest")
    generator = document["generator"]
    generator_path = RELEASE_PROOF_GENERATORS[kind]
    if (
        generator["path"] != generator_path
        or generator["version"] != f"danse-{kind}-proof-v1"
    ):
        raise ReleaseError(f"{label} names the wrong proof generator")
    generator_bytes = _git_bytes(
        root, "show", f"{subject['repository_head']}:{generator_path}"
    )
    generator_sha256 = hashlib.sha256(generator_bytes).hexdigest()
    if (
        generator["sha256"] != generator_sha256
        or document["issuer"]["identity"]
        != f"{generator_path}@sha256:{generator_sha256}"
    ):
        raise ReleaseError(f"{label} is not bound to its exact source generator")
    observed = _validate_receipt_time(
        document["observed_at"], f"{label} observation", committed_at
    )
    if kind != "rights-validation":
        # Continue validating the document's structural/source boundaries so a
        # malformed proof cannot hide another contract defect, but never let
        # opaque, self-authored check digests confer completion.  The caller
        # rejects every collected kind after all independently verifiable
        # receipts have been checked.
        unsupported_authenticity.add((gate["id"], kind))
    if kind == "submission-package":
        package_check = next(
            check for check in checks if check["id"] == "package-manifest"
        )
        if package_check["receipt_sha256"] != subject["package_manifest_sha256"]:
            raise ReleaseError(
                f"{label} does not bind its exact package manifest"
            )
    consumed_paths: set[str] = set()
    rights_binding = document["rights_binding"]
    if kind == "rights-validation":
        proof_validation_date = observed.astimezone(
            ZoneInfo("America/New_York")
        ).date()
        effective_validation_date = _current_rights_validation_date()
        tracked = _tracked_paths(root)
        register = rights_binding["register"]
        if register["path"] != "rights/register.json":
            raise ReleaseError(f"{label} names the wrong rights register")
        if register["path"] not in tracked:
            raise ReleaseError(f"{label} rights register is not tracked")
        register_path = verify_record(root, register, f"{label} rights register")
        _verify_commit_record(
            root,
            subject["repository_head"],
            register["path"],
            register["sha256"],
            f"{label} rights register",
        )
        rights_document = load_json(register_path, f"{label} rights register")
        asset_use_ids, redacted_receipt_sha256s, _redacted_paths = (
            _rights_register_inventory(
                root,
                rights_document,
                gate_id=gate["id"],
                source_head=subject["repository_head"],
                tracked=tracked,
                validation_date=effective_validation_date,
            )
        )
        zero_record = rights_binding["zero_blockers"]
        if zero_record["path"] != rights_validation_receipt_path(gate["id"]):
            raise ReleaseError(f"{label} does not use its canonical rights receipt path")
        if zero_record["path"] not in tracked:
            raise ReleaseError(f"{label} zero-blocker receipt is not tracked")
        zero_path = verify_record(root, zero_record, f"{label} zero-blocker receipt")
        _verify_commit_record(
            root,
            _git_output(root, "rev-parse", "HEAD"),
            zero_record["path"],
            zero_record["sha256"],
            f"{label} zero-blocker receipt",
        )
        zero_document = load_json(zero_path, f"{label} zero-blocker receipt")
        validate_public_safe_document(zero_document, f"{label} zero-blocker receipt")
        expected_keys = {
            "schema",
            "gate_id",
            "issue",
            "result",
            "subject",
            "generator",
            "receipt",
        }
        if set(zero_document) != expected_keys:
            raise ReleaseError(f"{label} has no closed rights validation receipt")
        generator = zero_document["generator"]
        if not isinstance(generator, dict) or set(generator) != {
            "path",
            "sha256",
            "receipt_schema",
        }:
            raise ReleaseError(f"{label} has no exact rights validator identity")
        if (
            generator["path"] != "scripts/rights_contract.py"
            or generator["receipt_schema"]
            != "danse.rights.clearance-receipt.v1"
            or not isinstance(generator["sha256"], str)
            or not HEX64.fullmatch(generator["sha256"])
        ):
            raise ReleaseError(f"{label} has no exact rights validator identity")
        _verify_commit_record(
            root,
            subject["repository_head"],
            generator["path"],
            generator["sha256"],
            f"{label} rights validator",
        )
        receipt = zero_document["receipt"]
        checker = _load_rights_checker(root, subject["repository_head"])
        try:
            expected_receipt = checker.build_clearance_scope_receipt(
                rights_document,
                scope=gate["id"],
                register_path=register_path,
                schema_path=source_file(
                    root,
                    RIGHTS_REGISTER_SCHEMA_PATH.as_posix(),
                    f"{label} rights schema",
                ),
                root=root,
                as_of=proof_validation_date,
            )
            current_receipt = checker.build_clearance_scope_receipt(
                rights_document,
                scope=gate["id"],
                register_path=register_path,
                schema_path=source_file(
                    root,
                    RIGHTS_REGISTER_SCHEMA_PATH.as_posix(),
                    f"{label} rights schema",
                ),
                root=root,
                as_of=effective_validation_date,
            )
        except Exception as exc:
            raise ReleaseError(f"{label} canonical rights validation failed") from exc
        if (
            zero_document["schema"] != "danse.release.rights-validation.v1"
            or zero_document["gate_id"] != gate["id"]
            or zero_document["issue"] != gate["issue"]
            or zero_document["result"] != "passed"
            or zero_document["subject"] != subject
            or receipt != expected_receipt
            or receipt["status"] != "ready"
            or receipt["blockers"] != []
            or current_receipt["status"] != "ready"
            or current_receipt["blockers"] != []
        ):
            raise ReleaseError(f"{label} has no exact ready rights validator receipt")
        inputs = receipt["inputs"]
        expected_validation_date = proof_validation_date.isoformat()
        if (
            not isinstance(inputs, dict)
            or set(inputs)
            != {
                "validation_date",
                "validation_timezone",
                "human_gate_ids",
                "asset_use_ids",
                "redacted_receipt_sha256s",
            }
            or inputs["validation_timezone"] != "America/New_York"
            or inputs["validation_date"] != expected_validation_date
            or inputs["human_gate_ids"] != sorted(RIGHTS_CLEARANCE_GATES[gate["id"]])
            or inputs["asset_use_ids"] != asset_use_ids
            or inputs["redacted_receipt_sha256s"] != redacted_receipt_sha256s
        ):
            raise ReleaseError(f"{label} rights validator inputs are not exact")
        check_by_id = {check["id"]: check["receipt_sha256"] for check in checks}
        scoped_asset_ids = {identity.split("/", 1)[0] for identity in asset_use_ids}
        credit_inventory = sorted(
            (
                {
                    "asset_id": asset["id"],
                    "state": asset["public_credit"]["state"],
                    "label": asset["public_credit"]["label"],
                }
                for asset in rights_document["assets"]
                if asset["id"] in scoped_asset_ids
            ),
            key=lambda item: item["asset_id"],
        )
        expected_rights_checks = {
            "asset-census": _rights_check_digest(
                "asset-census",
                {"gate_id": gate["id"], "asset_use_ids": asset_use_ids},
            ),
            "included-use-clearance": _rights_check_digest(
                "included-use-clearance",
                {"gate_id": gate["id"], "asset_use_ids": asset_use_ids},
            ),
            "credits": _rights_check_digest(
                "credits",
                {"gate_id": gate["id"], "credits": credit_inventory},
            ),
            "press-stills": _rights_check_digest(
                "press-stills",
                {
                    "gate_id": gate["id"],
                    "asset_use_ids": (
                        asset_use_ids
                        if gate["id"] == "press-stills-clearance"
                        else []
                    ),
                },
            ),
            "private-evidence": _rights_check_digest(
                "private-evidence",
                {
                    "gate_id": gate["id"],
                    "redacted_receipt_sha256s": redacted_receipt_sha256s,
                },
            ),
            "zero-blockers": zero_record["sha256"],
        }
        if any(
            check_by_id[check_id] != digest
            for check_id, digest in expected_rights_checks.items()
        ):
            raise ReleaseError(f"{label} check digests do not bind its rights inventory")
        consumed_paths.add(zero_record["path"])
    elif rights_binding is not None:
        raise ReleaseError(f"{label} invents an out-of-scope rights binding")
    authority_source = document["authority_source"]
    owner_identity: tuple[str, int, int] | None = None
    owner_url: str | None = None
    source_digests = set(check_digests)
    if document["issuer"]["kind"] in {"venue", "host"}:
        if not isinstance(authority_source, dict) or authority_source != pin.get(
            "source"
        ):
            raise ReleaseError(f"{label} has no pinned external authority source")
        if (
            authority_source["repository"] != RELEASE_REPOSITORY
            or authority_source["issue"] != gate["issue"]
            or authority_source["comment_author"] == RELEASE_OWNER_LOGIN
            or authority_source["comment_body_sha256"]
            != _external_authority_body_sha256(
                gate,
                kind,
                authority_source["comment_author"],
                subject,
                checks,
            )
        ):
            raise ReleaseError(
                f"{label} does not bind a distinct external authority payload"
            )
        _strict_owner_comment_url(
            authority_source["comment_url"],
            gate["issue"],
            authority_source["comment_id"],
        )
        created = _validate_receipt_time(
            authority_source["comment_created_at"],
            f"{label} authority source creation",
            committed_at,
        )
        updated = _validate_receipt_time(
            authority_source["comment_updated_at"],
            f"{label} authority source update",
            committed_at,
        )
        if not created <= updated <= observed:
            raise ReleaseError(f"{label} authority source times are inconsistent")
        owner_identity = (
            authority_source["repository"],
            authority_source["issue"],
            authority_source["comment_id"],
        )
        owner_url = authority_source["comment_url"]
        source_digests.add(authority_source["comment_body_sha256"])
    elif authority_source is not None or pin.get("source") is not None:
        raise ReleaseError(f"{label} invents an out-of-scope authority source")
    validate_public_safe_document(document, label)
    return (
        document["proof_id"],
        observed,
        source_digests,
        owner_identity,
        owner_url,
        consumed_paths,
    )


def validate_release_gate_receipt(
    root: Path,
    path: Path,
    gate: dict[str, Any],
    manifest: dict[str, Any],
    *,
    pins: dict[tuple[str, str], dict[str, Any]],
    consumed_pin_keys: set[tuple[str, str]],
    tracked: set[str],
    seen_proof_paths: set[str],
    seen_proof_digests: set[str],
    seen_proof_ids: set[str],
    seen_source_digests: set[str],
    seen_owner_comments: set[tuple[str, int, int]],
    seen_owner_urls: set[str],
    unsupported_authenticity: set[tuple[str, str]],
    verified_git_objects: set[tuple[str, str]],
) -> dict[str, Any]:
    """Validate one public-safe completion receipt for a non-live gate.

    A digest-bound arbitrary file is not evidence that a human decision, final
    package, restore, custody copy, deployment, or presentation occurred. The
    receipt therefore binds the exact gate and issue, the release identity,
    gate-specific review pins, and a real ancestor commit/tree. The ledger is
    structural integrity metadata; repository review remains the trust rail.
    Private evidence remains external behind typed public-safe redacted proofs.
    """

    receipt = load_json(path, f"release gate {gate['id']} receipt")
    _validate_schema(
        receipt,
        _load_closed_schema(
            root,
            RELEASE_GATE_RECEIPT_SCHEMA_PATH,
            "release gate receipt",
        ),
        f"release gate {gate['id']} receipt",
    )
    if receipt["schema"] != RELEASE_GATE_RECEIPT_SCHEMA:
        raise ReleaseError(f"release gate {gate['id']} receipt has the wrong schema")
    if receipt["gate_id"] != gate["id"] or receipt["issue"] != gate["issue"]:
        raise ReleaseError(f"release gate {gate['id']} receipt names a different owner gate")
    if receipt["result"] != "satisfied":
        raise ReleaseError(f"release gate {gate['id']} receipt is not satisfied")
    contract = RELEASE_GATE_CONTRACTS[gate["id"]]
    subject, committed_at = _validate_subject(
        root,
        receipt["subject"],
        manifest,
        contract,
        verified_git_objects,
    )
    recorded_at = _validate_receipt_time(
        receipt["recorded_at"],
        f"release gate {gate['id']} recording",
        committed_at,
    )

    expected_affirms = list(contract["affirms"])
    expected_non_affirms = sorted(set(RELEASE_HIGH_RISK_CLAIMS) - set(expected_affirms))
    if (
        receipt["affirms"] != expected_affirms
        or receipt["does_not_affirm"] != expected_non_affirms
    ):
        raise ReleaseError(
            f"release gate {gate['id']} receipt has a contradictory claim boundary"
        )

    evidence = receipt["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise ReleaseError(f"release gate {gate['id']} receipt has no evidence rows")
    evidence_ids: set[str] = set()
    evidence_kinds: list[str] = []
    proof_times: list[datetime] = []
    for index, row in enumerate(evidence):
        label = f"release gate {gate['id']} evidence row {index + 1}"
        if not isinstance(row, dict) or set(row) != {
            "id",
            "kind",
            "receipt",
        }:
            raise ReleaseError(f"{label} has an unknown shape")
        row_id = row.get("id")
        kind = row.get("kind")
        record = row.get("receipt")
        if not isinstance(row_id, str) or not SAFE_ID.fullmatch(row_id) or row_id in evidence_ids:
            raise ReleaseError(f"{label} has a malformed or duplicate id")
        evidence_ids.add(row_id)
        if kind not in contract["proofs"] or kind in evidence_kinds:
            raise ReleaseError(f"{label} has an unsupported or duplicate proof kind")
        evidence_kinds.append(kind)
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "schema"}:
            raise ReleaseError(f"{label} has no closed local receipt record")
        proof_path = safe_relative(record.get("path"), f"{label} receipt path")
        digest = record.get("sha256")
        schema_name = record.get("schema")
        if proof_path != gate_proof_path(gate["id"], kind):
            raise ReleaseError(f"{label} does not use its canonical proof path")
        if proof_path not in tracked:
            raise ReleaseError(
                f"{label} receipt is not tracked by the source repository"
            )
        if proof_path in seen_proof_paths or digest in seen_proof_digests:
            raise ReleaseError(
                f"{label} reuses a proof path or digest across release gates"
            )
        key = (gate["id"], kind)
        pin = pins.get(key)
        expected_schema = {
            "owner-attestation": RELEASE_OWNER_ATTESTATION_SCHEMA,
            "progressive-controls-replay": PROGRESSIVE_CONTROLS_SCHEMA,
        }.get(kind, RELEASE_GATE_PROOF_SCHEMA)
        if (
            pin is None
            or pin["gate_id"] != gate["id"]
            or pin["issue"] != gate["issue"]
            or record != pin["receipt"]
            or schema_name != expected_schema
        ):
            raise ReleaseError(f"{label} has no matching review-required proof pin")
        proof_file = verify_record(root, record, f"{label} receipt")
        if kind == "owner-attestation":
            (
                proof_id,
                proof_time,
                source_digests,
                owner_identity,
                owner_url,
                extra_paths,
            ) = _validate_owner_attestation(
                root,
                proof_file,
                gate,
                subject,
                committed_at,
                pin,
                manifest,
            )
        elif kind == "progressive-controls-replay":
            (
                proof_id,
                proof_time,
                source_digests,
                owner_identity,
                owner_url,
                extra_paths,
            ) = validate_progressive_controls_receipt(
                root,
                proof_file,
                gate,
                subject,
                committed_at,
                pin,
            )
        else:
            (
                proof_id,
                proof_time,
                source_digests,
                owner_identity,
                owner_url,
                extra_paths,
            ) = _validate_operational_proof(
                root,
                proof_file,
                gate,
                kind,
                subject,
                committed_at,
                pin,
                unsupported_authenticity,
            )
        if proof_id in seen_proof_ids:
            raise ReleaseError(f"{label} reuses a proof identity across release gates")
        reused_paths = extra_paths & seen_proof_paths
        if reused_paths:
            raise ReleaseError(f"{label} reuses a subordinate proof path")
        reused_sources = source_digests & seen_source_digests
        if reused_sources:
            raise ReleaseError(f"{label} reuses a source receipt across release gates")
        if owner_identity is not None:
            if owner_identity in seen_owner_comments or owner_url in seen_owner_urls:
                raise ReleaseError(f"{label} reuses an owner comment identity")
            seen_owner_comments.add(owner_identity)
            seen_owner_urls.add(owner_url)
        seen_proof_paths.add(proof_path)
        seen_proof_paths.update(extra_paths)
        seen_proof_digests.add(digest)
        seen_proof_ids.add(proof_id)
        seen_source_digests.update(source_digests)
        consumed_pin_keys.add(key)
        proof_times.append(proof_time)

    if evidence_kinds != list(contract["proofs"]):
        raise ReleaseError(
            f"release gate {gate['id']} receipt lacks its exact proof inventory"
        )
    if proof_times and recorded_at < max(proof_times):
        raise ReleaseError(
            f"release gate {gate['id']} receipt predates one of its proofs"
        )
    validate_public_safe_document(receipt, f"release gate {gate['id']} receipt")
    return subject


def public_copy_strings(manifest: dict[str, Any]) -> Iterator[str]:
    """Yield prose that enters a public artifact, excluding honest state labels.

    A public-approved artifact may legitimately report that release-only custody,
    restore, or presentation evidence is still pending. Those enum values are not
    editorial placeholders. Human-facing prose, including the actions attached to
    those later gates, must still be free of draft markers.
    """
    for section in ("identity", "copy", "installation", "accessibility", "press"):
        yield from strings(manifest[section])
    for claim in manifest["claims"]:
        yield claim["text"]
        if claim["evidence"]:
            yield claim["evidence"]["summary"]
    for credit in manifest["credits"]:
        yield credit["role"]
        yield credit["name"] or ""
        yield credit["note"]
    for medium in manifest["media"]:
        yield medium["label"]
        yield medium["alt_text"] or ""
    for product in manifest["products"]:
        yield product["label"]
    for gate in manifest["gates"]:
        yield gate["id"]
        yield gate["owner"]
        yield gate["action"]


def caption_milliseconds(value: str) -> int:
    hours, minutes, tail = value.split(":")
    seconds, milliseconds = tail.split(".")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(milliseconds)
    )


def unique_ids(records: Iterable[dict[str, Any]], label: str) -> set[str]:
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ReleaseError(f"{label} ids must be unique")
    return set(ids)


def _load_opportunity_checker(root: Path):
    checker_path = source_file(root, "scripts/check-opportunities.py", "opportunity checker")
    spec = importlib.util.spec_from_file_location("danse_release_opportunity_checker", checker_path)
    if spec is None or spec.loader is None:
        raise ReleaseError("cannot load the opportunity checker")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    return checker


def _load_installation_checker(root: Path):
    checker_path = source_file(root, "installation/contract.py", "installation checker")
    spec = importlib.util.spec_from_file_location("danse_release_installation_checker", checker_path)
    if spec is None or spec.loader is None:
        raise ReleaseError("cannot load the installation checker")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    return checker


def validate_installation_binding(root: Path, manifest: dict[str, Any]) -> None:
    binding = manifest["installation"]["reference_contract"]
    twin_path = verify_record(root, binding["digital_twin"], "installation digital twin")
    gates_path = verify_record(root, binding["gate_ledger"], "installation gate ledger")
    checker = _load_installation_checker(root)
    try:
        spec = checker.validate_digital_twin(checker.load_json(twin_path), root)
        gates = checker.validate_gates(checker.load_json(gates_path), spec)
    except Exception as exc:  # The checker owns its ContractError after dynamic import.
        raise ReleaseError(f"installation reference contract failed: {exc}") from exc

    expected_gate_ids = [gate["id"] for gate in gates["gates"]]
    expected = {
        "schema": "danse.installation.reference-binding.v1",
        "status": "reference-only",
        "spec_id": spec["identity"]["id"],
        "spec_contract_sha256": spec["identity"]["contract_sha256"],
        "physical_predicates_satisfied": gates["physical_predicates_satisfied"],
        "issue_14_can_close": gates["issue_14_can_close"],
        "blocked_gates": expected_gate_ids,
    }
    for key, value in expected.items():
        if binding[key] != value:
            raise ReleaseError(f"installation reference binding {key} drifted")


def validate_opportunity_binding(root: Path, manifest: dict[str, Any]) -> None:
    binding = manifest["opportunity_snapshot"]
    if binding["snapshot_id"] != EXPECTED_OPPORTUNITY_ID:
        raise ReleaseError("release manifest consumes the wrong opportunity snapshot id")
    if binding["frozen_at"] != EXPECTED_OPPORTUNITY_FROZEN_AT:
        raise ReleaseError("release manifest consumes the wrong opportunity freeze time")
    if binding["sha256"] != EXPECTED_OPPORTUNITY_SHA256:
        raise ReleaseError("release manifest does not consume the reviewed frozen opportunity digest")
    if binding["receipt_sha256"] != EXPECTED_OPPORTUNITY_RECEIPT_SHA256:
        raise ReleaseError("release manifest does not consume the reviewed opportunity receipt")
    if binding["source_evidence_sha256"] != EXPECTED_SOURCE_EVIDENCE_SHA256:
        raise ReleaseError("release manifest does not consume the reviewed source-evidence digest")
    expected_paths = {
        "path": "opportunities/omega-20260829.json",
        "receipt_path": "opportunities/omega-20260829.receipt.json",
        "source_evidence_path": "opportunities/source-evidence-20260826.json",
    }
    for key, expected in expected_paths.items():
        if binding[key] != expected:
            raise ReleaseError(f"release manifest {key} is not the canonical frozen registry path")

    snapshot_path = verify_record(root, binding, "opportunity snapshot")
    receipt_record = {
        "path": binding["receipt_path"],
        "sha256": binding["receipt_sha256"],
    }
    receipt_path = verify_record(root, receipt_record, "opportunity receipt")
    source_evidence_record = {
        "path": binding["source_evidence_path"],
        "sha256": binding["source_evidence_sha256"],
    }
    source_evidence_path = verify_record(root, source_evidence_record, "source-evidence manifest")
    snapshot = load_json(snapshot_path, "opportunity snapshot")
    receipt = load_json(receipt_path, "opportunity receipt")
    if snapshot.get("snapshot_id") != binding["snapshot_id"]:
        raise ReleaseError("opportunity snapshot id disagrees with the release manifest")
    if snapshot.get("frozen_at") != binding["frozen_at"]:
        raise ReleaseError("opportunity snapshot freeze time disagrees with the release manifest")
    expected_source_evidence = {
        "path": binding["source_evidence_path"],
        "sha256": binding["source_evidence_sha256"],
        "bytes": source_evidence_path.stat().st_size,
    }
    if snapshot.get("source_evidence") != expected_source_evidence:
        raise ReleaseError("opportunity snapshot source-evidence binding drifted")
    if receipt.get("snapshot", {}).get("sha256") != binding["sha256"]:
        raise ReleaseError("opportunity receipt does not bind the release digest")
    if receipt.get("snapshot", {}).get("frozen_at") != binding["frozen_at"]:
        raise ReleaseError("opportunity receipt does not bind the release freeze time")
    consumer = next(
        (row for row in receipt.get("consumers", []) if row.get("issue") == 12),
        None,
    )
    if consumer != {"issue": 12, "binding": "release/manifest.json", "status": "pending"}:
        raise ReleaseError("opportunity receipt no longer reserves issue 12 for release/manifest.json")

    checker = _load_opportunity_checker(root)
    try:
        checker.validate_all(
            snapshot_path=snapshot_path,
            schema_path=source_file(root, "opportunities/opportunity.schema.json", "opportunity schema"),
            receipt_path=receipt_path,
            consumer_path=source_file(root, "submission/screendance-2027.yaml", "ScreenDance consumer"),
            evidence_path=source_evidence_path,
            root=root,
        )
    except Exception as exc:  # The checker owns its RegistryError type after dynamic import.
        raise ReleaseError(f"frozen opportunity/ScreenDance consumer contract failed: {exc}") from exc


def validate_schema(root: Path, manifest: dict[str, Any]) -> None:
    schema = load_json(source_file(root, SCHEMA.as_posix(), "release schema"), "release schema")
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        errors = sorted(
            jsonschema.Draft202012Validator(
                schema, format_checker=jsonschema.FormatChecker()
            ).iter_errors(manifest),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
    except jsonschema.exceptions.SchemaError as exc:
        raise ReleaseError(f"release schema is invalid: {exc.message}") from exc
    if errors:
        error = errors[0]
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise ReleaseError(f"release manifest schema failure at {location}: {error.message}")


def _validate_graph(manifest: dict[str, Any], gate_ids: set[str]) -> None:
    nodes = manifest["installation"]["system_flow"]
    node_ids = unique_ids(nodes, "system-flow node")
    for node in nodes:
        unknown = sorted(set(node["feeds"]) - node_ids)
        if unknown:
            raise ReleaseError(f"system-flow node {node['id']} feeds unknown nodes: {unknown}")
        if node["id"] in node["feeds"]:
            raise ReleaseError(f"system-flow node {node['id']} cannot feed itself")

    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {node["id"]: node for node in nodes}

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ReleaseError("system-flow graph contains a cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for target in by_id[node_id]["feeds"]:
            visit(target)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in sorted(node_ids):
        visit(node_id)

    for section in ("spatial_requirements", "technical_rider"):
        for requirement in manifest["installation"][section]:
            if requirement["evidence_gate"] not in gate_ids:
                raise ReleaseError(
                    f"{section} item {requirement['item']!r} names an unknown evidence gate"
                )


def validate_live_interaction_receipt(path: Path) -> None:
    receipt = load_json(path, "live interaction replay receipt")
    if set(receipt) != {
        "schema",
        "gate_id",
        "result",
        "observed_at",
        "source",
        "deployment",
        "checks",
        "non_actions",
    }:
        raise ReleaseError("live interaction replay receipt has an unknown shape")
    expected_identity = {
        "schema": "danse.live-interaction-replay.v1",
        "gate_id": "live-interaction-replay",
        "result": "satisfied",
        "observed_at": "2026-08-04T08:58:18Z",
    }
    for key, expected in expected_identity.items():
        if receipt[key] != expected:
            raise ReleaseError(f"live interaction replay receipt {key} drifted")

    if receipt["source"] != {
        "repository": "organvm/the-thing-without-a-name",
        "issue": 17,
        "comment_id": 5176789674,
        "comment_url": "https://github.com/organvm/the-thing-without-a-name/issues/17#issuecomment-5176789674",
        "comment_author": "4444J99",
        "comment_created_at": "2026-08-04T08:58:19Z",
        "comment_updated_at": "2026-08-04T08:58:19Z",
        "comment_body_sha256": LIVE_INTERACTION_COMMENT_BODY_SHA256,
    }:
        raise ReleaseError("live interaction replay source identity drifted")
    if receipt["deployment"] != {
        "url": "https://organvm.github.io/the-thing-without-a-name/",
        "pages_manifest_schema": "danse.pages.v1",
        "source_commit": LIVE_INTERACTION_DEPLOYED_COMMIT,
        "pages_file_count": 680,
    }:
        raise ReleaseError("live interaction replay deployed source identity drifted")

    checks = receipt["checks"]
    expected_checks = [
        "initial-hidden-state",
        "touch-disclosure",
        "escape-close",
        "keyboard-h-toggle",
        "feedback-separation",
        "console-clean",
    ]
    if (
        not isinstance(checks, list)
        or not all(isinstance(check, dict) for check in checks)
        or [check.get("id") for check in checks] != expected_checks
    ):
        raise ReleaseError("live interaction replay check inventory drifted")
    for check in checks:
        if (
            not isinstance(check, dict)
            or set(check) != {"id", "observation", "result"}
            or check["result"] != "passed"
            or not isinstance(check["observation"], str)
            or not check["observation"].strip()
        ):
            raise ReleaseError("live interaction replay contains an invalid check")
    if receipt["non_actions"] != [
        "No camera permission was requested.",
        "No interaction receipt was saved.",
        "No public or account action was performed by this evidence record.",
    ]:
        raise ReleaseError("live interaction replay non-action boundary drifted")
    validate_public_safe_document(receipt, "live interaction replay receipt")


def validate_progressive_controls_receipt(
    root: Path,
    path: Path,
    gate: dict[str, Any],
    subject: dict[str, Any],
    committed_at: datetime,
    pin: dict[str, Any],
) -> tuple[str, datetime, set[str], None, None, set[str]]:
    """Validate the pinned raw-capture replay for the progressive UI gate."""
    receipt = load_json(path, "progressive controls replay receipt")
    schema_path = source_file(
        root,
        PROGRESSIVE_CONTROLS_SCHEMA_PATH,
        "progressive controls replay schema",
    )
    schema = load_json(schema_path, "progressive controls replay schema")
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(receipt)
    except jsonschema.SchemaError as exc:
        raise ReleaseError(f"progressive controls replay schema is invalid: {exc.message}") from exc
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "root"
        raise ReleaseError(
            f"progressive controls replay receipt violates schema at {location}: {exc.message}"
        ) from exc

    if (
        receipt["schema"] != PROGRESSIVE_CONTROLS_SCHEMA
        or receipt["proof_id"] != pin["proof_id"]
        or receipt["gate_id"] != gate["id"]
        or receipt["issue"] != gate["issue"]
        or receipt["subject"] != subject
    ):
        raise ReleaseError(
            "progressive controls replay belongs to a different proof or release"
        )
    generator = receipt["generator"]
    expected_issuer = {
        "kind": "tool",
        "identity": f"{generator['path']}@sha256:{generator['sha256']}",
    }
    if pin["issuer"] != expected_issuer or pin.get("source") is not None:
        raise ReleaseError(
            "progressive controls replay pin does not name only its exact generator"
        )
    observed = _validate_receipt_time(
        receipt["observed_at"],
        "progressive controls replay observation",
        committed_at,
    )
    check_ids = [check["id"] for check in receipt["checks"]]
    if check_ids != list(PROGRESSIVE_CONTROLS_CHECKS):
        raise ReleaseError("progressive controls replay check inventory drifted")
    raw_capture = receipt["raw_capture"]
    if (
        raw_capture["subject"] != subject
        or raw_capture["runtime"] != receipt["runtime"]
        or raw_capture["observed_at"] != receipt["observed_at"]
        or raw_capture["check_ids"] != list(PROGRESSIVE_CONTROLS_CHECKS)
    ):
        raise ReleaseError(
            "progressive controls raw capture belongs to a different source or runtime"
        )
    raw_digest = hashlib.sha256(canonical_json(raw_capture)).hexdigest()
    if raw_digest != receipt["raw_capture_sha256"]:
        raise ReleaseError("progressive controls raw capture digest drifted")
    empty_log_digest = hashlib.sha256(canonical_json([])).hexdigest()
    if (
        raw_capture["console_error_log_sha256"] != empty_log_digest
        or raw_capture["http_error_log_sha256"] != empty_log_digest
    ):
        raise ReleaseError("progressive controls passed replay carries an error log")
    screenshot_ids = [item["id"] for item in raw_capture["screenshots"]]
    if len(screenshot_ids) != len(set(screenshot_ids)):
        raise ReleaseError("progressive controls raw capture reuses a screenshot identity")
    for check in receipt["checks"]:
        expected_digest = hashlib.sha256(
            canonical_json(
                {
                    "check_id": check["id"],
                    "raw_capture_sha256": raw_digest,
                }
            )
        ).hexdigest()
        if check["receipt_sha256"] != expected_digest:
            raise ReleaseError(
                "progressive controls check is not bound to its raw capture"
            )
    digests = [receipt["raw_capture_sha256"]] + [
        check["receipt_sha256"] for check in receipt["checks"]
    ]
    if len(digests) != len(set(digests)):
        raise ReleaseError("progressive controls replay reuses a capture digest")
    generator_bytes = _git_bytes(
        root,
        "show",
        f"{subject['repository_head']}:{generator['path']}",
    )
    if hashlib.sha256(generator_bytes).hexdigest() != generator["sha256"]:
        raise ReleaseError(
            "progressive controls replay generator does not belong to its source"
        )
    validate_public_safe_document(receipt, "progressive controls replay receipt")
    return receipt["proof_id"], observed, set(digests), None, None, set()


def _validate_evidence_states(root: Path, manifest: dict[str, Any]) -> None:
    for claim in manifest["claims"]:
        evidence = claim["evidence"]
        if claim["status"] == "verified":
            if evidence is None:
                raise ReleaseError(f"verified claim {claim['id']} has no evidence")
            verify_record(root, evidence, f"claim {claim['id']} evidence")
        elif evidence is not None:
            raise ReleaseError(f"pending claim {claim['id']} may not carry completion evidence")

    for credit in manifest["credits"]:
        evidence = credit["evidence"]
        if credit["status"] == "cleared":
            if not credit["name"] or evidence is None:
                raise ReleaseError(f"cleared credit {credit['id']} needs a name and evidence")
            verify_record(root, evidence, f"credit {credit['id']} evidence")
        elif evidence is not None:
            raise ReleaseError(f"pending credit {credit['id']} may not carry completion evidence")

    destinations: set[str] = set()
    for medium in manifest["media"]:
        source = medium["source"]
        clearance = medium["clearance"]
        if source is not None:
            verify_record(root, source, f"media {medium['id']} source")
            destination = safe_relative(source["destination"], f"media {medium['id']} destination")
            if not destination.startswith("media/assets/"):
                raise ReleaseError(f"media {medium['id']} destination must stay under media/assets/")
            if destination in destinations:
                raise ReleaseError(f"media destination is not unique: {destination}")
            destinations.add(destination)
        if medium["status"] == "ready" and source is None:
            raise ReleaseError(f"ready media {medium['id']} has no source")
        if medium["status"] == "excluded" and source is not None:
            raise ReleaseError(f"excluded media {medium['id']} still names a source")
        if medium["kind"] in {"still", "film", "installation-evidence", "social-card"}:
            if medium["status"] == "ready" and not medium["alt_text"]:
                raise ReleaseError(f"ready visual media {medium['id']} has no alt text")
        evidence = clearance["evidence"]
        if clearance["status"] == "cleared":
            if evidence is None:
                raise ReleaseError(f"cleared media {medium['id']} has no clearance evidence")
            verify_record(root, evidence, f"media {medium['id']} clearance")
        elif evidence is not None:
            raise ReleaseError(
                f"media {medium['id']} with {clearance['status']} clearance may not carry completion evidence"
            )

    products = {product["id"]: product["path"] for product in manifest["products"]}
    if products != GENERATED_PRODUCT_PATHS:
        raise ReleaseError("generated release product inventory or destination drifted")

    pinned_gate_present = any(
        gate["state"] == "satisfied"
        and gate["evidence"] is not None
        and gate["id"] != "live-interaction-replay"
        for gate in manifest["gates"]
    )
    tracked = _tracked_paths(root) if pinned_gate_present else set()
    if pinned_gate_present and RELEASE_PROOF_PINS_PATH.as_posix() not in tracked:
        raise ReleaseError("pinned release has no tracked proof pin ledger")
    if pinned_gate_present:
        pin_ledger, pins = load_proof_pins(root)
        checkout_head = _git_output(root, "rev-parse", "HEAD")
    else:
        pin_ledger, pins, checkout_head = None, {}, None
    seen_outer_paths: set[str] = set()
    seen_outer_digests: set[str] = set()
    seen_proof_paths: set[str] = set()
    seen_proof_digests: set[str] = set()
    seen_proof_ids: set[str] = set()
    seen_source_digests: set[str] = set()
    seen_owner_comments: set[tuple[str, int, int]] = set()
    seen_owner_urls: set[str] = set()
    consumed_pin_keys: set[tuple[str, str]] = set()
    unsupported_authenticity: set[tuple[str, str]] = set()
    verified_git_objects: set[tuple[str, str]] = set()
    common_source: dict[str, Any] | None = None
    package_identity: dict[str, Any] | None = None

    for gate in manifest["gates"]:
        contract = RELEASE_GATE_CONTRACTS.get(gate["id"])
        if contract is None:
            raise ReleaseError(
                f"release gate {gate['id']} has no immutable evidence contract"
            )
        if gate["issue"] != contract["issue"] or gate["owner"] != contract["owner"]:
            raise ReleaseError(f"release gate {gate['id']} owner or issue drifted")
        evidence = gate["evidence"]
        if gate["state"] == "satisfied":
            if evidence is None:
                raise ReleaseError(f"satisfied gate {gate['id']} has no evidence")
            if gate["id"] != "live-interaction-replay":
                if evidence.get("summary") != gate_receipt_summary(gate["id"]):
                    raise ReleaseError(
                        f"release gate {gate['id']} evidence summary is not the exact neutral template"
                    )
            evidence_path = verify_record(root, evidence, f"gate {gate['id']} evidence")
            outer_path = evidence["path"]
            outer_digest = evidence["sha256"]
            if outer_path in seen_outer_paths or outer_digest in seen_outer_digests:
                raise ReleaseError(
                    f"release gate {gate['id']} reuses another gate receipt"
                )
            seen_outer_paths.add(outer_path)
            seen_outer_digests.add(outer_digest)
            if gate["id"] == "live-interaction-replay":
                if evidence["path"] != LIVE_INTERACTION_EVIDENCE_PATH:
                    raise ReleaseError("live interaction replay names the wrong evidence receipt")
                validate_live_interaction_receipt(evidence_path)
            else:
                if evidence["path"] != gate_receipt_path(gate["id"]):
                    raise ReleaseError(
                        f"release gate {gate['id']} does not use its canonical receipt path"
                    )
                if evidence["path"] not in tracked:
                    raise ReleaseError(
                        f"release gate {gate['id']} receipt is not tracked"
                    )
                subject = validate_release_gate_receipt(
                    root,
                    evidence_path,
                    gate,
                    manifest,
                    pins=pins,
                    consumed_pin_keys=consumed_pin_keys,
                    tracked=tracked,
                    seen_proof_paths=seen_proof_paths,
                    seen_proof_digests=seen_proof_digests,
                    seen_proof_ids=seen_proof_ids,
                    seen_source_digests=seen_source_digests,
                    seen_owner_comments=seen_owner_comments,
                    seen_owner_urls=seen_owner_urls,
                    unsupported_authenticity=unsupported_authenticity,
                    verified_git_objects=verified_git_objects,
                )
                source_identity = _source_identity(subject)
                if common_source is None:
                    common_source = source_identity
                elif source_identity != common_source:
                    raise ReleaseError(
                        "release gate repository source identity differs across gates"
                    )
                expected_package = _expected_package(subject)
                if expected_package is not None:
                    if package_identity is None:
                        package_identity = expected_package
                    elif expected_package != package_identity:
                        raise ReleaseError(
                            "complete package binding differs across release gates"
                        )
        elif evidence is not None:
            raise ReleaseError(f"pending gate {gate['id']} may not carry completion evidence")

    if pinned_gate_present:
        assert pin_ledger is not None and common_source is not None
        if pin_ledger["source"] != common_source:
            raise ReleaseError(
                "proof pin ledger names a different frozen release source"
            )
        if consumed_pin_keys != set(pins):
            raise ReleaseError(
                "proof pin ledger inventory differs from the satisfied gate proofs"
            )
        contract_paths = {
            gate_receipt_path(gate["id"])
            for gate in manifest["gates"]
            if gate["state"] == "satisfied"
            and gate["id"] != "live-interaction-replay"
        }
        for gate in manifest["gates"]:
            if gate["state"] != "satisfied" or gate["id"] == "live-interaction-replay":
                continue
            contract = RELEASE_GATE_CONTRACTS[gate["id"]]
            contract_paths.update(
                gate_proof_path(gate["id"], kind) for kind in contract["proofs"]
            )
            if "rights-validation" in contract["proofs"]:
                contract_paths.add(rights_validation_receipt_path(gate["id"]))
        allowed_paths = {
            MANIFEST.as_posix(),
            RELEASE_PROOF_PINS_PATH.as_posix(),
            *contract_paths,
        }
        assert checkout_head is not None
        _verify_git_object_integrity(
            root,
            common_source["repository_head"],
            checkout_head,
        )
        validate_evidence_only_descendant(
            root, common_source["repository_head"], allowed_paths
        )
        validate_clean_checkout(root)
        if _git_output(root, "rev-parse", "HEAD") != checkout_head:
            raise ReleaseError("repository HEAD changed during release validation")

    live_gate = next(
        (gate for gate in manifest["gates"] if gate["id"] == "live-interaction-replay"),
        None,
    )
    if live_gate is None or live_gate["state"] != "satisfied":
        raise ReleaseError("completed live interaction replay gate cannot regress")

    captions = manifest["accessibility"]["captions"]
    previous_end = -1
    for index, cue in enumerate(captions["cues"], start=1):
        start = caption_milliseconds(cue["start"])
        end = caption_milliseconds(cue["end"])
        if start >= end:
            raise ReleaseError(f"caption cue {index} must end after it starts")
        if start < previous_end:
            raise ReleaseError(f"caption cue {index} overlaps or precedes the prior cue")
        if not cue["text"].strip() or "-->" in cue["text"] or "\n" in cue["text"] or "\r" in cue["text"]:
            raise ReleaseError(f"caption cue {index} must contain one safe non-empty text line")
        previous_end = end

    if unsupported_authenticity:
        claims = ", ".join(
            f"{gate_id}:{kind}"
            for gate_id, kind in sorted(unsupported_authenticity)
        )
        raise ReleaseError(
            "release gates claim completion from operational proofs with no "
            f"trusted current authenticity verifier ({claims}); those gates "
            "must remain pending"
        )


def phase_blockers(manifest: dict[str, Any], phase: str) -> list[str]:
    if phase not in PHASES:
        raise ReleaseError(f"unknown release phase {phase!r}")
    if phase == "draft":
        return []

    blockers: list[str] = []
    allowed_status = {"public-approved", "released"} if phase == "public" else {"released"}
    if manifest["status"] not in allowed_status:
        blockers.append(
            f"manifest status is {manifest['status']!r}; {phase} requires {sorted(allowed_status)}"
        )

    for gate in manifest["gates"]:
        if phase in gate["required_for"] and gate["state"] != "satisfied":
            blockers.append(f"gate {gate['id']} is {gate['state']}: {gate['action']}")
    for medium in manifest["media"]:
        if phase not in medium["required_for"]:
            continue
        if medium["status"] != "ready":
            blockers.append(f"media {medium['id']} is {medium['status']}")
        if medium["clearance"]["status"] != "cleared":
            blockers.append(
                f"media {medium['id']} clearance is {medium['clearance']['status']}"
            )
        if medium["source"] is None:
            blockers.append(f"media {medium['id']} has no source")
    for product in manifest["products"]:
        if phase in product["required_for"] and product["status"] != "ready":
            blockers.append(f"generated product {product['id']} is {product['status']}")
    for claim in manifest["claims"]:
        if claim["publish"] and claim["status"] != "verified":
            blockers.append(f"published claim {claim['id']} is {claim['status']}")
    for credit in manifest["credits"]:
        if credit["status"] != "cleared":
            blockers.append(f"credit {credit['id']} is {credit['status']}")
    for section in ("spatial_requirements", "technical_rider"):
        for requirement in manifest["installation"][section]:
            if requirement["status"] != "verified":
                blockers.append(f"{section} item {requirement['item']!r} is proposed")

    contact = manifest["press"]["contact"]
    if contact["status"] != "approved" or not contact["url"]:
        blockers.append("public contact route is not approved")
    captions = manifest["accessibility"]["captions"]
    transcript = manifest["accessibility"]["transcript"]
    if captions["status"] == "pending":
        blockers.append("caption applicability/content is pending")
    if transcript["status"] == "pending":
        blockers.append("transcript applicability/content is pending")
    has_film = any(
        phase in medium["required_for"] and medium["kind"] == "film"
        for medium in manifest["media"]
    )
    if has_film and captions["status"] != "approved":
        blockers.append("release film requires an approved caption track")
    if captions["status"] == "approved" and not captions["cues"]:
        blockers.append("approved caption track contains no cues")
    if transcript["status"] == "approved" and not transcript["text"].strip():
        blockers.append("approved transcript is empty")

    leaked = next(
        (
            marker.group(0)
            for value in public_copy_strings(manifest)
            if (marker := PUBLIC_MARKERS.search(value))
        ),
        None,
    )
    if leaked:
        blockers.append(f"public-facing manifest retains a draft/pending marker: {leaked!r}")
    return blockers


def validate_release(
    root: Path = ROOT,
    *,
    manifest_path: Path | str = MANIFEST,
    phase: str = "draft",
) -> dict[str, Any]:
    root = root.absolute()
    if (root / ".git").exists():
        _reject_git_replace_refs(root)
    manifest_file = source_file(root, str(manifest_path), "release manifest")
    manifest = load_json(manifest_file, "release manifest")
    validate_schema(root, manifest)

    leaked = next(
        (marker.group(0).strip() for value in strings(manifest) if (marker := PRIVATE_PATH_MARKER.search(value))),
        None,
    )
    if leaked:
        raise ReleaseError(f"release manifest exposes a private/local path marker: {leaked}")

    claim_ids = unique_ids(manifest["claims"], "claim")
    credit_ids = unique_ids(manifest["credits"], "credit")
    media_ids = unique_ids(manifest["media"], "media")
    product_ids = unique_ids(manifest["products"], "product")
    gate_ids = unique_ids(manifest["gates"], "gate")
    identity_groups = (claim_ids, credit_ids, media_ids, product_ids)
    if any(
        left & right
        for index, left in enumerate(identity_groups)
        for right in identity_groups[index + 1 :]
    ):
        raise ReleaseError("claim, credit, media, and product ids must not collide")

    calendar = manifest["press"]["posting_calendar"]
    if [item["sequence"] for item in calendar] != list(range(1, len(calendar) + 1)):
        raise ReleaseError("posting calendar sequences must be contiguous and ordered from one")
    for item in calendar:
        if item["asset_id"] not in media_ids:
            raise ReleaseError(f"posting calendar names unknown media {item['asset_id']}")
    if manifest["accessibility"]["review_gate"] not in gate_ids:
        raise ReleaseError("accessibility review names an unknown gate")

    _validate_graph(manifest, gate_ids)
    _validate_evidence_states(root, manifest)
    validate_installation_binding(root, manifest)
    validate_opportunity_binding(root, manifest)
    blockers = phase_blockers(manifest, phase)
    if blockers:
        preview = "; ".join(blockers[:8])
        suffix = f"; and {len(blockers) - 8} more" if len(blockers) > 8 else ""
        raise ReleaseError(f"{phase} phase blocked by {len(blockers)} predicate(s): {preview}{suffix}")
    return manifest


def source_commit(root: Path, explicit: str | None = None) -> str:
    commit = explicit
    if commit is None:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            env=_git_environment(),
        )
        if done.returncode != 0:
            raise ReleaseError(f"cannot resolve source commit: {done.stderr.strip()}")
        commit = done.stdout.strip()
    commit = commit.lower()
    if not HEX40.fullmatch(commit):
        raise ReleaseError(f"source commit must be a full 40-character Git SHA: {commit!r}")
    return commit
