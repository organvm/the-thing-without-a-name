#!/usr/bin/env python3
"""Validate the immutable Alpha → Omega opportunity snapshot and its consumers.

Changing calls belong in a new snapshot under issue #22. This checker deliberately
does not contact the network: it proves the checked-at source registry, its content
digest, and the exact binding consumed by the ScreenDance filing register.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "opportunities" / "omega-20260829.json"
SCHEMA = ROOT / "opportunities" / "opportunity.schema.json"
RECEIPT = ROOT / "opportunities" / "omega-20260829.receipt.json"
EVIDENCE = ROOT / "opportunities" / "source-evidence-20260826.json"
CONSUMER = ROOT / "submission" / "screendance-2027.yaml"

FACT_STATUSES = ("verified", "unstated", "not-applicable", "conflicted")
DISPOSITIONS = ("active", "closed", "watch", "conflicted", "blocked", "historical")
ACTIVE_DISPOSITIONS = {"active", "blocked", "conflicted"}
REQUIRED_FACTS = {
    "deadline",
    "eligibility",
    "fee",
    "runtime",
    "container",
    "aspect",
    "captions",
    "credits",
    "installation",
    "terms",
}
EXPECTED_TARGETS = {
    "bakehouse-studio-residency-2026",
    "cinedans-2028-international-short",
    "cinedans-fest-2027-installation",
    "eyebeam-2026-residency",
    "ica-miami-unsolicited-proposal",
    "knight-arts-challenge-miami",
    "locust-projects-main-gallery",
    "miami-dade-tdc-2026-q2",
    "mignolo-screendance-2026",
    "miami-light-project-here-now-2027",
    "oolite-ellies-creator-2027",
    "oolite-studio-residency-2027",
    "screendance-miami-2027",
    "superblue-artist-route",
    "the-bass-unsolicited-proposal",
    "times-square-midnight-moment",
    "wavemaker-grants-2027-watch",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PRIVATE_PATH = re.compile(
    r"(?:^|[\s'\"`(\[=])"
    r"(?:/(?!/)[^\s'\"`)]*|//[^/\s]+/[^\s'\"`)]*|"
    r"[A-Za-z]:[\\/][^\s'\"`)]*|\\\\[^\\/\s]+[\\/][^\s'\"`)]*|"
    r"~[\\/][^\s'\"`)]*|file://[^\s'\"`)]*)"
)
ASSIGNED_PRIVATE_PATH = re.compile(
    r"(?:^|[\s;,])(?:[A-Za-z_][A-Za-z0-9_-]*[:=])"
    r"(?:/(?!/)[^\s'\"`)]*|[A-Za-z]:[\\/][^\s'\"`)]*|"
    r"\\\\[^\\/\s]+[\\/][^\s'\"`)]*|~[\\/][^\s'\"`)]*|file://[^\s'\"`)]*)"
)
EVIDENCE_CONTRACT = {
    "transport": "HTTPS GET",
    "redirects": "followed",
    "content_encoding": "decoded",
    "user_agent": "Danse source-evidence capture/1.0",
    "digest": "SHA-256 of response body bytes after HTTP content decoding",
    "retention": "Digest-only public-response receipt; response bodies are not vendored.",
}


class RegistryError(ValueError):
    """The frozen snapshot or one of its content bindings is invalid."""


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            value.update(chunk)
    return value.hexdigest()


def parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RegistryError(f"{label} is not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RegistryError(f"{label} must carry an explicit time zone")
    return parsed


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RegistryError(f"{label} must be a JSON object")
    return value


def string_values(value: Any) -> Iterator[str]:
    """Yield strings from a JSON-shaped value without serialisation artefacts."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from string_values(key)
            yield from string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from string_values(item)


def private_path_marker(value: Any) -> str | None:
    for text in string_values(value):
        for pattern in (PRIVATE_PATH, ASSIGNED_PRIVATE_PATH):
            match = pattern.search(text)
            if match:
                return match.group(0).strip()
    return None


