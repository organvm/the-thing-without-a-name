#!/usr/bin/env python3
"""Adversarial regressions for the bounded Danse installation contracts."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from installation.contract import (  # noqa: E402
    ARCHIVE_DISPOSITION,
    GATES,
    SPEC,
    ContractError,
    calibration_plan,
    canonical_sha256,
    frame_ticket,
    installation_workbook,
    installation_contract_sha256,
    load_json,
    load_reference_contracts,
    physical_configuration_sha256,
    runtime_plan,
    validate_archive_disposition,
    validate_digital_twin,
    validate_evidence,
    validate_gates,
)
from installation.runtime import Telemetry, supervise  # noqa: E402
from installation.simulation import run_portable_simulation  # noqa: E402


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refresh_identity(spec: dict) -> dict:
    spec["identity"]["contract_sha256"] = installation_contract_sha256(spec)
    return spec


def run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=120
    )


def make_release(root: Path, spec: dict) -> None:
    (root / "bin").mkdir()
    (root / "config").mkdir()
    configuration = root / "config/fixture.txt"
    configuration.write_text("fixture configuration\n", encoding="utf-8")
    launcher = root / "bin/danse-launcher"
    launcher.write_text(
        '#!/bin/sh\nrelease_root="${0%/*}/.."\n'
        'IFS= read -r line < "$release_root/config/fixture.txt"\n'
        '[ "$line" = "fixture configuration" ]\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    manifest = {
        "schema": spec["release"]["manifest_schema"],
        "spec_contract_sha256": spec["identity"]["contract_sha256"],
        "files": [
            {
                "path": "bin/danse-launcher",
                "bytes": launcher.stat().st_size,
                "sha256": file_digest(launcher),
                "executable": True,
            },
            {
                "path": "config/fixture.txt",
                "bytes": configuration.stat().st_size,
                "sha256": file_digest(configuration),
                "executable": False,
            },
        ],
    }
    (root / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def refresh_configuration(evidence: dict, spec: dict, release_root: Path) -> dict:
    configuration_sha256 = physical_configuration_sha256(
        evidence, spec, file_digest(release_root / "bin/danse-launcher")
    )
    evidence["restore_rehearsal"]["configuration_sha256"] = configuration_sha256
    for proof in evidence["wall_plug_proofs"]:
        proof["configuration_sha256"] = configuration_sha256
    return evidence


def evidence_for(spec: dict, release_root: Path, *, complete: bool = False) -> dict:
    contract_sha = spec["identity"]["contract_sha256"]
    argv = ["bin/danse-launcher", "--foreground"]
    proofs = []
    if complete:
        proofs = [
            {
                "id": f"wall-plug-{index}",
                "observer": "Fixture Observer",
                "observed_at": f"2026-08-04T12:0{index}:00Z",
                "power_removed_seconds": 2.0,
                "returned_to_display_seconds": 90.0 + index,
                "generative_display_returned": True,
                "manual_repair_required": False,
                "spec_contract_sha256": contract_sha,
                "runtime_telemetry_sha256": digest(f"telemetry-{index}"),
                "receipt_sha256": digest(f"wall-receipt-{index}"),
            }
            for index in range(1, 4)
        ]
    evidence = {
        "schema": "danse.installation.evidence.v1",
        "evidence_id": "synthetic-test-evidence",
        "spec_contract_sha256": contract_sha,
        "venue": {
            "id": "test-venue",
            "approved": True,
            "approved_by": "Fixture Venue Authority",
            "approved_at": "2026-08-04T11:00:00Z",
            "approval_receipt_sha256": digest("venue-approval"),
            "dimensions_m": {"width": 8.0, "height": 4.0, "depth": 8.0},
            "egress_approved": True,
            "mounting_approved": True,
            "power_approved": True,
            "ventilation_approved": True,
            "safety_receipt_sha256": digest("venue-safety"),
        },
        "geometry": {
            "surfaces": [
                {
                    "reference_surface": "reference-front-plane",
                    "hardware_role": "surface-front",
                    "center_m": [0.0, 0.0, 1.0],
                    "rotation_radians": [0.0, 0.0, 0.0],
                    "size_m": [4.0, 3.0],
                    "measurement_receipt_sha256": digest("surface-front-geometry"),
                },
                {
                    "reference_surface": "reference-rear-plane",
                    "hardware_role": "surface-rear",
                    "center_m": [0.0, 0.0, -1.0],
                    "rotation_radians": [0.0, 0.0, 0.0],
                    "size_m": [4.0, 3.0],
                    "measurement_receipt_sha256": digest("surface-rear-geometry"),
                },
            ],
            "projectors": [
                {
                    "output": "projection-a",
                    "hardware_role": "projection-a",
                    "surface": "reference-front-plane",
                    "position_m": [0.0, 0.0, 5.0],
                    "aim_point_m": [0.0, 0.0, 1.0],
                    "throw_distance_m": 4.0,
                    "resolution_px": [1920, 1080],
                    "refresh_hz": 60.0,
                    "lens_receipt_sha256": digest("projection-a-lens"),
                },
                {
                    "output": "projection-b",
                    "hardware_role": "projection-b",
                    "surface": "reference-rear-plane",
                    "position_m": [0.0, 0.0, 3.0],
                    "aim_point_m": [0.0, 0.0, -1.0],
                    "throw_distance_m": 4.0,
                    "resolution_px": [1920, 1080],
                    "refresh_hz": 60.0,
                    "lens_receipt_sha256": digest("projection-b-lens"),
                },
            ],
            "receipt_sha256": digest("venue-geometry"),
        },
        "release": {
            "root_kind": "canonical-release",
            "manifest_path": "release-manifest.json",
            "manifest_sha256": file_digest(release_root / "release-manifest.json"),
            "developer_checkout": False,
        },
        "hardware": {
            "assets": [
                {
                    "role": role,
                    "asset_id": f"fixture-{role}",
                    "verified": True,
                    "receipt_sha256": digest(f"asset-{role}"),
                }
                for role in spec["hardware_roles"]
            ],
            "cabling_receipt_sha256": digest("cabling"),
            "power_receipt_sha256": digest("power"),
            "ventilation_receipt_sha256": digest("ventilation"),
        },
        "calibration": {
            "spec_contract_sha256": contract_sha,
            "projector_registration_error_px": 1.0,
            "output_skew_ms": 10.0,
            "audio_visual_skew_ms": 20.0,
            "speaker_route_errors": 0,
            "limiter_ceiling_dbfs": -1.0,
            "visible_plane_cue": {
                "passed": True,
                "observer": "Fixture Observer",
                "observed_at": "2026-08-04T11:30:00Z",
                "receipt_sha256": digest("visible-plane-cue"),
            },
            "receipt_sha256": digest("calibration"),
        },
        "runtime": {
            "approved": True,
            "approved_by": "Fixture Venue Authority",
            "approval_receipt_sha256": digest("runtime-approval"),
            "argv": argv,
            "argv_sha256": canonical_sha256(argv),
            "health_url": None,
            "river": {"seed": 20170620, "stream": 7, "epoch_ms": 1785855600000},
        },
        "wall_plug_proofs": proofs,
        "restore_rehearsal": {
            "setup_passed": complete,
            "strike_passed": complete,
            "restore_passed": complete,
            "canonical_release_restored": complete,
            "observer": "Fixture Observer" if complete else None,
            "observed_at": "2026-08-04T13:00:00Z" if complete else None,
            "setup_receipt_sha256": digest("setup") if complete else None,
            "strike_receipt_sha256": digest("strike") if complete else None,
            "restore_receipt_sha256": digest("restore") if complete else None,
            "configuration_sha256": "0" * 64,
        },
    }
    return refresh_configuration(evidence, spec, release_root)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FailedProcess:
    def poll(self) -> int:
        return 1

    def wait(self, timeout: float | None = None) -> int:
        return 1


class SuccessfulProcess:
    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        return 0


class RunningProcess:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None):
        return self.returncode


class InstallationContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec, cls.gates, cls.archive = load_reference_contracts()

    def test_reference_contracts_and_schemas_are_explicit(self) -> None:
        self.assertEqual(validate_digital_twin(load_json(SPEC)), self.spec)
        self.assertEqual(validate_gates(load_json(GATES), self.spec), self.gates)
        self.assertEqual(
            validate_archive_disposition(load_json(ARCHIVE_DISPOSITION)), self.archive
        )
        for path in (
            ROOT / "installation/digital-twin.schema.json",
            ROOT / "installation/evidence.schema.json",
            ROOT / "installation/release-manifest.schema.json",
        ):
            schema = load_json(path)
            self.assertEqual(
                schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )
            self.assertFalse(schema["additionalProperties"])
        evidence_schema = load_json(ROOT / "installation/evidence.schema.json")
        restore = evidence_schema["properties"]["restore_rehearsal"]["properties"]
        self.assertEqual(
            restore["observed_at"]["anyOf"][0]["$ref"], "#/$defs/timestamp"
        )
        for field in (
            "setup_receipt_sha256",
            "strike_receipt_sha256",
            "restore_receipt_sha256",
            "configuration_sha256",
        ):
            self.assertEqual(restore[field]["anyOf"][0]["$ref"], "#/$defs/sha256")
        wall_plug = evidence_schema["$defs"]["wall_plug_proof"]
        self.assertIn("configuration_sha256", wall_plug["required"])
        self.assertEqual(
            wall_plug["properties"]["configuration_sha256"]["$ref"],
            "#/$defs/sha256",
        )
        timestamp_pattern = evidence_schema["$defs"]["timestamp"]["pattern"]
        self.assertIsNotNone(
            re.fullmatch(timestamp_pattern, "2026-08-04T12:34:56.789Z")
        )
        self.assertIsNotNone(re.fullmatch(timestamp_pattern, "2024-02-29T12:34:56Z"))
        self.assertIsNone(re.fullmatch(timestamp_pattern, "garbageZ"))
        self.assertIsNone(re.fullmatch(timestamp_pattern, "2026-02-31T12:34:56Z"))
        self.assertIsNone(re.fullmatch(timestamp_pattern, "1900-02-29T12:34:56Z"))

    def test_source_documents_are_reused_from_authenticated_buffers(self) -> None:
        with patch(
            "installation.contract.load_json",
            side_effect=AssertionError("source pathname was reopened"),
        ):
            self.assertEqual(validate_digital_twin(copy.deepcopy(self.spec)), self.spec)

    def test_projector_camera_is_value_identical_to_engine_room(self) -> None:
        script = """
          import { HALF_W, HALF_H, PROJECTOR_DIST, projector } from './engine/room.js';
          const value = projector();
          console.log(JSON.stringify({half:[HALF_W, HALF_H], distance:PROJECTOR_DIST, eye:value.eye, fovy:value.fovy, aspect:value.aspect}));
        """
        result = run("node", "--input-type=module", "--eval", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        camera = self.spec["projector_camera"]
        self.assertEqual(observed["half"], camera["picture_plane_half_extents"])
        self.assertEqual(observed["distance"], camera["eye"][2])
        self.assertEqual(observed["eye"], camera["eye"])
        self.assertAlmostEqual(observed["fovy"], camera["fovy_radians"], places=15)
        self.assertAlmostEqual(observed["aspect"], camera["aspect"], places=15)

    def test_frame_tickets_are_pure_seekable_and_shared_by_every_output(self) -> None:
        first = frame_ticket(self.spec, 0x12345678, 7, 120)
        again = frame_ticket(self.spec, 0x12345678, 7, 120)
        later = frame_ticket(self.spec, 0x12345678, 7, 900)
        out_of_order = frame_ticket(self.spec, 0x12345678, 7, 120)
        self.assertEqual(first, again)
        self.assertEqual(first, out_of_order)
        self.assertNotEqual(first["ticket_sha256"], later["ticket_sha256"])
        self.assertEqual(first["t"], 2.0)
        self.assertEqual(
            {row["id"] for row in first["outputs"]}, {"projection-a", "projection-b"}
        )
        self.assertEqual(
            len(
                {
                    (
                        first["river"]["seed"],
                        first["river"]["stream"],
                        first["frame"],
                        first["t"],
                    )
                }
            ),
            1,
        )

    def test_calibration_plan_is_deterministic_and_covers_outputs_and_speakers(
        self,
    ) -> None:
        first = calibration_plan(self.spec)
        second = calibration_plan(copy.deepcopy(self.spec))
        self.assertEqual(first, second)
        self.assertEqual(
            [row["output"] for row in first["projection"]],
            ["projection-a", "projection-b"],
        )
        self.assertEqual(
            [row["speaker"] for row in first["audio"]],
            self.spec["audio"]["speaker_ids"],
        )
        self.assertEqual(first["physical_measurements"], "required-not-present")
        self.assertEqual(
            first["plan_sha256"],
            canonical_sha256(
                {key: value for key, value in first.items() if key != "plan_sha256"}
            ),
        )

    def test_clean_setup_workbook_is_derived_complete_and_never_evidence(self) -> None:
        first = installation_workbook(self.spec, self.gates)
        second = installation_workbook(
            copy.deepcopy(self.spec), copy.deepcopy(self.gates)
        )
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "worksheet-not-evidence")
        self.assertEqual(first["private_values"], "collect-externally-never-commit")
        self.assertEqual(first["hardware_roles"], self.spec["hardware_roles"])
        self.assertEqual(len(first["surfaces"]), len(self.spec["surfaces"]))
        self.assertEqual(
            [row["output"] for row in first["projectors"]],
            ["projection-a", "projection-b"],
        )
        self.assertEqual(first["calibration"], calibration_plan(self.spec))
        self.assertFalse(first["completion"]["portable_simulation_is_physical_proof"])
        self.assertEqual(
            first["workbook_sha256"],
            canonical_sha256(
                {key: value for key, value in first.items() if key != "workbook_sha256"}
            ),
        )

    def test_portable_simulation_executes_real_bounded_control_paths(self) -> None:
        first = run_portable_simulation(self.spec)
        second = run_portable_simulation(copy.deepcopy(self.spec))
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "passed-not-physical-evidence")
        self.assertTrue(first["logical_sync"]["passed"])
        self.assertFalse(first["logical_sync"]["hardware_sync_measured"])
        self.assertEqual(
            first["scenarios"]["clean-exit"]["terminal_event"], "launcher-exit"
        )
        for name in ("crash-storm", "startup-health-failure"):
            self.assertEqual(first["scenarios"][name]["exit_code"], 75)
            self.assertEqual(
                first["scenarios"][name]["terminal_event"],
                "recovery-budget-exhausted",
            )
            self.assertEqual(first["scenarios"][name]["launcher_starts"], 4)
        self.assertEqual(
            first["scenarios"]["release-integrity-failure"]["exit_code"], 78
        )
        self.assertEqual(first["physical_claims"]["power_cycles_observed"], 0)
        self.assertFalse(first["physical_claims"]["issue_14_can_close"])
        self.assertEqual(
            first["receipt_sha256"],
            canonical_sha256(
                {key: value for key, value in first.items() if key != "receipt_sha256"}
            ),
        )

    def test_stale_identity_source_or_threshold_fails_closed(self) -> None:
        stale_identity = copy.deepcopy(self.spec)
        stale_identity["identity"]["contract_sha256"] = "f" * 64
        with self.assertRaisesRegex(ContractError, "contract_sha256 is stale"):
            validate_digital_twin(stale_identity)

        stale_source = copy.deepcopy(self.spec)
        next(row for row in stale_source["source_contracts"] if row["id"] == "score")[
            "sha256"
        ] = "f" * 64
        refresh_identity(stale_source)
        with self.assertRaisesRegex(ContractError, "score bytes drifted"):
            validate_digital_twin(stale_source)

        weakened_source = copy.deepcopy(self.spec)
        score = next(
            row for row in weakened_source["source_contracts"] if row["id"] == "score"
        )
        del score["embedded_contract_sha256"]
        refresh_identity(weakened_source)
        with self.assertRaisesRegex(ContractError, "score has an unknown shape"):
            validate_digital_twin(weakened_source)

        wider_gate = copy.deepcopy(self.spec)
        wider_gate["calibration"]["thresholds"]["max_output_skew_ms"] = 100.0
        refresh_identity(wider_gate)
        with self.assertRaisesRegex(ContractError, "thresholds disagree"):
            validate_digital_twin(wider_gate)

    def test_gate_ledger_cannot_claim_completion_or_omit_a_gate(self) -> None:
        promoted = copy.deepcopy(self.gates)
        promoted["status"] = "complete"
        promoted["physical_predicates_satisfied"] = True
        promoted["issue_14_can_close"] = True
        with self.assertRaisesRegex(ContractError, "cannot claim physical completion"):
            validate_gates(promoted, self.spec)
        missing = copy.deepcopy(self.gates)
        missing["gates"].pop()
        with self.assertRaisesRegex(ContractError, "inventory is incomplete"):
            validate_gates(missing, self.spec)

    def test_archive_proposal_is_dispositioned_without_authority_or_wholesale_merge(
        self,
    ) -> None:
        self.assertEqual(
            self.archive["source"]["authority"], "non-authoritative-evidence-only"
        )
        self.assertFalse(self.archive["source"]["merge_wholesale"])
        self.assertFalse(self.archive["result"]["physical_evidence_present"])
        value = copy.deepcopy(self.archive)
        value["source"]["authority"] = "venue-approved"
        with self.assertRaisesRegex(ContractError, "source identity drifted"):
            validate_archive_disposition(value)

    def test_runtime_phase_requires_external_venue_release_hardware_and_calibration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            evidence = evidence_for(self.spec, release)
            evidence["restore_rehearsal"]["configuration_sha256"] = None
            self.assertEqual(
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                ),
                evidence,
            )
            plan = runtime_plan(evidence, self.spec, release)
            self.assertEqual(plan["argv"], ["bin/danse-launcher", "--foreground"])
            self.assertEqual(
                plan["configuration_sha256"],
                physical_configuration_sha256(
                    evidence,
                    self.spec,
                    file_digest(release / "bin/danse-launcher"),
                ),
            )
            self.assertEqual(plan["outputs"], ["projection-a", "projection-b"])
            self.assertEqual(plan["evidence_sha256"], canonical_sha256(evidence))
            self.assertEqual(
                plan["launcher"]["sha256"],
                file_digest(release / "bin/danse-launcher"),
            )
            self.assertEqual(
                [record["path"] for record in plan["release_files"]],
                ["bin/danse-launcher", "config/fixture.txt"],
            )
            self.assertEqual(
                hashlib.sha256(
                    plan["release_manifest"]["content"].encode("utf-8")
                ).hexdigest(),
                plan["release_manifest_sha256"],
            )

            evidence["venue"]["approved"] = False
            with self.assertRaisesRegex(
                ContractError, "venue must be explicitly approved"
            ):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

            evidence = evidence_for(self.spec, release)
            evidence["runtime"]["health_url"] = "http://localhost:8787/health"
            with self.assertRaisesRegex(ContractError, "numeric loopback"):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

            for escaping_argument in (
                str(release / "config/fixture.txt"),
                "--config=../outside.json",
            ):
                evidence = evidence_for(self.spec, release)
                evidence["runtime"]["argv"].append(escaping_argument)
                evidence["runtime"]["argv_sha256"] = canonical_sha256(
                    evidence["runtime"]["argv"]
                )
                with self.assertRaisesRegex(ContractError, "verified release snapshot"):
                    validate_evidence(
                        evidence, self.spec, phase="runtime", release_root=release
                    )

    def test_release_and_launcher_paths_reject_developer_roots_symlinks_and_stale_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            evidence = evidence_for(self.spec, release)
            evidence["release"]["manifest_sha256"] = "f" * 64
            with self.assertRaisesRegex(ContractError, "manifest digest"):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

            evidence = evidence_for(self.spec, release)
            link = release / "bin/linked-launcher"
            link.symlink_to(release / "bin/danse-launcher")
            evidence["runtime"]["argv"][0] = "bin/linked-launcher"
            evidence["runtime"]["argv_sha256"] = canonical_sha256(
                evidence["runtime"]["argv"]
            )
            with self.assertRaisesRegex(ContractError, "symlink"):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

        with tempfile.TemporaryDirectory() as temporary:
            developer_root = Path(temporary)
            make_release(developer_root, self.spec)
            (developer_root / ".git").mkdir()
            evidence = evidence_for(self.spec, developer_root)
            with self.assertRaisesRegex(ContractError, "Git metadata"):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=developer_root
                )

        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            evidence = evidence_for(self.spec, release)
            launcher = release / "bin/danse-launcher"
            original = launcher.read_bytes()
            launcher.write_bytes(original[:-1] + b"#")
            with self.assertRaisesRegex(ContractError, "release file digest drifted"):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            manifest_path = release / "release-manifest.json"
            manifest = load_json(manifest_path)
            manifest["spec_contract_sha256"] = "f" * 64
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            evidence = evidence_for(self.spec, release)
            with self.assertRaisesRegex(ContractError, "another installation contract"):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            (release / "bin/unreviewed-helper").write_text(
                "unbound release byte\n", encoding="utf-8"
            )
            evidence = evidence_for(self.spec, release)
            with self.assertRaisesRegex(ContractError, "release inventory drifted"):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

    def test_hardware_and_calibration_must_cover_exact_roles_and_thresholds(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            evidence = evidence_for(self.spec, release)
            evidence["hardware"]["assets"].pop()
            with self.assertRaisesRegex(ContractError, "every required role"):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

            evidence = evidence_for(self.spec, release)
            evidence["geometry"]["surfaces"][1]["hardware_role"] = "surface-front"
            with self.assertRaisesRegex(ContractError, "present and unique"):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

            evidence = evidence_for(self.spec, release)
            front_role = evidence["geometry"]["surfaces"][0]["hardware_role"]
            rear_role = evidence["geometry"]["surfaces"][1]["hardware_role"]
            evidence["geometry"]["surfaces"][0]["hardware_role"] = rear_role
            evidence["geometry"]["surfaces"][1]["hardware_role"] = front_role
            with self.assertRaisesRegex(ContractError, "wrong surface hardware role"):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

            evidence = evidence_for(self.spec, release)
            evidence["calibration"]["output_skew_ms"] = 16.668
            with self.assertRaisesRegex(
                ContractError, "exceeds the admitted threshold"
            ):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

            evidence = evidence_for(self.spec, release)
            evidence["geometry"]["projectors"][0]["throw_distance_m"] = 3.5
            with self.assertRaisesRegex(ContractError, "throw distance disagrees"):
                validate_evidence(
                    evidence, self.spec, phase="runtime", release_root=release
                )

    def test_completion_requires_three_distinct_human_wall_plug_proofs_and_restore(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            evidence = evidence_for(self.spec, release, complete=True)
            self.assertEqual(
                validate_evidence(
                    evidence, self.spec, phase="complete", release_root=release
                ),
                evidence,
            )

            missing = copy.deepcopy(evidence)
            missing["wall_plug_proofs"].pop()
            with self.assertRaisesRegex(ContractError, "exactly 3 wall-plug proofs"):
                validate_evidence(
                    missing, self.spec, phase="complete", release_root=release
                )

            repaired = copy.deepcopy(evidence)
            repaired["wall_plug_proofs"][1]["manual_repair_required"] = True
            with self.assertRaisesRegex(ContractError, "required manual repair"):
                validate_evidence(
                    repaired, self.spec, phase="complete", release_root=release
                )

            duplicate = copy.deepcopy(evidence)
            duplicate["wall_plug_proofs"][1]["runtime_telemetry_sha256"] = duplicate[
                "wall_plug_proofs"
            ][0]["runtime_telemetry_sha256"]
            with self.assertRaisesRegex(ContractError, "distinct telemetry"):
                validate_evidence(
                    duplicate, self.spec, phase="complete", release_root=release
                )

            duplicate_time = copy.deepcopy(evidence)
            duplicate_time["wall_plug_proofs"][1]["observer"] = "Another Observer"
            duplicate_time["wall_plug_proofs"][1]["observed_at"] = duplicate_time[
                "wall_plug_proofs"
            ][0]["observed_at"]
            with self.assertRaisesRegex(ContractError, "distinct observation times"):
                validate_evidence(
                    duplicate_time,
                    self.spec,
                    phase="complete",
                    release_root=release,
                )

            duplicate_proof_receipt = copy.deepcopy(evidence)
            duplicate_proof_receipt["wall_plug_proofs"][1]["receipt_sha256"] = (
                duplicate_proof_receipt["wall_plug_proofs"][0]["receipt_sha256"]
            )
            with self.assertRaisesRegex(ContractError, "distinct observation receipts"):
                validate_evidence(
                    duplicate_proof_receipt,
                    self.spec,
                    phase="complete",
                    release_root=release,
                )

            stale_configuration = copy.deepcopy(evidence)
            stale_configuration["wall_plug_proofs"][0]["configuration_sha256"] = digest(
                "different-physical-configuration"
            )
            with self.assertRaisesRegex(
                ContractError, "another admitted physical configuration"
            ):
                validate_evidence(
                    stale_configuration,
                    self.spec,
                    phase="complete",
                    release_root=release,
                )

            stale_restore_configuration = copy.deepcopy(evidence)
            stale_restore_configuration["restore_rehearsal"]["configuration_sha256"] = (
                digest("different-restore-configuration")
            )
            with self.assertRaisesRegex(
                ContractError, "restore rehearsal belongs to another"
            ):
                validate_evidence(
                    stale_restore_configuration,
                    self.spec,
                    phase="complete",
                    release_root=release,
                )

            duplicate_restore_receipt = copy.deepcopy(evidence)
            duplicate_restore_receipt["restore_rehearsal"]["strike_receipt_sha256"] = (
                duplicate_restore_receipt["restore_rehearsal"]["setup_receipt_sha256"]
            )
            with self.assertRaisesRegex(ContractError, "receipts must be distinct"):
                validate_evidence(
                    duplicate_restore_receipt,
                    self.spec,
                    phase="complete",
                    release_root=release,
                )

            no_restore = copy.deepcopy(evidence)
            no_restore["restore_rehearsal"]["restore_passed"] = False
            with self.assertRaisesRegex(
                ContractError, "restore_passed must be explicitly approved"
            ):
                validate_evidence(
                    no_restore, self.spec, phase="complete", release_root=release
                )

            partial_runtime = evidence_for(self.spec, release)
            partial_runtime["restore_rehearsal"]["setup_passed"] = True
            with self.assertRaisesRegex(
                ContractError, "strike_passed must be explicitly approved"
            ):
                validate_evidence(
                    partial_runtime,
                    self.spec,
                    phase="runtime",
                    release_root=release,
                )

            malformed_runtime = evidence_for(self.spec, release)
            malformed_runtime["wall_plug_proofs"] = copy.deepcopy(
                evidence["wall_plug_proofs"][:1]
            )
            malformed_runtime["wall_plug_proofs"][0]["manual_repair_required"] = True
            with self.assertRaisesRegex(ContractError, "required manual repair"):
                validate_evidence(
                    malformed_runtime,
                    self.spec,
                    phase="runtime",
                    release_root=release,
                )

    def test_foreground_supervisor_exhausts_a_bounded_restart_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            plan = runtime_plan(evidence_for(self.spec, release), self.spec, release)
            clock = FakeClock()
            output = io.StringIO()
            telemetry = Telemetry(output, clock=clock)
            calls = []
            snapshot_configurations: list[bytes] = []
            snapshot_manifests: list[bytes] = []

            def popen(argv, **kwargs):
                calls.append((argv, kwargs))
                snapshot_configurations.append(
                    (Path(kwargs["cwd"]) / "config/fixture.txt").read_bytes()
                )
                snapshot_manifests.append(
                    (
                        Path(kwargs["cwd"]) / plan["release_manifest"]["path"]
                    ).read_bytes()
                )
                return FailedProcess()

            result = supervise(
                plan,
                release,
                telemetry,
                clock=clock,
                sleep=clock.sleep,
                popen=popen,
            )
            self.assertEqual(result, 75)
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(call[1]["shell"] is False for call in calls))
            self.assertTrue(
                all(
                    Path(call[1]["executable"]).name == "danse-launcher"
                    and str(release) not in call[1]["executable"]
                    for call in calls
                )
            )
            self.assertEqual(len({call[1]["cwd"] for call in calls}), 1)
            self.assertNotEqual(calls[0][1]["cwd"], release)
            self.assertEqual(
                snapshot_configurations,
                [b"fixture configuration\n"] * 4,
            )
            self.assertTrue(
                all(
                    hashlib.sha256(content).hexdigest()
                    == plan["release_manifest_sha256"]
                    for content in snapshot_manifests
                )
            )
            self.assertTrue(
                all(
                    call[1]["env"]["DANSE_INSTALLATION_EVIDENCE_SHA256"]
                    == plan["evidence_sha256"]
                    for call in calls
                )
            )
            self.assertTrue(
                all(
                    call[1]["env"]["DANSE_INSTALLATION_CONFIGURATION_SHA256"]
                    == plan["configuration_sha256"]
                    for call in calls
                )
            )
            self.assertTrue(
                all(
                    call[1]["env"]["DANSE_INSTALLATION_LAUNCHER_SHA256"]
                    == plan["launcher"]["sha256"]
                    for call in calls
                )
            )
            self.assertTrue(
                all(
                    call[1]["env"]["DANSE_INSTALLATION_RELEASE_MANIFEST_SHA256"]
                    == plan["release_manifest_sha256"]
                    for call in calls
                )
            )
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(
                [row["sequence"] for row in records], list(range(len(records)))
            )
            self.assertEqual(records[-1]["event"], "recovery-budget-exhausted")
            self.assertNotIn(str(release), output.getvalue())

    def test_foreground_supervisor_rechecks_every_release_file_before_exec(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            plan = runtime_plan(evidence_for(self.spec, release), self.spec, release)
            configuration = release / "config/fixture.txt"
            configuration.write_text("drifted configuration\n", encoding="utf-8")
            output = io.StringIO()
            calls = []

            result = supervise(
                plan,
                release,
                Telemetry(output),
                popen=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
            self.assertEqual(result, 78)
            self.assertEqual(calls, [])
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(records[-1]["event"], "release-integrity-failed")

    def test_runtime_plan_cannot_weaken_the_authenticated_release_inventory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            plan = runtime_plan(evidence_for(self.spec, release), self.spec, release)
            plan["release_files"].pop()
            output = io.StringIO()
            calls = []

            result = supervise(
                plan,
                release,
                Telemetry(output),
                popen=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
            self.assertEqual(result, 78)
            self.assertEqual(calls, [])
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(records[-1]["event"], "release-integrity-failed")

    def test_replaced_fifo_cannot_block_release_admission(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO replacement regression requires POSIX")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            release = base / "release"
            release.mkdir()
            make_release(release, self.spec)
            plan = runtime_plan(evidence_for(self.spec, release), self.spec, release)
            plan_path = base / "runtime-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            configuration = release / "config/fixture.txt"
            configuration.unlink()
            os.mkfifo(configuration)
            script = """
