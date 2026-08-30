#!/usr/bin/env python3
"""Strict, phase-aware contract for the Danse public/release artifacts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import jsonschema

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path("release/manifest.json")
SCHEMA = Path("release/manifest.schema.json")
RELEASE_SCHEMA = "danse.release.v1"
RELEASE_GATE_RECEIPT_SCHEMA_PATH = Path("release/gate-receipt.schema.json")
RELEASE_GATE_PROOF_SCHEMA_PATH = Path("release/gate-proof.schema.json")
RELEASE_OWNER_ATTESTATION_SCHEMA_PATH = Path("release/owner-attestation.schema.json")
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
    "map-gating",
    "console-clean",
    "http-clean",
)
RELEASE_GATE_RECEIPT_SCHEMA = "danse.release.gate.v2"
RELEASE_GATE_PROOF_SCHEMA = "danse.release.gate-proof.v1"
RELEASE_OWNER_ATTESTATION_SCHEMA = "danse.release.owner-attestation.v1"
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
    "rights-release": (
        "asset-census",
        "credits",
        "included-use-clearance",
        "press-stills",
        "private-evidence",
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
        "proofs": ("rights-release",),
        "affirms": ("rights-cleared",),
        "package": True,
    },
    "press-stills-clearance": {
        "issue": 16,
        "owner": "Issue 16 and Anthony",
        "proofs": ("owner-attestation", "rights-release"),
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
        "proofs": (),
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
        "proofs": ("custody-completion",),
        "affirms": ("custody-complete",),
        "package": True,
    },
    "restore-rehearsal": {
        "issue": 12,
        "owner": "Issue 12 and Anthony",
        "proofs": ("restore-completion",),
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
# A generic receipt cannot introduce its own authority. Each completed proof must
# be pinned here by a reviewed code change after the typed public-safe receipt
# has been checked against its private/domain source. No terminal proof is
# currently pinned; the tracked release intentionally remains a draft.
PINNED_GATE_PROOFS: dict[tuple[str, str], dict[str, Any]] = {}
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
ASSERTIVE_SUMMARY = re.compile(
    r"\b(?:accepted?|approved?|authorized?|cleared?|completed?|deployed?|filed?|"
    r"final(?:[- ]cut)?|ready|rights?|submitted?|submission|terms?|uploaded?|upload)\b",
    re.IGNORECASE,
)
APPLE_METAL_RENDERER = re.compile(
    r"^ANGLE \(Apple(?: Inc\.)?, ANGLE Metal Renderer: [^\r\n()]+\)$"
)
RUNTIME_NEGATION = re.compile(r"\b(?:no|not|without)\b", re.IGNORECASE)


def _decoded_variants(value: str) -> Iterator[str]:
    """Expose nested percent-encoding before applying public-safety rules."""

    current = value
    for _ in range(5):
        yield current
        decoded = unquote(current)
        if decoded == current:
            return
        current = decoded


def validate_public_safe_document(value: Any, label: str) -> None:
    """Reject private, contact, credential, encoded, or control data everywhere."""

    sensitive_key = next(
        (key for key in keys(value) if key.lower() in SENSITIVE_KEYS), None
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
            ):
                raise ReleaseError(f"{label} exposes private contact or path data")


def _git_output(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
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


def validate_evidence_only_descendant(root: Path, oid: str) -> None:
    """Require checkout drift from a reviewed source to be evidence-only.

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
                "--name-only",
                "-z",
                oid,
                "HEAD",
                "--",
            ],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ReleaseError("cannot compare the reviewed release source tree") from exc
    if source_diff.returncode != 0:
        raise ReleaseError("cannot compare the reviewed release source tree")
    try:
        changed = {
            item.decode("utf-8") for item in source_diff.stdout.split(b"\0") if item
        }
    except UnicodeDecodeError as exc:
        raise ReleaseError("reviewed release source diff contains a non-UTF-8 path") from exc
    invalid = sorted(
        path
        for path in changed
        if path != MANIFEST.as_posix()
        and path != PROGRESSIVE_CONTROLS_EVIDENCE_PATH
        and not re.fullmatch(r"release/evidence/[a-z0-9-]+-receipt\.json", path)
        and not re.fullmatch(r"release/evidence/proofs/[a-z0-9-]+\.json", path)
    )
    if invalid:
        raise ReleaseError(
            "release receipt source drift is not limited to its tracked evidence envelope"
        )


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


def _tracked_paths(root: Path) -> set[str]:
    payload = _git_output(root, "ls-files", "-z")
    return {item for item in payload.split("\0") if item}