def normalize_json(value: Any) -> Any:
    """Convert YAML's date values into one stable JSON representation."""
    if isinstance(value, dict):
        return {str(key): normalize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_json(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise RegistryError(f"submission register contains an unsupported value: {type(value).__name__}")


def canonical_register_digest(register: dict[str, Any]) -> str:
    """Digest every operational register field, excluding its snapshot pointer.

    The exclusion prevents a circular digest: the YAML points at the snapshot,
    while the snapshot commits to the canonical YAML content that does the work.
    """
    normalized = normalize_json(register)
    if not isinstance(normalized, dict):
        raise RegistryError("submission register must be a YAML mapping")
    normalized.pop("opportunity_snapshot", None)
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def checked_date(value: Any, label: str) -> date:
    """Normalize one YAML provenance date without accepting a floating timestamp."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise RegistryError(f"{label} must be a date or timezone-aware timestamp")
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise RegistryError(f"{label} is not an ISO-8601 date") from exc
    raise RegistryError(f"{label} is not an ISO-8601 date")


def safe_file(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise RegistryError(f"{label} must be a non-empty repository-relative path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or "\\" in relative:
        raise RegistryError(f"{label} escapes the repository: {relative!r}")
    root = root.resolve()
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise RegistryError(f"{label} traverses a symlink: {relative!r}")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise RegistryError(f"{label} is missing or outside the repository: {relative!r}") from exc
    if not resolved.is_file():
        raise RegistryError(f"{label} is not a regular file: {relative!r}")
    return resolved


def validate_source_evidence(
    snapshot: dict[str, Any],
    *,
    root: Path,
    evidence_path: Path,
    frozen: datetime,
) -> dict[str, Any]:
    """Validate the digest-only receipts for every checked public response."""
    record = snapshot.get("source_evidence")
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes"}:
        raise RegistryError("source-evidence binding has an unknown shape")
    bound_evidence = safe_file(root, record["path"], "source-evidence path")
    if bound_evidence != evidence_path.resolve():
        raise RegistryError("snapshot points at a different source-evidence manifest")
    if not HEX64.fullmatch(str(record["sha256"])) or record["sha256"] != digest(bound_evidence):
        raise RegistryError("source-evidence manifest digest is missing or stale")
    if record["bytes"] != bound_evidence.stat().st_size:
        raise RegistryError("source-evidence manifest byte count is stale")

    evidence = load_json(bound_evidence, "source-evidence manifest")
    if set(evidence) != {
        "schema",
        "capture_started_at",
        "capture_completed_at",
        "capture_contract",
        "responses",
    } or evidence.get("schema") != "danse.source-evidence.v1":
        raise RegistryError("source-evidence manifest has an unknown shape or schema")
    if evidence.get("capture_contract") != EVIDENCE_CONTRACT:
        raise RegistryError("source-evidence capture contract drifted")
    leaked = private_path_marker(evidence)
    if leaked:
        raise RegistryError(f"source-evidence manifest exposes a private/local path marker: {leaked}")
    started = parse_time(evidence["capture_started_at"], "source evidence capture_started_at")
    completed = parse_time(evidence["capture_completed_at"], "source evidence capture_completed_at")
    if not started <= completed <= frozen:
        raise RegistryError("source-evidence capture interval falls outside the frozen snapshot")

    source_checked: dict[str, list[datetime]] = {}
    for entry in snapshot["opportunities"]:
        for source in entry["sources"]:
            source_checked.setdefault(source["url"], []).append(
                parse_time(source["checked_at"], f"{entry['id']} source checked_at")
            )
    responses = evidence.get("responses")
    if not isinstance(responses, list) or any(not isinstance(row, dict) for row in responses):
        raise RegistryError("source-evidence responses must be an object list")
    response_urls = [row.get("url") for row in responses]
    if len(response_urls) != len(set(response_urls)) or set(response_urls) != set(source_checked):
        raise RegistryError("source-evidence response census disagrees with checked URLs")
    expected_keys = {
        "url",
        "final_url",
        "captured_at",
        "http_status",
        "content_type",
        "bytes",
        "sha256",
    }
    for row in responses:
        url = row.get("url")
        if set(row) != expected_keys:
            raise RegistryError(f"source evidence for {url!r} has an unknown shape")
        captured = parse_time(row.get("captured_at"), f"source evidence for {url}")
        if not started <= captured <= completed or any(checked > captured for checked in source_checked[url]):
            raise RegistryError(f"source evidence for {url} predates its check or falls outside capture")
        if row.get("http_status") != 200:
            raise RegistryError(f"source evidence for {url} did not capture a successful response")
        if not isinstance(row.get("final_url"), str) or not row["final_url"].startswith("https://"):
            raise RegistryError(f"source evidence for {url} lacks a secure final URL")
        if not isinstance(row.get("content_type"), str) or not row["content_type"].strip():
            raise RegistryError(f"source evidence for {url} lacks a content type")
        byte_count = row.get("bytes")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count <= 0:
            raise RegistryError(f"source evidence for {url} has an invalid byte count")
        if not HEX64.fullmatch(str(row.get("sha256", ""))):
            raise RegistryError(f"source evidence for {url} lacks a response digest")
    return evidence


def validate_registry(
    snapshot_path: Path = SNAPSHOT,
    schema_path: Path = SCHEMA,
    *,
    root: Path = ROOT,
    evidence_path: Path = EVIDENCE,
) -> dict[str, Any]:
    snapshot = load_json(snapshot_path, "opportunity snapshot")
    schema = load_json(schema_path, "opportunity schema")
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        ).validate(snapshot)
    except jsonschema.exceptions.SchemaError as exc:
        raise RegistryError(f"opportunity schema is invalid: {exc.message}") from exc
    except jsonschema.exceptions.ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path) or "<root>"
        raise RegistryError(f"snapshot schema failure at {location}: {exc.message}") from exc

    leaked = private_path_marker(snapshot)
    if leaked:
        raise RegistryError(f"snapshot exposes a private/local path marker: {leaked}")

    if snapshot["policy"] != {
        "immutable": True,
        "fact_statuses": list(FACT_STATUSES),
        "dispositions": list(DISPOSITIONS),
        "external_actions": "human-gated",
    }:
        raise RegistryError("snapshot policy vocabulary or immutability contract drifted")

    frozen = parse_time(snapshot["frozen_at"], "frozen_at")
    opportunities = snapshot["opportunities"]
    ids = [entry["id"] for entry in opportunities]
    if len(ids) != len(set(ids)):
        raise RegistryError("opportunity ids must be unique")
    if set(ids) != EXPECTED_TARGETS:
        missing = sorted(EXPECTED_TARGETS - set(ids))
        extra = sorted(set(ids) - EXPECTED_TARGETS)
        raise RegistryError(f"tracked-plan target census drifted; missing={missing}, extra={extra}")

    by_id: dict[str, dict[str, Any]] = {}
    for entry in opportunities:
        entry_id = entry["id"]
        by_id[entry_id] = entry
        urls = [source["url"] for source in entry["sources"]]
        if len(urls) != len(set(urls)):
            raise RegistryError(f"{entry_id}: source URLs must be unique")
        for source in entry["sources"]:
            checked = parse_time(source["checked_at"], f"{entry_id} source checked_at")
            if checked > frozen:
                raise RegistryError(f"{entry_id}: source was allegedly checked after the freeze")

        facts = entry["facts"]
        fact_ids = [fact["id"] for fact in facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise RegistryError(f"{entry_id}: fact ids must be unique")
        if entry["disposition"] in ACTIVE_DISPOSITIONS and not REQUIRED_FACTS.issubset(fact_ids):
            missing = sorted(REQUIRED_FACTS - set(fact_ids))
            raise RegistryError(f"{entry_id}: filing-relevant facts are missing: {missing}")

        for fact in facts:
            fact_id = fact["id"]
            status = fact["status"]
            source = fact.get("source")
            resolve = fact.get("resolve")
            if status == "verified":
                if source not in urls:
                    raise RegistryError(f"{entry_id}/{fact_id}: verified fact lacks a declared source")
                if fact.get("value") is None:
                    raise RegistryError(f"{entry_id}/{fact_id}: verified fact has no value")
            elif status == "unstated":
                if fact.get("value") is not None or not isinstance(resolve, str) or not resolve.strip():
                    raise RegistryError(f"{entry_id}/{fact_id}: unstated fact needs null value and resolution route")
            elif status == "conflicted":
                if source not in urls or not isinstance(resolve, str) or not resolve.strip():
                    raise RegistryError(f"{entry_id}/{fact_id}: conflict needs source and named resolution route")
            elif status == "not-applicable":
                if not isinstance(fact.get("value"), str) or not fact["value"].strip():
                    raise RegistryError(f"{entry_id}/{fact_id}: not-applicable fact needs an explanation")

        deadline_at = entry["deadline_at"]
        deadline = parse_time(deadline_at, f"{entry_id} deadline_at") if deadline_at else None
        disposition = entry["disposition"]
        if disposition == "closed" and (deadline is None or deadline > frozen):
            raise RegistryError(f"{entry_id}: closed call must carry an elapsed deadline")
        if disposition in ACTIVE_DISPOSITIONS and deadline is not None and deadline <= frozen:
            raise RegistryError(f"{entry_id}: current/blocked/conflicted call has an elapsed deadline")
        if disposition in ACTIVE_DISPOSITIONS and not entry["human_gates"]:
            raise RegistryError(f"{entry_id}: external current work lacks an explicit human gate")
        for gate in entry["human_gates"]:
            if gate["status"] != "required":
                raise RegistryError(f"{entry_id}/{gate['id']}: external action was falsely marked complete")

    validate_source_evidence(snapshot, root=root, evidence_path=evidence_path, frozen=frozen)

    ranks = snapshot["ranked_actions"]
    if [action["rank"] for action in ranks] != list(range(1, len(ranks) + 1)):
        raise RegistryError("ranked actions must be contiguous and ordered from one")
    ranked_ids = [action["opportunity_id"] for action in ranks]
    if len(ranked_ids) != len(set(ranked_ids)):
        raise RegistryError("ranked actions contain a duplicate target")
    expected_ranked = {entry["id"] for entry in opportunities if entry["disposition"] in ACTIVE_DISPOSITIONS}
    if set(ranked_ids) != expected_ranked:
        raise RegistryError("ranked actions must be exactly the active, blocked, and conflicted targets")
    ranked_deadlines = [
        parse_time(by_id[entry_id]["deadline_at"], f"{entry_id} deadline_at")
        for entry_id in ranked_ids
        if by_id[entry_id]["deadline_at"] is not None
    ]
    queue = snapshot.get("operational_queue")
    expected_queue = {
        "basis": "frozen_at",
        "expires_at": min(ranked_deadlines).isoformat().replace("+00:00", "Z"),
        "successor_issue": 22,
    }
    if queue != expected_queue:
        raise RegistryError("operational queue expiry must bind the earliest ranked deadline")

    consumers = {row["issue"]: row for row in snapshot["release_consumers"]}
    if set(consumers) != {2, 12} or consumers[2]["status"] != "bound" or consumers[12]["status"] != "pending":
        raise RegistryError("snapshot must bind issue #2 and reserve the identical digest for pending issue #12")

    corrections = {
        "bakehouse-studio-residency-2026": ("closed", "2026-05-01"),
        "locust-projects-main-gallery": ("watch", "2025-11-16"),
        "miami-dade-tdc-2026-q2": ("blocked", "2026-10-13"),
        "mignolo-screendance-2026": ("conflicted", "2026-10-13"),
        "oolite-ellies-creator-2027": ("closed", "2026-08-03"),
        "oolite-studio-residency-2027": ("closed", "2026-08-03"),
    }
    for entry_id, (want_disposition, want_date) in corrections.items():
        entry = by_id[entry_id]
        if entry["disposition"] != want_disposition or not str(entry["deadline_at"]).startswith(want_date):
            raise RegistryError(f"{entry_id}: source correction regressed")
    if by_id["cinedans-fest-2027-installation"]["disposition"] != "active":
        raise RegistryError("Cinedans FEST '27 installation route must remain an active current target")
    if by_id["screendance-miami-2027"]["owner_issue"] != 2:
        raise RegistryError("ScreenDance filing must remain owned by issue #2")

    return snapshot


def validate_operational(snapshot: dict[str, Any], as_of: datetime) -> None:
    """Fail a live queue once any freeze-time ranked deadline has elapsed."""
    frozen = parse_time(snapshot["frozen_at"], "frozen_at")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise RegistryError("operational as-of time must carry an explicit time zone")
    if as_of < frozen:
        raise RegistryError("operational as-of time cannot predate the frozen snapshot")
    by_id = {entry["id"]: entry for entry in snapshot["opportunities"]}
    expired = []
    for action in snapshot["ranked_actions"]:
        entry = by_id[action["opportunity_id"]]
        if entry["deadline_at"] is not None and parse_time(
            entry["deadline_at"], f"{entry['id']} deadline_at"
        ) <= as_of:
            expired.append(entry["id"])
    if expired:
        raise RegistryError(
            "operational queue has elapsed ranked deadlines; issue #22 must publish a successor: "
            + ", ".join(expired)
        )


def validate_binding(
    snapshot: dict[str, Any],
    *,
    root: Path = ROOT,
    snapshot_path: Path = SNAPSHOT,
    receipt_path: Path = RECEIPT,
    consumer_path: Path = CONSUMER,
) -> dict[str, Any]:
    receipt = load_json(receipt_path, "opportunity receipt")
    if set(receipt) != {
        "schema",
        "issued_at",
        "snapshot",
        "opportunity_count",
        "source_count",
        "ranked_action_count",
        "consumers",
    } or receipt.get("schema") != "danse.opportunity-receipt.v1":
        raise RegistryError("opportunity receipt has an unknown shape or schema")
    issued_at = parse_time(receipt["issued_at"], "receipt issued_at")
    if issued_at < parse_time(snapshot["frozen_at"], "frozen_at"):
        raise RegistryError("opportunity receipt predates its frozen snapshot")

    record = receipt.get("snapshot")
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes", "snapshot_id", "frozen_at"}:
        raise RegistryError("receipt snapshot record has an unknown shape")
    bound_snapshot = safe_file(root, record["path"], "receipt snapshot path")
    if bound_snapshot != snapshot_path.resolve():
        raise RegistryError("receipt points at a different snapshot path")
    actual = digest(bound_snapshot)
    if not HEX64.fullmatch(str(record["sha256"])) or record["sha256"] != actual:
        raise RegistryError("receipt snapshot digest is missing or stale")
    if record["bytes"] != bound_snapshot.stat().st_size:
        raise RegistryError("receipt snapshot byte count is stale")
    if record["snapshot_id"] != snapshot["snapshot_id"] or record["frozen_at"] != snapshot["frozen_at"]:
        raise RegistryError("receipt snapshot identity disagrees with the content")

    sources = {source["url"] for entry in snapshot["opportunities"] for source in entry["sources"]}
    if receipt["opportunity_count"] != len(snapshot["opportunities"]):
        raise RegistryError("receipt opportunity count is stale")
    if receipt["source_count"] != len(sources):
        raise RegistryError("receipt source count is stale")
    if receipt["ranked_action_count"] != len(snapshot["ranked_actions"]):
        raise RegistryError("receipt ranked-action count is stale")

    consumer_rows = {row.get("issue"): row for row in receipt["consumers"] if isinstance(row, dict)}
    if consumer_rows != {
        2: {
            "issue": 2,
            "binding": "submission/screendance-2027.yaml",
            "status": "verified",
        },
        12: {
            "issue": 12,
            "binding": "release/manifest.json",
            "status": "pending",
        },
    }:
        raise RegistryError("receipt consumer contract drifted")

    try:
        register = yaml.safe_load(consumer_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RegistryError(f"cannot read ScreenDance consumer register: {exc}") from exc
    if not isinstance(register, dict):
        raise RegistryError("ScreenDance consumer register must be a YAML mapping")
    binding = register.get("opportunity_snapshot")
    if not isinstance(binding, dict):
        raise RegistryError("ScreenDance register has no opportunity snapshot binding")
    timezone_name = binding.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise RegistryError("ScreenDance snapshot binding has no named timezone")
    try:
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise RegistryError("ScreenDance snapshot binding timezone is unavailable") from exc
    expected_binding = {
        "snapshot_id": snapshot["snapshot_id"],
        "path": record["path"],
        "sha256": actual,
        "receipt": receipt_path.resolve().relative_to(root.resolve()).as_posix(),
        "frozen_at": snapshot["frozen_at"],
        "opportunity_id": "screendance-miami-2027",
        "timezone": timezone_name,
    }
    if binding != expected_binding:
        raise RegistryError("ScreenDance register does not consume the exact frozen snapshot")
    targets = {entry["id"]: entry for entry in snapshot["opportunities"]}
    if binding["opportunity_id"] not in targets:
        raise RegistryError("ScreenDance consumer points at a missing opportunity")
    target = targets[binding["opportunity_id"]]
    target_source_checks = {
        source["url"]: parse_time(
            source["checked_at"],
            f"{binding['opportunity_id']} source checked_at",
        ).date()
        for source in target["sources"]
    }
    terms = register.get("terms")
    if not isinstance(terms, list) or any(not isinstance(term, dict) for term in terms):
        raise RegistryError("ScreenDance verified terms must be an object list")
    for term in terms:
        term_id = term.get("id", "<unnamed>")
        source = term.get("source")
        if term.get("status") != "verified" or source not in target_source_checks:
            raise RegistryError(
                f"ScreenDance term {term_id} lacks a verified frozen opportunity source"
            )
        if checked_date(term.get("checked"), f"ScreenDance term {term_id} checked") > (
            target_source_checks[source]
        ):
            raise RegistryError(
                f"ScreenDance term {term_id} postdates its frozen source check"
            )
    deadline = parse_time(
        target["deadline_at"],
        "ScreenDance snapshot deadline",
    )
    if deadline.astimezone(zone).replace(tzinfo=None) != deadline.replace(tzinfo=None):
        raise RegistryError("ScreenDance named timezone disagrees with the frozen deadline")
    deadline_fact = next(
        (fact for fact in target["facts"] if fact.get("id") == "deadline"),
        None,
    )
    if (
        not isinstance(deadline_fact, dict)
        or not isinstance(deadline_fact.get("value"), str)
        or timezone_name not in deadline_fact["value"]
    ):
        raise RegistryError("ScreenDance named timezone is not the frozen deadline timezone")
    expected_contract = {
        "path": "submission/screendance-2027.yaml",
        "excluded_fields": ["opportunity_snapshot"],
        "schema": "danse.submission.v2",
        "canonical_sha256": canonical_register_digest(register),
    }
    if targets[binding["opportunity_id"]].get("consumer_contract") != expected_contract:
        raise RegistryError("frozen opportunity does not bind the complete operational ScreenDance register")
    if register.get("schema") != expected_contract["schema"]:
        raise RegistryError("ScreenDance register schema disagrees with its frozen consumer contract")
    return receipt


def validate_all(
    snapshot_path: Path = SNAPSHOT,
    schema_path: Path = SCHEMA,
    receipt_path: Path = RECEIPT,
    consumer_path: Path = CONSUMER,
    evidence_path: Path = EVIDENCE,
    root: Path = ROOT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = validate_registry(
        snapshot_path,
        schema_path,
        root=root,
        evidence_path=evidence_path,
    )
    receipt = validate_binding(
        snapshot,
        root=root,
        snapshot_path=snapshot_path,
        receipt_path=receipt_path,
        consumer_path=consumer_path,
    )
    return snapshot, receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    parser.add_argument("--schema", type=Path, default=SCHEMA)
    parser.add_argument("--receipt", type=Path, default=RECEIPT)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    parser.add_argument("--consumer", type=Path, default=CONSUMER)
    parser.add_argument("--registry-only", action="store_true")
    parser.add_argument(
        "--operational-as-of",
        help="validate the live ranked queue at an ISO-8601 time, or use 'now'",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    try:
        snapshot = validate_registry(
            args.snapshot,
            args.schema,
            root=args.root,
            evidence_path=args.evidence,
        )
        if args.operational_as_of:
            as_of = (
                datetime.now(timezone.utc)
                if args.operational_as_of == "now"
                else parse_time(args.operational_as_of, "operational as-of")
            )
            validate_operational(snapshot, as_of)
        receipt = None
        if not args.registry_only:
            receipt = validate_binding(
                snapshot,
                root=args.root,
                snapshot_path=args.snapshot,
                receipt_path=args.receipt,
                consumer_path=args.consumer,
            )
    except RegistryError as exc:
        print(f"opportunity registry: FAIL — {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        suffix = "registry semantics"
        if receipt is not None:
            suffix += f" + {receipt['snapshot']['sha256']}"
        print(
            "opportunity registry: "
            f"{len(snapshot['opportunities'])} targets, {len(snapshot['ranked_actions'])} ranked actions, "
            f"{suffix} verified"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