import io
import json
import sys
from pathlib import Path
from installation.contract import ContractError, _stable_file_bytes
from installation.runtime import Telemetry, supervise

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
try:
    _stable_file_bytes(Path(sys.argv[3]), "FIFO fixture", descriptor_bound=True)
except ContractError:
    print("READER=BLOCKED")
else:
    print("READER=UNSAFE")
output = io.StringIO()
result = supervise(plan, Path(sys.argv[2]), Telemetry(output))
sys.stdout.write(output.getvalue())
print(f"RESULT={result}")
"""
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(plan_path),
                    str(release),
                    str(configuration),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(child.stdout.splitlines()[0], "READER=BLOCKED")
            self.assertEqual(child.stdout.splitlines()[-1], "RESULT=78")
            record = json.loads(child.stdout.splitlines()[1])
            self.assertEqual(record["event"], "release-integrity-failed")

    def test_verified_snapshot_survives_a_path_replacement_race(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            plan = runtime_plan(evidence_for(self.spec, release), self.spec, release)
            launcher = release / "bin/danse-launcher"
            configuration = release / "config/fixture.txt"
            expected_launcher = launcher.read_bytes()
            expected_configuration = configuration.read_bytes()
            observed: list[tuple[bytes, bytes]] = []

            def replace_during_popen(argv, **kwargs):
                malicious = release / "bin/malicious"
                malicious.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
                malicious.chmod(0o755)
                launcher.unlink()
                launcher.symlink_to(malicious)
                malicious_configuration = release / "config/malicious.txt"
                malicious_configuration.write_text(
                    "malicious configuration\n", encoding="utf-8"
                )
                configuration.unlink()
                configuration.symlink_to(malicious_configuration)
                snapshot_root = Path(kwargs["cwd"])
                observed.append(
                    (
                        Path(kwargs["executable"]).read_bytes(),
                        (snapshot_root / "config/fixture.txt").read_bytes(),
                    )
                )
                self.assertEqual(argv[0], "bin/danse-launcher")
                self.assertNotEqual(Path(kwargs["executable"]), launcher)
                self.assertNotEqual(snapshot_root, release)
                return SuccessfulProcess()

            result = supervise(
                plan,
                release,
                Telemetry(io.StringIO()),
                popen=replace_during_popen,
            )
            self.assertEqual(result, 0)
            self.assertEqual(observed, [(expected_launcher, expected_configuration)])
            self.assertNotEqual(
                observed[0][0], (release / "bin/malicious").read_bytes()
            )
            self.assertNotEqual(
                observed[0][1], (release / "config/malicious.txt").read_bytes()
            )

    def test_verified_snapshot_launcher_executes_on_the_supported_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            plan = runtime_plan(evidence_for(self.spec, release), self.spec, release)
            output = io.StringIO()
            result = supervise(plan, release, Telemetry(output))
            self.assertEqual(result, 0)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(records[-1]["event"], "launcher-exit")
            self.assertEqual(records[-1]["returncode"], 0)

    def test_health_failure_is_telemetried_and_cannot_restart_without_bound(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            evidence = evidence_for(self.spec, release)
            evidence["runtime"]["health_url"] = "http://127.0.0.1:8787/health"
            refresh_configuration(evidence, self.spec, release)
            plan = runtime_plan(evidence, self.spec, release)
            clock = FakeClock()
            output = io.StringIO()
            telemetry = Telemetry(output, clock=clock)

            result = supervise(
                plan,
                release,
                telemetry,
                clock=clock,
                sleep=clock.sleep,
                popen=lambda *_args, **_kwargs: RunningProcess(),
                health_probe=lambda _url, _timeout: False,
            )
            self.assertEqual(result, 75)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertTrue(any(row["event"] == "health-failed" for row in records))
            unhealthy = [row for row in records if row["event"] == "launcher-unhealthy"]
            self.assertEqual(len(unhealthy), 4)
            self.assertTrue(all(row["reason"] == "startup-health" for row in unhealthy))
            self.assertEqual(records[-1]["event"], "recovery-budget-exhausted")

    def test_zero_exit_before_health_never_claims_runtime_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            make_release(release, self.spec)
            evidence = evidence_for(self.spec, release)
            evidence["runtime"]["health_url"] = "http://127.0.0.1:8787/health"
            refresh_configuration(evidence, self.spec, release)
            plan = runtime_plan(evidence, self.spec, release)
            clock = FakeClock()
            output = io.StringIO()

            result = supervise(
                plan,
                release,
                Telemetry(output, clock=clock),
                clock=clock,
                sleep=clock.sleep,
                popen=lambda *_args, **_kwargs: SuccessfulProcess(),
                health_probe=lambda _url, _timeout: True,
            )
            self.assertEqual(result, 75)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            unhealthy = [row for row in records if row["event"] == "launcher-unhealthy"]
            self.assertEqual(len(unhealthy), 4)
            self.assertTrue(all(row["reason"] == "startup-exit" for row in unhealthy))
            self.assertTrue(all(row["returncode"] == 0 for row in unhealthy))
            self.assertFalse(
                any(
                    row["event"] == "launcher-exit" and row.get("returncode") == 0
                    for row in records
                )
            )

    def test_runtime_source_has_no_persistent_host_mutation_path(self) -> None:
        source = (ROOT / "installation/runtime.py").read_text(encoding="utf-8")
        for forbidden in (
            "launchctl",
            "crontab",
            "shell=True",
            "start_new_session=True",
            "plistlib",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("shell=False", source)

    def test_cli_exposes_reference_truth_and_blocks_physical_claims_without_evidence(
        self,
    ) -> None:
        reference = run("python3", "scripts/check-installation.py")
        self.assertEqual(reference.returncode, 0, reference.stderr)
        status = json.loads(reference.stdout)
        self.assertEqual(status["gate_status"], "blocked")
        self.assertFalse(status["physical_predicates_satisfied"])
        self.assertFalse(status["issue_14_can_close"])
        self.assertEqual(len(status["blocked_gates"]), 8)

        physical = run(
            "python3", "scripts/check-installation.py", "--phase", "complete"
        )
        self.assertNotEqual(physical.returncode, 0)
        self.assertIn("BLOCKED", physical.stderr)

        workbook = run("python3", "scripts/check-installation.py", "--emit", "workbook")
        self.assertEqual(workbook.returncode, 0, workbook.stderr)
        self.assertEqual(
            json.loads(workbook.stdout)["status"], "worksheet-not-evidence"
        )

        simulation = run(
            "python3", "scripts/check-installation.py", "--emit", "simulation"
        )
        self.assertEqual(simulation.returncode, 0, simulation.stderr)
        self.assertEqual(
            json.loads(simulation.stdout)["status"], "passed-not-physical-evidence"
        )


if __name__ == "__main__":
    unittest.main()