def _validate_subject(
    root: Path,
    subject: object,
    manifest: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[dict[str, Any], datetime]:
    if not isinstance(subject, dict) or set(subject) != {
        "release_id",
        "release_version",
        "repository_head",
        "repository_tree",
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
    tree, committed_at = git_commit_identity(root, subject.get("repository_head"))
    if subject.get("repository_tree") != tree:
        raise ReleaseError(
            "release gate receipt repository tree disagrees with its head"
        )
    validate_evidence_only_descendant(root, subject["repository_head"])
    return subject, committed_at


def _expected_package(subject: dict[str, Any]) -> dict[str, Any] | None:
    digest = subject["package_manifest_sha256"]
    if digest is None:
        return None
    return {
        "manifest": "manifest.json",
        "manifest_sha256": digest,
        "repository_head": subject["repository_head"],
    }


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


def _validate_owner_attestation(
    root: Path,
    path: Path,
    gate: dict[str, Any],
    subject: dict[str, Any],
    committed_at: datetime,
    pin: dict[str, Any],
) -> tuple[str, datetime, set[str]]:
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
        or document["subject"] != subject
        or document["package"] != _expected_package(subject)
    ):
        raise ReleaseError(f"{label} belongs to a different gate or release")
    if document["authority"] != {
        "name": RELEASE_OWNER_NAME,
        "github_login": RELEASE_OWNER_LOGIN,
    }:
        raise ReleaseError(f"{label} has no pinned canonical owner")
    source = document["source"]
    if source != pin.get("source"):
        raise ReleaseError(f"{label} does not match its reviewed owner-authored source")
    if (
        source["repository"] != RELEASE_REPOSITORY
        or source["issue"] != gate["issue"]
        or source["comment_author"] != RELEASE_OWNER_LOGIN
    ):
        raise ReleaseError(f"{label} names a different repository authority")
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
    return document["attestation_id"], recorded, {source["comment_body_sha256"]}


def _validate_operational_proof(
    root: Path,
    path: Path,
    gate: dict[str, Any],
    kind: str,
    subject: dict[str, Any],
    committed_at: datetime,
    pin: dict[str, Any],
) -> tuple[str, datetime, set[str]]:
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
        or document["subject"] != subject
        or document["package"] != _expected_package(subject)
        or document["issuer"] != pin.get("issuer")
    ):
        raise ReleaseError(f"{label} belongs to a different source, issuer, or release")
    expected_checks = RELEASE_PROOF_CHECKS[kind]
    checks = document["checks"]
    if [check["id"] for check in checks] != list(expected_checks):
        raise ReleaseError(f"{label} has a different executable check inventory")
    check_digests = [check["receipt_sha256"] for check in checks]
    if len(check_digests) != len(set(check_digests)):
        raise ReleaseError(f"{label} reuses a source receipt digest")
    if kind == "submission-package":
        package_check = next(
            check for check in checks if check["id"] == "package-manifest"
        )
        if package_check["receipt_sha256"] != subject["package_manifest_sha256"]:
            raise ReleaseError(
                f"{label} does not authenticate its exact package manifest"
            )
    observed = _validate_receipt_time(
        document["observed_at"], f"{label} observation", committed_at
    )
    validate_public_safe_document(document, label)
    return document["proof_id"], observed, set(check_digests)


