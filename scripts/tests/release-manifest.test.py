#!/usr/bin/env python3
"""Adversarial and reproducibility checks for the Danse release framework."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import marshal
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import release_contract as CONTRACT  # noqa: E402

_browser_spec = importlib.util.spec_from_file_location(
    "danse_release_browser_producer_test", ROOT / "render/browser.py"
)
assert _browser_spec and _browser_spec.loader
BROWSER = importlib.util.module_from_spec(_browser_spec)
_browser_spec.loader.exec_module(BROWSER)

TEST_COMMIT = "a" * 40
FIXTURE_FILES = (
    ".gitignore",
    "LINEAGE.json",
    "README.md",
    "index.html",
    "release/manifest.json",
    "release/manifest.schema.json",
    "release/gate-receipt.schema.json",
    "release/gate-proof.schema.json",
    "release/owner-attestation.schema.json",
    "release/proof-pins.schema.json",
    "release/evidence/proof-pins.json",
    "release/evidence/live-interaction-replay-20260804.json",
    "release/progressive-controls-replay.schema.json",
    "opportunities/omega-20260829.json",
    "opportunities/omega-20260829.receipt.json",
    "opportunities/source-evidence-20260826.json",
    "opportunities/opportunity.schema.json",
    "scripts/check-opportunities.py",
    "scripts/check-release.py",
    "scripts/build-release.py",
    "submission/screendance-2027.yaml",
    "corpus/manifest.json",
    "scripts/check-danse.py",
    "scripts/private_custody.py",
    "scripts/release_contract.py",
    "scripts/rights_contract.py",
    "rights/evidence/delibes-source-license-custody.json",
    "rights/evidence/mediapipe-attribution.json",
    "rights/evidence/mediapipe-distribution.json",
    "rights/register.json",
    "rights/register.schema.json",
    "installation/contract.py",
    "installation/digital-twin.json",
    "installation/gates.json",
    "engine/room.js",
    "render/program.json",
    "render/browser.py",
    "render/deliver.py",
    "music/score.json",
    "sound/room-layout.json",
    "interaction/adapter.js",
    "interaction/vendor/mediapipe/Apache-2.0.txt",
    "interaction/vendor/mediapipe/manifest.json",
    "music/adaptation.json",
    "music/audio-toolchain.json",
    "music/delibes-screendance-suite.mid",
    "music/licenses/MuseScore_General_License.md",
    "music/repertoire.yaml",
    "music/sources/Valse-Coppelia.mscz",
    "music/sources/Valse-Lente-Delibes.mscz",
    "sound/audio-uses.json",
    "submission/text/artist_statement.txt",
    "submission/text/bio.txt",
    "submission/text/rights_declaration.txt",
    "submission/text/synopsis_long.txt",
    "submission/text/synopsis_short.txt",
    "submission/text/technical_note.txt",
    "reference/projection-probe.png",
)


def load_release_builder():
    path = ROOT / "scripts/build-release.py"
    spec = importlib.util.spec_from_file_location("danse_release_builder_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = load_release_builder()


def load_pages_builder():
    path = ROOT / "scripts/build-pages.py"
    spec = importlib.util.spec_from_file_location("danse_release_pages_boundary", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAGES = load_pages_builder()


def commit_git_fixture(root: Path, message: str) -> str:
    subprocess.run(
        ["git", "-C", str(root), "add", "."],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Danse Test",
            "-c",
            "user.email=danse-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            message,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def initialize_git_fixture(root: Path) -> str:
    subprocess.run(
        ["git", "-C", str(root), "init", "-q"],
        check=True,
        capture_output=True,
        text=True,
    )
    return commit_git_fixture(root, "fixture")


def commit_empty_git_fixture(root: Path, message: str) -> str:
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Danse Test",
            "-c",
            "user.email=danse-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-qm",
            message,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class Markup(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.links: list[dict[str, str | None]] = []
        self.metas: list[dict[str, str | None]] = []
        self.scripts = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.by_id[str(values["id"])] = (tag, values)
        if tag == "a":
            self.links.append(values)
        if tag == "meta":
            self.metas.append(values)
        if tag == "script":
            self.scripts += 1


def fixture_root(base: Path) -> Path:
    root = base / "repo"
    for relative in FIXTURE_FILES:
        source = ROOT / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return root


def read_manifest(root: Path = ROOT) -> dict:
    return json.loads((root / "release/manifest.json").read_text(encoding="utf-8"))


def write_manifest(root: Path, manifest: dict) -> None:
    (root / "release/manifest.json").write_bytes(CONTRACT.canonical_json(manifest))


def _release_copy(value):
    """Remove draft-only prose from a synthetic fully evidenced test fixture."""
    if isinstance(value, str):
        replacements = (
            (r"\bdraft\b", "final"),
            (r"\bpending\b", "cleared"),
            (r"\bprovisional\b", "earlier"),
            (r"not for publication", "approved for publication"),
            (r"\bawaits?\b", "uses"),
            (r"\brequire(?:s|d)?\b", "carries"),
        )
        for pattern, replacement in replacements:
            value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
        return value
    if isinstance(value, list):
        return [_release_copy(item) for item in value]
    if isinstance(value, dict):
        return {key: _release_copy(item) for key, item in value.items()}
    return value


def complete_manifest(root: Path, *, phase: str = "release") -> dict:
    if phase not in {"public", "release"}:
        raise ValueError(f"unsupported complete fixture phase: {phase}")
    original = read_manifest(root)
    manifest = _release_copy(original)
    manifest["version"] = "1.0.0"
    manifest["status"] = "released" if phase == "release" else "public-approved"

    evidence_path = root / "release/evidence/public-receipt.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        '{"schema":"danse.release-evidence.v1","result":"satisfied"}\n',
        encoding="utf-8",
    )
    evidence = {
        "path": "release/evidence/public-receipt.json",
        "sha256": CONTRACT.sha256(evidence_path),
        "summary": "Synthetic public-safe evidence fixture.",
    }
    rights = json.loads((root / "rights/register.json").read_text(encoding="utf-8"))
    rights["status"] = "reviewed"
    rights_counter = 0

    def rights_record(identity: str, payload: dict | None = None) -> dict:
        nonlocal rights_counter
        rights_counter += 1
        safe_identity = re.sub(r"[^a-z0-9-]+", "-", identity.lower()).strip("-")
        path = root / f"release/evidence/rights/{rights_counter:03d}-{safe_identity}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            CONTRACT.canonical_json(
                payload
                or {
                    "schema": "danse.synthetic-redacted-rights-receipt.v1",
                    "receipt_id": f"{rights_counter:03d}-{safe_identity}",
                    "result": "satisfied",
                }
            )
        )
        return {
            "path": path.relative_to(root).as_posix(),
            "sha256": CONTRACT.sha256(path),
            "summary": "Synthetic typed redacted rights fixture.",
        }

    clearance_gate_ids = set().union(*CONTRACT.RIGHTS_CLEARANCE_GATES.values())
    for human_gate in rights["human_gates"]:
        if human_gate["id"] in clearance_gate_ids:
            human_gate["state"] = "satisfied"
        else:
            human_gate["state"] = "pending"
        human_gate["evidence"] = None
    for asset in rights["assets"]:
        asset_id = asset["id"]
        if asset["disposition"] == "excluded":
            asset["public_credit"] = {
                "state": "not-required",
                "label": None,
                "note": "Synthetic excluded fixture.",
            }
            asset["private_evidence"] = {
                "state": "not-required",
                "custodian": None,
                "receipt": None,
            }
            asset["blocker"] = None
            for use in asset["uses"]:
                use["status"] = "excluded"
                use["evidence"] = None
            continue
        if asset["disposition"] == "blocked":
            asset["disposition"] = "owned"
            asset["rights_holder"] = asset["rights_holder"] or "Synthetic fixture"
            asset["license"] = None
        if not asset["provenance"]:
            asset["provenance"] = [
                {
                    "path": evidence["path"],
                    "sha256": evidence["sha256"],
                    "summary": "Synthetic public-safe provenance fixture.",
                }
            ]
        asset["blocker"] = None
        asset["public_credit"] = {
            "state": "approved",
            "label": asset["public_credit"]["label"]
            or f"Synthetic credit for {asset_id}",
            "note": "Synthetic approved fixture wording.",
        }
        asset["private_evidence"] = {
            "state": "verified",
            "custodian": "Synthetic fixture",
            "receipt": rights_record(f"private-{asset_id}"),
        }
        for use in asset["uses"]:
            use.pop("conditional_exclusion", None)
            use["status"] = "cleared"
            use["territory"] = (
                "worldwide" if use["territory"] == "pending" else use["territory"]
            )
            use["term"] = (
                "project-duration" if use["term"] == "pending" else use["term"]
            )
            use["promotion"] = (
                "allowed" if use["promotion"] == "pending" else use["promotion"]
            )
            use["archive"] = (
                "allowed" if use["archive"] == "pending" else use["archive"]
            )
            use["evidence"] = rights_record(
                f"use-{asset_id}-{use['id']}",
                {
                    "schema": "danse.rights.use-decision.v1",
                    "asset_id": asset_id,
                    "use_id": use["id"],
                    "authority": asset["rights_holder"],
                    "decision": "cleared",
                    "medium": use["medium"],
                    "required_for": use["required_for"],
                    "territory": use["territory"],
                    "term": use["term"],
                    "expires": use["expires"],
                    "promotion": use["promotion"],
                    "archive": use["archive"],
                },
            )
    assets_by_id = {asset["id"]: asset for asset in rights["assets"]}
    for human_gate in rights["human_gates"]:
        if human_gate["id"] not in clearance_gate_ids:
            continue
        approved_credits = sorted(
            (
                {
                    "asset_id": rule["asset"],
                    "label": assets_by_id[rule["asset"]]["public_credit"]["label"],
                }
                for rule in rights["credit_rules"]
                if rule["gate"] == human_gate["id"]
            ),
            key=lambda row: row["asset_id"],
        )
        attestation = human_gate["attestation"]
        decision = (
            True
            if attestation is None or attestation["kind"] == "boolean"
            else attestation["values"][0]
        )
        human_gate["evidence"] = rights_record(
            f"human-gate-{human_gate['id']}",
            {
                "schema": "danse.rights.decision.v2",
                "gate_id": human_gate["id"],
                "authority": human_gate["authority"],
                "decision": decision,
                "required_for": human_gate["required_for"],
                "approved_credits": approved_credits,
            },
        )
    (root / "rights/register.json").write_bytes(CONTRACT.canonical_json(rights))
    for claim in manifest["claims"]:
        claim["status"] = "verified"
        claim["evidence"] = copy.deepcopy(evidence)
    for index, credit in enumerate(manifest["credits"], start=1):
        credit["status"] = "cleared"
        credit["name"] = credit["name"] or f"Cleared contributor {index}"
        credit["evidence"] = copy.deepcopy(evidence)
    for medium in manifest["media"]:
        source_path = root / f"release/media/{medium['id']}.bin"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(f"synthetic media {medium['id']}\n".encode())
        medium["status"] = "ready"
        medium["source"] = {
            "path": source_path.relative_to(root).as_posix(),
            "sha256": CONTRACT.sha256(source_path),
            "bytes": source_path.stat().st_size,
            "destination": f"media/assets/{medium['id']}.bin",
        }
        medium["clearance"] = {
            "status": "cleared",
            "owner": "Synthetic fixture",
            "evidence": copy.deepcopy(evidence),
        }
        medium["alt_text"] = f"Synthetic accessible description for {medium['label']}."
    for product in manifest["products"]:
        product["status"] = "ready"
    for section in ("spatial_requirements", "technical_rider"):
        for requirement in manifest["installation"][section]:
            requirement["status"] = "verified"

    manifest["press"]["contact"] = {
        "status": "approved",
        "label": "Project contact",
        "url": "https://organvm.github.io/the-thing-without-a-name/project/contact/",
    }
    manifest["accessibility"]["captions"] = {
        "status": "approved",
        "language": "en",
        "label": "English captions",
        "reason": None,
        "cues": [
            {
                "start": "00:00:00.000",
                "end": "00:00:02.000",
                "text": "Ambient room tone; photographic fragments emerge.",
            }
        ],
    }
    manifest["accessibility"]["transcript"] = {
        "status": "approved",
        "text": "No spoken dialogue. Ambient sound and image events are described in the caption track.",
        "reason": None,
    }
    if phase == "public":
        original_media = {item["id"]: item for item in original["media"]}
        for index, medium in enumerate(manifest["media"]):
            if medium["required_for"] == ["release"]:
                manifest["media"][index] = copy.deepcopy(original_media[medium["id"]])
        for gate in manifest["gates"]:
            if gate["required_for"] == ["release"]:
                gate["state"] = "pending"
                gate["evidence"] = None

    # Freeze the release content before minting any gate evidence. Receipts bind
    # this real ancestor commit and its exact tree; the later evidence commit is
    # allowed to carry the immutable receipts without a self-referential SHA.
    write_manifest(root, manifest)
    initialize_git_fixture(root)
    source_head = commit_empty_git_fixture(root, "freeze release source")
    source_tree = subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{source_head}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source_manifest_sha256 = CONTRACT.sha256(root / CONTRACT.MANIFEST)
    observed_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    package_digest = hashlib.sha256(b"synthetic exact package manifest\n").hexdigest()

    pin_records = []
    for gate_index, gate in enumerate(manifest["gates"], start=1):
        if phase == "public" and gate["required_for"] == ["release"]:
            continue
        gate["state"] = "satisfied"
        if gate["id"] == "live-interaction-replay":
            continue

        contract = CONTRACT.RELEASE_GATE_CONTRACTS[gate["id"]]
        subject = {
            "release_id": manifest["release_id"],
            "release_version": manifest["version"],
            "repository_head": source_head,
            "repository_tree": source_tree,
            "release_manifest_sha256": source_manifest_sha256,
            "package_manifest_sha256": package_digest if contract["package"] else None,
        }
        package = CONTRACT._expected_package(subject)
        rows = []
        for proof_index, kind in enumerate(contract["proofs"], start=1):
            proof_path = root / (
                CONTRACT.PROGRESSIVE_CONTROLS_EVIDENCE_PATH
                if kind == "progressive-controls-replay"
                else f"release/evidence/proofs/{gate['id']}-{kind}.json"
            )
            proof_path.parent.mkdir(parents=True, exist_ok=True)
            if kind == "owner-attestation":
                comment_id = 9_000_000_000 + gate_index
                source = {
                    "repository": CONTRACT.RELEASE_REPOSITORY,
                    "issue": gate["issue"],
                    "comment_id": comment_id,
                    "comment_url": (
                        f"https://github.com/{CONTRACT.RELEASE_REPOSITORY}/issues/"
                        f"{gate['issue']}#issuecomment-{comment_id}"
                    ),
                    "comment_author": CONTRACT.RELEASE_OWNER_LOGIN,
                    "comment_created_at": observed_at,
                    "comment_updated_at": observed_at,
                    "comment_body_sha256": CONTRACT._owner_approval_body_sha256(
                        root, gate, subject, manifest
                    ),
                }
                proof = {
                    "schema": CONTRACT.RELEASE_OWNER_ATTESTATION_SCHEMA,
                    "attestation_id": f"{gate['id']}-owner-{gate_index}",
                    "gate_id": gate["id"],
                    "issue": gate["issue"],
                    "kind": kind,
                    "decision": {
                        "claim": contract["affirms"][0],
                        "scope_sha256": CONTRACT._owner_scope_sha256(
                            root, gate, subject, manifest
                        ),
                    },
                    "recorded_at": observed_at,
                    "authority": {
                        "name": CONTRACT.RELEASE_OWNER_NAME,
                        "github_login": CONTRACT.RELEASE_OWNER_LOGIN,
                    },
                    "source": source,
                    "subject": subject,
                    "package": package,
                }
                proof_id = proof["attestation_id"]
                issuer = None
                schema_name = CONTRACT.RELEASE_OWNER_ATTESTATION_SCHEMA
            elif kind == "progressive-controls-replay":
                with mock.patch.object(BROWSER.sys, "platform", "darwin"):
                    proof = BROWSER.build_controls_receipt(
                        source=subject,
                        browser_version="123.0.0.0",
                        renderer=(
                            "ANGLE (Apple, ANGLE Metal Renderer: Apple M5, "
                            "Unspecified Version)"
                        ),
                        screenshots=[],
                        observed_at=observed_at,
                        root=root,
                    )
                generator_sha256 = proof["generator"]["sha256"]
                issuer = {
                    "kind": "tool",
                    "identity": f"render/browser.py@sha256:{generator_sha256}",
                }
                source = None
                proof_id = proof["proof_id"]
                schema_name = CONTRACT.PROGRESSIVE_CONTROLS_SCHEMA
            else:
                source = None
                proof_id = f"{gate['id']}-{kind}-{proof_index}"
                rights_binding = None
                check_receipts = {}
                if kind == "rights-validation":
                    zero_path = (
                        root
                        / f"release/evidence/proofs/{gate['id']}-rights-zero-blockers.json"
                    )
                    register_digest = CONTRACT.sha256(root / "rights/register.json")
                    rights_document = json.loads(
                        (root / "rights/register.json").read_text(encoding="utf-8")
                    )
                    asset_use_ids, redacted_receipt_sha256s, _ = (
                        CONTRACT._rights_register_inventory(
                            root,
                            rights_document,
                            gate_id=gate["id"],
                            source_head=source_head,
                            tracked=CONTRACT._tracked_paths(root),
                            validation_date=(
                                CONTRACT._parse_utc(
                                    observed_at, "synthetic rights observation"
                                )
                                .astimezone(CONTRACT.ZoneInfo("America/New_York"))
                                .date()
                            ),
                        )
                    )
                    rights_generator_digest = CONTRACT.sha256(
                        root / "scripts/rights_contract.py"
                    )
                    rights_checker = CONTRACT._load_rights_checker(root, source_head)
                    rights_receipt = rights_checker.build_clearance_scope_receipt(
                        rights_document,
                        scope=gate["id"],
                        register_path=root / "rights/register.json",
                        schema_path=root / "rights/register.schema.json",
                        root=root,
                        as_of=(
                            CONTRACT._parse_utc(
                                observed_at, "synthetic rights observation"
                            )
                            .astimezone(CONTRACT.ZoneInfo("America/New_York"))
                            .date()
                        ),
                    )
                    if rights_receipt["status"] != "ready":
                        raise AssertionError(rights_receipt["blockers"])
                    zero_path.write_bytes(
                        CONTRACT.canonical_json(
                            {
                                "schema": "danse.release.rights-validation.v1",
                                "gate_id": gate["id"],
                                "issue": gate["issue"],
                                "result": "passed",
                                "subject": subject,
                                "generator": {
                                    "path": "scripts/rights_contract.py",
                                    "sha256": rights_generator_digest,
                                    "receipt_schema": "danse.rights.clearance-receipt.v1",
                                },
                                "receipt": rights_receipt,
                            }
                        )
                    )
                    rights_binding = {
                        "register": {
                            "path": "rights/register.json",
                            "sha256": register_digest,
                        },
                        "zero_blockers": {
                            "path": zero_path.relative_to(root).as_posix(),
                            "sha256": CONTRACT.sha256(zero_path),
                        },
                    }
                    scoped_asset_ids = {
                        identity.split("/", 1)[0] for identity in asset_use_ids
                    }
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
                    check_receipts = {
                        "asset-census": CONTRACT._rights_check_digest(
                            "asset-census",
                            {"gate_id": gate["id"], "asset_use_ids": asset_use_ids},
                        ),
                        "included-use-clearance": CONTRACT._rights_check_digest(
                            "included-use-clearance",
                            {"gate_id": gate["id"], "asset_use_ids": asset_use_ids},
                        ),
                        "private-evidence": CONTRACT._rights_check_digest(
                            "private-evidence",
                            {
                                "gate_id": gate["id"],
                                "redacted_receipt_sha256s": redacted_receipt_sha256s,
                            },
                        ),
                        "credits": CONTRACT._rights_check_digest(
                            "credits",
                            {"gate_id": gate["id"], "credits": credit_inventory},
                        ),
                        "press-stills": CONTRACT._rights_check_digest(
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
                        "zero-blockers": CONTRACT.sha256(zero_path),
                    }
                checks = [
                    {
                        "id": check_id,
                        "result": "passed",
                        "receipt_sha256": (
                            package_digest
                            if kind == "submission-package"
                            and check_id == "package-manifest"
                            else check_receipts[check_id]
                            if check_id in check_receipts
                            else hashlib.sha256(
                                f"{gate['id']}:{kind}:{check_id}".encode()
                            ).hexdigest()
                        ),
                    }
                    for check_id in CONTRACT.RELEASE_PROOF_CHECKS[kind]
                ]
                generator_path = CONTRACT.RELEASE_PROOF_GENERATORS[kind]
                generator_sha256 = CONTRACT.sha256(root / generator_path)
                issuer = {
                    "kind": CONTRACT.RELEASE_PROOF_ISSUER_KINDS[kind],
                    "identity": f"{generator_path}@sha256:{generator_sha256}",
                }
                generator = {
                    "path": generator_path,
                    "sha256": generator_sha256,
                    "version": f"danse-{kind}-proof-v1",
                }
                authority_source = None
                if issuer["kind"] in {"venue", "host"}:
                    authority_login = f"synthetic-{issuer['kind']}-authority"
                    comment_id = 9_100_000_000 + gate_index
                    authority_source = {
                        "repository": CONTRACT.RELEASE_REPOSITORY,
                        "issue": gate["issue"],
                        "comment_id": comment_id,
                        "comment_url": (
                            f"https://github.com/{CONTRACT.RELEASE_REPOSITORY}/issues/"
                            f"{gate['issue']}#issuecomment-{comment_id}"
                        ),
                        "comment_author": authority_login,
                        "comment_created_at": observed_at,
                        "comment_updated_at": observed_at,
                        "comment_body_sha256": (
                            CONTRACT._external_authority_body_sha256(
                                gate,
                                kind,
                                authority_login,
                                subject,
                                checks,
                            )
                        ),
                    }
                    source = authority_source
                proof = {
                    "schema": CONTRACT.RELEASE_GATE_PROOF_SCHEMA,
                    "proof_id": proof_id,
                    "gate_id": gate["id"],
                    "issue": gate["issue"],
                    "kind": kind,
                    "result": "passed",
                    "observed_at": observed_at,
                    "issuer": issuer,
                    "generator": generator,
                    "authority_source": authority_source,
                    "subject": subject,
                    "package": package,
                    "rights_binding": rights_binding,
                    "checks": checks,
                }
                schema_name = CONTRACT.RELEASE_GATE_PROOF_SCHEMA
            proof_path.write_bytes(CONTRACT.canonical_json(proof))
            record = {
                "path": proof_path.relative_to(root).as_posix(),
                "sha256": CONTRACT.sha256(proof_path),
                "schema": schema_name,
            }
            pin_records.append(
                {
                    "gate_id": gate["id"],
                    "issue": gate["issue"],
                    "kind": kind,
                    "proof_id": proof_id,
                    "receipt": record,
                    "issuer": issuer,
                    "source": source,
                }
            )
            rows.append(
                {
                    "id": f"{kind}-{proof_index}",
                    "kind": kind,
                    "receipt": record,
                }
            )

        receipt_path = root / f"release/evidence/{gate['id']}-receipt.json"
        receipt_path.write_bytes(
            CONTRACT.canonical_json(
                {
                    "schema": CONTRACT.RELEASE_GATE_RECEIPT_SCHEMA,
                    "gate_id": gate["id"],
                    "issue": gate["issue"],
                    "result": "satisfied",
                    "recorded_at": observed_at,
                    "subject": subject,
                    "evidence": rows,
                    "affirms": list(contract["affirms"]),
                    "does_not_affirm": sorted(
                        set(CONTRACT.RELEASE_HIGH_RISK_CLAIMS)
                        - set(contract["affirms"])
                    ),
                }
            )
        )
        gate["evidence"] = {
            "path": receipt_path.relative_to(root).as_posix(),
            "sha256": CONTRACT.sha256(receipt_path),
            "summary": CONTRACT.gate_receipt_summary(gate["id"]),
        }

    pin_path = root / CONTRACT.RELEASE_PROOF_PINS_PATH
    pin_path.write_bytes(
        CONTRACT.canonical_json(
            {
                "schema": CONTRACT.RELEASE_PROOF_PINS_SCHEMA,
                "source": {
                    "release_id": manifest["release_id"],
                    "release_version": manifest["version"],
                    "repository_head": source_head,
                    "repository_tree": source_tree,
                    "release_manifest_sha256": source_manifest_sha256,
                },
                "records": sorted(
                    pin_records, key=lambda item: (item["gate_id"], item["kind"])
                ),
            }
        )
    )
    write_manifest(root, manifest)
    commit_git_fixture(root, "pinned release fixture")
    return manifest


def rewrite_progressive_fixture(root: Path, manifest: dict, mutate) -> None:
    gate = next(
        gate
        for gate in manifest["gates"]
        if gate["id"] == "progressive-controls-replay"
    )
    outer_path = root / gate["evidence"]["path"]
    outer = json.loads(outer_path.read_text(encoding="utf-8"))
    row = outer["evidence"][0]
    proof_path = root / row["receipt"]["path"]
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    mutate(proof)
    proof_path.write_bytes(CONTRACT.canonical_json(proof))
    digest = CONTRACT.sha256(proof_path)
    row["receipt"]["sha256"] = digest
    ledger_path = root / CONTRACT.RELEASE_PROOF_PINS_PATH
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    pin = next(
        record
        for record in ledger["records"]
        if record["gate_id"] == "progressive-controls-replay"
        and record["kind"] == "progressive-controls-replay"
    )
    pin["receipt"]["sha256"] = digest
    ledger_path.write_bytes(CONTRACT.canonical_json(ledger))
    outer_path.write_bytes(CONTRACT.canonical_json(outer))
    gate["evidence"]["sha256"] = CONTRACT.sha256(outer_path)
    write_manifest(root, manifest)


class ProductionManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = CONTRACT.validate_release(ROOT, phase="draft")
        cls.temporary = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temporary.name) / "release"
        cls.receipt = BUILD.build(ROOT, cls.output, "draft", TEST_COMMIT)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_public_urls_have_https_backstops_without_optional_format_plugins(self) -> None:
        schema = json.loads(
            (ROOT / "release/manifest.schema.json").read_text(encoding="utf-8")
        )
        mutations = (
            lambda manifest: manifest["press"]["contact"].update(
                {"url": "http://example.invalid/contact"}
            ),
            lambda manifest: manifest["press"]["canonical_links"][0].update(
                {"url": "http://example.invalid/"}
            ),
            lambda manifest: manifest["press"]["seed_sharing"].update(
                {"example_url": "http://example.invalid/#s=1"}
            ),
        )
        jsonschema = CONTRACT._load_jsonschema(ROOT)
        validator = jsonschema.Draft202012Validator(schema)
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                manifest = copy.deepcopy(self.manifest)
                mutate(manifest)
                errors = list(validator.iter_errors(manifest))
                self.assertTrue(
                    any(error.validator == "pattern" for error in errors),
                    [error.message for error in errors],
                )

    def test_release_validation_workflows_fetch_the_bound_commit_history(self) -> None:
        for relative in (".github/workflows/ci.yml", ".github/workflows/pages.yml"):
            with self.subTest(workflow=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("uses: actions/checkout@v6", text)
                self.assertEqual(
                    text.count("uses: actions/checkout@v6"),
                    text.count("fetch-depth: 0"),
                )

    def test_snapshot_binding_uses_final_merged_freeze_and_source_evidence(self) -> None:
        binding = self.manifest["opportunity_snapshot"]
        self.assertEqual(binding["sha256"], CONTRACT.EXPECTED_OPPORTUNITY_SHA256)
        self.assertEqual(binding["snapshot_id"], "omega-20260829")
        self.assertEqual(binding["frozen_at"], CONTRACT.EXPECTED_OPPORTUNITY_FROZEN_AT)
        self.assertEqual(
            binding["source_evidence_sha256"],
            CONTRACT.EXPECTED_SOURCE_EVIDENCE_SHA256,
        )
        snapshot = json.loads((ROOT / binding["path"]).read_text())
        screendance = next(item for item in snapshot["opportunities"] if item["id"] == "screendance-miami-2027")
        self.assertEqual(screendance["consumer_contract"]["schema"], "danse.submission.v2")
        self.assertEqual(
            screendance["consumer_contract"]["canonical_sha256"],
            "0c99bad6604b7061b3a404a02980e81df66422147ee51ac768dbe5fc2f3d0a14",
        )

    def test_installation_binding_consumes_reference_contract_without_clearing_gates(self) -> None:
        binding = self.manifest["installation"]["reference_contract"]
        ledger = json.loads((ROOT / binding["gate_ledger"]["path"]).read_text())
        self.assertEqual(binding["status"], "reference-only")
        self.assertEqual(
            binding["spec_contract_sha256"],
            "35cebf541a80788bce08586ada4299cbef77fffe1ebdbb4633f357214ecc9c66",
        )
        self.assertFalse(binding["physical_predicates_satisfied"])
        self.assertFalse(binding["issue_14_can_close"])
        self.assertEqual(binding["blocked_gates"], [gate["id"] for gate in ledger["gates"]])
        self.assertTrue(all(gate["status"] == "blocked" and gate["receipt"] is None for gate in ledger["gates"]))
        release_gate = next(gate for gate in self.manifest["gates"] if gate["id"] == "installation-evidence")
        self.assertEqual(release_gate["state"], "pending")
        self.assertIsNone(release_gate["evidence"])
        self.assertEqual(self.receipt["release"]["installation_reference"], binding)

    def test_custody_contract_is_bound_without_claiming_a_restore_or_cleanup_authority(self) -> None:
        claim = next(
            claim
            for claim in self.manifest["claims"]
            if claim["id"] == "private-custody-contract"
        )
        self.assertEqual(claim["status"], "verified")
        self.assertEqual(claim["evidence"]["path"], "scripts/private_custody.py")
        self.assertEqual(
            claim["evidence"]["sha256"],
            CONTRACT.sha256(ROOT / "scripts/private_custody.py"),
        )
        gate = next(
            gate for gate in self.manifest["gates"] if gate["id"] == "release-custody"
        )
        self.assertEqual(gate["state"], "pending")
        self.assertIsNone(gate["evidence"])

    def test_live_interaction_replay_is_bound_without_clearing_publication(self) -> None:
        gate = next(
            gate
            for gate in self.manifest["gates"]
            if gate["id"] == "live-interaction-replay"
        )
        self.assertEqual(gate["state"], "satisfied")
        self.assertEqual(
            gate["evidence"]["path"],
            CONTRACT.LIVE_INTERACTION_EVIDENCE_PATH,
        )
        receipt = CONTRACT.load_json(
            ROOT / gate["evidence"]["path"],
            "live interaction replay receipt",
        )
        CONTRACT.validate_live_interaction_receipt(ROOT / gate["evidence"]["path"])
        self.assertEqual(
            receipt["deployment"]["source_commit"],
            CONTRACT.LIVE_INTERACTION_DEPLOYED_COMMIT,
        )
        self.assertTrue(all(check["result"] == "passed" for check in receipt["checks"]))
        publication = next(
            gate
            for gate in self.manifest["gates"]
            if gate["id"] == "publication-approval"
        )
        self.assertEqual(publication["state"], "pending")
        self.assertIsNone(publication["evidence"])

    def test_progressive_controls_gate_has_a_distinct_fail_closed_receipt_contract(self) -> None:
        gate = next(
            gate
            for gate in self.manifest["gates"]
            if gate["id"] == "progressive-controls-replay"
        )
        self.assertEqual(gate["state"], "pending")
        self.assertIsNone(gate["evidence"])
        self.assertFalse((ROOT / CONTRACT.PROGRESSIVE_CONTROLS_EVIDENCE_PATH).exists())
        schema = CONTRACT.load_json(
            ROOT / CONTRACT.PROGRESSIVE_CONTROLS_SCHEMA_PATH,
            "progressive controls replay schema",
        )
        CONTRACT._load_jsonschema(ROOT).Draft202012Validator.check_schema(schema)
        self.assertEqual(
            schema["properties"]["gate_id"]["const"],
            "progressive-controls-replay",
        )
        self.assertEqual(
            schema["properties"]["checks"]["minItems"],
            len(CONTRACT.PROGRESSIVE_CONTROLS_CHECKS),
        )

    def test_tracked_manifest_is_honest_draft_but_public_and_release_fail_closed(self) -> None:
        public = CONTRACT.phase_blockers(self.manifest, "public")
        release = CONTRACT.phase_blockers(self.manifest, "release")
        self.assertGreaterEqual(len(public), 30)
        self.assertGreater(len(release), len(public))
        for phase in ("public", "release"):
            with self.assertRaisesRegex(CONTRACT.ReleaseError, f"{phase} phase blocked"):
                CONTRACT.validate_release(ROOT, phase=phase)
            target = Path(self.temporary.name) / f"blocked-{phase}"
            with self.assertRaisesRegex(CONTRACT.ReleaseError, f"{phase} phase blocked"):
                BUILD.build(ROOT, target, phase, TEST_COMMIT)
            self.assertFalse(target.exists(), "a blocked phase must fail before writing any byte")

    def test_draft_outputs_are_complete_local_artifacts_not_public_claims(self) -> None:
        paths = {record["path"] for record in self.receipt["files"]}
        self.assertEqual(paths, set(BUILD.GENERATED_PATHS))
        self.assertFalse(any(path.startswith("media/assets/") for path in paths))
        self.assertEqual(set(self.receipt["toolchain"]), {"python", "pypdf", "reportlab"})
        self.assertTrue(all(self.receipt["toolchain"].values()))
        self.assertEqual(self.receipt["release"]["manifest"]["path"], "release/manifest.json")
        self.assertEqual(
            self.receipt["release"]["opportunity_snapshot"]["path"],
            "opportunities/omega-20260829.json",
        )
        self.assertEqual(
            self.receipt["release"]["source_evidence"]["sha256"],
            CONTRACT.EXPECTED_SOURCE_EVIDENCE_SHA256,
        )
        project = (self.output / "project/index.html").read_text(encoding="utf-8")
        self.assertIn('name="robots" content="noindex,nofollow"', project)
        self.assertIn("Draft - not for publication", project)
        self.assertIn("@media (prefers-reduced-motion:reduce)", project)
        self.assertIn("viewport-fit=cover", project)
        self.assertNotIn("<script", project)
        self.assertNotIn("sound is not scored to the image", project.lower())
        reference = self.manifest["installation"]["reference_contract"]
        self.assertIn(reference["spec_id"], project)
        self.assertIn(reference["spec_contract_sha256"], project)
        self.assertIn("8 gates remain blocked", project)

    def test_project_markup_is_semantic_and_keeps_the_artwork_at_root(self) -> None:
        markup = Markup()
        markup.feed((self.output / "project/index.html").read_text(encoding="utf-8"))
        self.assertIn("content", markup.by_id)
        self.assertIn("access", markup.by_id)
        self.assertIn("evidence", markup.by_id)
        self.assertEqual(markup.scripts, 0)
        hrefs = {link.get("href") for link in markup.links}
        self.assertIn("../", hrefs)
        self.assertIn("#access", hrefs)
        self.assertIn("#evidence", hrefs)
        robots = [meta for meta in markup.metas if meta.get("name") == "robots"]
        self.assertEqual(robots[0]["content"], "noindex,nofollow")

    def test_project_resources_are_accessible_receipted_artifact_links(self) -> None:
        markup = Markup()
        markup.feed((self.output / "project/index.html").read_text(encoding="utf-8"))
        self.assertIn("resources", markup.by_id)
        hrefs = {link.get("href") for link in markup.links}
        products = {product["id"]: product for product in self.manifest["products"]}
        expected = {
            f"../{products[product_id]['path']}"
            for product_id, _label in BUILD.PROJECT_RESOURCES
        }
        self.assertTrue(expected <= hrefs)
        for href in expected:
            target = (self.output / "project" / href).resolve()
            self.assertTrue(target.is_relative_to(self.output.resolve()))
            self.assertTrue(target.is_file(), href)
        BUILD.verify_project_links(
            self.output,
            {record["path"] for record in self.receipt["files"]},
        )

    def test_pdf_is_deterministic_structured_and_visibly_draft(self) -> None:
        path = self.output / BUILD.PDF_NAME
        reader = PdfReader(str(path))
        self.assertGreaterEqual(len(reader.pages), 5)
        self.assertEqual(reader.metadata.title, "THE THING WITHOUT A NAME")
        self.assertFalse(reader.is_encrypted)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("DRAFT - NOT FOR PUBLICATION", text)
        self.assertIn("System flow", text)
        normalized = " ".join(text.split())
        for node in self.manifest["installation"]["system_flow"]:
            self.assertIn(" ".join(node["detail"].split()), normalized)
        self.assertIn("Reference installation contract", text)
        self.assertIn(self.manifest["installation"]["reference_contract"]["spec_id"], text)
        self.assertIn("Required evidence before publication", text)

    def test_pdf_paginates_tall_paragraphs_and_diagrams_without_losing_tail_text(self) -> None:
        pdf = BUILD.PitchPDF(self.manifest, "draft", TEST_COMMIT)
        pdf.new_page("Pagination stress")
        paragraph = " ".join(
            [*(f"word{index:04d}" for index in range(900)), "paragraph-tail"]
        )
        pdf.paragraph(paragraph)
        nodes = [
            {
                "label": f"Stress node {index:02d}",
                "detail": "Short deterministic diagram detail.",
            }
            for index in range(24)
        ]
        pdf.diagram(nodes)
        reader = PdfReader(io.BytesIO(pdf.finish()))
        extracted = " ".join(
            "\n".join(page.extract_text() or "" for page in reader.pages).split()
        )
        self.assertGreaterEqual(len(reader.pages), 5)
        self.assertIn("paragraph-tail", extracted)
        self.assertIn("Stress node 23", extracted)

    def test_accessibility_press_credit_and_media_outputs_come_from_manifest(self) -> None:
        access = (self.output / "accessibility/accessibility.md").read_text()
        captions = (self.output / "accessibility/captions.en.vtt").read_text()
        transcript = (self.output / "accessibility/transcript.txt").read_text()
        press = (self.output / "press/press-kit.md").read_text()
        credits = (self.output / "press/credits.txt").read_text()
        inventory = json.loads((self.output / "media/release-media.json").read_text())
        calendar = json.loads((self.output / "press/posting-calendar.json").read_text())
        self.assertIn(self.manifest["accessibility"]["alt_text"], access)
        self.assertTrue(captions.startswith("WEBVTT\n"))
        self.assertIn(self.manifest["accessibility"]["transcript"]["text"], transcript)
        self.assertIn(self.manifest["press"]["synopsis_short"], press)
        self.assertIn(self.manifest["credits"][0]["role"], credits)
        self.assertEqual(len(inventory["media"]), len(self.manifest["media"]))
        self.assertTrue(all(item["released"] is None for item in inventory["media"]))
        self.assertEqual(len(inventory["products"]), len(self.manifest["products"]))
        for product in inventory["products"]:
            artifact = product["artifact"]
            generated = self.output / artifact["path"]
            self.assertEqual(artifact["bytes"], generated.stat().st_size)
            self.assertEqual(artifact["sha256"], CONTRACT.sha256(generated))
        self.assertFalse(calendar["publishes_automatically"])

    def test_pages_allowlist_still_excludes_project_and_release_surfaces(self) -> None:
        pages = set(PAGES.source_files(ROOT))
        self.assertFalse(any(path.startswith("project/") for path in pages))
        self.assertFalse(any(path.startswith("release/") for path in pages))
        self.assertNotIn("scripts/build-release.py", pages)


class DeterminismAndCompletedPhaseTest(unittest.TestCase):
    def test_two_draft_builds_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = BUILD.build(ROOT, base / "one", "draft", TEST_COMMIT)
            second = BUILD.build(ROOT, base / "two", "draft", TEST_COMMIT)
            self.assertEqual(first, second)
            for record in first["files"]:
                relative = record["path"]
                self.assertEqual((base / "one" / relative).read_bytes(), (base / "two" / relative).read_bytes())
            self.assertEqual(
                (base / "one" / BUILD.ARTIFACT_MANIFEST).read_bytes(),
                (base / "two" / BUILD.ARTIFACT_MANIFEST).read_bytes(),
            )

    def test_synthetic_operational_hashes_cannot_build_public_or_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            complete_manifest(root)
            for phase in ("public", "release"):
                with self.subTest(phase=phase), self.assertRaisesRegex(
                    CONTRACT.ReleaseError,
                    "no trusted external authenticity verifier",
                ) as raised:
                    BUILD.build(root, base / f"{phase}-artifact", phase, TEST_COMMIT)
                if phase == "release":
                    for gate_kind in (
                        "accessibility-review:accessibility-review",
                        "actual-presentation:presentation-lifecycle",
                        "final-cut-evidence-gate:submission-package",
                        "final-cut-evidence-gate:submission-validation",
                        "installation-evidence:installation-completion",
                        "press-stills-clearance:rights-validation-external-authority",
                        "progressive-controls-replay:progressive-controls-replay-external-authenticity",
                        "release-custody:custody-completion",
                        "restore-rehearsal:restore-completion",
                        "rights-register:rights-validation-external-authority",
                    ):
                        self.assertIn(gate_kind, str(raised.exception))
                self.assertFalse((base / f"{phase}-artifact").exists())

    def test_media_source_swap_after_validation_fails_without_an_artifact_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            manifest = complete_manifest(root)
            medium = manifest["media"][0]
            source = medium["source"]
            source_path = root / source["path"]
            replacement = b"x" * source["bytes"]
            original_source_file = BUILD.source_file
            swapped = False

            def replace_after_validation(check_root, relative, label):
                nonlocal swapped
                path = original_source_file(check_root, relative, label)
                if relative == source["path"] and not swapped:
                    source_path.write_bytes(replacement)
                    swapped = True
                return path

            output = base / "artifact"
            with (
                mock.patch.object(
                    BUILD, "validate_release", return_value=manifest
                ),
                mock.patch.object(
                    BUILD, "source_file", side_effect=replace_after_validation
                ),
            ):
                with self.assertRaisesRegex(CONTRACT.ReleaseError, "changed after manifest validation"):
                    BUILD.build(root, output, "release", TEST_COMMIT)
            self.assertTrue(swapped)
            self.assertFalse((output / BUILD.ARTIFACT_MANIFEST).exists())
            self.assertFalse((output / source["destination"]).exists())

    def test_public_authenticity_blockers_exclude_release_only_lifecycle_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            complete_manifest(root, phase="public")

            with self.assertRaises(CONTRACT.ReleaseError) as raised:
                CONTRACT.validate_release(root, phase="public")
            message = str(raised.exception)
            self.assertIn("accessibility-review:accessibility-review", message)
            self.assertIn("final-cut-evidence-gate:submission-package", message)
            self.assertIn("installation-evidence:installation-completion", message)
            self.assertNotIn("actual-presentation", message)
            self.assertNotIn("release-custody", message)
            self.assertNotIn("restore-rehearsal", message)


class ProductionCliSourceTest(unittest.TestCase):
    def test_cli_accepts_only_the_clean_exact_git_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            (root / "tracked-sentinel.txt").write_text("clean\n", encoding="utf-8")
            commit = initialize_git_fixture(root)
            command = [
                sys.executable,
                str(ROOT / "scripts/build-release.py"),
                "--root",
                str(root),
                "--phase",
                "draft",
                "--source-commit",
                commit,
            ]

            clean_output = base / "clean-artifact"
            clean = subprocess.run(
                [*command, "--output", str(clean_output)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertTrue((clean_output / BUILD.ARTIFACT_MANIFEST).is_file())

            wrong_output = base / "wrong-artifact"
            wrong = subprocess.run(
                [
                    *command[:-1],
                    "b" * 40,
                    "--output",
                    str(wrong_output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(wrong.returncode, 0)
            self.assertIn("does not match checkout HEAD", wrong.stderr)
            self.assertFalse(wrong_output.exists())

            (root / "tracked-sentinel.txt").write_text("dirty\n", encoding="utf-8")
            dirty_output = base / "dirty-artifact"
            dirty = subprocess.run(
                [*command, "--output", str(dirty_output)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(dirty.returncode, 0)
            self.assertIn("tracked changes", dirty.stderr)
            self.assertFalse(dirty_output.exists())

            (root / "tracked-sentinel.txt").write_text("clean\n", encoding="utf-8")
            (root / "untracked-source.txt").write_text("not in the source commit\n", encoding="utf-8")
            untracked_output = base / "untracked-artifact"
            untracked = subprocess.run(
                [*command, "--output", str(untracked_output)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(untracked.returncode, 0)
            self.assertIn("untracked files", untracked.stderr)
            self.assertFalse(untracked_output.exists())


class AdversarialManifestTest(unittest.TestCase):
    def mutate(self, callback) -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = fixture_root(Path(temporary.name))
        manifest = read_manifest(root)
        callback(manifest)
        write_manifest(root, manifest)
        return temporary, root

    def test_unknown_manifest_key_fails_schema(self) -> None:
        temporary, root = self.mutate(lambda manifest: manifest.update({"surprise": True}))
        with temporary:
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "schema failure"):
                CONTRACT.validate_release(root)

    def test_superseded_opportunity_digest_fails(self) -> None:
        def change(manifest):
            manifest["opportunity_snapshot"]["sha256"] = "0" * 64

        temporary, root = self.mutate(change)
        with temporary:
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "reviewed frozen opportunity digest"):
                CONTRACT.validate_release(root)

    def test_verified_claim_digest_drift_fails(self) -> None:
        def change(manifest):
            manifest["claims"][0]["evidence"]["sha256"] = "f" * 64

        temporary, root = self.mutate(change)
        with temporary:
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "digest mismatch"):
                CONTRACT.validate_release(root)

    def test_installation_contract_digest_drift_fails(self) -> None:
        def change(manifest):
            manifest["installation"]["reference_contract"]["digital_twin"]["sha256"] = "f" * 64

        temporary, root = self.mutate(change)
        with temporary:
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "installation digital twin digest mismatch"):
                CONTRACT.validate_release(root)

    def test_fake_satisfied_gate_without_evidence_fails(self) -> None:
        def change(manifest):
            manifest["gates"][0]["state"] = "satisfied"

        temporary, root = self.mutate(change)
        with temporary:
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "satisfied gate .* has no evidence"):
                CONTRACT.validate_release(root)

    def test_arbitrary_digest_bound_file_cannot_satisfy_a_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            gate = next(
                gate
                for gate in manifest["gates"]
                if gate["id"] == "release-custody"
            )
            arbitrary = root / "release/evidence/public-receipt.json"
            gate["evidence"] = {
                "path": arbitrary.relative_to(root).as_posix(),
                "sha256": CONTRACT.sha256(arbitrary),
                "summary": CONTRACT.gate_receipt_summary(gate["id"]),
            }
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "canonical receipt path"):
                CONTRACT.validate_release(root)

    def test_release_gate_receipt_binds_gate_owner_subject_and_required_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            gate = next(
                gate
                for gate in manifest["gates"]
                if gate["id"] == "final-cut-evidence-gate"
            )
            receipt_path = root / gate["evidence"]["path"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["evidence"] = [
                row
                for row in receipt["evidence"]
                if row["kind"] != "submission-validation"
            ]
            receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
            gate["evidence"]["sha256"] = CONTRACT.sha256(receipt_path)
            write_manifest(root, manifest)
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError,
                "lacks its exact proof inventory",
            ):
                CONTRACT.validate_release(root)

    def test_release_gate_receipt_rejects_free_text_evidence_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            gate = next(
                gate
                for gate in manifest["gates"]
                if gate["id"] == "actual-presentation"
            )
            receipt_path = root / gate["evidence"]["path"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["evidence"][0]["summary"] = "Private source at /Users/operator/presentation.mov"
            receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
            gate["evidence"]["sha256"] = CONTRACT.sha256(receipt_path)
            write_manifest(root, manifest)
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError,
                "schema failure",
            ):
                CONTRACT.validate_release(root)

    def test_completed_live_interaction_gate_cannot_regress(self) -> None:
        def change(manifest):
            gate = next(
                gate
                for gate in manifest["gates"]
                if gate["id"] == "live-interaction-replay"
            )
            gate["state"] = "pending"
            gate["evidence"] = None

        temporary, root = self.mutate(change)
        with temporary:
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "cannot regress"):
                CONTRACT.validate_release(root)

    def test_rehashed_live_interaction_deployment_substitution_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            receipt_path = root / CONTRACT.LIVE_INTERACTION_EVIDENCE_PATH
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["deployment"]["source_commit"] = "b" * 40
            receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
            manifest = read_manifest(root)
            gate = next(
                gate
                for gate in manifest["gates"]
                if gate["id"] == "live-interaction-replay"
            )
            gate["evidence"]["sha256"] = CONTRACT.sha256(receipt_path)
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "deployed source identity drifted"):
                CONTRACT.validate_release(root)

    def test_rehashed_live_interaction_check_without_id_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            receipt_path = root / CONTRACT.LIVE_INTERACTION_EVIDENCE_PATH
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["checks"][0].pop("id")
            receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
            manifest = read_manifest(root)
            gate = next(
                gate
                for gate in manifest["gates"]
                if gate["id"] == "live-interaction-replay"
            )
            gate["evidence"]["sha256"] = CONTRACT.sha256(receipt_path)
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "check inventory drifted"):
                CONTRACT.validate_release(root)

    def test_progressive_controls_gate_rejects_a_generic_digested_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            gate = next(
                gate
                for gate in manifest["gates"]
                if gate["id"] == "progressive-controls-replay"
            )
            generic = root / "release/evidence/public-receipt.json"
            gate["evidence"] = {
                "path": "release/evidence/public-receipt.json",
                "sha256": CONTRACT.sha256(generic),
                "summary": CONTRACT.gate_receipt_summary(gate["id"]),
            }
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "canonical receipt path"):
                CONTRACT.validate_release(root)

    def test_progressive_controls_gate_rejects_a_claiming_evidence_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            gate = next(
                gate
                for gate in manifest["gates"]
                if gate["id"] == "progressive-controls-replay"
            )
            gate["evidence"]["summary"] = (
                "All final-cut, rights, package, upload, and submission gates are approved."
            )
            write_manifest(root, manifest)
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "evidence summary is not the exact neutral template"
            ):
                CONTRACT.validate_release(root)

    def test_rehashed_progressive_controls_receipt_with_missing_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            rewrite_progressive_fixture(
                root,
                manifest,
                lambda receipt: receipt["checks"].pop(),
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "violates schema"):
                CONTRACT.validate_release(root)

    def test_rehashed_progressive_controls_receipt_with_blank_digest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            rewrite_progressive_fixture(
                root,
                manifest,
                lambda receipt: receipt["checks"][0].update(
                    {"receipt_sha256": " \t\n"}
                ),
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "violates schema"):
                CONTRACT.validate_release(root)

    def test_progressive_controls_receipt_binds_its_source_generator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            rewrite_progressive_fixture(
                root,
                manifest,
                lambda receipt: receipt["generator"].update({"sha256": "f" * 64}),
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "generator"):
                CONTRACT.validate_release(root)

    def test_progressive_controls_receipt_rejects_a_different_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            complete_manifest(root)
            (root / "reviewed-tree-drift.txt").write_text(
                "different source tree\n", encoding="utf-8"
            )
            commit_git_fixture(root, "different reviewed tree")
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError,
                "source drift is not limited to its exact tracked evidence envelope",
            ):
                CONTRACT.validate_release(root)

    def test_progressive_controls_receipt_rejects_a_deleted_reviewed_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            complete_manifest(root)
            (root / ".gitignore").unlink()
            commit_git_fixture(root, "delete reviewed source")
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError,
                "source drift is not limited to its exact tracked evidence envelope",
            ):
                CONTRACT.validate_release(root)

    def test_progressive_controls_receipt_validates_before_unrelated_terminal_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            complete_manifest(root)
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "no trusted external authenticity verifier"
            ) as raised:
                CONTRACT.validate_release(root)
            self.assertIn(
                "progressive-controls-replay:progressive-controls-replay-external-authenticity",
                str(raised.exception),
            )

    def test_duplicate_ids_fail(self) -> None:
        def change(manifest):
            manifest["media"][1]["id"] = manifest["media"][0]["id"]

        temporary, root = self.mutate(change)
        with temporary:
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "media ids must be unique"):
                CONTRACT.validate_release(root)

    def test_media_path_escape_fails(self) -> None:
        def change(manifest):
            manifest["media"][0]["source"] = {
                "path": "../private/still.png",
                "sha256": "0" * 64,
                "destination": "media/assets/still.png",
            }

        temporary, root = self.mutate(change)
        with temporary:
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "schema failure"):
                CONTRACT.validate_release(root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_evidence_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            outside = base / "outside.json"
            shutil.copyfile(root / "corpus/manifest.json", outside)
            (root / "corpus/manifest.json").unlink()
            (root / "corpus/manifest.json").symlink_to(outside)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "traverses a symlink"):
                CONTRACT.validate_release(root)

    def test_approved_empty_caption_track_fails_public(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            manifest["accessibility"]["captions"]["cues"] = []
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "manifest changed outside gate state"):
                CONTRACT.validate_release(root, phase="public")

    def test_caption_cue_must_have_forward_timing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            manifest["accessibility"]["captions"]["cues"][0]["end"] = "00:00:00.000"
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "manifest changed outside gate state"):
                CONTRACT.validate_release(root, phase="public")

    def test_media_destinations_must_be_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            manifest["media"][1]["source"]["destination"] = manifest["media"][0]["source"]["destination"]
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "media destination is not unique"):
                CONTRACT.validate_release(root, phase="public")

    def test_media_byte_count_drift_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            manifest["media"][0]["source"]["bytes"] += 1
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "byte count mismatch"):
                CONTRACT.validate_release(root, phase="public")


class AdversarialReleaseGateReceiptHardeningTest(unittest.TestCase):
    """Keep terminal gate receipts fail-closed under hostile recomposition."""

    @staticmethod
    def _gate(manifest: dict, gate_id: str) -> dict:
        return next(gate for gate in manifest["gates"] if gate["id"] == gate_id)

    def _outer_receipt(
        self, root: Path, manifest: dict, gate_id: str
    ) -> tuple[Path, dict]:
        gate = self._gate(manifest, gate_id)
        path = root / gate["evidence"]["path"]
        return path, json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _pin_ledger(root: Path) -> dict:
        return json.loads(
            (root / CONTRACT.RELEASE_PROOF_PINS_PATH).read_text(encoding="utf-8")
        )

    def _pin(self, root: Path, gate_id: str, kind: str) -> dict:
        return next(
            record
            for record in self._pin_ledger(root)["records"]
            if record["gate_id"] == gate_id and record["kind"] == kind
        )

    @staticmethod
    def _write_pin_ledger(root: Path, ledger: dict) -> None:
        (root / CONTRACT.RELEASE_PROOF_PINS_PATH).write_bytes(
            CONTRACT.canonical_json(ledger)
        )

    def _mutate_pin(self, root: Path, gate_id: str, kind: str, mutate) -> None:
        ledger = self._pin_ledger(root)
        pin = next(
            record
            for record in ledger["records"]
            if record["gate_id"] == gate_id and record["kind"] == kind
        )
        mutate(pin)
        ledger["records"].sort(key=lambda item: (item["gate_id"], item["kind"]))
        self._write_pin_ledger(root, ledger)

    def _write_outer_receipt(
        self,
        root: Path,
        manifest: dict,
        gate_id: str,
        path: Path,
        receipt: dict,
    ) -> None:
        path.write_bytes(CONTRACT.canonical_json(receipt))
        self._gate(manifest, gate_id)["evidence"]["sha256"] = CONTRACT.sha256(path)
        write_manifest(root, manifest)

    def _rewrite_proof(
        self,
        root: Path,
        manifest: dict,
        gate_id: str,
        kind: str,
        mutate,
        *,
        repin_source: bool = False,
        repin_issuer: bool = False,
    ) -> dict:
        outer_path, outer = self._outer_receipt(root, manifest, gate_id)
        row = next(item for item in outer["evidence"] if item["kind"] == kind)
        proof_path = root / row["receipt"]["path"]
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        mutate(proof)
        proof_path.write_bytes(CONTRACT.canonical_json(proof))
        digest = CONTRACT.sha256(proof_path)
        row["receipt"]["sha256"] = digest
        def update_pin(pin: dict) -> None:
            pin["receipt"]["sha256"] = digest
            if repin_source:
                pin["source"] = copy.deepcopy(
                    proof.get("source", proof.get("authority_source"))
                )
            if repin_issuer:
                pin["issuer"] = copy.deepcopy(proof["issuer"])

        self._mutate_pin(root, gate_id, kind, update_pin)
        self._write_outer_receipt(root, manifest, gate_id, outer_path, outer)
        return proof

    def _rewrite_rights_validation_receipt(
        self,
        root: Path,
        manifest: dict,
        gate_id: str,
        mutate,
    ) -> dict:
        """Rewrite one subordinate rights receipt and every enclosing digest."""

        outer_path, outer = self._outer_receipt(root, manifest, gate_id)
        row = next(item for item in outer["evidence"] if item["kind"] == "rights-validation")
        proof_path = root / row["receipt"]["path"]
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        record = proof["rights_binding"]["zero_blockers"]
        receipt_path = root / record["path"]
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        mutate(receipt)
        receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
        receipt_digest = CONTRACT.sha256(receipt_path)
        record["sha256"] = receipt_digest
        next(
            check for check in proof["checks"] if check["id"] == "zero-blockers"
        )["receipt_sha256"] = receipt_digest
        proof_path.write_bytes(CONTRACT.canonical_json(proof))
        proof_digest = CONTRACT.sha256(proof_path)
        row["receipt"]["sha256"] = proof_digest
        self._mutate_pin(
            root,
            gate_id,
            "rights-validation",
            lambda pin: pin["receipt"].update({"sha256": proof_digest}),
        )
        self._write_outer_receipt(root, manifest, gate_id, outer_path, outer)
        commit_git_fixture(root, "rewrite subordinate rights validation receipt")
        return receipt

    def _rewrite_outer_subject_and_proofs(
        self,
        root: Path,
        manifest: dict,
        gate_id: str,
        mutate,
    ) -> None:
        outer_path, outer = self._outer_receipt(root, manifest, gate_id)
        mutate(outer["subject"])
        expected_package = CONTRACT._expected_package(outer["subject"])
        for row in outer["evidence"]:
            proof_path = root / row["receipt"]["path"]
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["subject"] = copy.deepcopy(outer["subject"])
            proof["package"] = copy.deepcopy(expected_package)
            if row["kind"] == "owner-attestation":
                proof["decision"]["scope_sha256"] = CONTRACT._owner_scope_sha256(
                    root,
                    self._gate(manifest, gate_id),
                    outer["subject"],
                    manifest,
                )
                proof["source"]["comment_body_sha256"] = (
                    CONTRACT._owner_approval_body_sha256(
                        root,
                        self._gate(manifest, gate_id),
                        outer["subject"],
                        manifest,
                    )
                )
            proof_path.write_bytes(CONTRACT.canonical_json(proof))
            digest = CONTRACT.sha256(proof_path)
            row["receipt"]["sha256"] = digest
            def update_pin(pin: dict, value=digest, rewritten=proof) -> None:
                pin["receipt"]["sha256"] = value
                if row["kind"] == "owner-attestation":
                    pin["source"] = copy.deepcopy(rewritten["source"])

            self._mutate_pin(root, gate_id, row["kind"], update_pin)
        self._write_outer_receipt(root, manifest, gate_id, outer_path, outer)

    def _rewrite_progressive(self, root: Path, manifest: dict, mutate) -> None:
        rewrite_progressive_fixture(root, manifest, mutate)

    def test_terminal_gate_inventory_and_phase_ownership_cannot_be_removed(
        self,
    ) -> None:
        for case in ("delete-gates", "reroute-release-gates"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                if case == "delete-gates":
                    manifest["gates"] = [
                        gate
                        for gate in manifest["gates"]
                        if gate["id"] == "live-interaction-replay"
                    ]
                    manifest["accessibility"]["review_gate"] = (
                        "live-interaction-replay"
                    )
                    for section in ("spatial_requirements", "technical_rider"):
                        for requirement in manifest["installation"][section]:
                            requirement["evidence_gate"] = "live-interaction-replay"
                    expected = "inventory|ordered terminal gate"
                else:
                    for gate in manifest["gates"]:
                        if gate["id"] == "live-interaction-replay":
                            continue
                        gate["required_for"] = ["public"]
                        gate["state"] = "pending"
                        gate["evidence"] = None
                    ledger_path = root / CONTRACT.RELEASE_PROOF_PINS_PATH
                    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                    ledger["records"] = []
                    ledger_path.write_bytes(CONTRACT.canonical_json(ledger))
                    expected = "required.*phase rout"
                write_manifest(root, manifest)
                commit_git_fixture(root, f"attempt to bypass terminal gates: {case}")
                with self.assertRaisesRegex(CONTRACT.ReleaseError, expected):
                    CONTRACT.validate_release(root, phase="release")

    def test_terminal_gate_order_and_identity_are_exact(self) -> None:
        expected = list(CONTRACT.RELEASE_GATE_CONTRACTS)
        self.assertEqual(
            [gate["id"] for gate in read_manifest()["gates"]],
            expected,
        )
        for case in ("reorder", "substitute"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = read_manifest(root)
                if case == "reorder":
                    manifest["gates"][0], manifest["gates"][1] = (
                        manifest["gates"][1],
                        manifest["gates"][0],
                    )
                else:
                    manifest["gates"][0]["id"] = "substituted-terminal-gate"
                write_manifest(root, manifest)
                with self.assertRaisesRegex(
                    CONTRACT.ReleaseError,
                    "inventory|ordered terminal gate",
                ):
                    CONTRACT.validate_release(root, phase="draft")

    def test_every_terminal_gate_has_an_exact_contract_owned_phase_route(
        self,
    ) -> None:
        manifest = read_manifest()
        self.assertEqual(
            {
                gate["id"]: tuple(gate["required_for"])
                for gate in manifest["gates"]
            },
            CONTRACT.RELEASE_GATE_REQUIRED_PHASES,
        )
        routes = (("public",), ("release",), ("public", "release"))
        for gate_id, required_for in CONTRACT.RELEASE_GATE_REQUIRED_PHASES.items():
            with self.subTest(gate_id=gate_id):
                changed = copy.deepcopy(manifest)
                gate = self._gate(changed, gate_id)
                gate["required_for"] = list(
                    next(route for route in routes if route != required_for)
                )
                with self.assertRaisesRegex(
                    CONTRACT.ReleaseError,
                    "required.*phase rout",
                ):
                    CONTRACT.phase_blockers(changed, "draft")

    def test_component_gate_routes_are_contract_owned(self) -> None:
        cases = ("accessibility", "installation-spatial", "installation-technical")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                if case == "accessibility":
                    manifest["accessibility"]["review_gate"] = (
                        "final-artistic-approval"
                    )
                    expected = "accessibility review.*canonical review gate"
                else:
                    section = (
                        "spatial_requirements"
                        if case == "installation-spatial"
                        else "technical_rider"
                    )
                    manifest["installation"][section][0]["evidence_gate"] = (
                        "accessibility-review"
                    )
                    expected = "canonical installation evidence gate"
                write_manifest(root, manifest)
                commit_git_fixture(root, f"attempt component gate reroute: {case}")
                with self.assertRaisesRegex(CONTRACT.ReleaseError, expected):
                    CONTRACT.validate_release(root, phase="release")

    def test_depth_two_clone_cannot_validate_terminal_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = fixture_root(base / "source")
            complete_manifest(source)
            clone = base / "depth-two"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "-q",
                    "--depth=2",
                    source.resolve().as_uri(),
                    str(clone),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            shallow = subprocess.run(
                ["git", "-C", str(clone), "rev-parse", "--is-shallow-repository"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(shallow, "true")
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "shallow repositories"):
                CONTRACT.validate_release(clone, phase="release")

    def test_hidden_index_flags_cannot_substitute_consumed_worktree_bytes(
        self,
    ) -> None:
        cases = (
            (
                "assume-unchanged-schema",
                "--assume-unchanged",
                "release/gate-proof.schema.json",
            ),
            (
                "skip-worktree-installation",
                "--skip-worktree",
                "installation/contract.py",
            ),
        )
        for case, flag, relative in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = fixture_root(base)
                initialize_git_fixture(root)
                marker = base / f"{case}-executed"
                subprocess.run(
                    ["git", "-C", str(root), "update-index", flag, "--", relative],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                target = root / relative
                if relative.endswith(".py"):
                    target.write_text(
                        target.read_text(encoding="utf-8")
                        + "\nPath("
                        + repr(str(marker))
                        + ").write_text('executed', encoding='utf-8')\n",
                        encoding="utf-8",
                    )
                else:
                    target.write_text(
                        target.read_text(encoding="utf-8") + "\n",
                        encoding="utf-8",
                    )
                status = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root),
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                self.assertEqual(status, "")
                with self.assertRaisesRegex(
                    CONTRACT.ReleaseError,
                    "skip-worktree or assume-unchanged index flags",
                ):
                    CONTRACT.validate_release(root, phase="draft")
                self.assertFalse(marker.exists())

    def test_hidden_rights_checker_cannot_execute_before_index_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            complete_manifest(root)
            marker = base / "hidden-rights-checker-executed"
            relative = "scripts/rights_contract.py"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "update-index",
                    "--skip-worktree",
                    "--",
                    relative,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            checker = root / relative
            checker.write_text(
                checker.read_text(encoding="utf-8")
                + "\nPath("
                + repr(str(marker))
                + ").write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root),
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "",
            )
            program = """
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts"))
import release_contract

