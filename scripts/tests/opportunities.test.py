#!/usr/bin/env python3
"""Portable regression tests for the frozen Alpha → Omega opportunity registry."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_checker():
    spec = importlib.util.spec_from_file_location(
        "danse_opportunity_test_checker", ROOT / "scripts/check-opportunities.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("opportunity checker module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load_checker()


class RegistryFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.snapshot = self.root / "opportunities/omega-20260829.json"
        self.schema = self.root / "opportunities/opportunity.schema.json"
        self.receipt = self.root / "opportunities/omega-20260829.receipt.json"
        self.evidence = self.root / "opportunities/source-evidence-20260826.json"
        self.consumer = self.root / "submission/screendance-2027.yaml"
        for source, target in (
            (CHECK.SNAPSHOT, self.snapshot),
            (CHECK.SCHEMA, self.schema),
            (CHECK.RECEIPT, self.receipt),
            (CHECK.EVIDENCE, self.evidence),
            (CHECK.CONSUMER, self.consumer),
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def close(self) -> None:
        self.temporary.cleanup()

    def data(self) -> dict:
        return json.loads(self.snapshot.read_text(encoding="utf-8"))

    def write(self, data: dict) -> None:
        self.snapshot.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class ProductionRegistryTest(unittest.TestCase):
    def test_original_august_4_freeze_remains_byte_for_byte_historical_evidence(self) -> None:
        self.assertEqual(
            CHECK.digest(ROOT / "opportunities/omega-20260804.json"),
            "3aeb84a8c919c6866272138e3ce1bd7d9222af77ad618d810ba69f6159544b3e",
        )
        self.assertEqual(
            CHECK.digest(ROOT / "opportunities/omega-20260804.receipt.json"),
            "3a2d3b9eaed6f14f869fc08fa2e0baf02b868116ec0e1b206f23c1ec3e2a94ee",
        )
        self.assertEqual(
            CHECK.digest(ROOT / "opportunities/source-evidence-20260804.json"),
            "0b28e98f151e8ea940c6c047fff91afc79f6b0df0343e623da79952b9aa87c37",
        )

    def test_immediate_predecessor_remains_byte_for_byte_historical_evidence(self) -> None:
        self.assertEqual(
            CHECK.digest(ROOT / "opportunities/omega-20260826-2.json"),
            "f5bb090a2cc1491055124d5e422b2e148f2e0327f9e096ead204eda86893c7af",
        )
        self.assertEqual(
            CHECK.digest(ROOT / "opportunities/omega-20260826-2.receipt.json"),
            "f8926f5777eb1d50d3d997493fa592a3c19004ebe5532803ea76f983f348756f",
        )

    def test_same_day_predecessor_remains_byte_for_byte_historical_evidence(self) -> None:
        self.assertEqual(
            CHECK.digest(ROOT / "opportunities/omega-20260826.json"),
            "91404ef677066de002819abd69d2fa2f320b4b215c3f80551e0a40edd2b6f667",
        )
        self.assertEqual(
            CHECK.digest(ROOT / "opportunities/omega-20260826.receipt.json"),
            "0f55ba2784bbe28659107d0e473d68f35bcd1f57ffa0bd7f547fdb5e55b9dfbe",
        )

    def test_exact_production_snapshot_and_consumers_validate(self) -> None:
        snapshot, receipt = CHECK.validate_all()
        self.assertEqual(len(snapshot["opportunities"]), 17)
        self.assertEqual(len(snapshot["ranked_actions"]), 6)
        self.assertEqual(receipt["snapshot"]["sha256"], CHECK.digest(CHECK.SNAPSHOT))

    def test_every_plan_target_has_one_explicit_disposition(self) -> None:
        snapshot = CHECK.validate_registry()
        by_id = {entry["id"]: entry for entry in snapshot["opportunities"]}
        self.assertEqual(set(by_id), CHECK.EXPECTED_TARGETS)
        self.assertEqual(by_id["bakehouse-studio-residency-2026"]["disposition"], "closed")
        self.assertEqual(by_id["locust-projects-main-gallery"]["disposition"], "watch")
        self.assertEqual(by_id["mignolo-screendance-2026"]["disposition"], "conflicted")
        self.assertEqual(by_id["miami-dade-tdc-2026-q2"]["disposition"], "blocked")

    def test_elapsed_oolite_extension_is_preserved_without_a_live_queue_claim(self) -> None:
        snapshot = CHECK.validate_registry()
        by_id = {entry["id"]: entry for entry in snapshot["opportunities"]}
        for entry_id in ("oolite-ellies-creator-2027", "oolite-studio-residency-2027"):
            entry = by_id[entry_id]
            self.assertEqual(entry["deadline_at"], "2026-08-03T23:59:00-04:00")
            self.assertEqual(entry["disposition"], "closed")
            self.assertTrue(entry["human_gates"])
            self.assertTrue(all(gate["status"] == "required" for gate in entry["human_gates"]))

    def test_date_only_cinedans_calls_use_start_of_day_boundaries(self) -> None:
        snapshot = CHECK.validate_registry()
        by_id = {entry["id"]: entry for entry in snapshot["opportunities"]}
        self.assertEqual(
            by_id["cinedans-fest-2027-installation"]["deadline_at"],
            "2026-09-15T00:00:00+02:00",
        )
        self.assertEqual(
            by_id["cinedans-2028-international-short"]["deadline_at"],
            "2027-06-30T00:00:00+02:00",
        )

    def test_ranked_view_contains_no_closed_watch_or_historical_target(self) -> None:
        snapshot = CHECK.validate_registry()
        by_id = {entry["id"]: entry for entry in snapshot["opportunities"]}
        for action in snapshot["ranked_actions"]:
            self.assertIn(by_id[action["opportunity_id"]]["disposition"], CHECK.ACTIVE_DISPOSITIONS)


class RegistryFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RegistryFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def validate_registry(self) -> dict:
        return CHECK.validate_registry(
            self.fixture.snapshot,
            self.fixture.schema,
            root=self.fixture.root,
            evidence_path=self.fixture.evidence,
        )

    def validate_all(self):
        return CHECK.validate_all(
            snapshot_path=self.fixture.snapshot,
            schema_path=self.fixture.schema,
            receipt_path=self.fixture.receipt,
            consumer_path=self.fixture.consumer,
            evidence_path=self.fixture.evidence,
            root=self.fixture.root,
        )

    def test_verified_fact_without_declared_source_fails_closed(self) -> None:
        data = self.fixture.data()
        data["opportunities"][0]["facts"][0]["source"] = "https://example.invalid/not-declared"
        self.fixture.write(data)
        with self.assertRaisesRegex(CHECK.RegistryError, "verified fact lacks a declared source"):
            self.validate_registry()

    def test_unstated_fact_without_named_resolution_route_fails_closed(self) -> None:
        data = self.fixture.data()
        entry = next(row for row in data["opportunities"] if row["id"] == "screendance-miami-2027")
        fact = next(row for row in entry["facts"] if row["id"] == "runtime")
        fact.pop("resolve")
        self.fixture.write(data)
        with self.assertRaisesRegex(CHECK.RegistryError, "snapshot schema failure|resolution route"):
            self.validate_registry()

    def test_closed_target_cannot_enter_ranked_current_actions(self) -> None:
        data = self.fixture.data()
        data["ranked_actions"].append(
            {
                "rank": len(data["ranked_actions"]) + 1,
                "opportunity_id": "bakehouse-studio-residency-2026",
                "action": "stale work",
            }
        )
        self.fixture.write(data)
        with self.assertRaisesRegex(CHECK.RegistryError, "ranked actions"):
            self.validate_registry()

    def test_missing_plan_target_fails_the_census(self) -> None:
        data = self.fixture.data()
        data["opportunities"] = data["opportunities"][:-1]
        self.fixture.write(data)
        with self.assertRaisesRegex(CHECK.RegistryError, "target census"):
            self.validate_registry()

    def test_external_action_cannot_be_marked_completed(self) -> None:
        data = self.fixture.data()
        entry = next(row for row in data["opportunities"] if row["id"] == "screendance-miami-2027")
        entry["human_gates"][0]["status"] = "completed"
        self.fixture.write(data)
        with self.assertRaisesRegex(CHECK.RegistryError, "snapshot schema failure|falsely marked complete"):
            self.validate_registry()

    def test_snapshot_byte_tamper_invalidates_receipt(self) -> None:
        data = self.fixture.data()
        data["ranked_actions"][0]["action"] += " Tampered."
        self.fixture.write(data)
        snapshot = self.validate_registry()
        with self.assertRaisesRegex(CHECK.RegistryError, "digest is missing or stale"):
            CHECK.validate_binding(
                snapshot,
                root=self.fixture.root,
                snapshot_path=self.fixture.snapshot,
                receipt_path=self.fixture.receipt,
                consumer_path=self.fixture.consumer,
            )

    def test_submission_consumer_must_name_exact_frozen_digest(self) -> None:
        text = self.fixture.consumer.read_text(encoding="utf-8")
        text = text.replace(CHECK.digest(self.fixture.snapshot), "0" * 64)
        self.fixture.consumer.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(CHECK.RegistryError, "does not consume the exact frozen snapshot"):
            self.validate_all()

    def test_operational_submission_drift_invalidates_frozen_contract(self) -> None:
        text = self.fixture.consumer.read_text(encoding="utf-8")
        text = text.replace(
            'hard_wall: "2026-08-31T22:00:00-04:00"',
            'hard_wall: "2026-08-31T21:00:00-04:00"',
        )
        self.fixture.consumer.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(CHECK.RegistryError, "complete operational ScreenDance register"):
            self.validate_all()

    def test_verified_term_cannot_postdate_its_frozen_source_evidence(self) -> None:
        text = self.fixture.consumer.read_text(encoding="utf-8")
        text = text.replace(
            "checked: 2026-08-26\n    check: manual\n\n  - id: festival-scheduling-discretion",
            "checked: 2026-08-27\n    check: manual\n\n  - id: festival-scheduling-discretion",
        )
        self.fixture.consumer.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(CHECK.RegistryError, "postdates its frozen source check"):
            self.validate_all()

    def test_submission_named_timezone_must_match_the_frozen_deadline(self) -> None:
        text = self.fixture.consumer.read_text(encoding="utf-8")
        text = text.replace("timezone: America/New_York", "timezone: America/Toronto")
        self.fixture.consumer.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(CHECK.RegistryError, "not the frozen deadline timezone"):
            self.validate_all()

    def test_submission_malformed_timezone_fails_closed(self) -> None:
        text = self.fixture.consumer.read_text(encoding="utf-8")
        text = text.replace("timezone: America/New_York", "timezone: /UTC")
        self.fixture.consumer.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(CHECK.RegistryError, "timezone is unavailable"):
            self.validate_all()

    def test_cross_platform_private_paths_fail_closed(self) -> None:
        markers = (
            "/var/tmp/private-source",
            r"C:\Users\artist\private-source",
            r"\\server\share\private-source",
            "~/private-source",
            "file:///private-source",
            "source=/home/artist/private-source",
            r"path:C:\Users\artist\private-source",
        )
        for marker in markers:
            with self.subTest(marker=marker):
                data = self.fixture.data()
                data["opportunities"][0]["next_action"] = marker
                self.fixture.write(data)
                with self.assertRaisesRegex(CHECK.RegistryError, "private/local path marker"):
                    self.validate_registry()

    def test_source_response_evidence_is_digest_bound(self) -> None:
        evidence = json.loads(self.fixture.evidence.read_text(encoding="utf-8"))
        evidence["responses"][0]["sha256"] = "0" * 64
        self.fixture.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(CHECK.RegistryError, "source-evidence manifest digest"):
            self.validate_registry()

    def test_live_queue_expires_without_mutating_frozen_snapshot(self) -> None:
        snapshot = self.validate_registry()
        CHECK.validate_operational(snapshot, datetime.fromisoformat("2026-09-01T01:58:00+00:00"))
        with self.assertRaisesRegex(CHECK.RegistryError, "issue #22 must publish a successor"):
            CHECK.validate_operational(snapshot, datetime.fromisoformat("2026-09-01T02:00:00+00:00"))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_receipt_path_cannot_follow_a_symlink(self) -> None:
        outside = Path(self.fixture.temporary.name) / "outside.json"
        outside.write_text(self.fixture.snapshot.read_text(encoding="utf-8"), encoding="utf-8")
        self.fixture.snapshot.unlink()
        self.fixture.snapshot.symlink_to(outside)
        with self.assertRaisesRegex(CHECK.RegistryError, "traverses a symlink"):
            CHECK.safe_file(
                self.fixture.root,
                "opportunities/omega-20260829.json",
                "receipt snapshot path",
            )


if __name__ == "__main__":
    unittest.main()
