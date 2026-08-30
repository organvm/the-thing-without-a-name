#!/usr/bin/env python3
"""Adversarial and reproducibility checks for the Danse release framework."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import release_contract as CONTRACT  # noqa: E402

TEST_COMMIT = "a" * 40
FIXTURE_FILES = (
    ".gitignore",
    "release/manifest.json",
    "release/manifest.schema.json",
    "release/gate-receipt.schema.json",
    "release/gate-proof.schema.json",
    "release/owner-attestation.schema.json",
    "release/evidence/live-interaction-replay-20260804.json",
    "release/progressive-controls-replay.schema.json",
    "opportunities/omega-20260829.json",
    "opportunities/omega-20260829.receipt.json",
    "opportunities/source-evidence-20260826.json",
    "opportunities/opportunity.schema.json",
    "scripts/check-opportunities.py",
    "submission/screendance-2027.yaml",
    "corpus/manifest.json",
    "scripts/check-danse.py",
    "scripts/private_custody.py",
    "rights/evidence/mediapipe-attribution.json",
    "installation/contract.py",
    "installation/digital-twin.json",
    "installation/gates.json",
    "engine/room.js",
    "render/program.json",
    "music/score.json",
    "sound/room-layout.json",
    "interaction/adapter.js",
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


def complete_manifest(root: Path) -> dict:
    manifest = _release_copy(read_manifest(root))
    manifest["version"] = "1.0.0"
    manifest["status"] = "released"

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

    # Freeze the release content before minting any gate evidence. Receipts bind
    # this real ancestor commit and its exact tree; the later evidence commit is
    # allowed to carry the immutable receipts without a self-referential SHA.
    write_manifest(root, manifest)
    source_head = initialize_git_fixture(root)
    source_tree = subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{source_head}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    observed_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    package_digest = hashlib.sha256(b"synthetic exact package manifest\n").hexdigest()

    progressive_path = root / CONTRACT.PROGRESSIVE_CONTROLS_EVIDENCE_PATH
    progressive_path.write_bytes(
        CONTRACT.canonical_json(
            {
                "schema": "danse.progressive-controls-replay.v1",
                "gate_id": "progressive-controls-replay",
                "result": "satisfied",
                "observed_at": observed_at,
                "source": {
                    "repository": CONTRACT.RELEASE_REPOSITORY,
                    "pull_request": 43,
                    "exact_head": source_head,
                    "tree": source_tree,
                },
                "runtime": {
                    "platform": "darwin",
                    "browser": {"name": "Google Chrome", "version": "fixture"},
                    "graphics": {
                        "vendor": "Apple",
                        "api": "Metal",
                        "renderer": "ANGLE (Apple, ANGLE Metal Renderer: fixture)",
                    },
                },
                "checks": [
                    {
                        "id": check_id,
                        "result": "passed",
                        "observation": f"Synthetic {check_id} observation.",
                    }
                    for check_id in CONTRACT.PROGRESSIVE_CONTROLS_CHECKS
                ],
                "non_actions": [
                    "No deployment, upload, submission, or publication action was performed by this receipt.",
                    "No rights, biography, final-cut, or archive-participation claim was made.",
                    "This browser replay does not satisfy final-cut, rights, package, upload, or filing readiness.",
                ],
            }
        )
    )
    progressive_evidence = {
        "path": CONTRACT.PROGRESSIVE_CONTROLS_EVIDENCE_PATH,
        "sha256": CONTRACT.sha256(progressive_path),
        "summary": "Synthetic exact-head progressive-controls replay fixture.",
    }

    pins: dict[tuple[str, str], dict] = {}
    for gate_index, gate in enumerate(manifest["gates"], start=1):
        gate["state"] = "satisfied"
        if gate["id"] == "live-interaction-replay":
            continue
        if gate["id"] == "progressive-controls-replay":
            gate["evidence"] = copy.deepcopy(progressive_evidence)
            continue

        contract = CONTRACT.RELEASE_GATE_CONTRACTS[gate["id"]]
        subject = {
            "release_id": manifest["release_id"],
            "release_version": manifest["version"],
            "repository_head": source_head,
            "repository_tree": source_tree,
            "package_manifest_sha256": package_digest if contract["package"] else None,
        }
        package = CONTRACT._expected_package(subject)
        rows = []
        for proof_index, kind in enumerate(contract["proofs"], start=1):
            proof_path = root / f"release/evidence/proofs/{gate['id']}-{kind}.json"
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
                    "comment_body_sha256": hashlib.sha256(
                        f"{gate['id']} owner fixture".encode()
                    ).hexdigest(),
                }
                proof = {
                    "schema": CONTRACT.RELEASE_OWNER_ATTESTATION_SCHEMA,
                    "attestation_id": f"{gate['id']}-owner-{gate_index}",
                    "gate_id": gate["id"],
                    "issue": gate["issue"],
                    "kind": kind,
                    "decision": True,
                    "recorded_at": observed_at,
                    "authority": {
                        "name": CONTRACT.RELEASE_OWNER_NAME,
                        "github_login": CONTRACT.RELEASE_OWNER_LOGIN,
                    },
                    "source": source,
                    "subject": subject,
                    "package": package,
                }
                pin_extra = {"source": source}
                schema_name = CONTRACT.RELEASE_OWNER_ATTESTATION_SCHEMA
            else:
                issuer_kind = (
                    "venue"
                    if kind == "installation-completion"
                    else "host"
                    if kind == "presentation-lifecycle"
                    else "tool"
                )
                issuer = {
                    "kind": issuer_kind,
                    "identity": f"synthetic-{gate['id']}-{kind}",
                }
                proof = {
                    "schema": CONTRACT.RELEASE_GATE_PROOF_SCHEMA,
                    "proof_id": f"{gate['id']}-{kind}-{proof_index}",
                    "gate_id": gate["id"],
                    "issue": gate["issue"],
                    "kind": kind,
                    "result": "passed",
                    "observed_at": observed_at,
                    "issuer": issuer,
                    "subject": subject,
                    "package": package,
                    "checks": [
                        {
                            "id": check_id,
                            "result": "passed",
                            "receipt_sha256": (
                                package_digest
                                if kind == "submission-package"
                                and check_id == "package-manifest"
                                else hashlib.sha256(
                                    f"{gate['id']}:{kind}:{check_id}".encode()
                                ).hexdigest()
                            ),
                        }
                        for check_id in CONTRACT.RELEASE_PROOF_CHECKS[kind]
                    ],
                }
                pin_extra = {"issuer": issuer}
                schema_name = CONTRACT.RELEASE_GATE_PROOF_SCHEMA
            proof_path.write_bytes(CONTRACT.canonical_json(proof))
            record = {
                "path": proof_path.relative_to(root).as_posix(),
                "sha256": CONTRACT.sha256(proof_path),
                "schema": schema_name,
            }
            pins[(gate["id"], kind)] = {**record, **pin_extra}
            rows.append(
                {
                    "id": f"{kind}-{proof_index}",
                    "kind": kind,
                    "receipt": record,
                    "summary": "Pinned typed proof fixture.",
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
            "summary": "Synthetic typed gate receipt fixture.",
        }

    write_manifest(root, manifest)
    commit_git_fixture(root, "authenticated release fixture")
    CONTRACT.PINNED_GATE_PROOFS.clear()
    CONTRACT.PINNED_GATE_PROOFS.update(pins)
    return manifest


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
        validator = CONTRACT.jsonschema.Draft202012Validator(schema)
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                manifest = copy.deepcopy(self.manifest)
                mutate(manifest)
                errors = list(validator.iter_errors(manifest))
                self.assertTrue(
                    any(error.validator == "pattern" for error in errors),
                    [error.message for error in errors],
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
        CONTRACT.jsonschema.Draft202012Validator.check_schema(schema)
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

    def test_fully_evidenced_fixture_builds_public_and_release_without_draft_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            manifest = complete_manifest(root)
            CONTRACT.validate_release(root, phase="public")
            CONTRACT.validate_release(root, phase="release")
            output = base / "artifact"
            receipt = BUILD.build(root, output, "release", TEST_COMMIT)
            project = (output / "project/index.html").read_text(encoding="utf-8")
            self.assertNotIn("noindex,nofollow", project)
            self.assertNotIn("Draft - not for publication", project)
            self.assertEqual(receipt["phase"], "release")
            assets = [record for record in receipt["files"] if record["path"].startswith("media/assets/")]
            self.assertEqual(len(assets), len(manifest["media"]))
            generated_inventory = json.loads(
                (output / "media/release-media.json").read_text()
            )["products"]
            self.assertEqual(
                {product["id"] for product in generated_inventory},
                {product["id"] for product in manifest["products"]},
            )
            self.assertFalse(
                any(
                    product["path"].startswith("media/assets/")
                    for product in manifest["products"]
                )
            )
            for product in generated_inventory:
                artifact = product["artifact"]
                path = output / artifact["path"]
                self.assertEqual(artifact["bytes"], path.stat().st_size)
                self.assertEqual(artifact["sha256"], CONTRACT.sha256(path))
            captions = (output / "accessibility/captions.en.vtt").read_text()
            self.assertIn("00:00:00.000 --> 00:00:02.000", captions)

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
            with mock.patch.object(BUILD, "source_file", side_effect=replace_after_validation):
                with self.assertRaisesRegex(CONTRACT.ReleaseError, "changed after manifest validation"):
                    BUILD.build(root, output, "release", TEST_COMMIT)
            self.assertTrue(swapped)
            self.assertFalse((output / BUILD.ARTIFACT_MANIFEST).exists())
            self.assertFalse((output / source["destination"]).exists())

    def test_public_phase_does_not_require_release_only_lifecycle_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            original = read_manifest(root)
            manifest = complete_manifest(root)
            manifest["status"] = "public-approved"
            original_media = {item["id"]: item for item in original["media"]}
            for index, medium in enumerate(manifest["media"]):
                if medium["required_for"] == ["release"]:
                    manifest["media"][index] = copy.deepcopy(original_media[medium["id"]])
            original_gates = {item["id"]: item for item in original["gates"]}
            for index, gate in enumerate(manifest["gates"]):
                if gate["required_for"] == ["release"]:
                    manifest["gates"][index] = copy.deepcopy(original_gates[gate["id"]])
            write_manifest(root, manifest)

            CONTRACT.validate_release(root, phase="public")
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "release phase blocked"):
                CONTRACT.validate_release(root, phase="release")
            receipt = BUILD.build(root, base / "public-artifact", "public", TEST_COMMIT)
            self.assertEqual(receipt["phase"], "public")


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
                "summary": "A digest alone must not impersonate custody evidence.",
            }
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "receipt schema failure"):
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

    def test_release_gate_receipt_rejects_private_contact_or_path_data(self) -> None:
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
                "exposes private contact or path data",
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
                "summary": "A matching digest without the gate-specific contract.",
            }
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "names the wrong evidence receipt"):
                CONTRACT.validate_release(root)

    def test_rehashed_progressive_controls_receipt_with_missing_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            receipt_path = root / CONTRACT.PROGRESSIVE_CONTROLS_EVIDENCE_PATH
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["checks"].pop()
            receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
            gate = next(
                gate
                for gate in manifest["gates"]
                if gate["id"] == "progressive-controls-replay"
            )
            gate["evidence"]["sha256"] = CONTRACT.sha256(receipt_path)
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "violates schema"):
                CONTRACT.validate_release(root)

    def test_progressive_controls_receipt_requires_a_real_exact_head_and_tree(self) -> None:
        for mutation, message in (
            (("exact_head", "f" * 40), "unavailable Git object or relationship"),
            (("tree", "f" * 40), "tree disagrees with its exact head"),
        ):
            with (
                self.subTest(field=mutation[0]),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                receipt_path = root / CONTRACT.PROGRESSIVE_CONTROLS_EVIDENCE_PATH
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                receipt["source"][mutation[0]] = mutation[1]
                receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
                gate = next(
                    gate
                    for gate in manifest["gates"]
                    if gate["id"] == "progressive-controls-replay"
                )
                gate["evidence"]["sha256"] = CONTRACT.sha256(receipt_path)
                write_manifest(root, manifest)
                with self.assertRaisesRegex(CONTRACT.ReleaseError, message):
                    CONTRACT.validate_release(root)

    def test_progressive_controls_receipt_rejects_a_different_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            receipt_path = root / CONTRACT.PROGRESSIVE_CONTROLS_EVIDENCE_PATH
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            (root / "reviewed-tree-drift.txt").write_text(
                "different source tree\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "-C", str(root), "add", "reviewed-tree-drift.txt"],
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
                    "different reviewed tree",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
            gate = next(
                gate
                for gate in manifest["gates"]
                if gate["id"] == "progressive-controls-replay"
            )
            gate["evidence"]["sha256"] = CONTRACT.sha256(receipt_path)
            write_manifest(root, manifest)
            with self.assertRaisesRegex(
                CONTRACT.ReleaseError,
                "source drift is not limited to its tracked evidence envelope",
            ):
                CONTRACT.validate_release(root)

    def test_progressive_controls_receipt_allows_committed_completion_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            receipt_path = root / CONTRACT.PROGRESSIVE_CONTROLS_EVIDENCE_PATH
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["checks"][0]["observation"] = (
                "Synthetic committed completion observation."
            )
            receipt_path.write_bytes(CONTRACT.canonical_json(receipt))
            gate = next(
                gate
                for gate in manifest["gates"]
                if gate["id"] == "progressive-controls-replay"
            )
            gate["evidence"]["sha256"] = CONTRACT.sha256(receipt_path)
            write_manifest(root, manifest)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "add",
                    "release/manifest.json",
                    CONTRACT.PROGRESSIVE_CONTROLS_EVIDENCE_PATH,
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
                    "-c",
                    "user.name=Danse Test",
                    "-c",
                    "user.email=danse-test@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-qm",
                    "record progressive controls completion",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            CONTRACT.validate_release(root)

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
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "approved caption track contains no cues"):
                CONTRACT.validate_release(root, phase="public")

    def test_caption_cue_must_have_forward_timing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            manifest = complete_manifest(root)
            manifest["accessibility"]["captions"]["cues"][0]["end"] = "00:00:00.000"
            write_manifest(root, manifest)
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "must end after it starts"):
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
        pin = CONTRACT.PINNED_GATE_PROOFS[(gate_id, kind)]
        pin["sha256"] = digest
        if repin_source:
            pin["source"] = copy.deepcopy(proof["source"])
        if repin_issuer:
            pin["issuer"] = copy.deepcopy(proof["issuer"])
        self._write_outer_receipt(root, manifest, gate_id, outer_path, outer)
        return proof

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
            proof_path.write_bytes(CONTRACT.canonical_json(proof))
            digest = CONTRACT.sha256(proof_path)
            row["receipt"]["sha256"] = digest
            CONTRACT.PINNED_GATE_PROOFS[(gate_id, row["kind"])]["sha256"] = digest
        self._write_outer_receipt(root, manifest, gate_id, outer_path, outer)

    def _rewrite_progressive(self, root: Path, manifest: dict, mutate) -> None:
        path = root / CONTRACT.PROGRESSIVE_CONTROLS_EVIDENCE_PATH
        receipt = json.loads(path.read_text(encoding="utf-8"))
        mutate(receipt)
        path.write_bytes(CONTRACT.canonical_json(receipt))
        gate = self._gate(manifest, "progressive-controls-replay")
        gate["evidence"]["sha256"] = CONTRACT.sha256(path)
        write_manifest(root, manifest)

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
                    CONTRACT.PINNED_GATE_PROOFS.pop((gate_id, "owner-attestation"))
                    expected = "no authenticated reviewed proof pin"
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
                    CONTRACT.PINNED_GATE_PROOFS[
                        (
                            "publication-approval",
                            "owner-attestation",
                        )
                    ] = copy.deepcopy(
                        CONTRACT.PINNED_GATE_PROOFS[
                            (
                                "contact-route-approval",
                                "owner-attestation",
                            )
                        ]
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
                    expected = "reuses a source receipt"
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
                    expected = "unavailable Git object or relationship"
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
                    "schema failure|encoded or malformed|immutable GitHub comment form",
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
                "source drift is not limited to its tracked evidence envelope",
            ):
                CONTRACT.validate_release(root)

    def test_progressive_receipt_rejects_contact_and_encoded_private_data(self) -> None:
        observations = {
            "email": "Contact operator@example.com after the replay.",
            "phone": "Call 212-555-0199 after the replay.",
            "encoded-path": "Capture stored at %252FUsers%252Foperator%252Fprivate.mov.",
        }
        for case, observation in observations.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                self._rewrite_progressive(
                    root,
                    manifest,
                    lambda receipt, value=observation: receipt["checks"][0].update(
                        {"observation": value}
                    ),
                )
                with self.assertRaisesRegex(
                    CONTRACT.ReleaseError,
                    "exposes private contact or path data",
                ):
                    CONTRACT.validate_release(root)

    def test_progressive_source_rejects_unavailable_wrong_tree_and_future_time(
        self,
    ) -> None:
        cases = ("unavailable", "wrong-tree", "future")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = fixture_root(Path(temporary))
                manifest = complete_manifest(root)
                if case == "unavailable":
                    expected = "unavailable Git object or relationship"
                elif case == "wrong-tree":
                    expected = "tree disagrees with its exact head"
                else:
                    expected = "is in the future"

                def mutate(receipt: dict, selected: str = case) -> None:
                    if selected == "unavailable":
                        receipt["source"]["exact_head"] = "f" * 40
                    elif selected == "wrong-tree":
                        receipt["source"]["tree"] = "f" * 40
                    else:
                        receipt["observed_at"] = "2999-01-01T00:00:00Z"

                self._rewrite_progressive(root, manifest, mutate)
                with self.assertRaisesRegex(CONTRACT.ReleaseError, expected):
                    CONTRACT.validate_release(root)

    def test_progressive_renderer_cannot_negate_apple_metal_in_free_text(self) -> None:
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
                            "not Apple and not Metal)"
                        )
                    }
                ),
            )
            with self.assertRaisesRegex(CONTRACT.ReleaseError, "Apple Metal"):
                CONTRACT.validate_release(root)

    def test_claim_partition_and_summary_cannot_contradict_gate_scope(self) -> None:
        cases = (
            "missing-affirm",
            "extra-affirm",
            "double-claim",
            "assertive-summary",
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
                elif case == "assertive-summary":
                    outer["evidence"][0]["summary"] = "Final cut approved by the owner."
                    expected = "authoritative or unsafe summary"
                else:
                    outer["non_actions"] = [
                        "This free-form field says submission is complete."
                    ]
                    expected = "schema failure"
                self._write_outer_receipt(root, manifest, gate_id, outer_path, outer)
                with self.assertRaisesRegex(CONTRACT.ReleaseError, expected):
                    CONTRACT.validate_release(root)


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