try:
    release_contract.validate_release(root, phase="release")
except release_contract.ReleaseError as exc:
    print(exc)
    raise SystemExit(0 if "index flags" in str(exc) else 2)
raise SystemExit(3)
"""
            result = subprocess.run(
                [sys.executable, "-c", program, str(root)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("index flags", result.stdout)
            self.assertFalse(marker.exists())

    def test_repository_config_cannot_redirect_or_execute_provenance_git(
        self,
    ) -> None:
        self.assertEqual(CONTRACT._git_environment()["GIT_NO_LAZY_FETCH"], "1")
        cases = (
            "core-worktree",
            "core-fsmonitor",
            "core-hooks-path",
            "filter-clean",
            "worktree-fsmonitor",
            "promisor-remote",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = fixture_root(base)
                initialize_git_fixture(root)
                marker = base / f"{case}-executed"
                probe = base / f"{case}-probe.py"
                probe.write_text(
                    "#!/usr/bin/env python3\n"
                    "from pathlib import Path\n"
                    f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
                    encoding="utf-8",
                )
                probe.chmod(0o755)
                if case == "core-worktree":
                    alternate = base / "alternate-worktree"
                    alternate.mkdir()
                    config = ("--local", "core.worktree", str(alternate))
                elif case == "core-fsmonitor":
                    config = ("--local", "core.fsmonitor", str(probe))
                elif case == "core-hooks-path":
                    hooks = base / "hooks"
                    hooks.mkdir()
                    hook = hooks / "post-checkout"
                    shutil.copyfile(probe, hook)
                    hook.chmod(0o755)
                    config = ("--local", "core.hooksPath", str(hooks))
                elif case == "filter-clean":
                    config = ("--local", "filter.release.clean", str(probe))
                elif case == "worktree-fsmonitor":
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(root),
                            "config",
                            "--local",
                            "extensions.worktreeConfig",
                            "true",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    config = ("--worktree", "core.fsmonitor", str(probe))
                else:
                    config = ("--local", "remote.origin.promisor", "true")
                subprocess.run(
                    ["git", "-C", str(root), "config", *config],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                with self.assertRaisesRegex(
                    CONTRACT.ReleaseError,
                    "repository-local Git config",
                ):
                    CONTRACT.validate_release(root, phase="draft")
                self.assertFalse(marker.exists())

    def test_normal_linked_worktree_passes_early_git_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = fixture_root(base / "source")
            initialize_git_fixture(source)
            linked = base / "linked"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "worktree",
                    "add",
                    "--detach",
                    "-q",
                    str(linked),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            validated = CONTRACT.validate_release(linked, phase="draft")
            self.assertEqual(validated["release_id"], read_manifest(source)["release_id"])

    def test_ignored_cached_rights_bytecode_is_never_executed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            manifest = complete_manifest(root)
            _, outer = self._outer_receipt(root, manifest, "rights-register")
            source_head = outer["subject"]["repository_head"]
            marker = base / "cached-rights-bytecode-executed"
            rights_path = root / "scripts/rights_contract.py"
            malicious = compile(
                "from pathlib import Path\n"
                + f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
                str(rights_path),
                "exec",
            )
            stat_result = rights_path.stat()
            bytecode_path = Path(importlib.util.cache_from_source(str(rights_path)))
            bytecode_path.parent.mkdir(parents=True, exist_ok=True)
            bytecode_path.write_bytes(
                importlib.util.MAGIC_NUMBER
                + struct.pack(
                    "<III",
                    0,
                    int(stat_result.st_mtime) & 0xFFFFFFFF,
                    stat_result.st_size & 0xFFFFFFFF,
                )
                + marshal.dumps(malicious)
            )
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root),
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "",
            )
            program = """