def validate_release_gate_receipt(
    root: Path,
    path: Path,
    gate: dict[str, Any],
    manifest: dict[str, Any],
    *,
    tracked: set[str],
    seen_proof_paths: set[str],
    seen_proof_digests: set[str],
    seen_proof_ids: set[str],
    seen_source_digests: set[str],
) -> str | None:
    """Authenticate one public-safe completion receipt for a non-live gate.

    A digest-bound arbitrary file is not evidence that a human decision, final
    package, restore, custody copy, deployment, or presentation occurred. The
    receipt therefore binds the exact gate and issue, the release identity, an
    gate-specific reviewed proof pins and a real ancestor commit/tree. Private
    evidence remains external behind a typed, pinned, public-safe redacted proof.
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
        root, receipt["subject"], manifest, contract
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
            "summary",
        }:
            raise ReleaseError(f"{label} has an unknown shape")
        row_id = row.get("id")
        kind = row.get("kind")
        record = row.get("receipt")
        summary = row.get("summary")
        if not isinstance(row_id, str) or not SAFE_ID.fullmatch(row_id) or row_id in evidence_ids:
            raise ReleaseError(f"{label} has a malformed or duplicate id")
        evidence_ids.add(row_id)
        if kind not in contract["proofs"] or kind in evidence_kinds:
            raise ReleaseError(f"{label} has an unsupported or duplicate proof kind")
        evidence_kinds.append(kind)
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or ASSERTIVE_SUMMARY.search(summary)
        ):
            raise ReleaseError(f"{label} has an authoritative or unsafe summary")
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "schema"}:
            raise ReleaseError(f"{label} has no closed local receipt record")
        proof_path = safe_relative(record.get("path"), f"{label} receipt path")
        digest = record.get("sha256")
        schema_name = record.get("schema")
        if proof_path not in tracked:
            raise ReleaseError(
                f"{label} receipt is not tracked by the source repository"
            )
        if proof_path in seen_proof_paths or digest in seen_proof_digests:
            raise ReleaseError(
                f"{label} reuses a proof path or digest across release gates"
            )
        pin = PINNED_GATE_PROOFS.get((gate["id"], kind))
        expected_schema = (
            RELEASE_OWNER_ATTESTATION_SCHEMA
            if kind == "owner-attestation"
            else RELEASE_GATE_PROOF_SCHEMA
        )
        if (
            pin is None
            or record != {key: pin.get(key) for key in ("path", "sha256", "schema")}
            or schema_name != expected_schema
        ):
            raise ReleaseError(f"{label} has no authenticated reviewed proof pin")
        proof_file = verify_record(root, record, f"{label} receipt")
        if kind == "owner-attestation":
            proof_id, proof_time, source_digests = _validate_owner_attestation(
                root,
                proof_file,
                gate,
                subject,
                committed_at,
                pin,
            )
        else:
            proof_id, proof_time, source_digests = _validate_operational_proof(
                root,
                proof_file,
                gate,
                kind,
                subject,
                committed_at,
                pin,
            )
        if proof_id in seen_proof_ids:
            raise ReleaseError(f"{label} reuses a proof identity across release gates")
        reused_sources = source_digests & seen_source_digests
        if reused_sources:
            raise ReleaseError(f"{label} reuses a source receipt across release gates")
        seen_proof_paths.add(proof_path)
        seen_proof_digests.add(digest)
        seen_proof_ids.add(proof_id)
        seen_source_digests.update(source_digests)
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
    return subject["package_manifest_sha256"]


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


def validate_progressive_controls_receipt(root: Path, path: Path) -> None:
    """Validate the distinct exact-head browser receipt for the progressive UI gate."""
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

    check_ids = [check["id"] for check in receipt["checks"]]
    if check_ids != list(PROGRESSIVE_CONTROLS_CHECKS):
        raise ReleaseError("progressive controls replay check inventory drifted")
    source = receipt["source"]
    tree, committed_at = git_commit_identity(root, source["exact_head"])
    if source["tree"] != tree:
        raise ReleaseError(
            "progressive controls replay tree disagrees with its exact head"
        )
    validate_evidence_only_descendant(root, source["exact_head"])
    _validate_receipt_time(
        receipt["observed_at"],
        "progressive controls replay observation",
        committed_at,
    )
    renderer = receipt["runtime"]["graphics"]["renderer"]
    if not APPLE_METAL_RENDERER.fullmatch(renderer) or RUNTIME_NEGATION.search(
        renderer
    ):
        raise ReleaseError(
            "progressive controls replay is not authenticated as Apple Metal"
        )
    validate_public_safe_document(receipt, "progressive controls replay receipt")


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

    authenticated_gate_present = any(
        gate["state"] == "satisfied"
        and gate["evidence"] is not None
        and gate["id"] != "live-interaction-replay"
        for gate in manifest["gates"]
    )
    tracked = _tracked_paths(root) if authenticated_gate_present else set()
    seen_outer_paths: set[str] = set()
    seen_outer_digests: set[str] = set()
    seen_proof_paths: set[str] = set()
    seen_proof_digests: set[str] = set()
    seen_proof_ids: set[str] = set()
    seen_source_digests: set[str] = set()
    package_digest: str | None = None

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
                summary = evidence.get("summary")
                if (
                    not isinstance(summary, str)
                    or not summary.strip()
                    or ASSERTIVE_SUMMARY.search(summary)
                ):
                    raise ReleaseError(
                        f"release gate {gate['id']} has an authoritative or unsafe evidence summary"
                    )
                validate_public_safe_document(
                    summary, f"release gate {gate['id']} evidence summary"
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
            elif gate["id"] == "progressive-controls-replay":
                if evidence["path"] != PROGRESSIVE_CONTROLS_EVIDENCE_PATH:
                    raise ReleaseError(
                        "progressive controls replay names the wrong evidence receipt"
                    )
                if evidence["path"] not in tracked:
                    raise ReleaseError(
                        "progressive controls replay receipt is not tracked"
                    )
                validate_progressive_controls_receipt(root, evidence_path)
            else:
                if evidence["path"] not in tracked:
                    raise ReleaseError(
                        f"release gate {gate['id']} receipt is not tracked"
                    )
                gate_package_digest = validate_release_gate_receipt(
                    root,
                    evidence_path,
                    gate,
                    manifest,
                    tracked=tracked,
                    seen_proof_paths=seen_proof_paths,
                    seen_proof_digests=seen_proof_digests,
                    seen_proof_ids=seen_proof_ids,
                    seen_source_digests=seen_source_digests,
                )
                if gate_package_digest is not None:
                    if package_digest is None:
                        package_digest = gate_package_digest
                    elif gate_package_digest != package_digest:
                        raise ReleaseError(
                            "package binding digest differs across release gates"
                        )
        elif evidence is not None:
            raise ReleaseError(f"pending gate {gate['id']} may not carry completion evidence")

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
        )
        if done.returncode != 0:
            raise ReleaseError(f"cannot resolve source commit: {done.stderr.strip()}")
        commit = done.stdout.strip()
    commit = commit.lower()
    if not HEX40.fullmatch(commit):
        raise ReleaseError(f"source commit must be a full 40-character Git SHA: {commit!r}")
    return commit
