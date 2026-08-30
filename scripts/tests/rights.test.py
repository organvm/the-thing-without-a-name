#!/usr/bin/env python3
"""Portable regressions for the Danse rights and attribution contract."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import rights_contract as RIGHTS  # noqa: E402


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


AUDIO_RENDER_RECEIPT_BYTES = b'{"schema":"danse.audio.render.v1","fixture":true}\n'
REPOSITORY_HEAD = "a" * 40


def source_evidence() -> dict:
    return copy.deepcopy(RIGHTS.load_register()["bindings"]["corpus"]["source"])


def clear_requirements(document: dict) -> None:
    evidence = source_evidence()
    for asset in document["assets"]:
        if asset["disposition"] == "blocked":
            asset["disposition"] = "owned"
            asset["rights_holder"] = asset["rights_holder"] or "Redacted rights holder"
            asset["blocker"] = None
        if asset["public_credit"]["state"] == "pending":
            asset["public_credit"] |= {
                "state": "approved",
                "label": asset["public_credit"]["label"] or f"Approved {asset['id']} credit",
            }
        for use in asset["uses"]:
            if use["status"] == "blocked":
                use["status"] = "cleared"
                use["evidence"] = copy.deepcopy(evidence)
                if use["territory"] == "pending":
                    use["territory"] = "worldwide"
                if use["term"] == "pending":
                    use["term"] = "project-duration"
                if use["promotion"] == "pending":
                    use["promotion"] = "allowed"
                if use["archive"] == "pending":
                    use["archive"] = "allowed"
    for gate in document["human_gates"]:
        gate["state"] = "satisfied"
        gate["evidence"] = copy.deepcopy(evidence)
    document["status"] = "cleared"


def make_package(base: Path, document: dict) -> Path:
    package = base / "package"
    (package / "stills").mkdir(parents=True)
    (package / "text").mkdir()
    (package / "provenance").mkdir()
    master = b"rights-test-master"
    screener = b"rights-test-screener"
    origin = b"rights-test-origin"
    score = b"rights-test-score-source"
    generated_stills = {
        f"stills/seed-0x{0x1000 + index:04X}.jpg": f"rights-test-still-{index}".encode()
        for index in range(6)
    }
    (package / "master.mov").write_bytes(master)
    (package / "screener.mp4").write_bytes(screener)
    (package / "stills/origin-2017.jpg").write_bytes(origin)
    (package / "provenance/passage-score.wav").write_bytes(score)
    (package / "provenance/audio-render.json").write_bytes(AUDIO_RENDER_RECEIPT_BYTES)
    for name, payload in generated_stills.items():
        (package / name).write_bytes(payload)
    for binding in document["package_text"]:
        source = ROOT / binding["source"]["path"]
        (package / binding["destination"]).write_bytes(source.read_bytes())

    submission = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
    origin_source = submission["package"]["origin_still"]["source_sha256"]
    source_tree = RIGHTS.expected_delivery_source_sha256("film")
    renderer_source_tree = RIGHTS.expected_renderer_source_sha256("film")
    passage = {
        "seed": "0x1234ABCD",
        "passage_seed": "0x1234ABCD",
        "passage": 0,
        "start": 0.0,
        "t0": 0.0,
        "t1": 390.0,
        "duration": 390.0,
        "corpus_tier": "film",
    }
    audio_identity = fixture_audio_identity(digest_bytes(score))
    text_items = []
    for binding in document["package_text"]:
        payload = (package / binding["destination"]).read_bytes()
        text_items.append(
            {
                "name": binding["destination"],
                "bytes": len(payload),
                "sha256": digest_bytes(payload),
            }
        )
    manifest = {
        "schema": "danse.delivery.manifest.v1",
        "title": "Rights contract test",
        **passage,
        "source_tree_sha256": source_tree,
        "repository_head": REPOSITORY_HEAD,
        "sound": copy.deepcopy(audio_identity),
        "items": [
            {
                "name": "master.mov",
                "bytes": len(master),
                "sha256": digest_bytes(master),
                "sound": copy.deepcopy(audio_identity),
            },
            {
                "name": "screener.mp4",
                "bytes": len(screener),
                "sha256": digest_bytes(screener),
                "sound": copy.deepcopy(audio_identity),
            },
            {
                "name": "provenance/passage-score.wav",
                "bytes": len(score),
                "sha256": digest_bytes(score),
                "sound": copy.deepcopy(audio_identity),
            },
            {
                "name": "provenance/audio-render.json",
                "bytes": len(AUDIO_RENDER_RECEIPT_BYTES),
                "sha256": digest_bytes(AUDIO_RENDER_RECEIPT_BYTES),
            },
            {
                "name": "stills/origin-2017.jpg",
                "bytes": len(origin),
                "sha256": digest_bytes(origin),
                "source": "IMG_1594.JPG",
                "source_sha256": origin_source,
                "copy_mode": "byte-identical",
            },
            *(
                {
                    "name": name,
                    "bytes": len(payload),
                    "sha256": digest_bytes(payload),
                }
                for name, payload in generated_stills.items()
            ),
            *text_items,
        ],
    }
    receipt_root = package / "provenance/producer-receipts"
    receipt_root.mkdir()

    def producer_receipt(name: str, value: dict) -> dict:
        path = receipt_root / f"{name}.json"
        path.write_text(json.dumps(value, indent=2) + "\n")
        return {
            "path": path.relative_to(package).as_posix(),
            "sha256": RIGHTS.sha256(path),
        }

    picture_segment_bytes = digest_bytes(b"fixture rendered picture segment")
    picture_segment = producer_receipt(
        "picture-segment",
        {
            "schema": "danse.render.segment.v1",
            "segment": 0,
            "frames": 11700,
            "inputs": {
                "window": "passage",
                "source_tree_sha256": renderer_source_tree,
                "tier": "film",
                "seed": None,
                "stream": 0,
                "codec": "prores",
                "width": None,
                "height": None,
                "fps": None,
                "segment_frames": 600,
                "start": 0.0,
            },
            "file_sha256": picture_segment_bytes,
        },
    )
    picture_bytes = digest_bytes(b"fixture rendered picture concat")
    picture_concat = producer_receipt(
        "picture-concat",
        {
            "schema": "danse.render.concat.v1",
            "codec": "prores",
            "segments": [
                {
                    "name": "passage-default-seg-000.mov",
                    "receipt_sha256": picture_segment["sha256"],
                }
            ],
            "file_sha256": picture_bytes,
        },
    )
    score_receipt = producer_receipt(
        "score",
        {
            "schema": "danse.score.receipt.v2",
            "sha256": digest_bytes(score),
            "t0": passage["t0"],
            "t1": passage["t1"],
            "duration": passage["duration"],
            **audio_identity,
        },
    )
    producers = [
        {
            "id": "picture-segment",
            "kind": "render-segment",
            "receipt": picture_segment,
            "output_sha256": picture_segment_bytes,
            "components": [],
        },
        {
            "id": "picture-concat",
            "kind": "render-concat",
            "receipt": picture_concat,
            "output_sha256": picture_bytes,
            "components": ["picture-segment"],
        },
        {
            "id": "score",
            "kind": "score",
            "receipt": score_receipt,
            "output_sha256": digest_bytes(score),
            "components": [],
        },
    ]
    outputs = [
        {
            "name": name,
            "bytes": len(payload),
            "sha256": digest_bytes(payload),
            "producers": ["picture-concat", "score"],
        }
        for name, payload in (("master.mov", master), ("screener.mp4", screener))
    ]
    outputs.append(
        {
            "name": "provenance/passage-score.wav",
            "bytes": len(score),
            "sha256": digest_bytes(score),
            "producers": ["score"],
        }
    )
    for index, (name, payload) in enumerate(generated_stills.items()):
        producer_id = f"still-{index}"
        render_sha = digest_bytes(f"fixture render for {name}".encode())
        receipt = producer_receipt(
            producer_id,
            {
                "schema": "danse.render.segment.v1",
                "segment": index,
                "frames": 1,
                "inputs": {
                    "window": "passage",
                    "source_tree_sha256": renderer_source_tree,
                    "tier": "film",
                    "seed": 0x1000 + index,
                    "stream": 0,
                    "codec": "prores",
                    "width": None,
                    "height": None,
                    "fps": None,
                    "segment_frames": 1,
                    "start": 0.0,
                },
                "file_sha256": render_sha,
            },
        )
        producers.append(
            {
                "id": producer_id,
                "kind": "render-segment",
                "receipt": receipt,
                "output_sha256": render_sha,
                "components": [],
            }
        )
        outputs.append(
            {
                "name": name,
                "bytes": len(payload),
                "sha256": digest_bytes(payload),
                "producers": [producer_id],
            }
        )
    production = {
        "schema": "danse.delivery.production.v1",
        "source_tree_sha256": source_tree,
        "repository_head": REPOSITORY_HEAD,
        "passage": passage,
        "sound": copy.deepcopy(audio_identity),
        "producers": producers,
        "outputs": outputs,
    }
    production_path = package / RIGHTS.PRODUCTION_RECEIPT
    production_path.write_text(json.dumps(production, indent=2) + "\n")
    manifest["production"] = {
        "path": RIGHTS.PRODUCTION_RECEIPT,
        "sha256": RIGHTS.sha256(production_path),
    }
    (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    attest = {}
    for gate in document["human_gates"]:
        record = gate["attestation"]
        if record is not None:
            attest[record["key"]] = record["values"][0]
    (package / "attest.yaml").write_text(yaml.safe_dump(attest, sort_keys=True))
    return package


def fixture_audio_identity(master_sha256: str | None = None) -> dict:
    master_sha256 = master_sha256 or digest_bytes(b"rights-test-score-source")
    return {
        "profile": RIGHTS.COMPETITION_AUDIO_PROFILE,
        "audio_uses_sha256": RIGHTS.AUDIO_USES_SHA256,
        "score_file_sha256": digest_bytes(b"fixture-score-contract-file"),
        "score_contract_sha256": digest_bytes(b"fixture-score-contract-value"),
        "choreography_file_sha256": digest_bytes(b"fixture-choreography-file"),
        "choreography_contract_sha256": digest_bytes(b"fixture-choreography-contract"),
        "midi_sha256": RIGHTS.ADAPTED_DELIBES_MIDI_SHA256,
        "adaptation_sha256": RIGHTS.DELIBES_ADAPTATION_SHA256,
        "toolchain_sha256": digest_bytes(b"fixture-toolchain"),
        "mix_sha256": digest_bytes(b"fixture-mix"),
        "soundfont_sha256": RIGHTS.MUSESCORE_GENERAL_SF3_SHA256,
        "audio_render_receipt_sha256": digest_bytes(AUDIO_RENDER_RECEIPT_BYTES),
        "master_sha256": master_sha256,
        "sources": list(RIGHTS.COMPETITION_SOURCE_IDS),
        "stems": [
            {"id": stem_id, "sha256": digest_bytes(f"fixture-stem:{stem_id}".encode())}
            for stem_id in RIGHTS.COMPETITION_STEM_IDS
        ],
        "credit": RIGHTS.REQUIRED_DELIBES_CREDIT,
    }


def validate_fixture_package(document: dict, package: Path) -> tuple[list[str], dict | None]:
    with mock.patch.object(RIGHTS, "current_audio_identity", return_value=fixture_audio_identity()):
        return RIGHTS.validate_package(document, package)


def fixture_phase_blockers(
    document: dict,
    phase: str,
    *,
    package: Path,
) -> tuple[list[str], dict]:
    with mock.patch.object(RIGHTS, "current_audio_identity", return_value=fixture_audio_identity()):
        return RIGHTS.phase_blockers(document, phase, package=package)


def make_release(base: Path, document: dict, *, phase: str = "release") -> tuple[Path, Path, Path]:
    root = base / "repository"
    root.mkdir(parents=True, exist_ok=True)
    evidence_path = root / "evidence.json"
    evidence_path.write_bytes((ROOT / source_evidence()["path"]).read_bytes())
    evidence = {
        "path": "evidence.json",
        "sha256": RIGHTS.sha256(evidence_path),
        "summary": "Tracked public-safe fixture evidence",
    }
    register_path = root / "rights" / "register.json"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    register_path.write_bytes(RIGHTS.REGISTER.read_bytes())
    media = []
    clearance_paths: list[str] = []
    for rule in document["release_rules"]:
        if phase not in rule["required_for"]:
            media.append(
                {
                    "id": rule["media_id"],
                    "required_for": rule["required_for"],
                    "status": "pending",
                    "source": None,
                    "clearance": {"status": "pending"},
                }
            )
            continue
        artifact_path = root / rule["destination"]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(f"exact fixture bytes for {rule['media_id']}\n".encode())
        artifact = {
            "path": rule["destination"],
            "sha256": RIGHTS.sha256(artifact_path),
        }
        artifact["destination"] = artifact["path"]
        artifact["bytes"] = artifact_path.stat().st_size
        clearance_relative = f"rights/evidence/media-{rule['media_id']}.json"
        clearance_path = root / clearance_relative
        clearance_path.parent.mkdir(parents=True, exist_ok=True)
        clearance_path.write_text(
            json.dumps(
                {
                    "schema": "danse.rights.media-clearance.v1",
                    "media_id": rule["media_id"],
                    "destination": rule["destination"],
                    "sha256": artifact["sha256"],
                    "bytes": artifact["bytes"],
                    "authority": "Rights test",
                    "decision": "cleared",
                    "required_for": rule["required_for"],
                },
                indent=2,
            )
            + "\n"
        )
        clearance_paths.append(clearance_relative)
        clearance_evidence = {
            "path": clearance_relative,
            "sha256": RIGHTS.sha256(clearance_path),
            "summary": f"Typed exact-byte clearance for {rule['media_id']}",
        }
        media.append(
            {
                "id": rule["media_id"],
                "required_for": rule["required_for"],
                "status": "ready",
                "source": artifact,
                "clearance": {
                    "status": "cleared",
                    "owner": "Rights test",
                    "evidence": clearance_evidence,
                },
            }
        )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "add",
            "evidence.json",
            "rights/register.json",
            *clearance_paths,
        ],
        check=True,
    )
    credit_rows = [
        {
            "id": rule["credit_id"],
            "name": next(
                asset["public_credit"]["label"]
                for asset in document["assets"]
                if asset["id"] == rule["asset"]
            ),
            "status": "cleared",
            "evidence": copy.deepcopy(evidence),
        }
        for rule in document["credit_rules"]
    ]
    manifest = {
        "schema": "danse.release.v1",
        "release_id": "rights-test",
        "status": "released",
        "media": media,
        "credits": credit_rows,
        "gates": [
            {
                "id": "rights-register",
                "required_for": ["public", "release"],
                "state": "satisfied",
                "evidence": {
                    "path": "rights/register.json",
                    "sha256": RIGHTS.sha256(register_path),
                    "summary": "Exact redacted rights register",
                },
            }
        ],
    }
    path = root / "release-manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path, root, register_path


class RightsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = RIGHTS.load_register()

    def test_draft_cli_validates_exact_sources_schema_and_inventory(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/check-rights.py", "--phase", "draft", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "ready")
        self.assertEqual(receipt["inventory"]["assets"], len(self.document["assets"]))
        self.assertEqual(
            {asset["category"] for asset in self.document["assets"]},
            RIGHTS.EXPECTED_CATEGORIES,
        )
        self.assertEqual(receipt["register"]["sha256"], RIGHTS.sha256(RIGHTS.REGISTER))
        self.assertEqual(receipt["register"]["schema_sha256"], RIGHTS.sha256(RIGHTS.SCHEMA))

    def test_delibes_source_license_and_soundfont_custody_is_exact_not_clearance(self) -> None:
        evidence = RIGHTS.load_json(
            ROOT / RIGHTS.DELIBES_CUSTODY_PATH,
            "selected Delibes custody evidence",
        )
        self.assertEqual(evidence["status"], "custody-only")
        self.assertEqual(evidence["required_credit"], RIGHTS.REQUIRED_DELIBES_CREDIT)
        self.assertEqual(evidence["clearance"]["state"], "pending")
        self.assertEqual(
            evidence["soundfont"]["soundfont_sha256"],
            RIGHTS.MUSESCORE_GENERAL_SF3_SHA256,
        )
        self.assertEqual(
            evidence["soundfont"]["notice"],
            {
                "path": RIGHTS.MUSESCORE_GENERAL_NOTICE_PATH,
                "sha256": RIGHTS.MUSESCORE_GENERAL_NOTICE_SHA256,
            },
        )
        self.assertEqual(
            [(row["path"], row["sha256"]) for row in evidence["source_arrangements"]],
            list(RIGHTS.DELIBES_SOURCE_FILES),
        )
        for relative, expected in (
            *RIGHTS.DELIBES_SOURCE_FILES,
            (RIGHTS.MUSESCORE_GENERAL_NOTICE_PATH, RIGHTS.MUSESCORE_GENERAL_NOTICE_SHA256),
        ):
            self.assertEqual(RIGHTS.sha256(ROOT / relative), expected)

        assets = {asset["id"]: asset for asset in self.document["assets"]}
        self.assertEqual(
            assets["paul-de-bra-source-arrangements"]["license"]["spdx"],
            "CC-BY-4.0",
        )
        self.assertEqual(assets["musescore-general-soundfont"]["license"]["spdx"], "MIT")
        self.assertEqual(
            assets["selected-music"]["public_credit"]["label"],
            RIGHTS.REQUIRED_DELIBES_CREDIT,
        )
        for asset_id in (
            "delibes-public-domain-compositions",
            "paul-de-bra-source-arrangements",
            "adapted-delibes-midi",
            "musescore-general-soundfont",
        ):
            use = assets[asset_id]["uses"][0]
            self.assertEqual(use["required_for"], [])
            self.assertEqual(use["status"], "blocked")
            self.assertIsNone(use["evidence"])
        music_gate = next(
            gate for gate in self.document["human_gates"] if gate["id"] == "music-cleared"
        )
        self.assertEqual(music_gate["state"], "pending")
        self.assertIsNone(music_gate["evidence"])

    def test_delibes_custody_rejects_license_credit_and_clearance_mutations(self) -> None:
        evidence = RIGHTS.load_json(
            ROOT / RIGHTS.DELIBES_CUSTODY_PATH,
            "selected Delibes custody evidence",
        )
        tracked = RIGHTS.tracked_paths(ROOT)

        def custody_errors(candidate: dict) -> list[str]:
            with mock.patch.object(RIGHTS, "load_json", return_value=candidate):
                return RIGHTS._validate_delibes_custody(ROOT, self.document, tracked)

        mutations = []
        wrong_source = copy.deepcopy(evidence)
        wrong_source["source_arrangements"][0]["sha256"] = "0" * 64
        mutations.append((wrong_source, "Paul De Bra source"))
        wrong_license = copy.deepcopy(evidence)
        wrong_license["source_arrangements"][1]["license"]["url"] = "https://example.invalid/"
        mutations.append((wrong_license, "CC BY 4.0"))
        wrong_credit = copy.deepcopy(evidence)
        wrong_credit["required_credit"] = "Music credit omitted."
        mutations.append((wrong_credit, "required credit"))
        false_clearance = copy.deepcopy(evidence)
        false_clearance["clearance"]["state"] = "cleared"
        mutations.append((false_clearance, "falsely changes the clearance gate"))
        wrong_notice = copy.deepcopy(evidence)
        wrong_notice["soundfont"]["notice"]["sha256"] = "f" * 64
        mutations.append((wrong_notice, "soundfont license custody"))
        for candidate, expected in mutations:
            with self.subTest(expected=expected):
                errors = custody_errors(candidate)
                self.assertTrue(any(expected in error for error in errors), errors)

        wrong_asset = copy.deepcopy(self.document)
        selected = next(asset for asset in wrong_asset["assets"] if asset["id"] == "selected-music")
        selected["public_credit"]["label"] = "Incomplete credit"
        errors = RIGHTS._validate_delibes_custody(ROOT, wrong_asset, tracked)
        self.assertTrue(any("exact required credit" in error for error in errors), errors)

        missing_source = copy.deepcopy(self.document)
        arrangement = next(
            asset
            for asset in missing_source["assets"]
            if asset["id"] == "paul-de-bra-source-arrangements"
        )
        arrangement["provenance"] = arrangement["provenance"][:-1]
        errors = RIGHTS._validate_delibes_custody(ROOT, missing_source, tracked)
        self.assertTrue(any("both exact sources" in error for error in errors), errors)

    def test_delibes_custody_rejects_same_name_replacement_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracked = {
                RIGHTS.DELIBES_CUSTODY_PATH,
                RIGHTS.MUSESCORE_GENERAL_NOTICE_PATH,
                *(path for path, _ in RIGHTS.DELIBES_SOURCE_FILES),
            }
            for relative in tracked:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
            source_path = root / RIGHTS.DELIBES_SOURCE_FILES[0][0]
            source_path.write_bytes(b"same filename, unrelated arrangement bytes\n")
            errors = RIGHTS._validate_delibes_custody(root, self.document, tracked)
            self.assertTrue(any("source digest drifted" in error for error in errors), errors)

    def test_audio_use_profiles_quarantine_private_grains_and_bind_licenses(self) -> None:
        tracked = RIGHTS.tracked_paths(ROOT)
        audio_uses = RIGHTS.load_json(ROOT / RIGHTS.AUDIO_USES_PATH, "audio-use profiles")
        competition = audio_uses["profiles"][RIGHTS.COMPETITION_AUDIO_PROFILE]
        hybrid = audio_uses["profiles"][RIGHTS.HYBRID_AUDIO_PROFILE]
        self.assertTrue(competition["package_eligible"])
        self.assertFalse(hybrid["package_eligible"])
        self.assertEqual(
            [row["id"] for row in competition["declared_sources"]],
            list(RIGHTS.COMPETITION_SOURCE_IDS),
        )
        self.assertEqual(competition["required_stems"], list(RIGHTS.COMPETITION_STEM_IDS))
        self.assertNotIn(
            "private-grain-bank",
            {row["kind"] for row in competition["declared_sources"]},
        )
        room = next(
            asset for asset in self.document["assets"] if asset["id"] == "room-source-recordings"
        )
        self.assertEqual(room["uses"][0]["required_for"], [])
        for rule_id in (
            "moving-image",
            "score-source",
            "audio-render-receipt",
            "score-motion-evidence",
        ):
            rule = next(rule for rule in self.document["package_rules"] if rule["id"] == rule_id)
            requirements = [(row["asset"], row["use"]) for row in rule["requirements"]]
            self.assertNotIn(RIGHTS.HYBRID_AUDIO_REQUIREMENT, requirements)
            self.assertEqual(
                [row for row in requirements if row in RIGHTS.COMPETITION_AUDIO_REQUIREMENTS],
                list(RIGHTS.COMPETITION_AUDIO_REQUIREMENTS),
            )

        real_load_json = RIGHTS.load_json

        def profile_errors(candidate: dict) -> list[str]:
            def load_candidate(path: Path, label: str, **kwargs) -> dict:
                if Path(path) == ROOT / RIGHTS.AUDIO_USES_PATH:
                    return candidate
                return real_load_json(path, label, **kwargs)

            with mock.patch.object(RIGHTS, "load_json", side_effect=load_candidate):
                return RIGHTS._validate_audio_use_profiles(ROOT, self.document, tracked)

        private_competition = copy.deepcopy(audio_uses)
        private_competition["profiles"][RIGHTS.COMPETITION_AUDIO_PROFILE][
            "declared_sources"
        ].append(
            {
                "id": "apartment-grain-bank",
                "kind": "private-grain-bank",
                "path": "sound/sources.json",
                "custody": "ignored-private-optional",
            }
        )
        errors = profile_errors(private_competition)
        self.assertTrue(any("forbidden private" in error for error in errors), errors)

        eligible_hybrid = copy.deepcopy(audio_uses)
        eligible_hybrid["profiles"][RIGHTS.HYBRID_AUDIO_PROFILE]["package_eligible"] = True
        errors = profile_errors(eligible_hybrid)
        self.assertTrue(any("package ineligible" in error for error in errors), errors)

    def test_competition_sound_identity_rejects_profile_source_stem_and_credit_substitution(self) -> None:
        identity = fixture_audio_identity()
        self.assertEqual(RIGHTS._audio_identity_blockers(identity, "fixture"), [])
        mutations = []
        wrong_profile = copy.deepcopy(identity)
        wrong_profile["profile"] = RIGHTS.HYBRID_AUDIO_PROFILE
        mutations.append((wrong_profile, "package-eligible competition-classical"))
        private_source = copy.deepcopy(identity)
        private_source["sources"].append("apartment-grain-bank")
        mutations.append((private_source, "exact competition-classical sources"))
        reordered_stems = copy.deepcopy(identity)
        reordered_stems["stems"][0], reordered_stems["stems"][1] = (
            reordered_stems["stems"][1],
            reordered_stems["stems"][0],
        )
        mutations.append((reordered_stems, "reordered stem"))
        wrong_credit = copy.deepcopy(identity)
        wrong_credit["credit"] = "Music by Léo Delibes."
        mutations.append((wrong_credit, "exact required Delibes credit"))
        wrong_notice_chain = copy.deepcopy(identity)
        wrong_notice_chain["toolchain_sha256"] = "not-a-digest"
        mutations.append((wrong_notice_chain, "toolchain_sha256"))
        for candidate, expected in mutations:
            with self.subTest(expected=expected):
                blockers = RIGHTS._audio_identity_blockers(candidate, "fixture")
                self.assertTrue(any(expected in blocker for blocker in blockers), blockers)

    def test_tracked_release_manifest_reserves_every_rights_row_without_inventing_clearance(self) -> None:
        manifest = json.loads((ROOT / "release/manifest.json").read_text(encoding="utf-8"))
        media = {row["id"]: row for row in manifest["media"]}
        release_rules = {row["media_id"]: row for row in self.document["release_rules"]}
        self.assertEqual(set(media), set(release_rules))
        for media_id, rule in release_rules.items():
            self.assertEqual(media[media_id]["required_for"], rule["required_for"])
            self.assertEqual(media[media_id]["status"], "pending")
            self.assertIsNone(media[media_id]["source"])
            self.assertEqual(media[media_id]["clearance"]["status"], "pending")
            self.assertIsNone(media[media_id]["clearance"]["evidence"])

        products = manifest["products"]
        self.assertEqual(
            [(row["id"], row["label"], row["path"]) for row in products],
            [
                ("project-page-copy", "Approved public project page", "project/index.html"),
                (
                    "pitch-pdf-copy",
                    "Approved installation pitch PDF",
                    "pitch/danse-installation-pitch.pdf",
                ),
                (
                    "accessibility-copy",
                    "Approved accessibility statement",
                    "accessibility/accessibility.md",
                ),
                (
                    "caption-track-copy",
                    "Approved English caption track",
                    "accessibility/captions.en.vtt",
                ),
                (
                    "transcript-copy",
                    "Approved public transcript",
                    "accessibility/transcript.txt",
                ),
                ("press-kit-copy", "Approved public press kit", "press/press-kit.md"),
                ("credits-copy", "Approved public credits", "press/credits.txt"),
            ],
        )
        for product in products:
            self.assertEqual(product["kind"], "generated-document")
            self.assertEqual(product["required_for"], ["public", "release"])
            self.assertEqual(product["status"], "pending")

        credits = {row["id"]: row for row in manifest["credits"]}
        credit_rules = {row["credit_id"]: row for row in self.document["credit_rules"]}
        self.assertEqual(set(credits), set(credit_rules))
        mediapipe = credits["mediapipe-credit"]
        pose_asset = next(row for row in self.document["assets"] if row["id"] == "mediapipe-pose-runtime")
        attribution_gate = next(
            row for row in self.document["human_gates"] if row["id"] == "mediapipe-attribution-retained"
        )
        self.assertEqual(mediapipe["status"], "cleared")
        self.assertEqual(mediapipe["name"], pose_asset["public_credit"]["label"])
        self.assertEqual(mediapipe["evidence"], attribution_gate["evidence"])
        for credit_id, credit in credits.items():
            if credit_id == "mediapipe-credit":
                continue
            self.assertEqual(credit["status"], "pending")
            self.assertIsNone(credit["evidence"])

        human_gates = {row["id"]: row for row in self.document["human_gates"]}
        self.assertEqual(
            {row["id"] for row in self.document["human_gates"] if row["state"] == "satisfied"},
            {"mediapipe-attribution-retained"},
        )
        for gate_id in (
            "final-cut-approved",
            "dancer-release-and-credit",
            "link-password-protected",
            "link-downloadable",
            "submitted-via-submittable",
            "accepted-film-no-withdrawal",
            "publicity-stills-free-of-rights",
            "submission-rights-warranty",
            "festival-scheduling-discretion",
            "archive-library-choice",
            "regulations-accepted",
        ):
            self.assertEqual(human_gates[gate_id]["state"], "pending")
            self.assertIsNone(human_gates[gate_id]["evidence"])

        release_gates = {row["id"]: row for row in manifest["gates"]}
        for gate_id in (
            "final-artistic-approval",
            "accessibility-review",
            "contact-route-approval",
            "publication-approval",
            "rights-register",
        ):
            self.assertEqual(release_gates[gate_id]["state"], "pending")
            self.assertIsNone(release_gates[gate_id]["evidence"])
        self.assertEqual(manifest["accessibility"]["captions"]["status"], "pending")
        self.assertEqual(manifest["accessibility"]["transcript"]["status"], "pending")

    def test_every_shipping_phase_fails_closed_without_human_or_exact_artifact_evidence(self) -> None:
        for phase in ("public", "package", "uploaded", "submitted", "release"):
            with self.subTest(phase=phase):
                _, receipt = RIGHTS.validate_all(phase=phase)
                self.assertEqual(receipt["status"], "blocked")
                self.assertTrue(receipt["blockers"])
                self.assertFalse(any("/Users/" in blocker for blocker in receipt["blockers"]))
        _, public = RIGHTS.validate_all(phase="public")
        self.assertTrue(any("dancer-release-and-credit" in blocker for blocker in public["blockers"]))
        self.assertTrue(any("--release-manifest" in blocker for blocker in public["blockers"]))
        _, package = RIGHTS.validate_all(phase="package")
        self.assertTrue(any("--package" in blocker for blocker in package["blockers"]))

    def test_private_paths_contacts_and_sensitive_fields_are_rejected(self) -> None:
        for mutation, expected in (
            (("note", "private release at /Users/example/release.pdf"), "machine-local path"),
            (("note", "private release at /workspace/Alice/private.json"), "machine-local path"),
            (("note", "private release at /tmp/private-release"), "machine-local path"),
            (("note", "private release at /Volumes/archive/evidence.pdf"), "machine-local path"),
            (("note", "private release at D:\\staging\\private.json"), "machine-local path"),
            (("note", "private release at \\\\server\\share\\private.json"), "machine-local path"),
            (("note", "contact dancer@example.test"), "email address"),
            (("note", "call 305-555-0123"), "phone number"),
            (("note", "call +44 20 7123 4567"), "phone number"),
            (("note", "call +33 1 42 68 53 00"), "phone number"),
        ):
            with self.subTest(expected=expected):
                candidate = copy.deepcopy(self.document)
                candidate["assets"][0]["uses"][0][mutation[0]] = mutation[1]
                errors = RIGHTS.validate_document(candidate)
                self.assertTrue(any(expected in error for error in errors), errors)
        candidate = copy.deepcopy(self.document)
        candidate["assets"][0]["private_evidence"]["signature"] = "redacted"
        errors = RIGHTS.validate_document(candidate)
        self.assertTrue(any("sensitive field" in error for error in errors), errors)

    def test_noncanonical_relative_path_spellings_are_rejected(self) -> None:
        for spelling in (
            "media/assets//press-still.webp",
            "media/assets/./press-still.webp",
            "media/assets/press-still.webp/",
        ):
            with self.subTest(spelling=spelling):
                with self.assertRaisesRegex(RIGHTS.RightsError, "safe portable relative path"):
                    RIGHTS.safe_relative(spelling, "test path")
                candidate = copy.deepcopy(self.document)
                candidate["release_rules"][0]["destination"] = spelling
                errors = RIGHTS.validate_document(candidate)
                self.assertTrue(any("safe portable relative path" in error for error in errors), errors)
    def test_stale_conflicting_untracked_and_symlink_evidence_are_rejected(self) -> None:
        stale = copy.deepcopy(self.document)
        stale["assets"][0]["provenance"][0]["sha256"] = "0" * 64
        errors = RIGHTS.validate_document(stale)
        self.assertTrue(any("conflicting digests" in error or "digest mismatch" in error for error in errors), errors)

        untracked = copy.deepcopy(self.document)
        untracked["assets"][0]["provenance"][0] = {
            "path": "rights/not-tracked.txt",
            "sha256": "0" * 64,
            "summary": "Must not validate",
        }
        errors = RIGHTS.validate_document(untracked)
        self.assertTrue(any("not tracked by Git" in error for error in errors), errors)

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repository"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("private")
            link = root / "evidence-link"
            link.symlink_to(outside)
            record = {
                "path": "evidence-link",
                "sha256": RIGHTS.sha256(outside),
                "summary": "Must not validate",
            }
            with self.assertRaisesRegex(RIGHTS.RightsError, "symlink"):
                RIGHTS.verify_record(root, record, "isolated evidence", {"evidence-link"})

    def test_completion_evidence_never_clears_the_wrong_state(self) -> None:
        candidate = copy.deepcopy(self.document)
        evidence = source_evidence()
        candidate["human_gates"][0]["evidence"] = evidence
        candidate["assets"][0]["uses"][0]["evidence"] = evidence
        candidate["assets"][0]["private_evidence"]["receipt"] = evidence
        errors = RIGHTS.validate_document(candidate)
        self.assertTrue(any("carries completion evidence" in error for error in errors), errors)
        self.assertTrue(any("private-evidence receipt" in error for error in errors), errors)

    def test_satisfied_gate_requires_a_typed_gate_authority_and_decision_receipt(self) -> None:
        gate = copy.deepcopy(
            next(
                row
                for row in self.document["human_gates"]
                if row["id"] == "mediapipe-attribution-retained"
            )
        )
        approved_credits = RIGHTS.approved_credit_contract(self.document, gate["id"])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decision.json"
            path.write_text(json.dumps({"schema": "danse.corpus.v1"}))
            decision, errors = RIGHTS.validate_gate_decision_receipt(
                path,
                gate,
                approved_credits,
            )
            self.assertIsNone(decision)
            self.assertTrue(any("typed decision contract" in item for item in errors), errors)

            path.write_text(
                json.dumps(
                    {
                        "schema": "danse.rights.decision.v2",
                        "gate_id": gate["id"],
                        "authority": gate["authority"],
                        "decision": True,
                        "required_for": gate["required_for"],
                        "approved_credits": approved_credits,
                    }
                )
            )
            decision, errors = RIGHTS.validate_gate_decision_receipt(
                path,
                gate,
                approved_credits,
            )
            self.assertIs(decision, True)
            self.assertEqual(errors, [])

            value = json.loads(path.read_text())
            value["approved_credits"][0]["label"] = "Unapproved alternate wording"
            path.write_text(json.dumps(value))
            decision, errors = RIGHTS.validate_gate_decision_receipt(
                path,
                gate,
                approved_credits,
            )
            self.assertIsNone(decision)
            self.assertTrue(any("exact approved credit wording" in item for item in errors), errors)

        mediapipe = next(
            rule for rule in self.document["credit_rules"] if rule["credit_id"] == "mediapipe-credit"
        )
        self.assertEqual(mediapipe["gate"], "mediapipe-attribution-retained")

    def test_cleared_asset_uses_require_typed_exact_scope_receipts(self) -> None:
        asset = next(
            row for row in self.document["assets"] if row["id"] == "mediapipe-pose-runtime"
        )
        use = asset["uses"][0]
        receipt = ROOT / use["evidence"]["path"]
        self.assertEqual(RIGHTS.validate_use_decision_receipt(receipt, asset, use), [])

        unrelated = copy.deepcopy(self.document)
        unrelated_asset = next(
            row for row in unrelated["assets"] if row["id"] == "mediapipe-pose-runtime"
        )
        unrelated_asset["uses"][0]["evidence"] = source_evidence()
        errors = RIGHTS.validate_document(unrelated)
        self.assertTrue(any("typed use-decision contract" in item for item in errors), errors)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "use-decision.json"
            value = json.loads(receipt.read_text())
            value["territory"] = "limited"
            path.write_text(json.dumps(value))
            errors = RIGHTS.validate_use_decision_receipt(path, asset, use)
            self.assertTrue(any("different territory" in item for item in errors), errors)

    def test_public_corpus_binding_authenticates_every_pages_derivative_byte(self) -> None:
        declared = self.document["bindings"]["corpus"]
        identity = RIGHTS.public_corpus_identity(ROOT, RIGHTS.tracked_paths(ROOT))
        self.assertEqual(identity["files"], declared["public_files"])
        self.assertEqual(identity["sha256"], declared["public_tree_sha256"])

        target = ROOT / "corpus/plates/browse/IMG_1570.webp"
        measure = RIGHTS._stable_file_measure

        def tampered_measure(
            path: Path,
            label: str,
            *,
            capture: bool = False,
        ) -> tuple[str, int, bytes | None]:
            digest, size, payload = measure(path, label, capture=capture)
            if path == target and label == "public corpus derivative":
                digest = "0" * 64
            return digest, size, payload

        with mock.patch.object(RIGHTS, "_stable_file_measure", side_effect=tampered_measure):
            errors = RIGHTS.validate_document(copy.deepcopy(self.document))
        self.assertTrue(any("public derivative tree digest has drifted" in item for item in errors), errors)

    def test_every_canonical_submission_assertion_remains_a_phase_owned_gate(self) -> None:
        missing = copy.deepcopy(self.document)
        missing["human_gates"] = [
            gate
            for gate in missing["human_gates"]
            if gate["attestation"] is None
            or gate["attestation"]["key"] != "link-downloadable"
        ]
        errors = RIGHTS.validate_document(missing)
        self.assertTrue(any("link-downloadable has no registered human gate" in item for item in errors), errors)

        wrong_phase = copy.deepcopy(self.document)
        gate = next(
            row
            for row in wrong_phase["human_gates"]
            if row["attestation"] is not None
            and row["attestation"]["key"] == "submitted-via-submittable"
        )
        gate["required_for"] = ["uploaded"]
        errors = RIGHTS.validate_document(wrong_phase)
        self.assertTrue(any("not owned by its canonical submitted phase" in item for item in errors), errors)

        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        candidate["human_gates"] = [
            gate
            for gate in candidate["human_gates"]
            if gate["attestation"] is None
            or gate["attestation"]["key"] != "submitted-via-submittable"
        ]
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            blockers, _ = fixture_phase_blockers(candidate, "submitted", package=package)
        self.assertTrue(
            any("submitted-via-submittable has no registered human gate" in item for item in blockers),
            blockers,
        )

    def test_license_and_permission_layers_cannot_clear_each_other(self) -> None:
        candidate = copy.deepcopy(self.document)
        vendor = next(asset for asset in candidate["assets"] if asset["id"] == "mediapipe-pose-runtime")
        vendor["license"] = None
        dancer = next(asset for asset in candidate["assets"] if asset["id"] == "dancer-performance-likeness")
        dancer["uses"][0]["status"] = "cleared"
        dancer["uses"][0]["evidence"] = source_evidence()
        errors = RIGHTS.validate_document(candidate)
        self.assertTrue(any("licensed asset mediapipe-pose-runtime has no license" in error for error in errors), errors)
        self.assertTrue(any("license disagrees with the exact package/model binding" in error for error in errors), errors)
        self.assertTrue(any("cleared from disposition blocked" in error for error in errors), errors)

    def test_fixed_permissions_cannot_expire_before_the_recorded_assessment(self) -> None:
        candidate = copy.deepcopy(self.document)
        use = candidate["assets"][0]["uses"][0]
        use["term"] = "fixed"
        use["expires"] = "2026-08-03"
        errors = RIGHTS.validate_document(candidate)
        self.assertTrue(any("expired before the assessment date" in error for error in errors), errors)

    def test_fixed_permissions_are_revalidated_on_the_shipping_date(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        use = candidate["assets"][0]["uses"][0]
        use["term"] = "fixed"
        use["expires"] = "2026-08-05"
        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate, phase="public")
            on_expiry, _ = RIGHTS.phase_blockers(
                candidate,
                "public",
                release_manifest=release,
                root=root,
                register_path=register,
                as_of=RIGHTS.date(2026, 8, 5),
            )
            self.assertEqual(on_expiry, [])
            expired, inputs = RIGHTS.phase_blockers(
                candidate,
                "public",
                release_manifest=release,
                root=root,
                register_path=register,
                as_of=RIGHTS.date(2026, 8, 6),
            )
            self.assertTrue(any("fixed permission expired" in item for item in expired), expired)
            self.assertEqual(inputs["validation_date"], "2026-08-06")
            self.assertEqual(inputs["validation_timezone"], "America/New_York")

    def test_active_release_rules_recheck_fixed_requirements_outside_their_broad_phase_scope(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        asset = next(row for row in candidate["assets"] if row["id"] == "final-cut-derived-media")
        use = next(row for row in asset["uses"] if row["id"] == "delivery")
        self.assertNotIn("public", use["required_for"])
        use["term"] = "fixed"
        use["expires"] = "2026-08-05"
        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate, phase="public")
            on_expiry, _ = RIGHTS.validate_release_manifest(
                candidate,
                release,
                "public",
                root=root,
                register_path=register,
                as_of=RIGHTS.date(2026, 8, 5),
            )
            self.assertEqual(on_expiry, [])
            expired, _ = RIGHTS.validate_release_manifest(
                candidate,
                release,
                "public",
                root=root,
                register_path=register,
                as_of=RIGHTS.date(2026, 8, 6),
            )
            self.assertTrue(
                any("accessible-trailer" in item and "fixed permission expired" in item for item in expired),
                expired,
            )

    def test_shipping_date_is_independent_of_the_ambient_host_timezone(self) -> None:
        submission = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        timezone_name, _ = RIGHTS._submission_zone(submission)
        self.assertEqual(timezone_name, submission["opportunity_snapshot"]["timezone"])
        wrong_zone = copy.deepcopy(submission)
        wrong_zone["opportunity_snapshot"]["timezone"] = "UTC"
        with self.assertRaisesRegex(RIGHTS.RightsError, "does not agree"):
            RIGHTS._submission_zone(wrong_zone)

        identities = []
        for timezone in ("Pacific/Honolulu", "America/New_York", "UTC"):
            environment = os.environ.copy()
            environment["TZ"] = timezone
            result = subprocess.run(
                [sys.executable, "scripts/check-rights.py", "--phase", "public", "--json"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            receipt = json.loads(result.stdout)
            identities.append(
                (receipt["inputs"]["validation_date"], receipt["inputs"]["validation_timezone"])
            )
        self.assertEqual(len(set(identities)), 1, identities)
        self.assertEqual(identities[0][1], "America/New_York")

    def test_exact_package_manifest_bytes_sources_text_and_rules_validate(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            blockers, identity = validate_fixture_package(candidate, package)
            self.assertEqual(blockers, [])
            self.assertEqual(identity["items"], 11 + len(candidate["package_text"]))

            (package / "master.mov").write_bytes(b"tampered")
            blockers, _ = validate_fixture_package(candidate, package)
            self.assertTrue(any("digest does not match" in blocker for blocker in blockers), blockers)

    def test_required_package_artifact_census_cannot_be_reduced_to_text(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        cases = (
            ({"master.mov"}, "missing required master artifact"),
            ({"screener.mp4"}, "missing required screener artifact"),
            ({"provenance/passage-score.wav"}, "missing required score source artifact"),
            ({"stills/origin-2017.jpg"}, "missing required origin still artifact"),
            ({"stills/seed-0x1000.jpg"}, "canonical submission requires 6"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for index, (removed, expected) in enumerate(cases):
                package = make_package(Path(temporary) / str(index), candidate)
                manifest_path = package / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["items"] = [
                    item for item in manifest["items"] if item.get("name") not in removed
                ]
                manifest_path.write_text(json.dumps(manifest))
                blockers, _ = validate_fixture_package(candidate, package)
                self.assertTrue(any(expected in blocker for blocker in blockers), blockers)

    def test_package_inventory_is_repeated_after_item_validation(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        inventory = RIGHTS._package_inventory
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            calls = 0

            def add_late_artifact(package_root: Path) -> tuple[set[str], list[str]]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    (package / "late-unmanifested.webp").write_bytes(b"late rights bytes")
                return inventory(package_root)

            with mock.patch.object(
                RIGHTS,
                "_package_inventory",
                side_effect=add_late_artifact,
            ):
                blockers, _ = validate_fixture_package(candidate, package)
            self.assertTrue(any("inventory changed during validation" in item for item in blockers), blockers)

    def test_package_attestation_is_rechecked_after_package_validation(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)

            def mutate_attestation(*_args, **_kwargs) -> tuple[list[str], dict]:
                (package / "attest.yaml").write_text("final-cut-only: false\n")
                return [], {"schema": "danse.delivery.manifest.v1", "sha256": "0" * 64}

            with mock.patch.object(
                RIGHTS,
                "validate_package",
                side_effect=mutate_attestation,
            ):
                blockers, _ = RIGHTS.phase_blockers(
                    candidate,
                    "package",
                    package=package,
                )
            self.assertTrue(
                any("attestation changed during phase validation" in item for item in blockers),
                blockers,
            )

    def test_phase_validators_fail_closed_on_invalid_register_graphs(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        candidate["package_rules"][0]["pattern"] = "["
        candidate["package_rules"][1]["requirements"][0]["asset"] = "missing-asset"
        fixed_asset = next(asset for asset in candidate["assets"] if asset["id"] == "selected-music")
        fixed_use = next(use for use in fixed_asset["uses"] if use["id"] == "score-audio")
        fixed_use["term"] = "fixed"
        fixed_use["expires"] = None
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            blockers, _ = fixture_phase_blockers(candidate, "package", package=package)
            self.assertTrue(any("invalid regex" in item for item in blockers), blockers)
            self.assertTrue(any("unknown asset/use" in item for item in blockers), blockers)
            self.assertTrue(any("fixed permission has no expiry" in item for item in blockers), blockers)

        release_candidate = copy.deepcopy(self.document)
        clear_requirements(release_candidate)
        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), release_candidate)
            release_candidate["credit_rules"][0]["gate"] = "missing-gate"
            release_candidate["credit_rules"][0]["asset"] = "missing-asset"
            blockers, _ = RIGHTS.validate_release_manifest(
                release_candidate,
                release,
                "release",
                root=root,
                register_path=register,
            )
            self.assertTrue(any("names unknown gate" in item for item in blockers), blockers)
            self.assertTrue(any("names unknown asset" in item for item in blockers), blockers)

    def test_package_binds_current_delivery_tree_and_every_text_manifest_row(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["schema"] = "/Users/Alice/private-schema"
            manifest["source_tree_sha256"] = "a" * 64
            missing_text = candidate["package_text"][0]["destination"]
            manifest["items"] = [item for item in manifest["items"] if item["name"] != missing_text]
            manifest_path.write_text(json.dumps(manifest))
            blockers, identity = validate_fixture_package(candidate, package)
            self.assertTrue(any("does not match the canonical delivery tree" in item for item in blockers), blockers)
            self.assertTrue(any("package text" in item and "absent from the manifest" in item for item in blockers), blockers)
            self.assertIsNone(identity["schema"])
            self.assertNotIn("/Users/", RIGHTS.canonical_json(identity))

    def test_package_audio_binds_identical_competition_identity_and_rule_ids(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            master = next(item for item in manifest["items"] if item["name"] == "master.mov")
            manifest["sound"]["master_sha256"] = "c" * 64
            master["sound"]["credit"] = "Invented incomplete music credit."
            manifest_path.write_text(json.dumps(manifest))
            blockers, _ = validate_fixture_package(candidate, package)
            self.assertTrue(any("manifested score source" in item for item in blockers), blockers)
            self.assertTrue(any("does not copy the manifest sound identity" in item for item in blockers), blockers)
            self.assertTrue(any("canonical competition audio render" in item for item in blockers), blockers)

            receipt_package = make_package(Path(temporary) / "receipt", candidate)
            production_path = receipt_package / RIGHTS.PRODUCTION_RECEIPT
            production = json.loads(production_path.read_text())
            score_producer = next(row for row in production["producers"] if row["kind"] == "score")
            score_receipt_path = receipt_package / score_producer["receipt"]["path"]
            score_receipt = json.loads(score_receipt_path.read_text())
            score_receipt["profile"] = RIGHTS.HYBRID_AUDIO_PROFILE
            score_receipt_path.write_text(json.dumps(score_receipt, indent=2) + "\n")
            score_producer["receipt"]["sha256"] = RIGHTS.sha256(score_receipt_path)
            production_path.write_text(json.dumps(production, indent=2) + "\n")
            receipt_manifest_path = receipt_package / "manifest.json"
            receipt_manifest = json.loads(receipt_manifest_path.read_text())
            receipt_manifest["production"]["sha256"] = RIGHTS.sha256(production_path)
            receipt_manifest_path.write_text(json.dumps(receipt_manifest, indent=2) + "\n")
            blockers, _ = validate_fixture_package(candidate, receipt_package)
            self.assertTrue(any("score producer" in item and "package-eligible" in item for item in blockers), blockers)
            self.assertTrue(any("does not copy the manifest sound identity" in item for item in blockers), blockers)

            renamed = copy.deepcopy(candidate)
            next(rule for rule in renamed["package_rules"] if rule["id"] == "moving-image")["id"] = "film"
            blockers, _ = validate_fixture_package(renamed, make_package(Path(temporary) / "renamed", renamed))
            self.assertTrue(any("missing required package rule moving-image" in item for item in blockers), blockers)

    def test_package_audio_render_receipt_is_durable_and_digest_bound(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            receipt = package / "provenance/audio-render.json"
            replacement = b'{"schema":"danse.audio.render.v1","fixture":"substituted"}\n'
            receipt.write_bytes(replacement)
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            item = next(
                row
                for row in manifest["items"]
                if row["name"] == "provenance/audio-render.json"
            )
            item["bytes"] = len(replacement)
            item["sha256"] = digest_bytes(replacement)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            blockers, _ = validate_fixture_package(candidate, package)
            self.assertTrue(
                any("audio-render receipt does not bind" in item for item in blockers),
                blockers,
            )

            missing = make_package(Path(temporary) / "missing", candidate)
            missing_manifest_path = missing / "manifest.json"
            missing_manifest = json.loads(missing_manifest_path.read_text())
            missing_manifest["items"] = [
                row
                for row in missing_manifest["items"]
                if row["name"] != "provenance/audio-render.json"
            ]
            missing_manifest_path.write_text(json.dumps(missing_manifest, indent=2) + "\n")
            blockers, _ = validate_fixture_package(candidate, missing)
            self.assertTrue(
                any("missing required audio-render receipt artifact" in item for item in blockers),
                blockers,
            )

    def test_package_repository_head_is_typed_and_copied_to_production(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["repository_head"] = "not-a-git-object-id"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            blockers, _ = validate_fixture_package(candidate, package)
            self.assertTrue(any("no exact repository head" in item for item in blockers), blockers)
            self.assertTrue(any("different repository head" in item for item in blockers), blockers)

    def test_package_media_cannot_be_substituted_with_a_self_rehashed_manifest(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            replacement = b"unrelated but self-reported master bytes"
            (package / "master.mov").write_bytes(replacement)
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            master = next(item for item in manifest["items"] if item["name"] == "master.mov")
            master["bytes"] = len(replacement)
            master["sha256"] = digest_bytes(replacement)
            manifest_path.write_text(json.dumps(manifest))
            blockers, _ = validate_fixture_package(candidate, package)
            self.assertTrue(
                any("production output master.mov" in item for item in blockers),
                blockers,
            )

    def test_package_producer_receipts_are_closed_reachable_and_canonical(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)

        with self.subTest("closed receipt inventory"), tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            (package / "provenance/producer-receipts/unclaimed.json").write_text("{}\n")
            blockers, _ = validate_fixture_package(candidate, package)
            self.assertTrue(
                any("producer-receipt inventory" in item for item in blockers),
                blockers,
            )

        with self.subTest("reachable producers"), tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            production_path = package / RIGHTS.PRODUCTION_RECEIPT
            production = json.loads(production_path.read_text())
            score = next(row for row in production["producers"] if row["id"] == "score")
            unused_path = package / "provenance/producer-receipts/unused-score.json"
            unused_path.write_bytes((package / score["receipt"]["path"]).read_bytes())
            production["producers"].append(
                {
                    **score,
                    "id": "unused-score",
                    "receipt": {
                        "path": unused_path.relative_to(package).as_posix(),
                        "sha256": RIGHTS.sha256(unused_path),
                    },
                }
            )
            production_path.write_text(json.dumps(production, indent=2) + "\n")
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["production"]["sha256"] = RIGHTS.sha256(production_path)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            blockers, _ = validate_fixture_package(candidate, package)
            self.assertTrue(
                any("unreferenced producer" in item for item in blockers),
                blockers,
            )

        with self.subTest("canonical render invocation"), tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            production_path = package / RIGHTS.PRODUCTION_RECEIPT
            production = json.loads(production_path.read_text())
            segment = next(
                row for row in production["producers"] if row["id"] == "picture-segment"
            )
            segment_path = package / segment["receipt"]["path"]
            segment_receipt = json.loads(segment_path.read_text())
            segment_receipt["inputs"]["window"] = "reel"
            segment_path.write_text(json.dumps(segment_receipt, indent=2) + "\n")
            segment["receipt"]["sha256"] = RIGHTS.sha256(segment_path)

            concat = next(
                row for row in production["producers"] if row["id"] == "picture-concat"
            )
            concat_path = package / concat["receipt"]["path"]
            concat_receipt = json.loads(concat_path.read_text())
            concat_receipt["segments"][0]["receipt_sha256"] = segment["receipt"]["sha256"]
            concat_path.write_text(json.dumps(concat_receipt, indent=2) + "\n")
            concat["receipt"]["sha256"] = RIGHTS.sha256(concat_path)

            production_path.write_text(json.dumps(production, indent=2) + "\n")
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["production"]["sha256"] = RIGHTS.sha256(production_path)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            blockers, _ = validate_fixture_package(candidate, package)
            self.assertTrue(
                any("canonical passage invocation" in item for item in blockers),
                blockers,
            )

    def test_package_rejects_unmanifested_media_unknown_rules_and_symlinks(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            (package / "unlisted.mp4").write_bytes(b"unlisted")
            (package / "unlisted.webp").write_bytes(b"unlisted webp")
            outside = Path(temporary) / "outside.jpg"
            outside.write_bytes(b"outside")
            (package / "stills/link.jpg").symlink_to(outside)
            manifest = json.loads((package / "manifest.json").read_text())
            unknown = b"unknown"
            (package / "unknown.bin").write_bytes(unknown)
            manifest["items"].append(
                {"name": "unknown.bin", "bytes": len(unknown), "sha256": digest_bytes(unknown)}
            )
            (package / "manifest.json").write_text(json.dumps(manifest))
            blockers, _ = validate_fixture_package(candidate, package)
            self.assertGreaterEqual(
                sum("absent from the manifest" in blocker for blocker in blockers),
                2,
                blockers,
            )
            self.assertTrue(any("symlink file" in blocker for blocker in blockers), blockers)
            self.assertTrue(any("manifest item" in blocker and "0 rights rules" in blocker for blocker in blockers), blockers)

    def test_package_attestations_are_scoped_and_never_replace_release_receipts(self) -> None:
        gate = next(row for row in self.document["human_gates"] if row["id"] == "dancer-release-and-credit")
        self.assertFalse(RIGHTS.gate_satisfied(gate, {}, allow_attestation=True))
        self.assertTrue(
            RIGHTS.gate_satisfied(gate, {"dancer-release-and-credit": True}, allow_attestation=True)
        )
        self.assertFalse(
            RIGHTS.gate_satisfied(gate, {"dancer-release-and-credit": 1}, allow_attestation=True)
        )
        self.assertFalse(
            RIGHTS.gate_satisfied(gate, {"dancer-release-and-credit": True}, allow_attestation=False)
        )
        choice = next(row for row in self.document["human_gates"] if row["id"] == "archive-library-choice")
        self.assertTrue(
            RIGHTS.gate_satisfied(choice, {"archive-library-choice": "include"}, allow_attestation=True)
        )
        self.assertFalse(
            RIGHTS.gate_satisfied(choice, {"archive-library-choice": True}, allow_attestation=True)
        )
        rejected = copy.deepcopy(gate)
        rejected["state"] = "rejected"
        self.assertFalse(
            RIGHTS.gate_satisfied(rejected, {"dancer-release-and-credit": True}, allow_attestation=True)
        )

        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            (package / "attest.yaml").write_text(
                "final-cut-only: true\nfinal-cut-only: false\n",
                encoding="utf-8",
            )
            _, blockers = RIGHTS.load_attestation(package)
            self.assertTrue(any("invalid or unreadable YAML" in blocker for blocker in blockers), blockers)

        candidate = {
            "final-cut-only": 1,
            "archive-library-choice": True,
            "link-downloadable": False,
            "unknown-private-field": {"nested": "value"},
        }
        blockers = RIGHTS.validate_attestation(self.document, candidate)
        self.assertTrue(any("final-cut-only must be boolean" in blocker for blocker in blockers), blockers)
        self.assertTrue(any("archive-library-choice must be one registered choice" in blocker for blocker in blockers), blockers)
        self.assertTrue(any("1 unknown key" in blocker for blocker in blockers), blockers)
        self.assertFalse(any("nested" in blocker or "value" in blocker for blocker in blockers), blockers)

    def test_archive_opt_out_excludes_only_the_conditional_archive_use(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        candidate["status"] = "reviewed"
        gate = next(row for row in candidate["human_gates"] if row["id"] == "archive-library-choice")
        gate["state"] = "pending"
        gate["evidence"] = None
        asset = next(row for row in candidate["assets"] if row["id"] == "festival-archive-copy")
        asset["disposition"] = "blocked"
        asset["rights_holder"] = None
        asset["blocker"] = "The filing choice controls this conditional use."
        use = asset["uses"][0]
        use |= {
            "territory": "pending",
            "term": "pending",
            "promotion": "not-applicable",
            "archive": "pending",
            "status": "blocked",
            "evidence": None,
        }
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            attestation = yaml.safe_load((package / "attest.yaml").read_text())
            attestation["archive-library-choice"] = "opt-out"
            (package / "attest.yaml").write_text(yaml.safe_dump(attestation, sort_keys=True))
            blockers, _ = fixture_phase_blockers(candidate, "submitted", package=package)
            self.assertFalse(any("festival-archive-copy/festival-archive" in item for item in blockers), blockers)

            attestation["archive-library-choice"] = "include"
            (package / "attest.yaml").write_text(yaml.safe_dump(attestation, sort_keys=True))
            blockers, _ = fixture_phase_blockers(candidate, "submitted", package=package)
            self.assertTrue(any("festival-archive-copy/festival-archive" in item for item in blockers), blockers)

    def test_release_manifest_binds_exact_rights_register_media_and_credits(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate)
            blockers, identity = RIGHTS.validate_release_manifest(
                candidate, release, "release", root=root, register_path=register
            )
            self.assertEqual(blockers, [])
            self.assertEqual(identity["schema"], "danse.release.v1")

            manifest = json.loads(release.read_text())
            manifest["gates"][0]["evidence"]["sha256"] = "0" * 64
            release.write_text(json.dumps(manifest))
            blockers, _ = RIGHTS.validate_release_manifest(
                candidate, release, "release", root=root, register_path=register
            )
            self.assertTrue(any("does not bind this exact rights register" in blocker for blocker in blockers), blockers)

    def test_release_clearance_receipt_binds_the_exact_staged_media_identity(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate)
            manifest = json.loads(release.read_text())
            manifest["media"][0]["clearance"]["evidence"] = copy.deepcopy(
                manifest["media"][1]["clearance"]["evidence"]
            )
            release.write_text(json.dumps(manifest))
            blockers, _ = RIGHTS.validate_release_manifest(
                candidate,
                release,
                "release",
                root=root,
                register_path=register,
            )
            self.assertTrue(
                any("typed clearance receipt" in item and "different media id" in item for item in blockers),
                blockers,
            )

    def test_release_manifest_is_closed_and_public_safe_before_semantic_use(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate)
            manifest = json.loads(release.read_text())
            private_value = "/Users/Alice/private-release.mov"
            contact_value = "artist@example.test"
            manifest["private_path"] = private_value
            manifest["contact"] = contact_value
            manifest["media"][0]["private_path"] = private_value
            release.write_text(json.dumps(manifest))
            blockers, identity = RIGHTS.validate_release_manifest(
                candidate,
                release,
                "release",
                root=root,
                register_path=register,
            )
            self.assertTrue(any("closed top-level schema" in item for item in blockers), blockers)
            self.assertTrue(any("closed media schema" in item for item in blockers), blockers)
            self.assertTrue(any("machine-local path" in item for item in blockers), blockers)
            self.assertTrue(any("email address" in item for item in blockers), blockers)
            rendered = RIGHTS.canonical_json({"blockers": blockers, "identity": identity})
            self.assertNotIn(private_value, rendered)
            self.assertNotIn(contact_value, rendered)

    def test_release_media_bytes_credit_labels_and_safe_identity_are_exact(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate)
            manifest = json.loads(release.read_text())
            manifest["schema"] = "/Users/Alice/private-schema"
            manifest["release_id"] = "/Users/Alice/final-cut"
            manifest["media"][0]["source"]["destination"] = "some/other/released.bin"
            manifest["media"][1]["source"]["bytes"] += 1
            manifest["credits"][0]["name"] = "Incorrect public attribution"
            release.write_text(json.dumps(manifest))
            blockers, identity = RIGHTS.validate_release_manifest(
                candidate, release, "release", root=root, register_path=register
            )
            self.assertTrue(any("invalid release identifier" in item for item in blockers), blockers)
            self.assertTrue(any("canonical destination" in item for item in blockers), blockers)
            self.assertTrue(any("byte count is missing or stale" in item for item in blockers), blockers)
            self.assertTrue(any("does not match its approved attribution" in item for item in blockers), blockers)
            self.assertIsNone(identity["release_id"])
            self.assertIsNone(identity["schema"])
            self.assertNotIn("/Users/", RIGHTS.canonical_json(identity))

    def test_release_boundary_rejects_every_unmanifested_or_symlinked_artifact(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate)
            (root / "media/assets/unlisted.mp4").write_bytes(b"unlisted")
            nested = root / "media/assets/nested"
            nested.mkdir()
            (nested / "unlisted.bin").write_bytes(b"ordinary unlisted bytes")
            outside = Path(temporary) / "outside.txt"
            outside.write_text("outside")
            (root / "media/assets/unlisted.txt").symlink_to(outside)
            outside_directory = Path(temporary) / "outside-directory"
            outside_directory.mkdir()
            (root / "media/assets/linked-directory").symlink_to(
                outside_directory,
                target_is_directory=True,
            )
            blockers, _ = RIGHTS.validate_release_manifest(
                candidate, release, "release", root=root, register_path=register
            )
            self.assertTrue(any("not listed in the release manifest" in item for item in blockers), blockers)
            self.assertTrue(any("symlink file" in item for item in blockers), blockers)
            self.assertTrue(any("symlink directory" in item for item in blockers), blockers)

            boundary = root / "media/assets"
            real_boundary = root / "media/assets-real"
            boundary.rename(real_boundary)
            boundary.symlink_to(real_boundary, target_is_directory=True)
            blockers, _ = RIGHTS.validate_release_manifest(
                candidate, release, "release", root=root, register_path=register
            )
            self.assertTrue(any("boundary must not be a symlink" in item for item in blockers), blockers)

    def test_release_validation_rejects_media_or_manifest_mutation_during_inventory(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        inventory = RIGHTS._release_boundary_inventory
        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate)
            media_path = root / candidate["release_rules"][0]["destination"]

            def mutate_media(repository: Path) -> tuple[set[str], list[str]]:
                media_path.write_bytes(b"changed after initial verification")
                return inventory(repository)

            with mock.patch.object(RIGHTS, "_release_boundary_inventory", side_effect=mutate_media):
                blockers, _ = RIGHTS.validate_release_manifest(
                    candidate, release, "release", root=root, register_path=register
                )
            self.assertTrue(any("changed during release validation" in item for item in blockers), blockers)

        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate)
            original_digest = RIGHTS.sha256(release)
            replacement = b'{"schema":"attacker.invalid","release_id":"evil"}\n'

            def mutate_manifest(repository: Path) -> tuple[set[str], list[str]]:
                release.write_bytes(replacement)
                return inventory(repository)

            with mock.patch.object(RIGHTS, "_release_boundary_inventory", side_effect=mutate_manifest):
                blockers, identity = RIGHTS.validate_release_manifest(
                    candidate, release, "release", root=root, register_path=register
                )
            self.assertTrue(any("manifest changed during validation" in item for item in blockers), blockers)
            self.assertEqual(identity["schema"], "danse.release.v1")
            self.assertEqual(identity["release_id"], "rights-test")
            self.assertEqual(identity["sha256"], original_digest)
            self.assertNotEqual(identity["sha256"], digest_bytes(replacement))

        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate)
            inventory_calls = 0

            def add_after_inventory(repository: Path) -> tuple[set[str], list[str]]:
                nonlocal inventory_calls
                inventory_calls += 1
                if inventory_calls == 2:
                    (root / "media/assets/late-extra.bin").write_bytes(b"late extra")
                return inventory(repository)

            with mock.patch.object(
                RIGHTS,
                "_release_boundary_inventory",
                side_effect=add_after_inventory,
            ):
                blockers, _ = RIGHTS.validate_release_manifest(
                    candidate, release, "release", root=root, register_path=register
                )
            self.assertTrue(any("boundary changed during validation" in item for item in blockers), blockers)

    def test_public_boundary_excludes_release_only_media_until_release_phase(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate, phase="public")
            manifest = json.loads(release.read_text())
            master = next(row for row in manifest["media"] if row["id"] == "score-driven-master")
            master_path = root / "media/assets/score-driven-master.mov"
            blockers, _ = RIGHTS.validate_release_manifest(
                candidate, release, "public", root=root, register_path=register
            )
            self.assertEqual(blockers, [])

            payload = b"uncleared release-only master"
            master_path.write_bytes(payload)
            master["source"] = {
                "path": "media/assets/score-driven-master.mov",
                "destination": "media/assets/score-driven-master.mov",
                "sha256": digest_bytes(payload),
                "bytes": len(payload),
            }
            release.write_text(json.dumps(manifest))
            blockers, _ = RIGHTS.validate_release_manifest(
                candidate, release, "public", root=root, register_path=register
            )
            self.assertTrue(any("not listed in the release manifest" in item for item in blockers), blockers)

    def test_release_manifest_cannot_hide_rights_rows_or_repeat_gate_identities(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            release, root, register = make_release(Path(temporary), candidate, phase="public")
            manifest = json.loads(release.read_text())
            manifest["media"][0]["required_for"] = ["release"]
            release.write_text(json.dumps(manifest))
            blockers, _ = RIGHTS.validate_release_manifest(
                candidate, release, "public", root=root, register_path=register
            )
            self.assertTrue(any("phase scope disagrees" in blocker for blocker in blockers), blockers)

            release, root, register = make_release(Path(temporary), candidate, phase="public")
            manifest = json.loads(release.read_text())
            manifest["status"] = []
            omitted_media_id = candidate["release_rules"][0]["media_id"]
            manifest["media"] = [
                row for row in manifest["media"] if row["id"] != omitted_media_id
            ]
            release.write_text(json.dumps(manifest))
            blockers, _ = RIGHTS.validate_release_manifest(
                candidate, release, "public", root=root, register_path=register
            )
            self.assertTrue(any("status is not valid" in blocker for blocker in blockers), blockers)
            self.assertTrue(any(omitted_media_id in blocker for blocker in blockers), blockers)

            release, root, register = make_release(Path(temporary), candidate, phase="public")
            manifest = json.loads(release.read_text())
            manifest["gates"][0]["required_for"] = ["release"]
            release.write_text(json.dumps(manifest))
            blockers, _ = RIGHTS.validate_release_manifest(
                candidate, release, "public", root=root, register_path=register
            )
            self.assertTrue(any("must govern public and release" in blocker for blocker in blockers), blockers)

            release, root, register = make_release(Path(temporary), candidate)
            manifest = json.loads(release.read_text())
            manifest["media"].append(copy.deepcopy(manifest["media"][0]))
            manifest["credits"].append(copy.deepcopy(manifest["credits"][0]))
            manifest["gates"].append(copy.deepcopy(manifest["gates"][0]))
            release.write_text(json.dumps(manifest))
            blockers, _ = RIGHTS.validate_release_manifest(
                candidate, release, "release", root=root, register_path=register
            )
            self.assertTrue(any("repeats media id" in blocker for blocker in blockers), blockers)
            self.assertTrue(any("repeats credit id" in blocker for blocker in blockers), blockers)
            self.assertTrue(any("repeats gate id" in blocker for blocker in blockers), blockers)

    def test_release_clearance_evidence_must_be_tracked_but_media_may_be_hydrated(self) -> None:
        evidence = source_evidence()
        self.assertEqual(
            RIGHTS._verify_release_source(
                ROOT,
                evidence,
                "test evidence",
                tracked=set(),
                require_tracked=False,
            ),
            [],
        )
        blockers = RIGHTS._verify_release_source(
            ROOT,
            evidence,
            "test evidence",
            tracked=set(),
            require_tracked=True,
        )
        self.assertEqual(blockers, ["test evidence source is not tracked public-safe evidence"])

    def test_receipts_are_deterministic_redacted_and_contain_exact_input_digests(self) -> None:
        first_document, first = RIGHTS.validate_all(phase="draft")
        second_document, second = RIGHTS.validate_all(phase="draft")
        self.assertEqual(first_document, second_document)
        self.assertEqual(RIGHTS.canonical_json(first), RIGHTS.canonical_json(second))
        rendered = RIGHTS.canonical_json(first)
        self.assertNotIn(str(ROOT), rendered)
        self.assertNotIn("Anthony J. Padavano and the performer", rendered)
        self.assertEqual(first["register"]["sha256"], RIGHTS.sha256(RIGHTS.REGISTER))

        _, missing_package = RIGHTS.validate_all(
            phase="package",
            package=Path("/Users/private-person/unavailable-package"),
        )
        self.assertNotIn("/Users/", RIGHTS.canonical_json(missing_package))

    def test_receipt_output_cannot_overwrite_any_validated_input_or_artifact(self) -> None:
        with self.assertRaisesRegex(RIGHTS.RightsError, "validated input"):
            RIGHTS.validate_receipt_destination(
                self.document,
                RIGHTS.REGISTER,
                phase="draft",
                register_path=RIGHTS.REGISTER,
                schema_path=RIGHTS.SCHEMA,
                package=None,
                release_manifest=None,
            )
        with self.assertRaisesRegex(RIGHTS.RightsError, "artifact boundary"):
            RIGHTS.validate_receipt_destination(
                self.document,
                ROOT / "media/assets/future-trailer.mp4",
                phase="public",
                register_path=RIGHTS.REGISTER,
                schema_path=RIGHTS.SCHEMA,
                package=None,
                release_manifest=ROOT / "release/manifest.json",
            )

        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), self.document)
            target = package / "master.mov"
            original = target.read_bytes()
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = RIGHTS.main(
                    [
                        "--phase",
                        "package",
                        "--package",
                        str(package),
                        "--receipt",
                        str(target),
                        "--json",
                    ]
                )
            self.assertEqual(result, 1)
            self.assertIn("overlaps a validated artifact boundary", stderr.getvalue())
            self.assertEqual(target.read_bytes(), original)

    def test_package_receipt_binds_canonical_attestation_choices(self) -> None:
        candidate = copy.deepcopy(self.document)
        clear_requirements(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            package = make_package(Path(temporary), candidate)
            include = yaml.safe_load((package / "attest.yaml").read_text())
            include["archive-library-choice"] = "include"
            (package / "attest.yaml").write_text(yaml.safe_dump(include, sort_keys=True))
            blockers, inputs = fixture_phase_blockers(candidate, "submitted", package=package)
            self.assertEqual(blockers, [])
            include_identity = inputs["attestation"]
            self.assertEqual(include_identity["values"]["archive-library-choice"], "include")

            include["archive-library-choice"] = "opt-out"
            (package / "attest.yaml").write_text(yaml.safe_dump(include, sort_keys=True))
            blockers, inputs = fixture_phase_blockers(candidate, "submitted", package=package)
            self.assertEqual(blockers, [])
            opt_out_identity = inputs["attestation"]
            self.assertEqual(opt_out_identity["values"]["archive-library-choice"], "opt-out")
            self.assertNotEqual(include_identity["sha256"], opt_out_identity["sha256"])

    def test_frozen_submission_terms_and_selected_music_state_are_exactly_bound(self) -> None:
        submission = yaml.safe_load((ROOT / self.document["bindings"]["submission"]["source"]["path"]).read_text())
        term_ids = {row["id"] for row in submission["terms"]}
        self.assertTrue(set(self.document["bindings"]["submission"]["required_terms"]) <= term_ids)
        music = yaml.safe_load((ROOT / self.document["bindings"]["music"]["source"]["path"]).read_text())
        self.assertEqual(music["artistic_gate"]["status"], "accepted")
        self.assertEqual(music["works"][0]["role"], "repertoire")
        self.assertEqual(music["works"][0]["selection"]["status"], "selected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