import sys
from pathlib import Path

root = Path(sys.argv[1])
source_head = sys.argv[2]
sys.path.insert(0, str(root / "scripts"))
import release_contract

checker = release_contract._load_rights_checker(root, source_head)
assert checker.__loader__ is None
assert callable(checker.build_clearance_scope_receipt)
"""
            result = subprocess.run(
                [sys.executable, "-c", program, str(root), source_head],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(marker.exists())

    def test_excludes_file_cannot_hide_an_import_shadow_before_rights_guard(
        self,
    ) -> None:
        for relative in (
            "scripts/argparse.py",
            "scripts/hashlib.py",
            "scripts/jsonschema.py",
            "scripts/subprocess.py",
            "scripts/yaml.py",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = fixture_root(base)
                complete_manifest(root)
                marker = base / f"ignored-{Path(relative).stem}-shadow-executed"
                excludes = base / "release-excludes"
                excludes.write_text(relative + "\n", encoding="utf-8")
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root),
                        "config",
                        "--local",
                        "core.excludesFile",
                        str(excludes),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                shadow = root / relative
                shadow.write_text(
                    "from pathlib import Path\n"
                    + f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(root),
                            "status",
                            "--porcelain=v1",
                            "--untracked-files=all",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout,
                    "",
                )
                program = """
import sys
from pathlib import Path

root = Path(sys.argv[1])
scripts_path = Path(sys.argv[2])
sys.path.insert(0, str(scripts_path))
import release_contract

try:
    release_contract.validate_release(root, phase="release")
except release_contract.ReleaseError as exc:
    print(exc)
    raise SystemExit(0 if "repository-local Git config" in str(exc) else 2)
raise SystemExit(3)
"""
                result = subprocess.run(
                    [sys.executable, "-c", program, str(root), str(root / "scripts")],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("repository-local Git config", result.stdout)
                self.assertFalse(marker.exists())

                if relative == "scripts/hashlib.py":
                    alias = base / "repository-alias"
                    alias.symlink_to(root, target_is_directory=True)
                    for scripts_path in (
                        root / "scripts/../scripts",
                        alias / "scripts",
                    ):
                        with self.subTest(scripts_path=str(scripts_path)):
                            aliased = subprocess.run(
                                [
                                    sys.executable,
                                    "-c",
                                    program,
                                    str(root),
                                    str(scripts_path),
                                ],
                                check=False,
                                capture_output=True,
                                text=True,
                            )
                            self.assertEqual(
                                aliased.returncode,
                                0,
                                aliased.stdout + aliased.stderr,
                            )
                            self.assertIn(
                                "repository-local Git config",
                                aliased.stdout,
                            )
                            self.assertFalse(marker.exists())

                entrypoint: list[str] | None = None
                if relative == "scripts/argparse.py":
                    entrypoint = [
                        sys.executable,
                        str(root / "scripts/check-release.py"),
                        "--root",
                        str(root),
                        "--phase",
                        "draft",
                    ]
                elif relative == "scripts/hashlib.py":
                    entrypoint = [
                        sys.executable,
                        str(root / "scripts/build-release.py"),
                        "--root",
                        str(root),
                        "--phase",
                        "draft",
                        "--output",
                        str(base / "release-output"),
                    ]
                if entrypoint is not None:
                    launched = subprocess.run(
                        entrypoint,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(launched.returncode, 0)
                    self.assertIn(
                        "repository-local Git config",
                        launched.stdout + launched.stderr,
                    )
                    self.assertFalse(marker.exists())

    def test_target_checkout_import_roots_are_removed_before_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base / "target")
            source_head = initialize_git_fixture(root)
            entry_marker = base / "target-hashlib-shadow-executed"
            rights_marker = base / "target-yaml-shadow-executed"
            excludes = root / ".git/info/exclude"
            excludes.write_text(
                excludes.read_text(encoding="utf-8")
                + "scripts/hashlib.py\n"
                + "scripts/yaml.py\n",
                encoding="utf-8",
            )
            (root / "scripts/hashlib.py").write_text(
                "from pathlib import Path\n"
                + f"Path({str(entry_marker)!r}).write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            (root / "scripts/yaml.py").write_text(
                "from pathlib import Path\n"
                + f"Path({str(rights_marker)!r}).write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root),
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "",
            )

            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join(
                filter(
                    None,
                    (str(root / "scripts"), environment.get("PYTHONPATH", "")),
                )
            )
            entrypoints = (
                ("check", ROOT / "scripts/check-release.py"),
                ("build", ROOT / "scripts/build-release.py"),
            )
            accepted_root_forms = (
                ("explicit-equals", [f"--root={root}"], None),
                ("empty-equals", ["--root="], root),
                ("empty-value", ["--root", ""], root),
            )
            for entrypoint_name, entrypoint in entrypoints:
                for form, root_arguments, cwd in accepted_root_forms:
                    with self.subTest(entrypoint=entrypoint_name, form=form):
                        command = [
                            sys.executable,
                            str(entrypoint),
                            *root_arguments,
                            "--phase",
                            "draft",
                        ]
                        if entrypoint_name == "build":
                            command.extend(
                                [
                                    "--output",
                                    str(
                                        base
                                        / f"cross-checkout-{entrypoint_name}-{form}"
                                    ),
                                ]
                            )
                        launched = subprocess.run(
                            command,
                            cwd=cwd,
                            check=False,
                            capture_output=True,
                            text=True,
                            env=environment,
                        )
                        self.assertEqual(
                            launched.returncode,
                            0,
                            launched.stdout + launched.stderr,
                        )
                        self.assertFalse(entry_marker.exists())
                        self.assertFalse(rights_marker.exists())

                abbreviated_root_forms = (
                    ("separate", ["--roo", str(root)]),
                    ("equals", [f"--roo={root}"]),
                )
                for form, root_arguments in abbreviated_root_forms:
                    with self.subTest(
                        entrypoint=entrypoint_name,
                        form=f"abbreviated-{form}",
                    ):
                        command = [
                            sys.executable,
                            str(entrypoint),
                            *root_arguments,
                            "--phase",
                            "draft",
                        ]
                        if entrypoint_name == "build":
                            command.extend(
                                [
                                    "--output",
                                    str(
                                        base
                                        / f"abbreviated-{entrypoint_name}-{form}"
                                    ),
                                ]
                            )
                        rejected = subprocess.run(
                            command,
                            check=False,
                            capture_output=True,
                            text=True,
                            env=environment,
                        )
                        self.assertNotEqual(rejected.returncode, 0)
                        self.assertIn(
                            "unrecognized arguments",
                            rejected.stdout + rejected.stderr,
                        )
                        self.assertFalse(entry_marker.exists())
                        self.assertFalse(rights_marker.exists())

            program = """
import sys
from pathlib import Path

root = Path(sys.argv[1])
trusted_scripts = Path(sys.argv[2])
source_head = sys.argv[3]
sys.path.insert(0, str(trusted_scripts))
import release_contract
sys.path.insert(0, str(root / "scripts"))

checker = release_contract._load_rights_checker(root, source_head)
assert callable(checker.build_clearance_scope_receipt)
"""
            rights_loaded = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    program,
                    str(root),
                    str(ROOT / "scripts"),
                    source_head,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                rights_loaded.returncode,
                0,
                rights_loaded.stdout + rights_loaded.stderr,
            )
            self.assertFalse(rights_marker.exists())

    def test_active_repository_venv_dependency_path_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            venv = root / ".venv"
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv)],
                check=True,
                capture_output=True,
                text=True,
            )
            venv_python = (
                venv / "Scripts/python.exe"
                if os.name == "nt"
                else venv / "bin/python"
            )
            site_packages = Path(
                subprocess.run(
                    [
                        str(venv_python),
                        "-c",
                        "import site; print(site.getsitepackages()[0])",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env={**os.environ, "PYTHONPATH": ""},
                ).stdout.strip()
            )
            (site_packages / "jsonschema.py").write_text(
                "SENTINEL = 'active-repository-venv'\n",
                encoding="utf-8",
            )
            program = """
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts"))
import release_contract

module = release_contract._load_jsonschema(root)
assert module.SENTINEL == "active-repository-venv"
assert any(
    Path(entry).resolve().is_relative_to(Path(sys.prefix).resolve())
    for entry in sys.path
    if entry
)
"""
            loaded = subprocess.run(
                [str(venv_python), "-c", program, str(root)],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": ""},
            )
            self.assertEqual(loaded.returncode, 0, loaded.stdout + loaded.stderr)

    def test_executable_checker_modules_require_exact_tracked_head_bytes(
        self,
    ) -> None:
        for relative in (
            "installation/contract.py",
            "scripts/check-opportunities.py",
        ):
            for mode in ("dirty-tracked", "ignored-untracked"):
                with (
                    self.subTest(relative=relative, mode=mode),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    base = Path(temporary)
                    root = fixture_root(base)
                    initialize_git_fixture(root)
                    marker = base / f"{Path(relative).stem}-{mode}-executed"
                    target = root / relative
                    if mode == "ignored-untracked":
                        exclude = root / ".git/info/exclude"
                        exclude.write_text(
                            exclude.read_text(encoding="utf-8") + relative + "\n",
                            encoding="utf-8",
                        )
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(root),
                                "rm",
                                "--cached",
                                "--",
                                relative,
                            ],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(root),
                                "commit",
                                "-q",
                                "-m",
                                f"remove {relative} from tracked source",
                            ],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                    target.write_text(
                        target.read_text(encoding="utf-8")
                        + "\nPath("
                        + repr(str(marker))
                        + ").write_text('executed', encoding='utf-8')\n",
                        encoding="utf-8",
                    )
                    expected = (
                        "not tracked at the checkout head"
                        if mode == "ignored-untracked"
                        else "differs from its exact tracked HEAD bytes"
                    )
                    with self.assertRaisesRegex(CONTRACT.ReleaseError, expected):
                        CONTRACT.validate_release(root, phase="draft")
                    self.assertFalse(marker.exists())

    def test_rights_checker_must_remain_tracked_at_current_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            source_head = initialize_git_fixture(root)
            relative = "scripts/rights_contract.py"
            marker = base / "removed-rights-checker-executed"
            excludes = root / ".git/info/exclude"
            excludes.write_text(
                excludes.read_text(encoding="utf-8") + relative + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(root), "rm", "--cached", "--", relative],
                check=True,
                capture_output=True,
                text=True,
            )
            commit_git_fixture(root, "remove current rights checker")
            target = root / relative
            target.write_text(
                target.read_text(encoding="utf-8")
                + "\nPath("
                + repr(str(marker))
                + ").write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError,
                "rights checker is not tracked at the checkout head",
            ):
                CONTRACT._load_rights_checker(root, source_head)
            self.assertFalse(marker.exists())

    def test_gate_owner_pin_and_local_typed_proof_cannot_be_self_asserted(self) -> None:
        cases = (
            "manifest-owner",
            "missing-reviewed-pin",
            "recorded-by-field",
            "fake-urn-record",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                gate_id = "final-artistic-approval"
                if case == "manifest-owner":
                    self._gate(manifest, gate_id)["owner"] = "Any caller"
                    write_manifest(root, manifest)
                    expected = "owner or issue drifted"
                elif case == "missing-reviewed-pin":
                    ledger = self._pin_ledger(root)
                    ledger["records"] = [
                        record
                        for record in ledger["records"]
                        if (record["gate_id"], record["kind"])
                        != (gate_id, "owner-attestation")
                    ]
                    self._write_pin_ledger(root, ledger)
                    expected = "no matching review-required proof pin"
                elif case == "recorded-by-field":
                    self._rewrite_proof(
                        root,
                        manifest,
                        gate_id,
                        "owner-attestation",
                        lambda proof: proof.update({"recorded_by": "self-appointed"}),
                    )
                    expected = "schema failure"
                else:
                    outer_path, outer = self._outer_receipt(root, manifest, gate_id)
                    outer["evidence"][0]["receipt"]["path"] = (
                        "urn:sha256:" + outer["evidence"][0]["receipt"]["sha256"]
                    )
                    self._write_outer_receipt(
                        root, manifest, gate_id, outer_path, outer
                    )
                    expected = "schema failure"
                with self.assertRaisesRegex(CONTRACT.ReleaseError, expected):
                    CONTRACT.validate_release(root)

    def test_outer_receipt_cannot_overwrite_or_authorize_a_contract_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            gate_id = "contact-route-approval"
            outer_path, outer = self._outer_receipt(root, manifest, gate_id)
            schema_path = root / "release/gate-receipt.schema.json"
            schema_path.write_bytes(CONTRACT.canonical_json(outer))
            gate = self._gate(manifest, gate_id)
            gate["evidence"] = {
                "path": "release/gate-receipt.schema.json",
                "sha256": CONTRACT.sha256(schema_path),
                "summary": CONTRACT.gate_receipt_summary(gate_id),
            }
            outer_path.unlink()
            write_manifest(root, manifest)
            commit_git_fixture(root, "attempt receipt and schema path collision")
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "canonical receipt path"):
                CONTRACT.validate_release(root)

    def test_cross_gate_proof_path_identity_and_source_digest_reuse_fail(self) -> None:
        cases = ("path", "identity", "source-digest", "owner-source-digest")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                if case == "path":
                    _, source = self._outer_receipt(
                        root, manifest, "contact-route-approval"
                    )
                    target_path, target = self._outer_receipt(
                        root, manifest, "publication-approval"
                    )
                    source_record = copy.deepcopy(source["evidence"][0]["receipt"])
                    target["evidence"][0]["receipt"] = source_record
                    self._mutate_pin(
                        root,
                        "publication-approval",
                        "owner-attestation",
                        lambda pin: pin.update(
                            {"receipt": copy.deepcopy(source_record)}
                        ),
                    )
                    self._write_outer_receipt(
                        root,
                        manifest,
                        "publication-approval",
                        target_path,
                        target,
                    )
                    expected = "reuses a proof path or digest"
                elif case == "identity":
                    _, source = self._outer_receipt(root, manifest, "rights-register")
                    source_proof_path = root / source["evidence"][0]["receipt"]["path"]
                    source_proof = json.loads(
                        source_proof_path.read_text(encoding="utf-8")
                    )
                    self._rewrite_proof(
                        root,
                        manifest,
                        "release-custody",
                        "custody-completion",
                        lambda proof: proof.update(
                            {"proof_id": source_proof["proof_id"]}
                        ),
                    )
                    self._mutate_pin(
                        root,
                        "release-custody",
                        "custody-completion",
                        lambda pin: pin.update(
                            {"proof_id": source_proof["proof_id"]}
                        ),
                    )
                    expected = "reuses a proof identity"
                elif case == "source-digest":
                    _, source = self._outer_receipt(root, manifest, "rights-register")
                    source_proof_path = root / source["evidence"][0]["receipt"]["path"]
                    source_proof = json.loads(
                        source_proof_path.read_text(encoding="utf-8")
                    )
                    reused_digest = source_proof["checks"][0]["receipt_sha256"]
                    self._rewrite_proof(
                        root,
                        manifest,
                        "release-custody",
                        "custody-completion",
                        lambda proof: proof["checks"][0].update(
                            {"receipt_sha256": reused_digest}
                        ),
                    )
                    expected = "reuses a source receipt"
                else:
                    _, source = self._outer_receipt(
                        root, manifest, "final-artistic-approval"
                    )
                    source_proof_path = root / source["evidence"][0]["receipt"]["path"]
                    source_proof = json.loads(
                        source_proof_path.read_text(encoding="utf-8")
                    )
                    reused_digest = source_proof["source"]["comment_body_sha256"]
                    self._rewrite_proof(
                        root,
                        manifest,
                        "contact-route-approval",
                        "owner-attestation",
                        lambda proof: proof["source"].update(
                            {"comment_body_sha256": reused_digest}
                        ),
                        repin_source=True,
                    )
                    expected = "canonical owner approval payload"
                with self.assertRaisesRegex(CONTRACT.ReleaseError, expected):
                    CONTRACT.validate_release(root)

    def test_repository_subject_rejects_unavailable_nonancestor_wrong_tree_and_time(
        self,
    ) -> None:
        cases = ("unavailable", "nonancestor", "wrong-tree", "before-commit", "future")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                gate_id = "final-artistic-approval"
                outer_path, outer = self._outer_receipt(root, manifest, gate_id)
                if case == "unavailable":
                    outer["subject"]["repository_head"] = "f" * 40
                    expected = "unavailable Git object or relationship|raw Git object integrity"
                elif case == "nonancestor":
                    tree = subprocess.run(
                        ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                    environment = {
                        **os.environ,
                        "GIT_AUTHOR_NAME": "Danse Test",
                        "GIT_AUTHOR_EMAIL": "danse-test@example.invalid",
                        "GIT_COMMITTER_NAME": "Danse Test",
                        "GIT_COMMITTER_EMAIL": "danse-test@example.invalid",
                    }
                    side = subprocess.run(
                        ["git", "-C", str(root), "commit-tree", tree, "-m", "side"],
                        check=True,
                        capture_output=True,
                        text=True,
                        env=environment,
                    ).stdout.strip()
                    outer["subject"]["repository_head"] = side
                    outer["subject"]["repository_tree"] = tree
                    expected = "unavailable Git object or relationship"
                elif case == "wrong-tree":
                    outer["subject"]["repository_tree"] = "f" * 40
                    expected = "tree disagrees with its head"
                elif case == "before-commit":
                    outer["recorded_at"] = "2000-01-01T00:00:00Z"
                    expected = "predates its source commit"
                else:
                    outer["recorded_at"] = "2999-01-01T00:00:00Z"
                    expected = "is in the future"
                self._write_outer_receipt(root, manifest, gate_id, outer_path, outer)
                with self.assertRaisesRegex(CONTRACT.ReleaseError, expected):
                    CONTRACT.validate_release(root)

    def test_git_replace_cannot_spoof_a_frozen_source_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            _, outer = self._outer_receipt(
                root, manifest, "final-artistic-approval"
            )
            source_head = outer["subject"]["repository_head"]
            evidence_head = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(root), "replace", source_head, evidence_head],
                check=True,
                capture_output=True,
                text=True,
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "replacement refs"):
                CONTRACT.validate_release(root)

    def test_tampered_loose_git_object_cannot_spoof_a_reachable_source_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            _, outer = self._outer_receipt(
                root, manifest, "final-artistic-approval"
            )
            source_head = outer["subject"]["repository_head"]
            blob = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "rev-parse",
                    f"{source_head}:README.md",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            loose = root / ".git/objects" / blob[:2] / blob[2:]
            raw = zlib.decompress(loose.read_bytes())
            header, payload = raw.split(b"\0", 1)
            self.assertTrue(payload)
            replacement = bytes([payload[0] ^ 1]) + payload[1:]
            loose.chmod(0o600)
            loose.write_bytes(zlib.compress(header + b"\0" + replacement))

            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "raw Git object integrity"
            ):
                CONTRACT.validate_release(root)

    def test_historical_rights_checker_is_never_executed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            marker = base / "historical-checker-executed"
            historical_checker = root / "scripts/rights_contract.py"
            historical_checker.write_text(
                historical_checker.read_text(encoding="utf-8")
                + "\nPath("
                + json.dumps(str(marker))
                + ").write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            source_head = initialize_git_fixture(root)

            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "trusted current verifier"
            ):
                CONTRACT._load_rights_checker(root, source_head)
            self.assertFalse(marker.exists())

    def test_ambient_git_redirect_cannot_hide_a_dirty_release_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base / "source")
            complete_manifest(root)
            alternate = base / "alternate-clean-clone"
            subprocess.run(
                ["git", "clone", "-q", str(root), str(alternate)],
                check=True,
                capture_output=True,
                text=True,
            )
            hidden = root / "release/evidence/ambient-redirect-bypass.json"
            hidden.write_text('{"attempt":"hide dirty checkout"}\n', encoding="utf-8")
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "GIT_DIR": str(alternate / ".git"),
                        "GIT_WORK_TREE": str(alternate),
                        "GIT_INDEX_FILE": str(alternate / ".git/index"),
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(
                    CONTRACT.ReleaseError, "clean committed checkout"
                ),
            ):
                CONTRACT.validate_release(root)

    def test_one_gate_cannot_substitute_a_different_package_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            self._rewrite_outer_subject_and_proofs(
                root,
                manifest,
                "final-artistic-approval",
                lambda subject: subject.update({"package_manifest_sha256": "0" * 64}),
            )
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "package.*(?:binding|digest)"
            ):
                CONTRACT.validate_release(root)

    def test_owner_comment_url_rejects_encoding_query_userinfo_and_loopback_aliases(
        self,
    ) -> None:
        suffix = "/organvm/the-thing-without-a-name/issues/10#issuecomment-9000000001"
        urls = {
            "encoded": "https://github.com/%6Frganvm/the-thing-without-a-name/issues/10#issuecomment-9000000001",
            "query": "https://github.com/organvm/the-thing-without-a-name/issues/10?token=value#issuecomment-9000000001",
            "userinfo": "https://operator@github.com" + suffix,
            "loopback": "https://127.0.0.1" + suffix,
            "loopback-alias": "https://github.com.nip.io" + suffix,
        }
        for case, url in urls.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                self._rewrite_proof(
                    root,
                    manifest,
                    "final-artistic-approval",
                    "owner-attestation",
                    lambda proof, value=url: proof["source"].update(
                        {"comment_url": value}
                    ),
                    repin_source=True,
                )
                with self.assertRaisesRegex(
                    CONTRACT.ReleaseError,
                    "schema failure|encoded or malformed|immutable GitHub comment form|private contact or path data or credentials",
                ):
                    CONTRACT.validate_release(root)

    def test_owner_source_time_must_follow_the_bound_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            self._rewrite_proof(
                root,
                manifest,
                "final-artistic-approval",
                "owner-attestation",
                lambda proof: proof["source"].update(
                    {
                        "comment_created_at": "2000-01-01T00:00:00Z",
                        "comment_updated_at": "2000-01-01T00:00:00Z",
                    }
                ),
                repin_source=True,
            )
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "predates its source commit"
            ):
                CONTRACT.validate_release(root)

    def test_receipt_rejects_descendant_source_drift_outside_evidence_envelope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            complete_manifest(root)
            (root / "reviewed-source-drift.txt").write_text(
                "not part of the reviewed source\n",
                encoding="utf-8",
            )
            commit_git_fixture(root, "unreviewed source drift")
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError,
                "source drift is not limited to its exact tracked evidence envelope",
            ):
                CONTRACT.validate_release(root)

    def test_progressive_receipt_has_no_free_text_observation_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            self._rewrite_progressive(
                root,
                manifest,
                lambda receipt: receipt["checks"][0].update(
                    {"observation": "Rights cleared; final cut and upload complete."}
                ),
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "violates schema"):
                CONTRACT.validate_release(root)

    def test_browser_producer_receipt_passes_the_release_digest_validator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            gate = self._gate(manifest, "progressive-controls-replay")
            proof_path = root / CONTRACT.gate_proof_path(
                gate["id"], "progressive-controls-replay"
            )
            receipt = json.loads(proof_path.read_text(encoding="utf-8"))
            _, committed_at = CONTRACT.git_commit_identity(
                root, receipt["subject"]["repository_head"]
            )

            proof_id, *_ = CONTRACT.validate_progressive_controls_receipt(
                root,
                proof_path,
                gate,
                receipt["subject"],
                committed_at,
                self._pin(root, gate["id"], "progressive-controls-replay"),
            )
            self.assertEqual(proof_id, "progressive-controls-exact-head-replay")

    def test_progressive_source_rejects_unavailable_wrong_tree_and_future_time(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            self._rewrite_progressive(
                root,
                manifest,
                lambda receipt: receipt.update(
                    {"observed_at": "2999-01-01T00:00:00Z"}
                ),
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "is in the future"):
                CONTRACT.validate_release(root)

    def test_progressive_runtime_cannot_carry_renderer_claim_prose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            self._rewrite_progressive(
                root,
                manifest,
                lambda receipt: receipt["runtime"]["graphics"].update(
                    {"tier": "Final Cut Approved Rights Cleared"}
                ),
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "violates schema"):
                CONTRACT.validate_release(root)

    def test_progressive_raw_renderer_field_is_not_in_the_receipt_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            self._rewrite_progressive(
                root,
                manifest,
                lambda receipt: receipt["runtime"]["graphics"].update(
                    {
                        "renderer": (
                            "ANGLE (Apple, ANGLE Metal Renderer: "
                            "Final Cut Approved Rights Cleared)"
                        )
                    }
                ),
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "violates schema"):
                CONTRACT.validate_release(root)

    def test_progressive_check_digest_must_bind_the_raw_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            self._rewrite_progressive(
                root,
                manifest,
                lambda receipt: receipt["checks"][0].update(
                    {"receipt_sha256": hashlib.sha256(b"fabricated").hexdigest()}
                ),
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "raw capture"):
                CONTRACT.validate_release(root)

    def test_claim_partition_and_summary_cannot_contradict_gate_scope(self) -> None:
        cases = (
            "missing-affirm",
            "extra-affirm",
            "double-claim",
            "row-summary",
            "outer-summary",
            "free-form",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                gate_id = "final-artistic-approval"
                outer_path, outer = self._outer_receipt(root, manifest, gate_id)
                if case == "missing-affirm":
                    outer["affirms"] = []
                    expected = "contradictory claim boundary"
                elif case == "extra-affirm":
                    outer["affirms"].append("submission-complete")
                    outer["does_not_affirm"].remove("submission-complete")
                    expected = "contradictory claim boundary"
                elif case == "double-claim":
                    outer["does_not_affirm"].append("final-cut-approved")
                    outer["does_not_affirm"].sort()
                    expected = "contradictory claim boundary"
                elif case == "row-summary":
                    outer["evidence"][0]["summary"] = "Neutral prose is not allowed."
                    expected = "schema failure"
                elif case == "outer-summary":
                    self._gate(manifest, gate_id)["evidence"]["summary"] = (
                        "Certified for public exhibition and festival delivery."
                    )
                    expected = "exact neutral template"
                else:
                    outer["non_actions"] = [
                        "This free-form field says submission is complete."
                    ]
                    expected = "schema failure"
                self._write_outer_receipt(root, manifest, gate_id, outer_path, outer)
                with self.assertRaisesRegex(CONTRACT.ReleaseError, expected):
                    CONTRACT.validate_release(root)

    def test_manifest_copy_cannot_change_after_receipts_or_ship(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            manifest = complete_manifest(root)
            manifest["copy"]["logline"] = "Post-receipt replacement copy."
            write_manifest(root, manifest)
            commit_git_fixture(root, "attempt post-receipt copy substitution")
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "manifest changed outside gate state"
            ):
                CONTRACT.validate_release(root, phase="release")
            output = base / "substituted-artifact"
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "manifest changed outside gate state"
            ):
                BUILD.build(root, output, "release", TEST_COMMIT)
            self.assertFalse((output / BUILD.ARTIFACT_MANIFEST).exists())

    def test_all_gate_receipts_share_one_frozen_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            _, outer = self._outer_receipt(
                root, manifest, "final-artistic-approval"
            )
            earlier_source = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "rev-parse",
                    f"{outer['subject']['repository_head']}^",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            earlier_tree = subprocess.run(
                ["git", "-C", str(root), "rev-parse", f"{earlier_source}^{{tree}}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self._rewrite_outer_subject_and_proofs(
                root,
                manifest,
                "final-artistic-approval",
                lambda subject: subject.update(
                    {
                        "repository_head": earlier_source,
                        "repository_tree": earlier_tree,
                    }
                ),
            )
            commit_git_fixture(root, "attempt split release source")
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "source identity differs across gates"
            ):
                CONTRACT.validate_release(root)

    def test_pinned_validation_rejects_dirty_tracked_staged_and_untracked_bytes(
        self,
    ) -> None:
        for case in ("tracked", "staged", "untracked"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                complete_manifest(root)
                if case == "untracked":
                    (root / "untracked-contract-shadow.txt").write_text(
                        "shadow\n", encoding="utf-8"
                    )
                else:
                    ignore = root / ".gitignore"
                    ignore.write_text(
                        ignore.read_text(encoding="utf-8") + "# dirty fixture\n",
                        encoding="utf-8",
                    )
                    if case == "staged":
                        subprocess.run(
                            ["git", "-C", str(root), "add", ".gitignore"],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                with self.assertRaisesRegex(
                    CONTRACT.ReleaseError, "clean committed checkout"
                ):
                    CONTRACT.validate_release(root)

    def test_required_rights_records_cannot_be_replaced_by_ignored_files(self) -> None:
        for relative in (
            "rights/register.json",
            "release/evidence/proofs/rights-register-rights-zero-blockers.json",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                complete_manifest(root)
                subprocess.run(
                    ["git", "-C", str(root), "rm", "--cached", "--", relative],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                exclude = root / ".git/info/exclude"
                exclude.write_text(
                    exclude.read_text(encoding="utf-8") + relative + "\n",
                    encoding="utf-8",
                )
                commit_git_fixture(root, "remove required rights record from index")
                self.assertEqual(
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(root),
                            "status",
                            "--porcelain=v1",
                            "--untracked-files=all",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout,
                    "",
                )
                with self.assertRaisesRegex(
                    CONTRACT.ReleaseError, "not tracked|ignored contract or evidence"
                ):
                    CONTRACT.validate_release(root)

    def test_owner_comment_identity_cannot_be_reused_with_a_new_claimed_body(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            _, source_outer = self._outer_receipt(
                root, manifest, "contact-route-approval"
            )
            source_path = root / source_outer["evidence"][0]["receipt"]["path"]
            source = json.loads(source_path.read_text(encoding="utf-8"))["source"]
            reused = copy.deepcopy(source)
            target_gate = self._gate(manifest, "publication-approval")
            _, target_outer = self._outer_receipt(
                root, manifest, "publication-approval"
            )
            reused["comment_body_sha256"] = CONTRACT._owner_approval_body_sha256(
                root, target_gate, target_outer["subject"], manifest
            )
            self._rewrite_proof(
                root,
                manifest,
                "publication-approval",
                "owner-attestation",
                lambda proof: proof.update({"source": reused}),
                repin_source=True,
            )
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "reuses an owner comment identity"
            ):
                CONTRACT.validate_release(root)

    def test_owner_comment_body_must_hash_the_exact_decision_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            self._rewrite_proof(
                root,
                manifest,
                "rights-register",
                "owner-attestation",
                lambda proof: proof["source"].update(
                    {"comment_body_sha256": "0" * 64}
                ),
                repin_source=True,
            )
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "canonical owner approval payload"
            ):
                CONTRACT.validate_release(root)

    def test_rights_zero_blocker_check_binds_the_exact_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            self._rewrite_proof(
                root,
                manifest,
                "rights-register",
                "rights-validation",
                lambda proof: next(
                    check
                    for check in proof["checks"]
                    if check["id"] == "zero-blockers"
                ).update({"receipt_sha256": "0" * 64}),
            )
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "check digests do not bind"
            ):
                CONTRACT.validate_release(root)

    def test_rights_validation_date_must_match_the_observation_calendar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            self._rewrite_rights_validation_receipt(
                root,
                manifest,
                "rights-register",
                lambda receipt: receipt["receipt"]["inputs"].update(
                    {"validation_date": "2000-01-01"}
                ),
            )
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "exact ready rights validator receipt"
            ):
                CONTRACT.validate_release(root)

    def test_expired_fixed_right_cannot_enter_a_ready_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            complete_manifest(root)
            document = json.loads(
                (root / "rights/register.json").read_text(encoding="utf-8")
            )
            use = next(
                use
                for asset in document["assets"]
                for use in asset["uses"]
                if use["status"] == "cleared"
            )
            use["term"] = "fixed"
            use["expires"] = "2000-01-01"
            record_path = root / use["evidence"]["path"]
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record.update({"term": "fixed", "expires": "2000-01-01"})
            record_path.write_bytes(CONTRACT.canonical_json(record))
            use["evidence"]["sha256"] = CONTRACT.sha256(record_path)
            (root / "rights/register.json").write_bytes(
                CONTRACT.canonical_json(document)
            )
            source_head = commit_git_fixture(root, "expire fixed permission")
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "expired before validation"):
                CONTRACT._rights_register_inventory(
                    root,
                    document,
                    gate_id="rights-register",
                    source_head=source_head,
                    tracked=CONTRACT._tracked_paths(root),
                    validation_date=datetime.now(timezone.utc).date(),
                )

    def test_nested_rights_receipt_must_remain_public_safe_and_schema_closed(
        self,
    ) -> None:
        mutations = (
            lambda receipt: receipt["receipt"]["inputs"].update(
                {"%74oken": "supersecret"}
            ),
            lambda receipt: receipt["receipt"]["inputs"].update(
                {"private_path": "/Users/operator/release.pdf"}
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                self._rewrite_rights_validation_receipt(
                    root, manifest, "rights-register", mutate
                )
                with self.assertRaisesRegex(
                    CONTRACT.ReleaseError,
                    "sensitive field|private contact or path|credentials",
                ):
                    CONTRACT.validate_release(root)

    def test_rights_proofs_do_not_import_filing_or_archive_decisions(self) -> None:
        forbidden = {
            "final-cut-only",
            "bio-approved",
            "submission-copy-approved",
            "link-password-protected",
            "link-downloadable",
            "submitted-via-submittable",
            "accepted-film-no-withdrawal",
            "publicity-stills-free-of-rights",
            "submission-rights-warranty",
            "festival-scheduling-discretion",
            "archive-library-choice",
            "regulations-accepted",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            rights_document = json.loads(
                (root / "rights/register.json").read_text(encoding="utf-8")
            )
            archive_gate = next(
                gate
                for gate in rights_document["human_gates"]
                if gate["id"] == "archive-library-choice"
            )
            self.assertEqual(archive_gate["state"], "pending")
            self.assertIsNone(archive_gate["evidence"])
            for gate_id in ("rights-register", "press-stills-clearance"):
                _, outer = self._outer_receipt(root, manifest, gate_id)
                row = next(
                    item
                    for item in outer["evidence"]
                    if item["kind"] == "rights-validation"
                )
                proof = json.loads(
                    (root / row["receipt"]["path"]).read_text(encoding="utf-8")
                )
                subordinate = json.loads(
                    (
                        root
                        / proof["rights_binding"]["zero_blockers"]["path"]
                    ).read_text(encoding="utf-8")
                )
                serialized = CONTRACT.canonical_json(subordinate).decode("utf-8")
                self.assertTrue(forbidden.isdisjoint(serialized.split('"')))
                self.assertEqual(
                    subordinate["receipt"]["inputs"]["human_gate_ids"],
                    sorted(CONTRACT.RIGHTS_CLEARANCE_GATES[gate_id]),
                )
                scoped_uses = subordinate["receipt"]["inputs"]["asset_use_ids"]
                self.assertFalse(
                    any(identity.endswith("/festival-archive") for identity in scoped_uses)
                )
                self.assertNotIn(
                    "room-source-recordings/hybrid-apartment-grains", scoped_uses
                )

            self._rewrite_rights_validation_receipt(
                root,
                manifest,
                "rights-register",
                lambda receipt: receipt["receipt"]["inputs"].update(
                    {"submitted-via-submittable": True}
                ),
            )
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError, "exact ready rights validator receipt"
            ):
                CONTRACT.validate_release(root)

    def test_rights_proof_expires_when_a_fixed_scope_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            today = CONTRACT._current_rights_validation_date()
            rights_path = root / "rights/register.json"
            rights = json.loads(rights_path.read_text(encoding="utf-8"))
            scoped_use = next(
                use
                for asset in rights["assets"]
                for use in asset["uses"]
                if set(use.get("required_for", []))
                & CONTRACT.RIGHTS_CLEARANCE_REQUIRED_PHASES
                and use.get("medium")
                not in CONTRACT.RIGHTS_STILL_MEDIA
                | CONTRACT.RIGHTS_FILING_ONLY_MEDIA
            )
            scoped_use["term"] = "fixed"
            scoped_use["expires"] = today.isoformat()
            rights_path.write_bytes(CONTRACT.canonical_json(rights))
            complete_manifest(root)
            with mock.patch.object(
                CONTRACT, "_current_rights_validation_date", return_value=today
            ):
                with self.assertRaisesRegex(
                    CONTRACT.ReleaseError, "no trusted external authenticity verifier"
                ):
                    CONTRACT.validate_release(root)
            with mock.patch.object(
                CONTRACT,
                "_current_rights_validation_date",
                return_value=today + timedelta(days=1),
            ):
                with self.assertRaisesRegex(CONTRACT.ReleaseError, "expired"):
                    CONTRACT.validate_release(root)

    def test_progressive_pin_issuer_is_derived_from_the_exact_generator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            self._mutate_pin(
                root,
                "progressive-controls-replay",
                "progressive-controls-replay",
                lambda pin: pin.update(
                    {
                        "issuer": {
                            "kind": "tool",
                            "identity": f"render/browser.py@sha256:{'0' * 64}",
                        }
                    }
                ),
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "exact generator"):
                CONTRACT.validate_release(root)

    def test_progressive_tool_pin_cannot_invent_an_authority_comment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            source = copy.deepcopy(
                self._pin(root, "installation-evidence", "installation-completion")[
                    "source"
                ]
            )
            self._mutate_pin(
                root,
                "progressive-controls-replay",
                "progressive-controls-replay",
                lambda pin: pin.update({"source": source}),
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "schema failure"):
                CONTRACT.validate_release(root)

    def test_operational_tool_proof_must_bind_its_frozen_generator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)

            def replace_generator(proof: dict) -> None:
                proof["generator"]["sha256"] = "0" * 64
                proof["issuer"]["identity"] = (
                    f"{proof['generator']['path']}@sha256:{'0' * 64}"
                )

            self._rewrite_proof(
                root,
                manifest,
                "accessibility-review",
                "accessibility-review",
                replace_generator,
                repin_issuer=True,
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "source generator"):
                CONTRACT.validate_release(root)

    def test_venue_and_host_proofs_require_distinct_canonical_authority_sources(
        self,
    ) -> None:
        cases = (
            ("installation-evidence", "installation-completion"),
            ("actual-presentation", "presentation-lifecycle"),
        )
        for gate_id, kind in cases:
            with self.subTest(gate_id=gate_id), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                gate = self._gate(manifest, gate_id)

                def impersonate_owner(proof: dict) -> None:
                    source = proof["authority_source"]
                    source["comment_author"] = CONTRACT.RELEASE_OWNER_LOGIN
                    source["comment_body_sha256"] = (
                        CONTRACT._external_authority_body_sha256(
                            gate,
                            kind,
                            CONTRACT.RELEASE_OWNER_LOGIN,
                            proof["subject"],
                            proof["checks"],
                        )
                    )

                self._rewrite_proof(
                    root,
                    manifest,
                    gate_id,
                    kind,
                    impersonate_owner,
                    repin_source=True,
                )
                with self.assertRaisesRegex(
                    CONTRACT.ReleaseError, "distinct external authority"
                ):
                    CONTRACT.validate_release(root)

    def test_rights_gate_cannot_pass_with_machine_proof_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            gate_id = "rights-register"
            outer_path, outer = self._outer_receipt(root, manifest, gate_id)
            outer["evidence"] = [
                row for row in outer["evidence"] if row["kind"] != "owner-attestation"
            ]
            ledger = self._pin_ledger(root)
            ledger["records"] = [
                row
                for row in ledger["records"]
                if (row["gate_id"], row["kind"])
                != (gate_id, "owner-attestation")
            ]
            self._write_pin_ledger(root, ledger)
            self._write_outer_receipt(root, manifest, gate_id, outer_path, outer)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "exact proof inventory"):
                CONTRACT.validate_release(root)

    def test_recursive_public_safety_rejects_deep_encoding_and_credentials(self) -> None:
        encoded = "/Users/private/evidence"
        for _ in range(10):
            encoded = quote(encoded, safe="")
        with self.assertRaisesRegex(
            CONTRACT.ReleaseError, "private contact or path|depth limit"
        ):
            CONTRACT.validate_public_safe_document({"value": encoded}, "fixture")
        with self.assertRaisesRegex(CONTRACT.ReleaseError, "credentials"):
            CONTRACT.validate_public_safe_document(
                {"value": "API token=supersecret"}, "fixture"
            )
        with self.assertRaisesRegex(CONTRACT.ReleaseError, "sensitive field"):
            CONTRACT.validate_public_safe_document(
                {"%74oken": "not otherwise suspicious"}, "fixture"
            )


class AdversarialArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.output = self.base / "artifact"
        BUILD.build(ROOT, self.output, "draft", TEST_COMMIT)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_tampered_pdf_digest_fails(self) -> None:
        with (self.output / BUILD.PDF_NAME).open("ab") as handle:
            handle.write(b"tamper")
        with self.assertRaisesRegex(CONTRACT.ReleaseError, "digest mismatch"):
            BUILD.verify_artifact(self.output, TEST_COMMIT)

    def test_unrecorded_file_fails(self) -> None:
        (self.output / "private.txt").write_text("not allowlisted\n")
        with self.assertRaisesRegex(CONTRACT.ReleaseError, "inventory mismatch"):
            BUILD.verify_artifact(self.output, TEST_COMMIT)

    def test_receipt_omitting_required_output_fails_before_post_inventory_reads(self) -> None:
        for relative in (
            "project/index.html",
            "accessibility/captions.en.vtt",
            BUILD.PDF_NAME,
        ):
            with self.subTest(relative=relative):
                case = self.base / relative.replace("/", "-")
                shutil.copytree(self.output, case)
                (case / relative).unlink()
                receipt_path = case / BUILD.ARTIFACT_MANIFEST
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                receipt["files"] = [
                    record for record in receipt["files"] if record["path"] != relative
                ]
                receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
                with self.assertRaisesRegex(CONTRACT.ReleaseError, "omits required outputs"):
                    BUILD.verify_artifact(case, TEST_COMMIT)

    def test_self_rehashed_invalid_utf8_output_fails_as_release_error(self) -> None:
        for relative in ("project/index.html", "accessibility/captions.en.vtt"):
            with self.subTest(relative=relative):
                case = self.base / f"invalid-utf8-{relative.replace('/', '-')}"
                shutil.copytree(self.output, case)
                path = case / relative
                path.write_bytes(b"\xff\xfe\n")
                receipt_path = case / BUILD.ARTIFACT_MANIFEST
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                record = next(
                    record for record in receipt["files"] if record["path"] == relative
                )
                record["bytes"] = path.stat().st_size
                record["sha256"] = CONTRACT.sha256(path)
                receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
                with self.assertRaisesRegex(CONTRACT.ReleaseError, "not readable UTF-8"):
                    BUILD.verify_artifact(case, TEST_COMMIT)

    def test_receipted_project_link_to_source_manifest_fails(self) -> None:
        project = self.output / "project/index.html"
        project.write_text(
            project.read_text(encoding="utf-8").replace(
                "</main>",
                '<p><a href="../release/manifest.json">Source manifest</a></p></main>',
            ),
            encoding="utf-8",
        )
        receipt_path = self.output / BUILD.ARTIFACT_MANIFEST
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        record = next(
            record for record in receipt["files"] if record["path"] == "project/index.html"
        )
        record["bytes"] = project.stat().st_size
        record["sha256"] = CONTRACT.sha256(project)
        receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
        with self.assertRaisesRegex(CONTRACT.ReleaseError, "missing internal target"):
            BUILD.verify_artifact(self.output, TEST_COMMIT)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlinked_project_file_fails(self) -> None:
        outside = self.base / "outside.html"
        outside.write_text("outside\n")
        project = self.output / "project/index.html"
        project.unlink()
        project.symlink_to(outside)
        with self.assertRaisesRegex(CONTRACT.ReleaseError, "missing or non-regular"):
            BUILD.verify_artifact(self.output, TEST_COMMIT)

    def test_wrong_source_commit_fails(self) -> None:
        with self.assertRaisesRegex(CONTRACT.ReleaseError, "does not match expected"):
            BUILD.verify_artifact(self.output, "b" * 40)

    def test_noncanonical_manifest_binding_fails(self) -> None:
        receipt_path = self.output / BUILD.ARTIFACT_MANIFEST
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["release"]["manifest"]["path"] = "release/other-manifest.json"
        receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
        with self.assertRaisesRegex(CONTRACT.ReleaseError, "non-canonical release manifest"):
            BUILD.verify_artifact(self.output, TEST_COMMIT)

    def test_duplicate_receipt_key_fails(self) -> None:
        receipt_path = self.output / BUILD.ARTIFACT_MANIFEST
        receipt_path.write_text(
            '{"schema":"danse.release-build.v1","schema":"danse.release-build.v1"}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(CONTRACT.ReleaseError, "duplicate key 'schema'"):
            BUILD.verify_artifact(self.output, TEST_COMMIT)


if __name__ == "__main__":
    unittest.main()
