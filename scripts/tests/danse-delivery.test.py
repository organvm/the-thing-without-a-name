#!/usr/bin/env python3
"""Portable regression tests for Danse's delivery-trunk interfaces."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DELIVER = load("danse_deliver_test", ROOT / "render/deliver.py")
SCORE = load("danse_score_test", ROOT / "sound/score.py")
CHECK = load("danse_submission_check_test", ROOT / "submission/check.py")
RIGHTS = load("danse_rights_contract_delivery_test", ROOT / "scripts/rights_contract.py")
BROWSER = load("danse_browser_test", ROOT / "render/browser.py")
OFFLINE = load("danse_offline_test", ROOT / "render/render.py")
BANK_CONTRACT = sys.modules["bank_contract"]
CORPUS_CONTRACT = load("danse_corpus_contract_test", ROOT / "pipeline/corpus_contract.py")
_prior_corpus_contract = sys.modules.get("corpus_contract")
sys.modules["corpus_contract"] = CORPUS_CONTRACT
try:
    CORPUS_PIPELINE = load("danse_corpus_pipeline_test", ROOT / "pipeline/4_corpus.py")
finally:
    if _prior_corpus_contract is None:
        del sys.modules["corpus_contract"]
    else:
        sys.modules["corpus_contract"] = _prior_corpus_contract
RESOLVE = load("danse_resolve_test", ROOT / "sound/resolve.py")
SPAN = {
    "t0": 0.0,
    "t1": 312.54,
    "duration": 312.54,
    "seed": 0xAF6B7BE5,
    "river_seed": 20170620,
    "passage": 0,
    "capture": "passage",
}
SUBMISSION_AUDIO_RENDER_RECEIPT = b'{"schema":"danse.audio.render.v1","fixture":true}\n'
SUBMISSION_REPOSITORY_HEAD = "a" * 40


def submission_sound_identity(master_sha256: str) -> dict:
    uses_path = ROOT / "sound/audio-uses.json"
    uses = json.loads(uses_path.read_text())
    profile = uses["profiles"][uses["competition_profile"]]
    sources = {row["id"]: row for row in profile["declared_sources"]}
    return {
        "profile": uses["competition_profile"],
        "audio_uses_sha256": CHECK.sha256(uses_path),
        "score_file_sha256": CHECK.sha256(ROOT / "music/score.json"),
        "score_contract_sha256": "1" * 64,
        "choreography_file_sha256": "2" * 64,
        "choreography_contract_sha256": "3" * 64,
        "midi_sha256": sources["delibes-chamber-midi"]["sha256"],
        "adaptation_sha256": CHECK.sha256(ROOT / "music/adaptation.json"),
        "toolchain_sha256": CHECK.sha256(ROOT / "music/audio-toolchain.json"),
        "mix_sha256": CHECK.sha256(ROOT / "music/delibes-mix.json"),
        "soundfont_sha256": sources["musescore-general-sf3"]["sha256"],
        "audio_render_receipt_sha256": hashlib.sha256(
            SUBMISSION_AUDIO_RENDER_RECEIPT
        ).hexdigest(),
        "master_sha256": master_sha256,
        "sources": [row["id"] for row in profile["declared_sources"]],
        "stems": [
            {"id": stem_id, "sha256": hashlib.sha256(stem_id.encode()).hexdigest()}
            for stem_id in profile["required_stems"]
        ],
        "credit": yaml.safe_load(
            (ROOT / "submission/screendance-2027.yaml").read_text()
        )["package"]["audio"]["credit"],
    }


def bind_submission_score_receipt(package: Path, manifest: dict, sound: dict) -> None:
    manifest.setdefault("repository_head", SUBMISSION_REPOSITORY_HEAD)
    score_relative = "provenance/passage-score.wav"
    score_path = package / score_relative
    score_path.parent.mkdir(parents=True, exist_ok=True)
    if not score_path.exists():
        score_path.write_bytes(b"submission score fixture")
    assert CHECK.sha256(score_path) == sound["master_sha256"]
    manifest.setdefault("items", []).append(
        {"name": score_relative, "sha256": sound["master_sha256"], "sound": sound}
    )
    audio_render_relative = "provenance/audio-render.json"
    audio_render_path = package / audio_render_relative
    audio_render_path.write_bytes(SUBMISSION_AUDIO_RENDER_RECEIPT)
    manifest["items"].append(
        {
            "name": audio_render_relative,
            "bytes": len(SUBMISSION_AUDIO_RENDER_RECEIPT),
            "sha256": sound["audio_render_receipt_sha256"],
        }
    )
    receipt_root = package / "provenance/producer-receipts"
    receipt_root.mkdir(parents=True, exist_ok=True)
    score_receipt_path = receipt_root / "score.json"
    score_receipt = {
        "schema": "danse.score.receipt.v2",
        "sha256": sound["master_sha256"],
        "t0": manifest["t0"],
        "t1": manifest["t1"],
        "duration": manifest["duration"],
        **sound,
    }
    score_receipt_path.write_text(json.dumps(score_receipt, indent=2) + "\n")
    production = {
        "schema": "danse.delivery.production.v1",
        "source_tree_sha256": "5" * 64,
        "repository_head": manifest["repository_head"],
        "passage": {key: manifest[key] for key in ("t0", "t1", "duration")},
        "sound": sound,
        "producers": [
            {
                "id": "score",
                "kind": "score",
                "receipt": {
                    "path": score_receipt_path.relative_to(package).as_posix(),
                    "sha256": CHECK.sha256(score_receipt_path),
                },
                "output_sha256": sound["master_sha256"],
                "components": [],
            }
        ],
        "outputs": [],
    }
    production_path = package / "provenance/production.json"
    production_path.write_text(json.dumps(production, indent=2) + "\n")
    manifest["production"] = {
        "path": "provenance/production.json",
        "sha256": CHECK.sha256(production_path),
    }


def submission_attestation_values(register: dict) -> dict[str, object]:
    return {key: None for key in CHECK.full_attestation_contracts(register)}


def affirm_submission_phase(register: dict, values: dict[str, object], phase: str) -> None:
    selected = CHECK.PHASES.index(phase)
    for key, contract in CHECK.full_attestation_contracts(register).items():
        if CHECK.PHASES.index(contract["phase"]) > selected:
            continue
        values[key] = True if contract["kind"] == "manual" else contract["values"][0]


def write_submission_attestation(package: Path, values: dict[str, object]) -> Path:
    path = package / "attest.yaml"
    path.write_text(yaml.safe_dump(values, sort_keys=True))
    return path


def submission_package_binding(package: Path) -> dict[str, str]:
    return {
        "manifest": "manifest.json",
        "manifest_sha256": CHECK.sha256(package / "manifest.json"),
        "repository_head": SUBMISSION_REPOSITORY_HEAD,
    }


def write_submission_phase_receipt(
    package: Path,
    register: dict,
    values: dict[str, object],
    phase: str,
    recorded_at: str,
    *,
    event_at: str | None = None,
) -> Path:
    attest = write_submission_attestation(package, values)
    assertions, _ = CHECK.attestation_snapshot(register, values, phase)
    payload: dict[str, object] = {
        "schema": CHECK.PHASE_RECEIPT_SCHEMAS[phase],
        "receipt_id": f"screendance-{phase}-001",
        "recorded_at": recorded_at,
        "signer": {
            "name": CHECK.canonical_phase_signer(phase),
            "role": CHECK.PHASE_SIGNER_ROLES[phase],
        },
        "package": submission_package_binding(package),
        "opportunity": {
            "snapshot_id": register["opportunity_snapshot"]["snapshot_id"],
            "sha256": register["opportunity_snapshot"]["sha256"],
        },
        "deadline": CHECK.deadline_binding(register)[0],
        "attestation": {
            "path": "attest.yaml",
            "sha256": CHECK.sha256(attest),
            "canonical_sha256": CHECK.canonical_json_sha256(values),
            "document": attest.read_text(),
        },
        "assertions": {
            "sha256": CHECK.canonical_json_sha256(assertions),
            "values": assertions,
        },
    }
    if phase != "package":
        prior = CHECK.PHASES[CHECK.PHASES.index(phase) - 1]
        prior_path = package / CHECK.PHASE_RECEIPTS[prior]
        prior_value = json.loads(prior_path.read_text())
        payload["prior_receipt"] = {
            "phase": prior,
            "path": CHECK.PHASE_RECEIPTS[prior],
            "sha256": CHECK.sha256(prior_path),
            "receipt_id": prior_value["receipt_id"],
        }
    if phase == "uploaded":
        manifest = json.loads((package / "manifest.json").read_text())
        screener = next(item for item in manifest["items"] if item["name"].startswith("screener."))
        payload["upload"] = {
            "provider": "vimeo.com",
            "asset_id": "123456789",
            "url": "https://vimeo.com/123456789",
            "uploaded_at": event_at or recorded_at,
            "manifest_item": screener["name"],
            "sha256": screener["sha256"],
        }
    elif phase == "submitted":
        payload["submission"] = {
            "portal": register["portal"],
            "confirmation_id": "submission-123456",
            "submitted_at": event_at or recorded_at,
        }
    path = package / CHECK.PHASE_RECEIPTS[phase]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def build_submission_receipt_chain(
    package: Path,
    register: dict,
    through: str = "submitted",
    *,
    values: dict[str, object] | None = None,
) -> dict[str, object]:
    item = package / "screener.mp4"
    item.write_text("exact package bytes\n")
    manifest = {
        "schema": "danse.delivery.manifest.v1",
        "title": "THE THING WITHOUT A NAME",
        "seed": "0x0133D62C",
        "repository_head": SUBMISSION_REPOSITORY_HEAD,
        "items": [
            {
                "name": item.name,
                "bytes": item.stat().st_size,
                "sha256": CHECK.sha256(item),
            }
        ],
    }
    (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    values = submission_attestation_values(register) if values is None else values
    timestamps = {
        "package": "2026-08-31T20:00:00Z",
        "uploaded": "2026-08-31T21:59:00Z",
        "submitted": "2026-09-01T01:59:00Z",
    }
    for phase in CHECK.PHASES[: CHECK.PHASES.index(through) + 1]:
        affirm_submission_phase(register, values, phase)
        write_submission_phase_receipt(
            package,
            register,
            values,
            phase,
            timestamps[phase],
        )
    return values


def corpus_fixture(root: Path) -> tuple[Path, Path]:
    work = root / "work"
    out = root / "out"
    raw = work / "raw/IMG_1570.png"
    mask = work / "vision/mask/IMG_1570.png"
    pose = work / "vision/pose/IMG_1570.json"
    raw.parent.mkdir(parents=True)
    mask.parent.mkdir(parents=True)
    pose.parent.mkdir(parents=True)
    CORPUS_PIPELINE.Image.init()
    CORPUS_PIPELINE.Image.new("RGB", (4, 3), "white").save(raw, "PNG")
    CORPUS_PIPELINE.Image.new("L", (4, 3), 255).save(mask, "PNG")
    pose.write_text("{}")
    return work, out


def corpus_public_manifest(work: Path) -> dict:
    items, incomplete = CORPUS_CONTRACT.frame_inventory(work)
    assert not incomplete and len(items) == 1
    fid, raw, mask, pose = items[0]
    with CORPUS_PIPELINE.Image.open(raw) as image:
        native = list(image.size)
    return {
        "schema": "danse.corpus.v1",
        "camera": native,
        "tiers": {name: {"sentinel": name} for name in CORPUS_PIPELINE.SHIPPED},
        "score": None,
        "frames": [
            {
                "id": fid,
                "source": raw.name,
                "native": native,
                "registered": True,
                "figure": CORPUS_PIPELINE.figure_geometry(mask),
                "joints": CORPUS_PIPELINE.joints_of(pose),
                "score_area": 0.0,
            }
        ],
        "sentinel": "public bytes must survive",
    }


def run_corpus_pipeline(work: Path, out: Path, tiers: str, *extra: str) -> int:
    argv = ["4_corpus.py", "--work", str(work), "--out", str(out), "--skip-room", "--tiers", tiers, *extra]
    with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return CORPUS_PIPELINE.main()


def write_fake_reel_segment(render_out: Path, payload: bytes = b"rendered reel") -> Path:
    """Write the typed renderer evidence expected by the reel delivery boundary."""
    segment = render_out / "reel-default-seg-000.mp4"
    segment.write_bytes(payload)
    receipt = {
        "schema": "danse.render.segment.v1",
        "segment": 0,
        "frames": 450,
        "inputs": {
            "source_tree_sha256": "fixture-renderer-source",
            "tier": "film",
        },
        "file_sha256": DELIVER.digest(segment),
    }
    segment.with_name(segment.name + ".receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    return segment


def write_fake_reel_concat(render_out: Path, payload: bytes = b"rendered reel") -> Path:
    """Write a one-segment concat and its exact receipt chain."""
    segment = render_out / "reel-default-seg-000.mp4"
    if not segment.is_file():
        write_fake_reel_segment(render_out)
    segment_receipt = segment.with_name(segment.name + ".receipt.json")
    picture = render_out / "reel-default.mp4"
    picture.write_bytes(payload)
    receipt = {
        "schema": "danse.render.concat.v1",
        "codec": "h264",
        "segments": [
            {
                "name": segment.name,
                "receipt_sha256": DELIVER.digest(segment_receipt),
            }
        ],
        "file_sha256": DELIVER.digest(picture),
    }
    picture.with_name(picture.name + ".receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    return picture


def retained_score_package(
    out: Path,
    package: Path,
    *,
    repository_head: str = "b" * 40,
) -> SimpleNamespace:
    """Create a passage package whose restart receipts live below capture_root."""
    program = json.loads((ROOT / "render/program.json").read_text())
    package.mkdir(parents=True)
    score = package / DELIVER.SCORE_SOURCE_ITEM
    score.parent.mkdir(parents=True)
    score.write_bytes(b"retained passage score")
    score_sha = DELIVER.digest(score)
    score_item = {
        "name": DELIVER.SCORE_SOURCE_ITEM,
        "bytes": score.stat().st_size,
        "sha256": score_sha,
    }
    passage = {
        "seed": DELIVER.hexseed(program["seed"]),
        "passage_seed": DELIVER.hexseed(SPAN["seed"]),
        "passage": SPAN["passage"],
        "start": 0.0,
        "t0": SPAN["t0"],
        "t1": SPAN["t1"],
        "duration": SPAN["duration"],
        "corpus_tier": "film",
    }
    render_root = DELIVER.capture_root(out, SPAN, SPAN["t0"])
    render_root.mkdir(parents=True)
    score_receipt = render_root / "passage-score.json"
    score_receipt.write_text(
        json.dumps(
            {
                "schema": "danse.score.receipt.v2",
                "sha256": score_sha,
                "t0": SPAN["t0"],
                "t1": SPAN["t1"],
                "duration": SPAN["duration"],
            },
            indent=2,
        )
        + "\n"
    )
    receipt_sha = DELIVER.digest(score_receipt)
    producer_id = f"score-{receipt_sha[:20]}"
    packaged_receipt = package / DELIVER.PRODUCER_RECEIPTS / f"{producer_id}.json"
    packaged_receipt.parent.mkdir(parents=True)
    shutil.copy2(score_receipt, packaged_receipt)
    source_tree = "5" * 64
    production = {
        "schema": "danse.delivery.production.v1",
        "repository_head": repository_head,
        "source_tree_sha256": source_tree,
        "passage": passage,
        "sound": None,
        "producers": [
            {
                "id": producer_id,
                "kind": "score",
                "receipt": {
                    "path": packaged_receipt.relative_to(package).as_posix(),
                    "sha256": receipt_sha,
                },
                "output_sha256": score_sha,
                "components": [],
            }
        ],
        "outputs": [{**score_item, "producers": [producer_id]}],
    }
    production_path = package / DELIVER.PRODUCTION_RECEIPT
    production_path.write_text(json.dumps(production, indent=2) + "\n")
    manifest = {
        "schema": "danse.delivery.manifest.v1",
        "title": program["title"],
        "repository_head": repository_head,
        **passage,
        "source_tree_sha256": source_tree,
        "items": [score_item],
        "production": {
            "path": DELIVER.PRODUCTION_RECEIPT,
            "sha256": DELIVER.digest(production_path),
        },
    }
    (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return SimpleNamespace(
        manifest=manifest,
        render_root=render_root,
        score_receipt=score_receipt,
        packaged_receipt=packaged_receipt,
        production_path=production_path,
    )


class DeliveryContractTest(unittest.TestCase):
    def test_normalization_targets_are_owned_by_the_mix_contract(self) -> None:
        settings = {
            "target_lufs": -18.0,
            "tolerance_lu": 0.75,
            "target_true_peak_dbtp": -2.1,
            "max_true_peak_dbtp": -2.0,
            "target_lra_lu": 9.0,
        }
        self.assertEqual(
            DELIVER.normalization_targets_from_mix(settings),
            {
                "integrated_lufs": -18.0,
                "tolerance_lu": 0.75,
                "target_true_peak_dbtp": -2.1,
                "max_true_peak_dbtp": -2.0,
                "lra_lu": 9.0,
            },
        )
        with self.assertRaisesRegex(SystemExit, "internally inconsistent"):
            DELIVER.normalization_targets_from_mix({**settings, "tolerance_lu": 0})

    def test_production_delivery_rejects_nonzero_score_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    ["deliver.py", "--only", "master", "--start", "1", "--out", tmp],
                ),
                self.assertRaisesRegex(SystemExit, "score time 0"),
            ):
                DELIVER.main()

    def test_competition_package_rejects_fixture_or_pending_repertoire(self) -> None:
        score = {
            "identity": {"work_id": "delibes-screendance-suite", "midi_sha256": "a" * 64},
            "release_status": "production-selected",
            "time": {"passage_mapping": "native-tempo", "duration_seconds": SPAN["duration"]},
        }
        placeholders = [
            score,
            {"identity": {}},
            {"output": {"sha256": "a" * 64}},
            {},
            {},
            {},
            {},
        ]
        for artistic, role in (("pending", "repertoire"), ("accepted", "fixture")):
            with self.subTest(artistic=artistic, role=role), tempfile.TemporaryDirectory() as tmp:
                register = Path(tmp) / "repertoire.yaml"
                register.write_text(
                    yaml.safe_dump(
                        {
                            "artistic_gate": {"status": artistic},
                            "works": [
                                {
                                    "id": "delibes-screendance-suite",
                                    "role": role,
                                    "selection": {"status": "selected"},
                                }
                            ],
                        }
                    )
                )
                with (
                    mock.patch.object(DELIVER, "MUSIC_REPERTOIRE", register),
                    mock.patch.object(DELIVER, "regular_json", side_effect=placeholders.copy()),
                    self.assertRaisesRegex(SystemExit, "fixture or pending artistic repertoire"),
                ):
                    DELIVER.competition_audio_provenance(SPAN)

    def test_offline_url_preserves_zero_seed_and_every_capture_override(self) -> None:
        args = SimpleNamespace(
            window="passage",
            start=120.25,
            tier="film",
            seed=0,
            stream=7,
            width=3840,
            height=2160,
            fps=24,
            timing_score="music/score.json",
        )
        query = parse_qs(urlparse(OFFLINE.film_url("http://render.test", args)).query)
        self.assertEqual(
            query,
            {
                "capture": ["passage"],
                "from": ["120.25"],
                "tier": ["film"],
                "s": ["0"],
                "u": ["7"],
                "width": ["3840"],
                "height": ["2160"],
                "fps": ["24"],
                "passage-seconds": ["350.896343125"],
            },
        )
        with self.assertRaisesRegex(SystemExit, "non-empty score file"):
            OFFLINE.music_score_identity(SimpleNamespace(score=""))
        with self.assertRaisesRegex(SystemExit, "non-empty score file"):
            OFFLINE.timing_score_identity(SimpleNamespace(timing_score=""))

    def test_timing_control_output_stem_cannot_overwrite_the_scored_producer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            common = {
                "window": "passage",
                "seed": 20170620,
                "stream": 0,
                "out": Path(tmp),
            }
            scored = SimpleNamespace(**common, timing_score=None)
            control = SimpleNamespace(**common, timing_score="music/score.json")
            ordinary = SimpleNamespace(**common)
            scored_stem = OFFLINE.output_stem(scored)
            control_stem = OFFLINE.output_stem(control)
            self.assertEqual(scored_stem.name, "passage-20170620")
            self.assertEqual(ordinary.out / "passage-20170620", OFFLINE.output_stem(ordinary))
            self.assertEqual(control_stem.name, "passage-20170620-control")
            self.assertNotEqual(scored_stem, control_stem)
            scored_segments = OFFLINE.segment_paths(scored_stem, "prores", [0, 1])
            control_segments = OFFLINE.segment_paths(control_stem, "prores", [0, 1])
            scored_paths = {
                *scored_segments,
                *(OFFLINE.segment_receipt_path(path) for path in scored_segments),
                OFFLINE.concat_listing_path(scored_stem),
                scored_stem.with_suffix(".mov"),
                OFFLINE.concat_receipt_path(scored_stem.with_suffix(".mov")),
                *(OFFLINE.determinism_path(scored_stem, "prores", pass_) for pass_ in (1, 2)),
            }
            control_paths = {
                *control_segments,
                *(OFFLINE.segment_receipt_path(path) for path in control_segments),
                OFFLINE.concat_listing_path(control_stem),
                control_stem.with_suffix(".mov"),
                OFFLINE.concat_receipt_path(control_stem.with_suffix(".mov")),
                *(OFFLINE.determinism_path(control_stem, "prores", pass_) for pass_ in (1, 2)),
            }
            self.assertTrue(scored_paths.isdisjoint(control_paths))

    def test_offline_render_rejects_an_unauthorized_tier_before_capture(self) -> None:
        render = mock.Mock(side_effect=AssertionError("unauthorized tier reached the renderer"))
        authorization = mock.Mock(return_value=(False, "stale receipt"))
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(sys, "argv", ["render.py", "--segment", "0"]),
                mock.patch.object(OFFLINE, "authorize_render_tier", authorization),
                mock.patch.object(OFFLINE, "render_segment", render),
                mock.patch.dict(OFFLINE.os.environ, {"DANSE_WORK": tmp}, clear=True),
                redirect_stderr(io.StringIO()) as error,
            ):
                self.assertEqual(OFFLINE.main(), 1)
            authorization.assert_called_once_with(OFFLINE.APP / "corpus", Path(tmp), "screen")
        self.assertFalse(render.called)
        self.assertIn("stale receipt", error.getvalue())

    def test_render_and_delivery_source_identities_bind_the_tier_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = root / "corpus/tier-receipts/screen.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_bytes(b"first receipt")

            with mock.patch.object(OFFLINE, "APP", root):
                first = OFFLINE.source_tree_sha256(SimpleNamespace(tier="screen"))
                receipt.write_bytes(b"second receipt")
                second = OFFLINE.source_tree_sha256(SimpleNamespace(tier="screen"))
            self.assertNotEqual(first, second)

            render_dir = root / "render"
            render_dir.mkdir()
            program = render_dir / "program.json"
            bank = root / "sound/bank/bank.json"
            bank.parent.mkdir(parents=True)
            program.write_text("{}")
            bank.write_text("{}")
            DELIVER.delivery_source_sha256.cache_clear()
            with (
                mock.patch.object(DELIVER, "DANSE", root),
                mock.patch.object(DELIVER, "HERE", render_dir),
                mock.patch.object(DELIVER, "PROGRAM", program),
                mock.patch.object(DELIVER, "BANK", bank),
            ):
                third = DELIVER.delivery_source_sha256("screen")
                receipt.write_bytes(b"third receipt")
                DELIVER.delivery_source_sha256.cache_clear()
                fourth = DELIVER.delivery_source_sha256("screen")
            DELIVER.delivery_source_sha256.cache_clear()
            self.assertNotEqual(third, fourth)

    def test_decoded_rgb_identity_hashes_exact_frames_and_fails_closed(self) -> None:
        first = bytes(range(6))
        second = bytes(range(6, 12))
        identity = OFFLINE.rgb24_stream_identity(
            io.BytesIO(first + second),
            width=2,
            height=1,
            expected_frames=2,
        )
        self.assertEqual(
            identity,
            {
                "algorithm": "rgb24-stream-sha256-v1",
                "sha256": hashlib.sha256(first + second).hexdigest(),
                "frames": 2,
                "width": 2,
                "height": 1,
            },
        )
        with self.assertRaisesRegex(OFFLINE.MediaIdentityError, "partial frame"):
            OFFLINE.rgb24_stream_identity(
                io.BytesIO(first + second + b"partial"),
                width=2,
                height=1,
            )
        with self.assertRaisesRegex(
            OFFLINE.MediaIdentityError,
            "more than the expected 1 frames",
        ):
            OFFLINE.rgb24_stream_identity(
                io.BytesIO(first + second),
                width=2,
                height=1,
                expected_frames=1,
            )

    def test_segment_encoder_uses_bounded_stderr_and_reaps_every_exit(self) -> None:
        events: list[str] = []

        class FakeStdin:
            def __init__(self, close_error: OSError | None = None) -> None:
                self.closed = False
                self.close_error = close_error

            def close(self) -> None:
                events.append("close")
                if self.close_error is not None:
                    raise self.close_error
                self.closed = True

        class FakeProcess:
            def __init__(
                self,
                returncode: int = 0,
                close_error: OSError | None = None,
            ) -> None:
                self.stdin = FakeStdin(close_error)
                self.returncode = returncode

            def poll(self):
                events.append("poll")
                return None

            def kill(self) -> None:
                events.append("kill")

            def wait(self) -> int:
                events.append("wait")
                return self.returncode

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryFile() as stderr:
            process = FakeProcess()
            with mock.patch.object(OFFLINE.subprocess, "Popen", return_value=process) as popen:
                self.assertIs(
                    OFFLINE.ffmpeg_for(
                        Path(tmp) / "segment.mov",
                        1920,
                        1080,
                        30,
                        "prores",
                        stderr=stderr,
                    ),
                    process,
                )
            command = popen.call_args.args[0]
            self.assertIn("-nostdin", command)
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)
            self.assertIs(popen.call_args.kwargs["stderr"], stderr)
            self.assertNotEqual(popen.call_args.kwargs["stderr"], subprocess.PIPE)

        class TrackingStderr:
            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> None:
                events.append("stderr-close")

            def flush(self) -> None:
                events.append("flush")

            def seek(self, offset: int) -> None:
                self.assert_waited = "wait" in events
                events.append(f"seek:{offset}")

            def read(self, size: int) -> bytes:
                if not self.assert_waited:
                    raise AssertionError("encoder stderr read before wait")
                events.append(f"read:{size}")
                return b"x" * size

        diagnostic = TrackingStderr()
        failed = FakeProcess(returncode=1)
        events.clear()
        with (
            mock.patch.object(OFFLINE.tempfile, "TemporaryFile", return_value=diagnostic),
            mock.patch.object(OFFLINE, "ffmpeg_for", return_value=failed),
            self.assertRaisesRegex(
                SystemExit,
                r"(?s)ffmpeg failed on segment 7:.*stderr truncated",
            ),
        ):
            with OFFLINE.encoder_for_segment(Path("segment.mov"), 2, 1, 30, "prores", 7):
                pass
        self.assertEqual(events[:2], ["close", "wait"])
        self.assertIn(f"read:{OFFLINE.ENCODER_DIAGNOSTIC_MAX_BYTES + 1}", events)
        self.assertNotIn("kill", events)

        close_failed = FakeProcess(close_error=OSError("refused EOF"))
        events.clear()
        with (
            mock.patch.object(OFFLINE.tempfile, "TemporaryFile", return_value=diagnostic),
            mock.patch.object(OFFLINE, "ffmpeg_for", return_value=close_failed),
            self.assertRaisesRegex(
                SystemExit,
                r"(?s)ffmpeg stdin close failed: refused EOF.*stderr truncated",
            ),
        ):
            with OFFLINE.encoder_for_segment(Path("segment.mov"), 2, 1, 30, "prores", 8):
                pass
        self.assertEqual(events[:4], ["close", "poll", "kill", "wait"])
        self.assertIn(f"read:{OFFLINE.ENCODER_DIAGNOSTIC_MAX_BYTES + 1}", events)

        interrupted = FakeProcess()
        events.clear()
        with (
            mock.patch.object(OFFLINE, "ffmpeg_for", return_value=interrupted),
            self.assertRaisesRegex(RuntimeError, "capture failed"),
        ):
            with OFFLINE.encoder_for_segment(Path("segment.mov"), 2, 1, 30, "prores", 9):
                raise RuntimeError("capture failed")
        self.assertEqual(events[:4], ["poll", "kill", "close", "wait"])

    def test_render_resume_receipt_binds_inputs_source_and_output_bytes(self) -> None:
        args = SimpleNamespace(
            window="passage",
            start=0.0,
            tier="film",
            seed=0,
            stream=7,
            codec="prores",
            width=3840,
            height=2160,
            fps=30,
            segment_frames=900,
            timing_score="music/score.json",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            OFFLINE, "source_tree_sha256", return_value="source-tree"
        ):
            dest = Path(tmp) / "passage-0-seg-000.mov"
            dest.write_bytes(b"encoded segment")
            stream = {"width": 3840, "height": 2160, "fps": 30}
            decoded = {
                "algorithm": "rgb24-stream-sha256-v1",
                "sha256": "d" * 64,
                "frames": 30,
                "width": 3840,
                "height": 2160,
            }
            capture = {
                "frames": 30,
                "missing": 0,
                "sha256": "a" * 64,
                "signature": "renderer-signature",
                "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M5, Unspecified Version)",
                "width": 3840,
                "height": 2160,
                "fps": 30,
                "passage": {
                    "index": 0,
                    "seed": OFFLINE.passage_seed(0, 0, 7),
                    "t0": 0.0,
                    "seconds": 350.896343125,
                },
            }
            with (
                mock.patch.object(OFFLINE, "video_stream_info", return_value=stream),
                mock.patch.object(OFFLINE, "decoded_video_identity", return_value=decoded),
            ):
                OFFLINE.write_segment_receipt(dest, args, 0, 30, capture=capture)
                receipt = json.loads(OFFLINE.segment_receipt_path(dest).read_text())
                self.assertEqual(receipt["file_bytes"], dest.stat().st_size)
                self.assertEqual(receipt["decoded_video"], decoded)
                self.assertEqual(
                    receipt["capture"],
                    {
                        "renderer": capture["renderer"],
                        "raw_rgba_sha256": "a" * 64,
                        "missing": 0,
                        "signature": "renderer-signature",
                        "passage": capture["passage"],
                    },
                )
                expected = OFFLINE.segment_identity(args, 0, 30)
                self.assertEqual(
                    expected["inputs"]["passage_timing"],
                    {"mode": "fixed-passage", "seconds": 350.896343125},
                )
                self.assertEqual(
                    expected["inputs"]["timing_score"]["contract_sha256"],
                    json.loads((ROOT / "music/score.json").read_text())["identity"]["contract_sha256"],
                )
                self.assertTrue(OFFLINE.complete(dest, 30, expected))
                for invalid_bytes in (None, False, 0, receipt["file_bytes"] + 1):
                    with self.subTest(segment_file_bytes=invalid_bytes):
                        stale_bytes = copy.deepcopy(receipt)
                        if invalid_bytes is None:
                            stale_bytes.pop("file_bytes")
                        else:
                            stale_bytes["file_bytes"] = invalid_bytes
                        OFFLINE.segment_receipt_path(dest).write_text(
                            json.dumps(stale_bytes, indent=2) + "\n"
                        )
                        self.assertFalse(OFFLINE.complete(dest, 30, expected))
                receipt_without_capture = copy.deepcopy(receipt)
                receipt_without_capture.pop("capture")
                OFFLINE.segment_receipt_path(dest).write_text(
                    json.dumps(receipt_without_capture, indent=2) + "\n"
                )
                self.assertFalse(OFFLINE.complete(dest, 30, expected))
                stale_capture = copy.deepcopy(receipt)
                stale_capture["capture"]["passage"]["seed"] ^= 1
                OFFLINE.segment_receipt_path(dest).write_text(
                    json.dumps(stale_capture, indent=2) + "\n"
                )
                self.assertFalse(OFFLINE.complete(dest, 30, expected))
                for field in ("index", "t0"):
                    with self.subTest(falsy_passage_field=field):
                        falsy_capture = copy.deepcopy(receipt)
                        falsy_capture["capture"]["passage"][field] = False
                        OFFLINE.segment_receipt_path(dest).write_text(
                            json.dumps(falsy_capture, indent=2) + "\n"
                        )
                        self.assertFalse(OFFLINE.complete(dest, 30, expected))
                invalid_capture = copy.deepcopy(capture)
                invalid_capture["passage"]["t0"] = False
                with self.assertRaisesRegex(SystemExit, "invalid provenance identity"):
                    OFFLINE.write_segment_receipt(dest, args, 0, 30, capture=invalid_capture)
                OFFLINE.segment_receipt_path(dest).write_text(json.dumps(receipt, indent=2) + "\n")
                args._timing_score_identity = {
                    **expected["inputs"]["timing_score"],
                    "duration_seconds": 312.540051998,
                }
                self.assertFalse(OFFLINE.complete(dest, 30, OFFLINE.segment_identity(args, 0, 30)))
                args._timing_score_identity = expected["inputs"]["timing_score"]
                args.start = 1.0
                self.assertFalse(OFFLINE.complete(dest, 30, OFFLINE.segment_identity(args, 0, 30)))
                args.start = 0.0
                dest.write_bytes(b"different segment")
                self.assertFalse(OFFLINE.complete(dest, 30, expected))

    def test_emergency_source_identity_revalidates_cached_inputs(self) -> None:
        args = SimpleNamespace(emergency_software_render=True, seed=0)

        def source_probe(command, **_kwargs):
            if command[:2] == ["git", "status"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if command[:3] == ["git", "rev-parse", "--verify"]:
                identity = "a" * 40 if command[-1] == "HEAD^{commit}" else "b" * 40
                return subprocess.CompletedProcess(command, 0, stdout=identity + "\n", stderr="")
            if command[-1] == "--version":
                return subprocess.CompletedProcess(command, 0, stdout="Chromium fixture\n", stderr="")
            raise AssertionError(f"unexpected source probe: {command}")

        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "chromium"
            executable.write_bytes(b"first Chromium bytes")
            with (
                mock.patch.dict(os.environ, {"DANSE_CHROME_EXECUTABLE": str(executable)}),
                mock.patch.object(OFFLINE.subprocess, "run", side_effect=source_probe),
            ):
                identity = OFFLINE.emergency_source_identity(args)
                self.assertEqual(identity["browser_toolchain"]["executable_sha256"], OFFLINE.file_sha256(executable))
                executable.write_bytes(b"changed Chromium bytes")
                with self.assertRaisesRegex(SystemExit, "identity changed during capture"):
                    OFFLINE.emergency_source_identity(args)

    def test_film_rejects_legacy_duration_and_uses_exact_passage_span_selection(self) -> None:
        film = (ROOT / "film.html").read_text(encoding="utf-8")
        self.assertIn('if (q.has("duration"))', film)
        self.assertIn("Program.captureSpan", film)
        self.assertNotIn("t1 + 1e-6", film)

    def test_concat_uses_only_explicitly_planned_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stem = root / "passage-default"
            args = SimpleNamespace(codec="prores")
            parts = OFFLINE.segment_paths(stem, args.codec, [0, 1])
            all_parts = [*parts, root / "passage-default-seg-002.mov"]
            for index, part in enumerate(all_parts):
                part.write_bytes(part.name.encode())
                OFFLINE.segment_receipt_path(part).write_text(
                    json.dumps(
                        {
                            "schema": "danse.render.segment.v1",
                            "segment": index,
                            "frames": 1,
                            "inputs": {},
                            "file_sha256": OFFLINE.file_sha256(part),
                        }
                    )
                )

            def fake_concat(*_args, **_kwargs):
                stem.with_suffix(".mov").write_bytes(b"planned concat")
                return subprocess.CompletedProcess([], 0)

            stream = {"width": 1920, "height": 1080, "fps": 30}
            decoded = {
                "algorithm": "rgb24-stream-sha256-v1",
                "sha256": "b" * 64,
                "frames": 2,
                "width": 1920,
                "height": 1080,
            }
            with (
                mock.patch.object(OFFLINE.subprocess, "run", side_effect=fake_concat),
                mock.patch.object(OFFLINE, "video_stream_info", return_value=stream),
                mock.patch.object(
                    OFFLINE,
                    "decoded_video_identity",
                    side_effect=lambda *_args, **_kwargs: dict(decoded),
                ),
            ):
                OFFLINE.concat(stem, args, parts)
                listing = (root / "passage-default-segments.txt").read_text()
                self.assertIn(parts[0].name, listing)
                self.assertIn(parts[1].name, listing)
                self.assertNotIn("seg-002", listing)
                receipt_path = OFFLINE.concat_receipt_path(stem.with_suffix(".mov"))
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt["file_bytes"], stem.with_suffix(".mov").stat().st_size)
                self.assertEqual([item["name"] for item in receipt["segments"]], [part.name for part in parts])
                self.assertEqual(receipt["decoded_video"], {**decoded, "fps": 30})
                self.assertTrue(OFFLINE.concat_complete(stem, args, parts))
                receipt["file_bytes"] = False
                receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
                self.assertFalse(OFFLINE.concat_complete(stem, args, parts))
                receipt["file_bytes"] = stem.with_suffix(".mov").stat().st_size
                receipt["decoded_video"]["sha256"] = "c" * 64
                receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
                self.assertFalse(OFFLINE.concat_complete(stem, args, parts))

    def test_query_and_exact_tier_contracts_fail_closed(self) -> None:
        script = """
          import { numericParam } from './engine/query.js';
          import { requireTier } from './engine/tier.js';
          import { fromData } from './engine/corpus.js';
          const zero = numericParam(new URLSearchParams('s=0'), 's', 99, {integer:true,min:0});
          let invalid = false;
          try { numericParam(new URLSearchParams('s=nope'), 's', 99, {integer:true,min:0}); } catch { invalid = true; }
          const corpus = {
            ensure: async () => {},
            has: (kind) => kind === 'plates',
          };
          let missingMatte = false;
          try { await requireTier(corpus, 'film', ['IMG_1570']); } catch { missingMatte = true; }
          const requested = [];
          globalThis.Image = class { set src(value) { requested.push(value); } };
          const progressive = fromData('/corpus/', {
            tiers: {browse:{width:512,eager:true}, screen:{width:1024,eager:false}},
            frames: [],
          });
          const fallback = {};
          progressive.textures.set('plates/browse/IMG_1570', fallback);
          const got = progressive.get(null, 'plates', 'IMG_1570', 'screen');
          progressive.get(null, 'plates', 'IMG_1570', 'screen');
          console.log(JSON.stringify({
            zero, invalid, missingMatte,
            progressiveFallback: got === fallback,
            progressiveRequests: requested,
          }));
        """
        done = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            json.loads(done.stdout),
            {
                "zero": 0,
                "invalid": True,
                "missingMatte": True,
                "progressiveFallback": True,
                "progressiveRequests": ["/corpus/plates/screen/IMG_1570.webp"],
            },
        )

    def test_progressive_tier_failure_is_cached_until_invalidation(self) -> None:
        script = """
          import { fromData } from './engine/corpus.js';
          const requested = [];
          let shouldFail = true;
          globalThis.Image = class {
            set src(value) {
              requested.push(value);
              const fail = shouldFail;
              queueMicrotask(() => fail ? this.onerror() : this.onload());
            }
          };
          const settle = async () => {
            await Promise.resolve();
            await Promise.resolve();
            await Promise.resolve();
          };
          const corpus = fromData('/corpus/', {
            tiers: {browse:{width:512,eager:true}, screen:{width:1024,eager:false}},
            frames: [],
          });
          const key = 'plates/screen/IMG_1570';
          const fallbackKey = 'plates/browse/IMG_1570';
          const fallback = {};
          corpus.textures.set(fallbackKey, fallback);

          const firstFallback = corpus.get(null, 'plates', 'IMG_1570', 'screen') === fallback;
          await settle();
          const repeatedFallback = Array.from(
            {length: 20},
            () => corpus.get(null, 'plates', 'IMG_1570', 'screen') === fallback,
          ).every(Boolean);
          const requestsAfterFailure = requested.length;

          shouldFail = false;
          await corpus.ensure('plates', 'screen', ['IMG_1570']);
          const recovered = corpus.has('plates', 'screen', 'IMG_1570') && !corpus.failed.has(key);
          corpus.images.delete(key);

          shouldFail = true;
          const recoveredFallback = corpus.get(null, 'plates', 'IMG_1570', 'screen') === fallback;
          await settle();
          corpus.get(null, 'plates', 'IMG_1570', 'screen');
          const requestsAfterRecoveryFailure = requested.length;

          corpus.invalidate();
          corpus.textures.set(fallbackKey, fallback);
          const invalidatedFallback = corpus.get(null, 'plates', 'IMG_1570', 'screen') === fallback;
          const requestsAfterInvalidation = requested.length;

          console.log(JSON.stringify({
            firstFallback,
            repeatedFallback,
            requestsAfterFailure,
            recovered,
            recoveredFallback,
            requestsAfterRecoveryFailure,
            invalidatedFallback,
            requestsAfterInvalidation,
          }));
        """
        done = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            json.loads(done.stdout),
            {
                "firstFallback": True,
                "repeatedFallback": True,
                "requestsAfterFailure": 1,
                "recovered": True,
                "recoveredFallback": True,
                "requestsAfterRecoveryFailure": 3,
                "invalidatedFallback": True,
                "requestsAfterInvalidation": 4,
            },
        )

    def test_invalidation_starts_a_fresh_request_while_the_old_one_is_pending(self) -> None:
        script = """
          import { fromData } from './engine/corpus.js';
          const requested = [];
          globalThis.Image = class {
            set src(value) { this.url = value; requested.push(this); }
          };
          const settle = async () => {
            await Promise.resolve();
            await Promise.resolve();
            await Promise.resolve();
          };
          const corpus = fromData('/corpus/', {
            tiers: {browse:{width:512,eager:true}, screen:{width:1024,eager:false}},
            frames: [],
          });
          const key = 'plates/screen/IMG_1570';
          const fallbackKey = 'plates/browse/IMG_1570';
          const fallback = {};
          corpus.textures.set(fallbackKey, fallback);
          corpus.get(null, 'plates', 'IMG_1570', 'screen');
          const firstEpoch = corpus.failed;

          corpus.invalidate();
          corpus.textures.set(fallbackKey, fallback);
          corpus.get(null, 'plates', 'IMG_1570', 'screen');
          const secondEpoch = corpus.failed;
          const freshRequestStarted = requested.length === 2 && firstEpoch !== secondEpoch;
          const newRequestOwnsPending = corpus.pending.get(key) === secondEpoch;

          requested[0].onerror();
          await settle();
          const oldCompletionPreservesPending = corpus.pending.get(key) === secondEpoch;
          const oldFailureDidNotPoison = !secondEpoch.has(key);
          corpus.get(null, 'plates', 'IMG_1570', 'screen');
          const repeatedGetDeduplicated = requested.length === 2;

          requested[1].onerror();
          await settle();
          console.log(JSON.stringify({
            freshRequestStarted,
            newRequestOwnsPending,
            oldCompletionPreservesPending,
            oldFailureDidNotPoison,
            repeatedGetDeduplicated,
            currentFailureRecorded: corpus.failed.has(key),
            pendingCleared: !corpus.pending.has(key),
          }));
        """
        done = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            json.loads(done.stdout),
            {
                "freshRequestStarted": True,
                "newRequestOwnsPending": True,
                "oldCompletionPreservesPending": True,
                "oldFailureDidNotPoison": True,
                "repeatedGetDeduplicated": True,
                "currentFailureRecorded": True,
                "pendingCleared": True,
            },
        )

    def test_closing_signature_names_reproducible_river_position(self) -> None:
        script = """
          import { signature } from './engine/engine.js';
          const program = {signature:{format:'river 0x%RIVER_SEED%/%RIVER_STREAM% from %PASSAGE_T0%s passage %PASSAGE%'}};
          console.log(signature(program, {riverSeed:0, riverStream:7, passageSeed:123, passageT0:12.5, passage:4}));
        """
        done = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "river 0x000000/000007 from 12.500s passage 4")

    def test_room_cache_identity_changes_with_same_count_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, mask, pose = root / "raw.jpg", root / "mask.png", root / "pose.json"
            raw.write_bytes(b"first original")
            mask.write_bytes(b"first matte")
            pose.write_text("{}")
            items = [("IMG_1570", raw, mask, pose)]
            first = CORPUS_CONTRACT.room_cache_key(items)
            source_receipt = CORPUS_CONTRACT.source_set_receipt([raw])
            source_identity = CORPUS_CONTRACT.corpus_source_identity(items)
            tier_identity = CORPUS_CONTRACT.tier_source_identity(source_identity, {"width": 512}, 85)
            raw.write_bytes(b"corrected original")
            self.assertNotEqual(first, CORPUS_CONTRACT.room_cache_key(items))
            self.assertNotEqual(source_receipt, CORPUS_CONTRACT.source_set_receipt([raw]))
            self.assertNotEqual(source_identity, CORPUS_CONTRACT.corpus_source_identity(items))
            self.assertNotEqual(tier_identity, CORPUS_CONTRACT.tier_source_identity(source_identity, {"width": 1024}, 85))

    def test_tier_output_identity_rejects_missing_and_extra_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plate = root / "plates/browse/IMG_1570.webp"
            matte = root / "mattes/browse/IMG_1570.webp"
            plate.parent.mkdir(parents=True)
            matte.parent.mkdir(parents=True)
            plate.write_bytes(b"plate bytes")
            matte.write_bytes(b"matte bytes")

            identity = CORPUS_CONTRACT.tier_output_identity(root, "browse", ["IMG_1570"])
            self.assertIsNotNone(identity)
            linked_root = root / "linked-root"
            linked_root.symlink_to(root, target_is_directory=True)
            self.assertIsNone(CORPUS_CONTRACT.tier_output_identity(linked_root, "browse", ["IMG_1570"]))
            matte.unlink()
            self.assertIsNone(CORPUS_CONTRACT.tier_output_identity(root, "browse", ["IMG_1570"]))
            matte.write_bytes(b"matte bytes")
            surplus = matte.parent / "unexpected.webp"
            surplus.write_bytes(b"surplus")
            self.assertIsNone(CORPUS_CONTRACT.tier_output_identity(root, "browse", ["IMG_1570"]))
            surplus.unlink()

            outside = root / "outside.webp"
            outside.write_bytes(b"bytes outside the corpus tier")
            matte.unlink()
            matte.symlink_to(outside)
            self.assertIsNone(CORPUS_CONTRACT.tier_output_identity(root, "browse", ["IMG_1570"]))
            matte.unlink()
            matte.write_bytes(b"matte bytes")

            outside_tier = root / "outside-tier"
            outside_tier.mkdir()
            (outside_tier / plate.name).write_bytes(b"plate bytes")
            plate.unlink()
            plate.parent.rmdir()
            plate.parent.symlink_to(outside_tier, target_is_directory=True)
            self.assertIsNone(CORPUS_CONTRACT.tier_output_identity(root, "browse", ["IMG_1570"]))

            plate.parent.unlink()
            plate.parent.mkdir()
            plate.write_bytes(b"plate bytes")
            outside_plates = root / "outside-plates"
            (outside_plates / "browse").mkdir(parents=True)
            (outside_plates / "browse" / plate.name).write_bytes(b"plate bytes")
            shutil.rmtree(root / "plates")
            (root / "plates").symlink_to(outside_plates, target_is_directory=True)
            self.assertIsNone(CORPUS_CONTRACT.tier_output_identity(root, "browse", ["IMG_1570"]))

            (root / "plates").unlink()
            plate.parent.mkdir(parents=True)
            plate.write_bytes(b"plate bytes")
            outside_mattes = root / "outside-mattes"
            (outside_mattes / "browse").mkdir(parents=True)
            (outside_mattes / "browse" / matte.name).write_bytes(b"matte bytes")
            shutil.rmtree(root / "mattes")
            (root / "mattes").symlink_to(outside_mattes, target_is_directory=True)
            self.assertIsNone(CORPUS_CONTRACT.tier_output_identity(root, "browse", ["IMG_1570"]))

    def test_tier_receipt_validator_rejects_mutation_versions_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plate = root / "plates/browse/IMG_1570.webp"
            matte = root / "mattes/browse/IMG_1570.webp"
            receipt = root / "tier-receipts/browse.json"
            plate.parent.mkdir(parents=True)
            matte.parent.mkdir(parents=True)
            receipt.parent.mkdir(parents=True)
            plate.write_bytes(b"plate bytes")
            matte.write_bytes(b"matte bytes")
            output = CORPUS_CONTRACT.tier_output_identity(root, "browse", ["IMG_1570"])
            payload = {
                "schema": "danse.corpus.tier-receipt.v2",
                "tier": "browse",
                "source_sha256": "1" * 64,
                "output_sha256": output,
            }
            receipt.write_text(json.dumps(payload))
            self.assertTrue(CORPUS_CONTRACT.tier_receipt_is_current(root, "browse", ["IMG_1570"]))

            plate.write_bytes(b"mutated")
            self.assertFalse(CORPUS_CONTRACT.tier_receipt_is_current(root, "browse", ["IMG_1570"]))
            plate.write_bytes(b"plate bytes")
            payload["schema"] = "danse.corpus.tier-receipt.v1"
            receipt.write_text(json.dumps(payload))
            self.assertFalse(CORPUS_CONTRACT.tier_receipt_is_current(root, "browse", ["IMG_1570"]))

            payload["schema"] = "danse.corpus.tier-receipt.v2"
            target = root / "receipt-target.json"
            target.write_text(json.dumps(payload))
            receipt.unlink()
            receipt.symlink_to(target)
            self.assertFalse(CORPUS_CONTRACT.tier_receipt_is_current(root, "browse", ["IMG_1570"]))

            receipt.unlink()
            receipt.write_text(json.dumps(payload))
            external_receipts = root / "external-tier-receipts"
            receipt.parent.rename(external_receipts)
            receipt.parent.symlink_to(external_receipts, target_is_directory=True)
            self.assertFalse(CORPUS_CONTRACT.tier_receipt_is_current(root, "browse", ["IMG_1570"]))

    def test_tracked_shipped_tier_receipts_match_every_committed_byte(self) -> None:
        manifest = json.loads((ROOT / "corpus/manifest.json").read_text())
        ids = [frame["id"] for frame in manifest["frames"]]
        for tier in ("browse", "screen"):
            with self.subTest(tier=tier):
                self.assertTrue(CORPUS_CONTRACT.tier_receipt_is_current(ROOT / "corpus", tier, ids))

    def test_local_render_authorization_binds_hydrated_sources_and_output_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work, corpus = corpus_fixture(root)
            public = corpus_public_manifest(work)
            corpus.mkdir(parents=True)
            (corpus / "manifest.json").write_text(json.dumps(public))
            plate = corpus / "plates/film/IMG_1570.webp"
            matte = corpus / "mattes/film/IMG_1570.webp"
            plate.parent.mkdir(parents=True)
            matte.parent.mkdir(parents=True)
            plate.write_bytes(b"film plate")
            matte.write_bytes(b"film matte")
            nbytes = plate.stat().st_size + matte.stat().st_size
            (corpus / "manifest.local.json").write_text(
                json.dumps(
                    {
                        "schema": "danse.corpus.local.v1",
                        "tiers": {"film": CORPUS_CONTRACT.tier_manifest_entry("film", nbytes)},
                    }
                )
            )
            items, incomplete = CORPUS_CONTRACT.frame_inventory(work)
            self.assertFalse(incomplete)
            source = CORPUS_CONTRACT.tier_source_identity(
                CORPUS_CONTRACT.corpus_source_identity(items),
                CORPUS_CONTRACT.TIER_SPECS["film"],
                CORPUS_CONTRACT.MATTE_QUALITY,
            )
            receipt = corpus / "tier-receipts/film.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text(
                json.dumps(
                    {
                        "schema": "danse.corpus.tier-receipt.v2",
                        "tier": "film",
                        "source_sha256": source,
                        "output_sha256": CORPUS_CONTRACT.tier_output_identity(
                            corpus, "film", ["IMG_1570"]
                        ),
                    }
                )
            )
            self.assertEqual(
                CORPUS_CONTRACT.authorize_render_tier(corpus, work, "film"),
                (True, "1 exact plate+matte pairs"),
            )

            plate.write_bytes(b"FILM PLATE")
            allowed, detail = CORPUS_CONTRACT.authorize_render_tier(corpus, work, "film")
            self.assertFalse(allowed)
            self.assertIn("receipt", detail)

            plate.write_bytes(b"film plate")
            raw = work / "raw/IMG_1570.png"
            raw.unlink()
            raw.symlink_to(work / "raw/missing.png")
            allowed, detail = CORPUS_CONTRACT.authorize_render_tier(corpus, work, "film")
            self.assertFalse(allowed)
            self.assertIn("source bytes are unreadable", detail)

    def test_tier_retention_rejects_mutated_bytes_and_source_only_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work, out = corpus_fixture(root)
            plate = out / "plates/browse/IMG_1570.webp"
            matte = out / "mattes/browse/IMG_1570.webp"
            receipt = out / "tier-receipts/browse.json"
            plate.parent.mkdir(parents=True)
            matte.parent.mkdir(parents=True)
            receipt.parent.mkdir(parents=True)
            plate.write_bytes(b"encoded plate")
            matte.write_bytes(b"encoded matte")
            items, incomplete = CORPUS_CONTRACT.frame_inventory(work)
            self.assertFalse(incomplete)
            source = CORPUS_CONTRACT.tier_source_identity(
                CORPUS_CONTRACT.corpus_source_identity(items),
                CORPUS_PIPELINE.TIERS["browse"],
                CORPUS_PIPELINE.MATTE_QUALITY,
            )
            output = CORPUS_CONTRACT.tier_output_identity(out, "browse", ["IMG_1570"])
            receipt.write_text(
                json.dumps(
                    {
                        "schema": "danse.corpus.tier-receipt.v2",
                        "tier": "browse",
                        "source_sha256": source,
                        "output_sha256": output,
                    }
                )
            )

            self.assertEqual(run_corpus_pipeline(work, out, ""), 0)
            self.assertEqual(set(json.loads((out / "manifest.json").read_text())["tiers"]), {"browse"})

            plate.write_bytes(b"mutated plate")
            self.assertEqual(run_corpus_pipeline(work, out, ""), 0)
            self.assertEqual(json.loads((out / "manifest.json").read_text())["tiers"], {})

            plate.write_bytes(b"encoded plate")
            receipt.write_text(
                json.dumps(
                    {
                        "schema": "danse.corpus.tier-receipt.v1",
                        "tier": "browse",
                        "source_sha256": source,
                    }
                )
            )
            self.assertEqual(run_corpus_pipeline(work, out, ""), 0)
            self.assertEqual(json.loads((out / "manifest.json").read_text())["tiers"], {})

    def test_partial_shipped_rebuild_retains_only_receipted_current_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work, out = corpus_fixture(root)
            screen_plate = out / "plates/screen/IMG_1570.webp"
            screen_matte = out / "mattes/screen/IMG_1570.webp"
            screen_receipt = out / "tier-receipts/screen.json"
            screen_plate.parent.mkdir(parents=True)
            screen_matte.parent.mkdir(parents=True)
            screen_receipt.parent.mkdir(parents=True)
            screen_plate.write_bytes(b"screen plate")
            screen_matte.write_bytes(b"screen matte")
            items, incomplete = CORPUS_CONTRACT.frame_inventory(work)
            self.assertFalse(incomplete)
            screen_source = CORPUS_CONTRACT.tier_source_identity(
                CORPUS_CONTRACT.corpus_source_identity(items),
                CORPUS_PIPELINE.TIERS["screen"],
                CORPUS_PIPELINE.MATTE_QUALITY,
            )
            screen_receipt.write_text(
                json.dumps(
                    {
                        "schema": "danse.corpus.tier-receipt.v2",
                        "tier": "screen",
                        "source_sha256": screen_source,
                        "output_sha256": CORPUS_CONTRACT.tier_output_identity(
                            out, "screen", ["IMG_1570"]
                        ),
                    }
                )
            )

            def fake_encode(_src, dest, *_args, **_kwargs):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(dest.relative_to(out).as_posix().encode())
                return dest.stat().st_size

            with mock.patch.object(CORPUS_PIPELINE, "encode", side_effect=fake_encode):
                self.assertEqual(run_corpus_pipeline(work, out, "browse"), 0)
                self.assertEqual(set(json.loads((out / "manifest.json").read_text())["tiers"]), {"browse", "screen"})

                screen_plate.write_bytes(b"mutated screen plate")
                self.assertEqual(run_corpus_pipeline(work, out, "browse"), 0)
                self.assertEqual(set(json.loads((out / "manifest.json").read_text())["tiers"]), {"browse"})

    def test_limited_smoke_build_isolated_from_canonical_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work, canonical = corpus_fixture(root)
            second_raw = work / "raw/IMG_1571.png"
            second_mask = work / "vision/mask/IMG_1571.png"
            second_pose = work / "vision/pose/IMG_1571.json"
            CORPUS_PIPELINE.Image.new("RGB", (4, 3), "black").save(second_raw, "PNG")
            CORPUS_PIPELINE.Image.new("L", (4, 3), 0).save(second_mask, "PNG")
            second_pose.write_text("{}")
            canonical.mkdir(parents=True)
            sentinel = canonical / "manifest.json"
            sentinel.write_bytes(b"canonical corpus bytes")

            def fake_encode(_src, dest, *_args, **_kwargs):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(dest.as_posix().encode())
                return dest.stat().st_size

            argv = ["4_corpus.py", "--work", str(work), "--limit", "1", "--skip-room"]
            with (
                mock.patch.object(CORPUS_PIPELINE, "OUT", canonical),
                mock.patch.object(CORPUS_PIPELINE, "encode", side_effect=fake_encode),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(CORPUS_PIPELINE.main(), 0)
            self.assertEqual(sentinel.read_bytes(), b"canonical corpus bytes")
            smoke = work / "corpus-smoke-1"
            self.assertEqual(set(json.loads((smoke / "manifest.json").read_text())["tiers"]), {"browse", "screen"})

            extra = smoke / "plates/browse/old-extra.webp"
            extra.write_bytes(b"stale smoke output")
            with (
                mock.patch.object(CORPUS_PIPELINE, "OUT", canonical),
                mock.patch.object(CORPUS_PIPELINE, "encode", side_effect=fake_encode),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(CORPUS_PIPELINE.main(), 0)
            self.assertFalse(extra.exists())
            self.assertEqual(sentinel.read_bytes(), b"canonical corpus bytes")

            external = root / "external-plates"
            (external / "browse").mkdir(parents=True)
            outside_sentinel = external / "browse/DO_NOT_DELETE"
            outside_sentinel.write_bytes(b"outside smoke custody")
            shutil.rmtree(smoke / "plates")
            (smoke / "plates").symlink_to(external, target_is_directory=True)
            with (
                mock.patch.object(CORPUS_PIPELINE, "OUT", canonical),
                mock.patch.object(CORPUS_PIPELINE, "encode", side_effect=fake_encode),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(CORPUS_PIPELINE.main(), 0)
            self.assertEqual(outside_sentinel.read_bytes(), b"outside smoke custody")

            explicit = [*argv, "--out", str(canonical)]
            encoder = mock.Mock(side_effect=AssertionError("canonical smoke target encoded"))
            with (
                mock.patch.object(CORPUS_PIPELINE, "OUT", canonical),
                mock.patch.object(CORPUS_PIPELINE, "encode", encoder),
                mock.patch.object(sys, "argv", explicit),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(CORPUS_PIPELINE.main(), 1)
            self.assertFalse(encoder.called)
            self.assertEqual(sentinel.read_bytes(), b"canonical corpus bytes")

    def test_interrupted_tier_rebuild_cannot_retain_its_old_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work, out = corpus_fixture(root)
            public = out / "manifest.json"
            public.parent.mkdir(parents=True)
            public_bytes = (json.dumps(corpus_public_manifest(work), indent=1) + "\n").encode()
            public.write_bytes(public_bytes)
            receipt = out / "tier-receipts/film.json"
            local = out / "manifest.local.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text('{"schema":"danse.corpus.tier-receipt.v2"}')
            local.write_text('{"schema":"danse.corpus.local.v1"}')

            with mock.patch.object(CORPUS_PIPELINE, "encode", side_effect=RuntimeError("interrupted")):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    run_corpus_pipeline(work, out, "film")
            self.assertEqual(public.read_bytes(), public_bytes)
            self.assertFalse(receipt.exists())
            self.assertFalse(local.exists())

    def test_local_tier_build_preserves_the_compatible_public_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work, out = corpus_fixture(root)
            public = out / "manifest.json"
            public.parent.mkdir(parents=True)
            public_bytes = (json.dumps(corpus_public_manifest(work), indent=1) + "\n").encode()
            public.write_bytes(public_bytes)

            def fake_encode(_src, dest, *_args, **_kwargs):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(dest.relative_to(out).as_posix().encode())
                return dest.stat().st_size

            with mock.patch.object(CORPUS_PIPELINE, "encode", side_effect=fake_encode):
                self.assertEqual(run_corpus_pipeline(work, out, "film"), 0)

            self.assertEqual(public.read_bytes(), public_bytes)
            local = json.loads((out / "manifest.local.json").read_text())
            self.assertEqual(set(local["tiers"]), {"film"})
            receipt = json.loads((out / "tier-receipts/film.json").read_text())
            self.assertEqual(receipt["schema"], "danse.corpus.tier-receipt.v2")
            self.assertEqual(len(receipt["output_sha256"]), 64)
            self.assertIn("tier-receipts/film.json", (ROOT / "corpus/.gitignore").read_text().splitlines())

    def test_local_tier_mismatch_preserves_prior_authorization_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work, out = corpus_fixture(root)
            public = out / "manifest.json"
            incompatible = corpus_public_manifest(work)
            incompatible["frames"][0]["source"] = "different-source.png"
            public.parent.mkdir(parents=True)
            public_bytes = (json.dumps(incompatible, indent=1) + "\n").encode()
            public.write_bytes(public_bytes)
            receipt = out / "tier-receipts/film.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_bytes(b"prior film receipt")
            local = out / "manifest.local.json"
            local.write_bytes(b"prior local manifest")
            encoder = mock.Mock(side_effect=AssertionError("incompatible build encoded output"))

            with mock.patch.object(CORPUS_PIPELINE, "encode", encoder):
                self.assertEqual(run_corpus_pipeline(work, out, "film"), 1)

            self.assertFalse(encoder.called)
            self.assertEqual(public.read_bytes(), public_bytes)
            self.assertEqual(receipt.read_bytes(), b"prior film receipt")
            self.assertEqual(local.read_bytes(), b"prior local manifest")

    def test_unregistered_recording_cannot_become_room_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "unregistered.mov"
            candidate.write_bytes(b"private recording bytes")
            self.assertEqual(RESOLVE.room_content_matches(candidate, None), (False, None))
            expected = RESOLVE.sha256_file(candidate)
            self.assertEqual(RESOLVE.room_content_matches(candidate, expected), (True, expected))
            self.assertEqual(RESOLVE.room_content_matches(candidate, "0" * 64), (False, expected))

    def test_pipeline_inputs_fail_closed_before_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "raw").mkdir()
            (work / "vision/mask").mkdir(parents=True)
            (work / "vision/pose").mkdir(parents=True)
            complete_raw = work / "raw/IMG_1570.JPG"
            incomplete_raw = work / "raw/IMG_1571.JPG"
            complete_raw.write_bytes(b"raw one")
            incomplete_raw.write_bytes(b"raw two")
            (work / "vision/mask/IMG_1570.png").write_bytes(b"mask")
            (work / "vision/pose/IMG_1570.json").write_text("{}")
            complete, incomplete = CORPUS_CONTRACT.frame_inventory(work)
            self.assertEqual([row[0] for row in complete], ["IMG_1570"])
            self.assertEqual([row[0].name for row in incomplete], ["IMG_1571.JPG"])

            marker = work / "vision/.incomplete"
            marker.write_text("danse.vision.incomplete\n")
            marked_complete, marked_incomplete = CORPUS_CONTRACT.frame_inventory(work)
            self.assertEqual(marked_complete, [])
            self.assertEqual([row[0].name for row in marked_incomplete], ["IMG_1570.JPG", "IMG_1571.JPG"])
            self.assertTrue(all(marker in missing for _, missing in marked_incomplete))
            marker.unlink()

            absent = work / "missing.png"
            self.assertEqual(CORPUS_CONTRACT.missing_measurement_inputs([complete_raw, absent]), [absent])
            self.assertIsNone(CORPUS_CONTRACT.block_shape_error(1024, 768, 16))
            self.assertIn("evenly divide", CORPUS_CONTRACT.block_shape_error(1024, 768, 30))

            readme = (ROOT / "README.md").read_text()
            self.assertIn("../reference/T-2017-full.png", readme)
            self.assertNotIn(".work/reference/T-2017-full.png", readme)

    @unittest.skipUnless(
        sys.platform == "darwin" and (ROOT / "pipeline/1_vision/danse-vision").is_file(),
        "requires the locally built macOS Vision extractor",
    )
    def test_failed_vision_rerun_cannot_retain_any_prior_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            out = root / "vision"
            raw.mkdir()
            for frame_id in ("IMG_1570", "IMG_1571"):
                (raw / f"{frame_id}.jpg").write_bytes(b"not an image")
                pose = out / "pose" / f"{frame_id}.json"
                mask = out / "mask" / f"{frame_id}.png"
                pose.parent.mkdir(parents=True, exist_ok=True)
                mask.parent.mkdir(parents=True, exist_ok=True)
                pose.write_text('{"stale":true}')
                mask.write_bytes(b"stale mask")
            unrelated_pose = out / "pose/NOT_A_DANSE_ARTIFACT.txt"
            unrelated_mask = out / "mask/NOT_A_DANSE_ARTIFACT.txt"
            unrelated_pose.write_text("preserve me")
            unrelated_mask.write_text("preserve me")
            (out / "vision.json").write_text('{"stale":true}')

            done = subprocess.run(
                [str(ROOT / "pipeline/1_vision/danse-vision"), str(raw), str(out)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(done.returncode, 1, done.stderr)
            self.assertFalse((out / "vision.json").exists())
            self.assertTrue((out / ".incomplete").is_file())
            self.assertEqual(unrelated_pose.read_text(), "preserve me")
            self.assertEqual(unrelated_mask.read_text(), "preserve me")
            self.assertFalse(any((out / "pose" / f"{frame_id}.json").exists() for frame_id in ("IMG_1570", "IMG_1571")))
            self.assertFalse(any((out / "mask" / f"{frame_id}.png").exists() for frame_id in ("IMG_1570", "IMG_1571")))

    def test_impractical_passage_offsets_are_rejected_without_walking(self) -> None:
        script = """
          import { readFileSync } from 'node:fs';
          import { passageAt } from './engine/program.js';
          const program = JSON.parse(readFileSync('./render/program.json', 'utf8'));
          let rejected = false;
          try { passageAt(program, 1, 1000000000000); } catch (error) { rejected = error instanceof RangeError; }
          console.log(JSON.stringify({rejected}));
        """
        done = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"rejected": True})

    def test_registered_room_requires_content_identity_not_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "registered.MOV"
            candidate.write_bytes(b"confirmed recording")
            expected = RESOLVE.sha256_file(candidate)
            self.assertEqual(RESOLVE.room_content_matches(candidate, expected), (True, expected))
            candidate.write_bytes(b"different recording under the same name")
            matched, actual = RESOLVE.room_content_matches(candidate, expected)
            self.assertFalse(matched)
            self.assertNotEqual(actual, expected)

    def test_peak_normalised_grain_restores_original_level(self) -> None:
        self.assertAlmostEqual(SCORE.original_level_gain(0.5, 0.125), 0.25)

    def test_span_queries_are_metadata_only(self) -> None:
        payload = {
            **SPAN,
            "seed": 20170620,
            "passage": 0,
            "passageSeed": SPAN["seed"],
            "origin": "IMG_1594",
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        DELIVER._capture_span_items.cache_clear()
        with mock.patch.object(DELIVER, "sh", return_value=completed) as run:
            span = DELIVER.query_capture_span("passage", start=120.0)
            span["t0"] = 999
            again = DELIVER.query_capture_span("passage", start=120.0)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--rate") + 1], "0")
        self.assertEqual(span["origin"], "IMG_1594")
        self.assertEqual(again["t0"], 0.0)
        self.assertEqual(run.call_count, 1)
        DELIVER._capture_span_items.cache_clear()

    def test_score_forwards_absolute_start_to_control(self) -> None:
        payload = {"capture": "passage", "t0": 120.0, "t1": 432.54, "duration": 312.54}
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with mock.patch.object(SCORE.subprocess, "run", return_value=completed) as run:
            self.assertEqual(SCORE.control_track("passage", 123, 30, 120.0, stream=7), payload)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--from") + 1], "120.0")
        self.assertEqual(command[command.index("--seed") + 1], "123")
        self.assertEqual(command[command.index("--stream") + 1], "7")

    def test_score_rebases_absolute_control_times_into_the_capture(self) -> None:
        self.assertAlmostEqual(SCORE.local_time({"t0": 312.54}, 313.79), 1.25)

    def test_missing_command_is_a_controlled_subprocess_failure(self) -> None:
        with mock.patch.object(DELIVER.subprocess, "run", side_effect=FileNotFoundError("missing")):
            done = DELIVER.sh(["absent-command"])
        self.assertEqual(done.returncode, 127)
        self.assertIn("missing", done.stderr)

    def test_capture_roots_do_not_mix_start_offsets(self) -> None:
        root = Path("/render")
        first = DELIVER.capture_root(root, SPAN, 0.0)
        later = DELIVER.capture_root(root, {**SPAN, "seed": 7}, 120.25)
        self.assertNotEqual(first, later)
        self.assertEqual(first.parent, root)
        self.assertEqual(later.parent, root)

    def test_only_text_never_invokes_picture_or_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "render"
            argv = ["deliver.py", "--only", "text", "--out", str(out)]
            forbidden = mock.Mock(side_effect=AssertionError("render dependency invoked"))
            nonmedia_probe = mock.Mock(side_effect=AssertionError("text passed to ffprobe"))
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(DELIVER, "query_capture_span", forbidden),
                mock.patch.object(DELIVER, "passage_picture", forbidden),
                mock.patch.object(DELIVER, "passage_sound", forbidden),
                mock.patch.object(DELIVER, "probe", nonmedia_probe),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(DELIVER.main(), 0)
            self.assertFalse(forbidden.called)
            self.assertFalse(nonmedia_probe.called)
            self.assertTrue((out / "package/text/synopsis_short.txt").is_file())
            attest = yaml.safe_load((out / "package/attest.yaml").read_text())
            self.assertTrue(attest)
            self.assertTrue(all(value is None for value in attest.values()))
            items = json.loads((out / "package/manifest.json").read_text())["items"]
            self.assertTrue(items)
            self.assertTrue(all(set(item) == {"name", "bytes", "sha256"} for item in items))

    def test_only_origin_copies_source_bytes_under_stills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "IMG_1594.JPG"
            source.write_bytes(b"camera-original")
            source_digest = DELIVER.digest(source)
            out = root / "render"
            argv = ["deliver.py", "--only", "origin", "--out", str(out)]
            forbidden = mock.Mock(side_effect=AssertionError("render dependency invoked"))
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(DELIVER, "registered_origin", return_value=source),
                mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=source_digest),
                mock.patch.object(DELIVER, "query_capture_span", forbidden),
                mock.patch.object(DELIVER, "passage_picture", forbidden),
                mock.patch.object(DELIVER, "passage_sound", forbidden),
                mock.patch.object(DELIVER, "probe", return_value=None),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(DELIVER.main(), 0)
            copied = out / "package/stills/origin-2017.jpg"
            self.assertEqual(copied.read_bytes(), source.read_bytes())
            self.assertFalse(forbidden.called)
            register = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
            origin_spec = dict(register["package"]["origin_still"], source_sha256=source_digest)
            report = CHECK.Report()
            CHECK.check_origin_still(origin_spec, out / "package", report)
            self.assertEqual(report.failures, 0)
            wrong = CHECK.Report()
            CHECK.check_origin_still({**origin_spec, "source_sha256": "0" * 64}, out / "package", wrong)
            self.assertEqual(wrong.failures, 1)

            source.write_bytes(b"different camera bytes")
            with (
                mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=source_digest),
                self.assertRaises(SystemExit),
            ):
                DELIVER.deliver_origin(source, True)

    def test_origin_source_is_owned_by_the_submission_register(self) -> None:
        self.assertEqual(
            DELIVER.registered_origin_source_sha256(),
            "72b4f8f1c553c40bd4ec2de9956d547493ed17aaa5eabe172260c2156c8fde42",
        )
        with mock.patch.dict(DELIVER.os.environ, {}, clear=True):
            self.assertEqual(DELIVER.hydrated_work_root(), DELIVER.RAW.parent)
            self.assertEqual(DELIVER.registered_origin(), DELIVER.RAW / "IMG_1594.JPG")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            DELIVER.os.environ, {"DANSE_WORK": tmp}, clear=True
        ):
            self.assertEqual(DELIVER.hydrated_work_root(), Path(tmp))
            self.assertEqual(DELIVER.registered_origin(), Path(tmp) / "raw/IMG_1594.JPG")

    def test_reused_origin_repairs_missing_or_stale_manifest_receipt(self) -> None:
        for prior_receipt in ("missing", "stale"):
            with self.subTest(prior_receipt=prior_receipt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                out = root / "render"
                package = out / "package"
                origin_copy = package / "stills/origin-2017.jpg"
                origin_copy.parent.mkdir(parents=True)
                origin_copy.write_bytes(b"preserved camera original")
                expected = DELIVER.digest(origin_copy)
                if prior_receipt == "stale":
                    (package / "manifest.json").write_text(
                        json.dumps(
                            {
                                "items": [
                                    {
                                        "name": "stills/origin-2017.jpg",
                                        "source": "wrong.jpg",
                                        "copy_mode": "reencoded",
                                        "sha256": "0" * 64,
                                        "source_sha256": "0" * 64,
                                    }
                                ]
                            }
                        )
                    )
                missing_raw = root / "unmounted/IMG_1594.JPG"
                forbidden = mock.Mock(side_effect=AssertionError("passage dependency invoked"))
                with (
                    mock.patch.object(sys, "argv", ["deliver.py", "--only", "origin", "--out", str(out)]),
                    mock.patch.object(DELIVER, "registered_origin", return_value=missing_raw),
                    mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=expected),
                    mock.patch.object(DELIVER, "query_capture_span", forbidden),
                    mock.patch.object(DELIVER, "passage_picture", forbidden),
                    mock.patch.object(DELIVER, "passage_sound", forbidden),
                    mock.patch.object(DELIVER, "probe", return_value=None),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(DELIVER.main(), 0)
                self.assertFalse(forbidden.called)
                self.assertEqual(origin_copy.read_bytes(), b"preserved camera original")
                item = next(
                    entry
                    for entry in json.loads((package / "manifest.json").read_text())["items"]
                    if entry["name"] == "stills/origin-2017.jpg"
                )
                self.assertEqual(
                    item,
                    {
                        "name": "stills/origin-2017.jpg",
                        "bytes": len(b"preserved camera original"),
                        "sha256": expected,
                        "source": "IMG_1594.JPG",
                        "source_sha256": expected,
                        "copy_mode": "byte-identical",
                    },
                )

    def test_forged_origin_receipt_cannot_approve_tampered_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            origin_copy = package / "stills/origin-2017.jpg"
            origin_copy.parent.mkdir(parents=True)
            origin_copy.write_bytes(b"tampered bytes")
            tampered = CHECK.sha256(origin_copy)
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "name": "stills/origin-2017.jpg",
                                "source": "IMG_1594.JPG",
                                "copy_mode": "byte-identical",
                                "sha256": tampered,
                                "source_sha256": tampered,
                            }
                        ]
                    }
                )
            )
            spec = {
                "filename": "origin-2017.jpg",
                "source_filename": "IMG_1594.JPG",
                "source_sha256": hashlib.sha256(b"camera original").hexdigest(),
                "copy_mode": "byte-identical",
            }
            report = CHECK.Report()
            CHECK.check_origin_still(spec, package, report)
            status = next(
                row[2]
                for row in report.rows
                if row[1] == "origin is byte-identical to its registered source"
            )
            self.assertEqual(status, CHECK.FAIL)

    def test_origin_registration_rejects_missing_or_malformed_sha256(self) -> None:
        cases = {
            "missing": {},
            "null": {"source_sha256": None},
            "short": {"source_sha256": "0" * 63},
            "non-hex": {"source_sha256": "g" * 64},
            "non-string": {"source_sha256": 7},
        }
        for label, digest_field in cases.items():
            register = {
                "package": {
                    "origin_still": {
                        "source_filename": "IMG_1594.JPG",
                        "copy_mode": "byte-identical",
                        **digest_field,
                    }
                }
            }
            with mock.patch.object(DELIVER.yaml, "safe_load", return_value=register):
                for reader in (DELIVER.registered_origin, DELIVER.registered_origin_source_sha256):
                    with self.subTest(case=label, reader=reader.__name__), self.assertRaises(SystemExit):
                        reader()

    def test_attestation_template_survives_unowned_manual_requirement(self) -> None:
        register = {
            "requirements": [
                {"id": "later", "rule": "declare ownership", "check": "manual"},
                {"rule": "has no identifier", "check": "manual"},
                {"id": "without-rule", "check": "manual"},
            ]
        }
        with mock.patch.object(DELIVER.yaml, "safe_load", return_value=register):
            text = DELIVER.attestation_template()
        self.assertIn("[UNOWNED]", text)
        self.assertIn("later: null", text)
        self.assertIn("without-rule: null", text)
        self.assertNotIn("has no identifier", text)
        self.assertIn("dancer-release-and-credit: null", text)
        self.assertIn("pictured-objects-reviewed: null", text)
        self.assertIn("music-cleared: null", text)
        self.assertIn("submission-copy-approved: null", text)
        self.assertIn("archive-library-choice: null", text)
        self.assertIn('choose one of ["include", "opt-out"]', text)

    def test_attestation_template_rejects_duplicate_rights_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            duplicate = Path(temporary) / "register.json"
            duplicate.write_text(
                '{"human_gates":[],"human_gates":[{"attestation":'
                '{"key":"injected-private-gate","kind":"boolean","values":[true]}}]}'
            )
            with (
                mock.patch.object(DELIVER, "RIGHTS_REGISTER", duplicate),
                mock.patch.object(DELIVER.yaml, "safe_load", return_value={}),
                self.assertRaisesRegex(SystemExit, "invalid or unreadable JSON"),
            ):
                DELIVER.attestation_template()

    def test_text_preflight_is_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "absent"
            argv = ["deliver.py", "--preflight", "--only", "text", "--out", str(out)]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(DELIVER, "query_capture_span", return_value=SPAN),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(DELIVER.main(), 0)
            self.assertFalse(out.exists())

    def test_atomic_package_publication_host_requirement_fails_closed(self) -> None:
        with (
            mock.patch.object(DELIVER.sys, "platform", "linux"),
            mock.patch.object(DELIVER.ctypes, "CDLL", return_value=SimpleNamespace()),
            self.assertRaisesRegex(OSError, "glibc 2.28.*renameat2"),
        ):
            DELIVER.require_atomic_rename_host()

    def test_atomic_package_publication_probes_the_running_kernel(self) -> None:
        def unsupported_kernel(*_args) -> int:
            DELIVER.ctypes.set_errno(DELIVER.errno.ENOSYS)
            return -1

        with (
            mock.patch.object(DELIVER.sys, "platform", "linux"),
            mock.patch.object(
                DELIVER.ctypes,
                "CDLL",
                return_value=SimpleNamespace(renameat2=unsupported_kernel),
            ),
            self.assertRaisesRegex(OSError, "capability probe failed.*not implemented"),
        ):
            DELIVER.require_atomic_rename_host()

    def test_atomic_package_publication_uses_darwin_at_fdcwd(self) -> None:
        calls = []

        def rename(*args) -> int:
            calls.append(args)
            return 0

        with (
            mock.patch.object(DELIVER.sys, "platform", "darwin"),
            mock.patch.object(
                DELIVER,
                "require_atomic_rename_host",
                return_value=(rename, 4, "macOS renameatx_np"),
            ),
        ):
            DELIVER.atomic_rename_noreplace("source", "destination")
        self.assertEqual(calls, [(-2, b"source", -2, b"destination", 4)])

    def test_atomic_package_publication_checks_target_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(
                    DELIVER,
                    "require_atomic_rename_host",
                    return_value=(object(), 1, "Linux renameat2"),
                ),
                mock.patch.object(
                    DELIVER,
                    "atomic_rename_noreplace",
                    side_effect=OSError(DELIVER.errno.EOPNOTSUPP, "unsupported"),
                ),
                self.assertRaisesRegex(OSError, "filesystem at .*unsupported"),
            ):
                DELIVER.require_atomic_rename_filesystem(root / "package")
            self.assertEqual(list(root.iterdir()), [])

    def test_atomic_package_probe_never_deletes_a_concurrent_winner(self) -> None:
        def inject_winner(
            _source,
            destination,
            *,
            src_dir_fd,
            dst_dir_fd,
        ) -> None:
            self.assertEqual(src_dir_fd, dst_dir_fd)
            DELIVER.write_new_regular_bytes_at(
                dst_dir_fd,
                destination,
                b"winner",
                mode=0o600,
            )
            raise OSError(DELIVER.errno.EEXIST, "concurrent winner")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(
                    DELIVER,
                    "require_atomic_rename_host",
                    return_value=(object(), 1, "Linux renameat2"),
                ),
                mock.patch.object(
                    DELIVER,
                    "atomic_rename_noreplace",
                    side_effect=inject_winner,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "cleanup did not complete safely.*unowned probe entry retained",
                ),
            ):
                DELIVER.require_atomic_rename_filesystem(root / "package")
            winners = list(root.iterdir())
            self.assertEqual(len(winners), 1)
            winner = winners[0]
            self.assertEqual(winner.read_bytes(), b"winner")
            winner.unlink()

    def test_atomic_package_probe_never_removes_an_exclusive_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / f".danse-atomic-source-{os.getpid()}-fixed"
            marker.write_bytes(b"keep")
            with (
                mock.patch.object(
                    DELIVER,
                    "require_atomic_rename_host",
                    return_value=(object(), 1, "Linux renameat2"),
                ),
                mock.patch.object(DELIVER.secrets, "token_hex", return_value="fixed"),
                self.assertRaisesRegex(
                    OSError,
                    "cleanup did not complete safely.*unowned probe entry retained",
                ),
            ):
                DELIVER.require_atomic_rename_filesystem(root / "package")
            self.assertEqual(marker.read_bytes(), b"keep")

    def test_atomic_package_probe_never_adopts_a_replaced_source(self) -> None:
        original = DELIVER.create_atomic_probe_entry_at
        replaced = False

        def replace_after_create(directory_fd, name, payload):
            nonlocal replaced
            descriptor, owned_identity = original(directory_fd, name, payload)
            if not replaced and name.startswith(".danse-atomic-source-"):
                replaced = True
                os.unlink(name, dir_fd=directory_fd)
                foreign_descriptor, _ = original(directory_fd, name, b"foreign")
                os.close(foreign_descriptor)
            return descriptor, owned_identity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(
                    DELIVER,
                    "require_atomic_rename_host",
                    wraps=DELIVER.require_atomic_rename_host,
                ),
                mock.patch.object(
                    DELIVER,
                    "create_atomic_probe_entry_at",
                    side_effect=replace_after_create,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "cleanup did not complete safely.*unowned probe entry retained",
                ),
            ):
                DELIVER.require_atomic_rename_filesystem(root / "package")
            foreign = list(root.iterdir())
            self.assertEqual(len(foreign), 1)
            self.assertEqual(foreign[0].read_bytes(), b"foreign")
            foreign[0].unlink()

    def test_bounded_path_and_descriptor_reads_share_one_stable_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "receipt.json"
            target.write_bytes(b'{"ok":true}\n')
            directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                by_path = DELIVER.bounded_regular_bytes(
                    target,
                    max_bytes=64,
                    description="receipt",
                )
                by_descriptor = DELIVER.bounded_regular_bytes_at(
                    directory_fd,
                    target.name,
                    max_bytes=64,
                    description="receipt",
                )
            finally:
                os.close(directory_fd)
            self.assertEqual(by_path, by_descriptor)

    def test_phase_predecessors_reuse_the_receipt_row_schemas(self) -> None:
        schema = json.loads((ROOT / "submission/receipt.schema.json").read_text())
        definitions = schema["$defs"]
        self.assertNotIn("packagePriorReceipt", definitions)
        self.assertNotIn("uploadedPriorReceipt", definitions)
        self.assertEqual(
            definitions["uploadedReceipt"]["properties"]["prior_receipt"]["$ref"],
            "#/$defs/packageReceiptRow",
        )
        self.assertEqual(
            definitions["submittedReceipt"]["properties"]["prior_receipt"]["$ref"],
            "#/$defs/uploadedReceiptRow",
        )

    def test_preflight_reports_failed_capture_query_without_aborting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "absent"
            argv = ["deliver.py", "--preflight", "--only", "master", "--out", str(out)]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(DELIVER, "query_capture_span", side_effect=SystemExit("node query failed")),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(DELIVER.main(), 1)
            self.assertIn("node query failed", output.getvalue())
            self.assertIn("NOT READY", output.getvalue())
            self.assertFalse(out.exists())

    def test_preflight_reuses_a_provenanced_cached_score(self) -> None:
        program = json.loads((ROOT / "render/program.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "package"
            picture = root / "passage-default.mov"
            score = root / "passage-score.wav"

            def fake_probe(path: Path):
                if path == picture:
                    return {"seconds": SPAN["duration"], "fps": 30}
                if path == score:
                    return {"seconds": SPAN["duration"]}
                return None

            external_work = root / "external-work"
            authorization = mock.Mock(return_value=(True, "fixture tier"))
            with (
                mock.patch.object(DELIVER, "probe", side_effect=fake_probe),
                mock.patch.object(DELIVER, "score_provenance", return_value={"sources": ["a", "b"]}),
                mock.patch.object(DELIVER, "authorize_render_tier", authorization),
                mock.patch.object(DELIVER.shutil, "which", side_effect=lambda command: f"/tools/{command}"),
                mock.patch.object(
                    DELIVER.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                mock.patch.object(
                    DELIVER,
                    "repository_state",
                    return_value={
                        "head": SUBMISSION_REPOSITORY_HEAD,
                        "clean": True,
                        "changes": [],
                    },
                ),
                mock.patch.dict(DELIVER.os.environ, {"DANSE_WORK": str(external_work)}, clear=True),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = DELIVER.preflight(program, SPAN, {"master"}, set(), "film", root, package, None)
            self.assertEqual(result, 0)
            authorization.assert_called_once_with(DELIVER.DANSE / "corpus", external_work, "film")
            self.assertNotIn("Python module numpy", output.getvalue())
            self.assertNotIn("grain bank", output.getvalue())

    def test_preflight_rejects_a_picture_with_stale_concat_receipts(self) -> None:
        program = json.loads((ROOT / "render/program.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            picture = root / "passage-default.mov"
            score = root / "passage-score.wav"

            def fake_probe(path: Path):
                if path == picture:
                    return {"seconds": SPAN["duration"], "fps": 30}
                if path == score:
                    return {"seconds": SPAN["duration"]}
                return None

            with (
                mock.patch.object(DELIVER, "probe", side_effect=fake_probe),
                mock.patch.object(DELIVER, "score_provenance", return_value={"sources": ["a", "b"]}),
                mock.patch.object(DELIVER.shutil, "which", side_effect=lambda command: f"/tools/{command}"),
                mock.patch.object(DELIVER.importlib.util, "find_spec", return_value=None),
                mock.patch.object(
                    DELIVER.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 1),
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = DELIVER.preflight(program, SPAN, {"master"}, set(), "film", root, root / "package", None)
            self.assertEqual(result, 1)
            self.assertIn("Playwright", output.getvalue())

    def test_hash_navigation_discards_superseded_program_loads(self) -> None:
        source = (ROOT / "index.html").read_text()
        self.assertIn("const generation = ++navigationGeneration", source)
        self.assertGreaterEqual(source.count("generation !== navigationGeneration"), 2)

    def test_sound_depth_uses_the_renderers_view_space(self) -> None:
        script = """
          import { camera, viewDepth } from './engine/room.js';
          const view = camera(0.8, 0.7, 0.35).view;
          const point = [0.4, -0.2, 0.9];
          console.log(JSON.stringify({world: point[2], viewed: viewDepth(view, point)}));
        """
        done = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        depths = json.loads(done.stdout)
        self.assertNotAlmostEqual(depths["world"], depths["viewed"])
        self.assertIn("viewDepth(view.view, p.position)", (ROOT / "sound/control.mjs").read_text())

    def test_cached_passage_picture_requires_current_concat_receipt(self) -> None:
        program = json.loads((ROOT / "render/program.json").read_text())
        span = {**SPAN, "duration": 300.0, "t0": 0.0}
        info = {"seconds": 300.0, "width": 3840, "height": 2160, "fps": 30}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(DELIVER, "OUT", Path(tmp)):
            dest = Path(tmp) / "passage-default.mov"
            dest.write_bytes(b"cached picture")
            completed = subprocess.CompletedProcess([], 0)
            stale = subprocess.CompletedProcess([], 1)
            with (
                mock.patch.object(DELIVER, "query_capture_span", return_value=span),
                mock.patch.object(DELIVER, "probe_required", return_value=info),
                mock.patch.object(DELIVER.subprocess, "run", side_effect=[stale, completed]) as run,
            ):
                self.assertEqual(DELIVER.passage_picture(program, "film", False), dest)
            self.assertIn("--check-concat", run.call_args_list[0].args[0])
            self.assertIn("--resume", run.call_args_list[1].args[0])

    def test_preflight_reuses_registered_origin_bytes_without_raw_source(self) -> None:
        program = json.loads((ROOT / "render/program.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "package"
            origin_copy = package / "stills/origin-2017.jpg"
            origin_copy.parent.mkdir(parents=True)
            origin_copy.write_bytes(b"preserved origin")
            missing_raw = root / "unmounted/IMG_1594.JPG"
            with (
                mock.patch.object(DELIVER.shutil, "which", return_value="/tools/node"),
                mock.patch.object(
                    DELIVER, "registered_origin_source_sha256", return_value=DELIVER.digest(origin_copy)
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    DELIVER.preflight(
                        program,
                        SPAN,
                        {"origin"},
                        set(),
                        "film",
                        root,
                        package,
                        missing_raw,
                        passage_requested=False,
                    ),
                    0,
                )
                self.assertEqual(
                    DELIVER.preflight(
                        program,
                        SPAN,
                        {"origin"},
                        {"origin"},
                        "film",
                        root,
                        package,
                        missing_raw,
                        passage_requested=False,
                    ),
                    1,
                )

    def test_preflight_reports_unreadable_origin_without_aborting(self) -> None:
        program = json.loads((ROOT / "render/program.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "package"
            source = root / "raw/IMG_1594.JPG"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"registered origin bytes")
            with (
                mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value="f" * 64),
                mock.patch.object(DELIVER, "digest", side_effect=PermissionError("access denied")),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    DELIVER.preflight(
                        program,
                        SPAN,
                        {"origin"},
                        {"origin"},
                        "film",
                        root,
                        package,
                        source,
                        passage_requested=False,
                    ),
                    1,
                )
            self.assertIn("registered origin photograph identity", output.getvalue())
            self.assertIn("source bytes are unreadable (access denied)", output.getvalue())
            self.assertIn("NOT READY", output.getvalue())

    def test_symlinked_origin_cannot_be_adopted_or_approved(self) -> None:
        program = json.loads((ROOT / "render/program.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "raw/IMG_1594.JPG"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"registered origin bytes")
            expected = DELIVER.digest(source)
            package = root / "package"
            origin_copy = package / "stills/origin-2017.jpg"
            origin_copy.parent.mkdir(parents=True)
            origin_copy.symlink_to(source)

            for forced in (False, True):
                with (
                    self.subTest(forced=forced),
                    mock.patch.object(DELIVER, "PACKAGE", package),
                    mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=expected),
                    self.assertRaises(SystemExit),
                ):
                    DELIVER.deliver_origin(source, forced)

                with (
                    mock.patch.object(DELIVER.shutil, "which", return_value="/tools/node"),
                    mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=expected),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        DELIVER.preflight(
                            program,
                            SPAN,
                            {"origin"},
                            {"origin"} if forced else set(),
                            "film",
                            root,
                            package,
                            source,
                            passage_requested=False,
                        ),
                        1,
                    )

            spec = {
                "filename": "origin-2017.jpg",
                "source_filename": source.name,
                "source_sha256": expected,
                "copy_mode": "byte-identical",
            }
            report = CHECK.Report()
            CHECK.check_origin_still(spec, package, report)
            self.assertEqual(report.failures, 1)

            origin_copy.unlink()
            origin_copy.symlink_to(root / "missing-origin.jpg")
            with (
                mock.patch.object(DELIVER.shutil, "which", return_value="/tools/node"),
                mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=expected),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    DELIVER.preflight(
                        program,
                        SPAN,
                        {"origin"},
                        {"origin"},
                        "film",
                        root,
                        package,
                        source,
                        passage_requested=False,
                    ),
                    1,
                )
            dangling_report = CHECK.Report()
            CHECK.check_origin_still(spec, package, dangling_report)
            self.assertEqual(dangling_report.failures, 1)

            origin_copy.unlink()
            origin_copy.parent.rmdir()
            external_stills = root / "external-stills"
            external_stills.mkdir()
            origin_copy.parent.symlink_to(external_stills, target_is_directory=True)
            (external_stills / origin_copy.name).write_bytes(source.read_bytes())
            with (
                mock.patch.object(DELIVER, "PACKAGE", package),
                mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=expected),
                self.assertRaises(SystemExit),
            ):
                DELIVER.deliver_origin(source, False)
            with (
                mock.patch.object(DELIVER.shutil, "which", return_value="/tools/node"),
                mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=expected),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    DELIVER.preflight(
                        program,
                        SPAN,
                        {"origin"},
                        set(),
                        "film",
                        root,
                        package,
                        source,
                        passage_requested=False,
                    ),
                    1,
                )
            linked_parent_report = CHECK.Report()
            CHECK.check_origin_still(spec, package, linked_parent_report)
            self.assertEqual(linked_parent_report.failures, 1)

    def test_symlinked_package_root_cannot_receive_origin(self) -> None:
        program = json.loads((ROOT / "render/program.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "raw/IMG_1594.JPG"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"registered origin bytes")
            expected = DELIVER.digest(source)
            external_package = root / "external-package"
            external_package.mkdir()
            package = root / "package"
            package.symlink_to(external_package, target_is_directory=True)

            with (
                mock.patch.object(DELIVER, "PACKAGE", package),
                mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=expected),
                self.assertRaises(SystemExit),
            ):
                DELIVER.deliver_origin(source, True)
            self.assertFalse((external_package / "stills/origin-2017.jpg").exists())

            external_origin = external_package / "stills/origin-2017.jpg"
            external_origin.parent.mkdir()
            external_origin.write_bytes(source.read_bytes())
            with (
                mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=expected),
                mock.patch.object(DELIVER, "digest", side_effect=AssertionError("invalid package was read")),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    DELIVER.preflight(
                        program,
                        SPAN,
                        {"origin"},
                        set(),
                        "film",
                        root,
                        package,
                        source,
                        passage_requested=False,
                    ),
                    1,
                )
            self.assertIn("staged origin is a regular file", output.getvalue())
            self.assertIn("NOT READY", output.getvalue())

            spec = {
                "filename": "origin-2017.jpg",
                "source_filename": source.name,
                "source_sha256": expected,
                "copy_mode": "byte-identical",
            }
            report = CHECK.Report()
            CHECK.check_origin_still(spec, package, report)
            self.assertEqual(report.failures, 1)

            external_main = root / "external-main-package"
            external_main.mkdir()
            package_main = root / "main-package"
            package_main.symlink_to(external_main, target_is_directory=True)
            argv = ["deliver.py", "--only", "text", "--out", str(root / "render"), "--package", str(package_main)]
            with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
                DELIVER.main()
            self.assertEqual(list(external_main.iterdir()), [])

    def test_non_directory_package_slots_fail_closed(self) -> None:
        program = json.loads((ROOT / "render/program.json").read_text())
        for blocked in ("package", "stills"):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "raw/IMG_1594.JPG"
                source.parent.mkdir(parents=True)
                source.write_bytes(b"registered origin bytes")
                expected = DELIVER.digest(source)
                package = root / "package"
                if blocked == "package":
                    package.write_bytes(b"not a package directory")
                else:
                    package.mkdir()
                    (package / "stills").write_bytes(b"not a stills directory")

                with (
                    mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=expected),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(
                        DELIVER.preflight(
                            program,
                            SPAN,
                            {"origin"},
                            {"origin"},
                            "film",
                            root,
                            package,
                            source,
                            passage_requested=False,
                        ),
                        1,
                    )
                self.assertIn("NOT READY", output.getvalue())

                with (
                    mock.patch.object(DELIVER, "PACKAGE", package),
                    mock.patch.object(DELIVER, "registered_origin_source_sha256", return_value=expected),
                    self.assertRaises(SystemExit),
                ):
                    DELIVER.deliver_origin(source, True)

    def test_text_only_preserves_existing_sound_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = out / "package"
            package.mkdir(parents=True)
            old_sound = {"bank_fingerprint": "old-bank", "sources": ["IMG_0226.MOV", "IMG_0227.MOV"]}
            evidence = package / DELIVER.SCORE_MOTION_EVIDENCE_ITEM
            evidence.parent.mkdir(parents=True)
            evidence.write_text('{"schema":"danse.evidence.score-to-motion.production.v1"}\n')
            evidence_reference = {
                "path": DELIVER.SCORE_MOTION_EVIDENCE_ITEM,
                "sha256": DELIVER.digest(evidence),
            }
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "passage_seed": DELIVER.hexseed(SPAN["seed"]),
                        "passage": SPAN["passage"],
                        "t0": SPAN["t0"],
                        "t1": SPAN["t1"],
                        "duration": SPAN["duration"],
                        "sound": old_sound,
                        "score_motion_evidence": evidence_reference,
                        "items": [
                            {
                                "name": DELIVER.SCORE_MOTION_EVIDENCE_ITEM,
                                "bytes": evidence.stat().st_size,
                                "sha256": evidence_reference["sha256"],
                            }
                        ],
                    }
                )
            )
            current_bank = root / "bank.json"
            current_bank.write_text(
                json.dumps(
                    {
                        "fingerprint": "new-bank",
                        "sources": [{"name": "IMG_0226.MOV"}, {"name": "IMG_0227.MOV"}],
                    }
                )
            )
            with (
                mock.patch.object(sys, "argv", ["deliver.py", "--only", "text", "--out", str(out)]),
                mock.patch.object(DELIVER, "BANK", current_bank),
                mock.patch.object(
                    DELIVER,
                    "query_capture_span",
                    side_effect=AssertionError("text-only update queried passage metadata"),
                ),
                mock.patch.object(DELIVER, "probe", return_value=None),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(DELIVER.main(), 0)
            manifest = json.loads((package / "manifest.json").read_text())
            self.assertEqual(manifest["sound"], old_sound)
            self.assertEqual(manifest["score_motion_evidence"], evidence_reference)

    def test_text_only_rebuild_uses_the_retained_passage_capture_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            expected_receipt = fixture.score_receipt.read_bytes()
            retained_manifest = dict(fixture.manifest)

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "deliver.py",
                        "--only",
                        "text",
                        "--out",
                        str(out),
                        "--package",
                        str(package),
                    ],
                ),
                mock.patch.object(
                    DELIVER,
                    "query_capture_span",
                    side_effect=AssertionError("text-only update resolved a new passage"),
                ),
                mock.patch.object(
                    DELIVER,
                    "require_clean_repository",
                    return_value={
                        "head": SUBMISSION_REPOSITORY_HEAD,
                        "clean": True,
                        "changes": [],
                    },
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(DELIVER.main(), 0)

            manifest = json.loads((package / "manifest.json").read_text())
            production_path = package / manifest["production"]["path"]
            production = json.loads(production_path.read_text())
            self.assertEqual(manifest["repository_head"], SUBMISSION_REPOSITORY_HEAD)
            self.assertEqual(production["repository_head"], SUBMISSION_REPOSITORY_HEAD)
            expected_passage = {
                key: retained_manifest[key]
                for key in (
                    "seed",
                    "passage_seed",
                    "passage",
                    "start",
                    "t0",
                    "t1",
                    "duration",
                    "corpus_tier",
                )
            }
            self.assertEqual(production["passage"], expected_passage)
            copied = package / production["producers"][0]["receipt"]["path"]
            self.assertEqual(copied.read_bytes(), expected_receipt)

    def test_failed_producer_rebuild_preserves_the_prior_package_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            previous = dict(fixture.manifest)
            current = copy.deepcopy(previous)
            current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
            receipt_root = package / DELIVER.PRODUCER_RECEIPTS
            before_receipts = {
                path.name: path.read_bytes()
                for path in receipt_root.iterdir()
            }
            before_production = fixture.production_path.read_bytes()
            fixture.score_receipt.unlink()

            with self.assertRaisesRegex(SystemExit, "producer receipt is missing"):
                DELIVER.write_production_receipt(
                    package,
                    fixture.render_root,
                    current,
                    previous,
                )

            self.assertEqual(
                {path.name: path.read_bytes() for path in receipt_root.iterdir()},
                before_receipts,
            )
            self.assertEqual(fixture.production_path.read_bytes(), before_production)
            self.assertEqual(
                sorted(
                    path.name
                    for path in fixture.production_path.parent.iterdir()
                    if path.name.startswith(".production-receipts-")
                ),
                [],
            )

    def test_interrupted_producer_publish_rolls_back_the_prior_package_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            previous = dict(fixture.manifest)
            current = copy.deepcopy(previous)
            current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
            receipt_root = package / DELIVER.PRODUCER_RECEIPTS
            before_receipts = {
                path.name: path.read_bytes()
                for path in receipt_root.iterdir()
            }
            before_production = fixture.production_path.read_bytes()
            rename = DELIVER.atomic_rename_noreplace

            def interrupt_new_production(source, destination, **kwargs):
                if Path(source).name == "production.new.json":
                    raise OSError("injected production-receipt publication failure")
                return rename(source, destination, **kwargs)

            with (
                mock.patch.object(
                    DELIVER,
                    "atomic_rename_noreplace",
                    side_effect=interrupt_new_production,
                ),
                self.assertRaisesRegex(SystemExit, "replacement failed"),
            ):
                DELIVER.write_production_receipt(
                    package,
                    fixture.render_root,
                    current,
                    previous,
                )

            self.assertEqual(
                {path.name: path.read_bytes() for path in receipt_root.iterdir()},
                before_receipts,
            )
            self.assertEqual(fixture.production_path.read_bytes(), before_production)

            def interrupt_with_keyboard(source, destination, **kwargs):
                if Path(source).name == "production.new.json":
                    raise KeyboardInterrupt
                return rename(source, destination, **kwargs)

            with (
                mock.patch.object(
                    DELIVER,
                    "atomic_rename_noreplace",
                    side_effect=interrupt_with_keyboard,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                DELIVER.write_production_receipt(
                    package,
                    fixture.render_root,
                    current,
                    previous,
                )

            self.assertEqual(
                {path.name: path.read_bytes() for path in receipt_root.iterdir()},
                before_receipts,
            )
            self.assertEqual(fixture.production_path.read_bytes(), before_production)

    def test_manifest_publication_failure_rolls_back_the_entire_receipt_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            manifest_path = package / "manifest.json"
            previous = copy.deepcopy(fixture.manifest)
            current = copy.deepcopy(previous)
            current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
            receipt_root = package / DELIVER.PRODUCER_RECEIPTS
            before_manifest = manifest_path.read_bytes()
            before_production = fixture.production_path.read_bytes()
            before_receipts = {
                path.name: path.read_bytes() for path in receipt_root.iterdir()
            }
            rename = DELIVER.atomic_rename_noreplace

            def interrupt_manifest(source, destination, **kwargs):
                if Path(source).name == "manifest.new.json":
                    raise OSError("injected manifest publication failure")
                return rename(source, destination, **kwargs)

            with (
                mock.patch.object(
                    DELIVER,
                    "atomic_rename_noreplace",
                    side_effect=interrupt_manifest,
                ),
                self.assertRaisesRegex(SystemExit, "replacement failed"),
            ):
                DELIVER.write_production_receipt(
                    package,
                    fixture.render_root,
                    current,
                    previous,
                    publish_manifest=True,
                    previous_manifest_sha256=DELIVER.digest(manifest_path),
                )

            self.assertEqual(manifest_path.read_bytes(), before_manifest)
            self.assertEqual(fixture.production_path.read_bytes(), before_production)
            self.assertEqual(
                {path.name: path.read_bytes() for path in receipt_root.iterdir()},
                before_receipts,
            )
            self.assertEqual(
                list(root.glob(".danse-package-transaction-*")),
                [],
            )

    def test_manifest_commit_rejects_a_stale_prior_manifest_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            manifest_path = package / "manifest.json"
            previous = copy.deepcopy(fixture.manifest)
            current = copy.deepcopy(previous)
            current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
            expected_manifest_sha = DELIVER.digest(manifest_path)
            before_production = fixture.production_path.read_bytes()
            manifest_path.write_text(manifest_path.read_text() + "\n")
            changed_manifest = manifest_path.read_bytes()

            with self.assertRaisesRegex(
                SystemExit,
                "manifest changed after it was read",
            ):
                DELIVER.write_production_receipt(
                    package,
                    fixture.render_root,
                    current,
                    previous,
                    publish_manifest=True,
                    previous_manifest_sha256=expected_manifest_sha,
                )

            self.assertEqual(manifest_path.read_bytes(), changed_manifest)
            self.assertEqual(fixture.production_path.read_bytes(), before_production)
            self.assertEqual(
                list(root.glob(".danse-package-transaction-*")),
                [],
            )

    def test_reused_production_is_revalidated_at_the_manifest_commit_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            manifest_path = package / "manifest.json"
            previous = copy.deepcopy(fixture.manifest)
            current = copy.deepcopy(previous)
            before_manifest = manifest_path.read_bytes()
            before_production = fixture.production_path.read_bytes()
            rename = DELIVER.atomic_rename_noreplace
            published_manifest = False

            def mutate_after_manifest_publication(source, destination, **kwargs):
                nonlocal published_manifest
                result = rename(source, destination, **kwargs)
                if Path(source).name == "manifest.new.json":
                    published_manifest = True
                    fixture.production_path.write_bytes(b"concurrent mutation")
                return result

            with (
                mock.patch.object(
                    DELIVER,
                    "atomic_rename_noreplace",
                    side_effect=mutate_after_manifest_publication,
                ),
                self.assertRaisesRegex(SystemExit, "replacement failed"),
            ):
                DELIVER.write_production_receipt(
                    package,
                    fixture.render_root,
                    current,
                    previous,
                    publish_manifest=True,
                    previous_manifest_sha256=DELIVER.digest(manifest_path),
                )

            self.assertTrue(published_manifest)
            self.assertEqual(manifest_path.read_bytes(), before_manifest)
            self.assertEqual(fixture.production_path.read_bytes(), before_production)
            self.assertEqual(
                list(root.glob(".danse-package-transaction-*")),
                [],
            )

    def test_rebuilt_graph_is_validated_before_manifest_publication(self) -> None:
        for mutation in ("production", "producer-receipt"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                out = root / "render"
                package = root / "package"
                fixture = retained_score_package(out, package)
                manifest_path = package / "manifest.json"
                previous = copy.deepcopy(fixture.manifest)
                current = copy.deepcopy(previous)
                current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
                before_manifest = manifest_path.read_bytes()
                before_production = fixture.production_path.read_bytes()
                before_receipts = {
                    path.name: path.read_bytes()
                    for path in (package / DELIVER.PRODUCER_RECEIPTS).iterdir()
                }
                rename = DELIVER.atomic_rename_noreplace
                mutated = False
                manifest_published = False

                def mutate_after_graph_moves(source, destination, **kwargs):
                    nonlocal mutated, manifest_published
                    result = rename(source, destination, **kwargs)
                    if Path(source).name == "production.new.json":
                        mutated = True
                        if mutation == "production":
                            fixture.production_path.write_bytes(b"concurrent production mutation")
                        else:
                            receipt = next(
                                (package / DELIVER.PRODUCER_RECEIPTS).iterdir()
                            )
                            receipt.write_bytes(b"concurrent producer mutation")
                    elif Path(source).name == "manifest.new.json":
                        manifest_published = True
                    return result

                with (
                    mock.patch.object(
                        DELIVER,
                        "atomic_rename_noreplace",
                        side_effect=mutate_after_graph_moves,
                    ),
                    self.assertRaisesRegex(SystemExit, "replacement failed"),
                ):
                    DELIVER.write_production_receipt(
                        package,
                        fixture.render_root,
                        current,
                        previous,
                        publish_manifest=True,
                        previous_manifest_sha256=DELIVER.digest(manifest_path),
                    )

                self.assertTrue(mutated)
                self.assertFalse(manifest_published)
                self.assertEqual(manifest_path.read_bytes(), before_manifest)
                self.assertEqual(fixture.production_path.read_bytes(), before_production)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in (package / DELIVER.PRODUCER_RECEIPTS).iterdir()
                    },
                    before_receipts,
                )
                self.assertEqual(list(root.glob(".danse-package-transaction-*")), [])

    def test_concurrent_publication_destination_is_preserved_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            manifest_path = package / "manifest.json"
            previous = copy.deepcopy(fixture.manifest)
            current = copy.deepcopy(previous)
            current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
            old_production = fixture.production_path.read_bytes()
            concurrent = b'{"schema":"concurrent-production-winner"}\n'
            rename = DELIVER.atomic_rename_noreplace

            def install_winner_before_new_production(source, destination, **kwargs):
                if Path(source).name == "production.new.json":
                    fixture.production_path.write_bytes(concurrent)
                return rename(source, destination, **kwargs)

            with (
                mock.patch.object(
                    DELIVER,
                    "atomic_rename_noreplace",
                    side_effect=install_winner_before_new_production,
                ),
                self.assertRaisesRegex(SystemExit, "recovery preserved at") as caught,
            ):
                DELIVER.write_production_receipt(
                    package,
                    fixture.render_root,
                    current,
                    previous,
                    publish_manifest=True,
                    previous_manifest_sha256=DELIVER.digest(manifest_path),
                )

            self.assertEqual(fixture.production_path.read_bytes(), concurrent)
            match = re.search(r"recovery preserved at (.+)$", str(caught.exception))
            self.assertIsNotNone(match)
            recovery = Path(match.group(1))
            self.assertEqual((recovery / "production.old.json").read_bytes(), old_production)

    def test_concurrent_rollback_destination_is_preserved_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            manifest_path = package / "manifest.json"
            previous = copy.deepcopy(fixture.manifest)
            current = copy.deepcopy(previous)
            current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
            old_production = fixture.production_path.read_bytes()
            concurrent = b'{"schema":"concurrent-rollback-winner"}\n'
            rename = DELIVER.atomic_rename_noreplace

            def fail_manifest_then_install_rollback_winner(source, destination, **kwargs):
                source_name = Path(source).name
                destination_name = Path(destination).name
                if source_name == "manifest.new.json":
                    raise OSError("injected manifest publication failure")
                result = rename(source, destination, **kwargs)
                if source_name == "production.json" and destination_name == "production.failed.json":
                    fixture.production_path.write_bytes(concurrent)
                return result

            with (
                mock.patch.object(
                    DELIVER,
                    "atomic_rename_noreplace",
                    side_effect=fail_manifest_then_install_rollback_winner,
                ),
                self.assertRaisesRegex(SystemExit, "recovery preserved at") as caught,
            ):
                DELIVER.write_production_receipt(
                    package,
                    fixture.render_root,
                    current,
                    previous,
                    publish_manifest=True,
                    previous_manifest_sha256=DELIVER.digest(manifest_path),
                )

            self.assertEqual(fixture.production_path.read_bytes(), concurrent)
            match = re.search(r"recovery preserved at (.+)$", str(caught.exception))
            self.assertIsNotNone(match)
            recovery = Path(match.group(1))
            self.assertEqual((recovery / "production.old.json").read_bytes(), old_production)

    def test_displaced_stage_edge_preserves_recovery_and_same_name_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            manifest_path = package / "manifest.json"
            previous = copy.deepcopy(fixture.manifest)
            current = copy.deepcopy(previous)
            current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
            old_manifest = manifest_path.read_bytes()
            old_production = fixture.production_path.read_bytes()
            write_staged = DELIVER.write_new_regular_bytes_at
            displaced = root / "displaced-original-stage"
            replacement = None

            def displace_before_first_staging_write(directory_fd, name, payload, **kwargs):
                nonlocal replacement
                if replacement is None:
                    stages = list(root.glob(".danse-package-transaction-*"))
                    self.assertEqual(len(stages), 1)
                    replacement = stages[0]
                    DELIVER.os.replace(replacement, displaced)
                    replacement.mkdir()
                    (replacement / "unrelated.txt").write_text("do not remove\n")
                return write_staged(directory_fd, name, payload, **kwargs)

            with (
                mock.patch.object(
                    DELIVER,
                    "write_new_regular_bytes_at",
                    side_effect=displace_before_first_staging_write,
                ),
                self.assertRaisesRegex(SystemExit, "displaced originally opened"),
            ):
                DELIVER.write_production_receipt(
                    package,
                    fixture.render_root,
                    current,
                    previous,
                    publish_manifest=True,
                    previous_manifest_sha256=DELIVER.digest(manifest_path),
                )

            self.assertIsNotNone(replacement)
            self.assertEqual((replacement / "unrelated.txt").read_text(), "do not remove\n")
            self.assertEqual((displaced / "manifest.old.json").read_bytes(), old_manifest)
            self.assertEqual((displaced / "production.old.json").read_bytes(), old_production)
            self.assertEqual(
                json.loads(manifest_path.read_text())["repository_head"],
                SUBMISSION_REPOSITORY_HEAD,
            )

    def test_post_rename_interrupts_restore_the_prior_package_commit(self) -> None:
        rename_sources = (
            "manifest.json",
            "production.json",
            "producer-receipts",
            "producer-receipts.new",
            "production.new.json",
            "manifest.new.json",
        )
        for rename_source in rename_sources:
            with self.subTest(rename_source=rename_source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                out = root / "render"
                package = root / "package"
                fixture = retained_score_package(out, package)
                manifest_path = package / "manifest.json"
                previous = copy.deepcopy(fixture.manifest)
                current = copy.deepcopy(previous)
                current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
                receipt_root = package / DELIVER.PRODUCER_RECEIPTS
                before_manifest = manifest_path.read_bytes()
                before_production = fixture.production_path.read_bytes()
                before_receipts = {
                    path.name: path.read_bytes() for path in receipt_root.iterdir()
                }
                rename = DELIVER.atomic_rename_noreplace
                interrupted = False

                def interrupt_after_rename(source, destination, **kwargs):
                    nonlocal interrupted
                    result = rename(source, destination, **kwargs)
                    if not interrupted and Path(source).name == rename_source:
                        interrupted = True
                        raise KeyboardInterrupt
                    return result

                with (
                    mock.patch.object(
                        DELIVER,
                        "atomic_rename_noreplace",
                        side_effect=interrupt_after_rename,
                    ),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    DELIVER.write_production_receipt(
                        package,
                        fixture.render_root,
                        current,
                        previous,
                        publish_manifest=True,
                        previous_manifest_sha256=DELIVER.digest(manifest_path),
                    )

                self.assertTrue(interrupted)
                self.assertEqual(manifest_path.read_bytes(), before_manifest)
                self.assertEqual(fixture.production_path.read_bytes(), before_production)
                self.assertEqual(
                    {path.name: path.read_bytes() for path in receipt_root.iterdir()},
                    before_receipts,
                )
                self.assertEqual(
                    list(root.glob(".danse-package-transaction-*")),
                    [],
                )

    def test_provenance_ancestor_swap_cannot_redirect_receipt_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            manifest_path = package / "manifest.json"
            previous = copy.deepcopy(fixture.manifest)
            current = copy.deepcopy(previous)
            current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
            before_manifest = manifest_path.read_bytes()
            before_production = fixture.production_path.read_bytes()
            before_receipts = {
                path.name: path.read_bytes()
                for path in (package / DELIVER.PRODUCER_RECEIPTS).iterdir()
            }

            external = root / "external-provenance"
            shutil.copytree(package / "provenance", external)
            external_before = {
                path.relative_to(external).as_posix(): path.read_bytes()
                for path in external.rglob("*")
                if path.is_file()
            }
            displaced = root / "displaced-provenance"
            replace = DELIVER.os.replace
            rename = DELIVER.atomic_rename_noreplace
            swapped = False

            def swap_ancestor_before_first_rename(source, destination, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    replace(package / "provenance", displaced)
                    (package / "provenance").symlink_to(
                        external,
                        target_is_directory=True,
                    )
                return rename(source, destination, **kwargs)

            with (
                mock.patch.object(
                    DELIVER,
                    "atomic_rename_noreplace",
                    side_effect=swap_ancestor_before_first_rename,
                ),
                self.assertRaisesRegex(SystemExit, "replacement failed"),
            ):
                DELIVER.write_production_receipt(
                    package,
                    fixture.render_root,
                    current,
                    previous,
                    publish_manifest=True,
                    previous_manifest_sha256=DELIVER.digest(manifest_path),
                )

            self.assertTrue(swapped)
            self.assertTrue((package / "provenance").is_symlink())
            self.assertEqual(manifest_path.read_bytes(), before_manifest)
            self.assertEqual(
                {
                    path.relative_to(external).as_posix(): path.read_bytes()
                    for path in external.rglob("*")
                    if path.is_file()
                },
                external_before,
            )
            self.assertEqual(
                (displaced / "production.json").read_bytes(),
                before_production,
            )
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (displaced / "producer-receipts").iterdir()
                },
                before_receipts,
            )
            self.assertEqual(
                list(root.glob(".danse-package-transaction-*")),
                [],
            )

    def test_failed_rollback_preserves_an_external_recovery_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            manifest_path = package / "manifest.json"
            previous = copy.deepcopy(fixture.manifest)
            current = copy.deepcopy(previous)
            current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
            old_receipts = {
                path.name: path.read_bytes()
                for path in (package / DELIVER.PRODUCER_RECEIPTS).iterdir()
            }
            rename = DELIVER.atomic_rename_noreplace

            def fail_manifest_and_receipt_restore(source, destination, **kwargs):
                if Path(source).name == "manifest.new.json":
                    raise OSError("injected manifest publication failure")
                if Path(source).name == "producer-receipts.old":
                    raise OSError("injected prior-receipt rollback failure")
                return rename(source, destination, **kwargs)

            with (
                mock.patch.object(
                    DELIVER,
                    "atomic_rename_noreplace",
                    side_effect=fail_manifest_and_receipt_restore,
                ),
                self.assertRaisesRegex(SystemExit, "recovery preserved at") as caught,
            ):
                DELIVER.write_production_receipt(
                    package,
                    fixture.render_root,
                    current,
                    previous,
                    publish_manifest=True,
                    previous_manifest_sha256=DELIVER.digest(manifest_path),
                )

            match = re.search(r"recovery preserved at (.+)$", str(caught.exception))
            self.assertIsNotNone(match)
            recovery = Path(match.group(1))
            try:
                recovered_receipts = recovery / "producer-receipts.old"
                self.assertTrue(recovered_receipts.is_dir())
                self.assertEqual(
                    {path.name: path.read_bytes() for path in recovered_receipts.iterdir()},
                    old_receipts,
                )
                self.assertFalse(recovery.is_relative_to(package))
            finally:
                shutil.rmtree(recovery)

    def test_prior_score_motion_reference_rejects_symlinked_ancestors(self) -> None:
        for ancestor in ("provenance", "provenance/score-to-motion"):
            with self.subTest(ancestor=ancestor), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                package = root / "package"
                package.mkdir()
                outside = root / "outside"
                outside.mkdir()
                receipt = outside / "score-to-motion-production.json"
                receipt.write_text('{"schema":"danse.evidence.score-to-motion.production.v1"}\n')
                digest = DELIVER.digest(receipt)
                linked = package / ancestor
                linked.parent.mkdir(parents=True, exist_ok=True)
                linked.symlink_to(outside, target_is_directory=True)
                previous = {
                    "score_motion_evidence": {
                        "path": DELIVER.SCORE_MOTION_EVIDENCE_ITEM,
                        "sha256": digest,
                    }
                }
                items = {
                    DELIVER.SCORE_MOTION_EVIDENCE_ITEM: {
                        "name": DELIVER.SCORE_MOTION_EVIDENCE_ITEM,
                        "bytes": receipt.stat().st_size,
                        "sha256": digest,
                    }
                }
                self.assertIsNone(
                    DELIVER.prior_score_motion_evidence(package, previous, items)
                )

    def test_reused_media_without_producer_receipt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "package"
            package.mkdir()
            master = package / "master.mov"
            master.write_bytes(b"modified after packaging")
            score = root / "passage-score.wav"
            score.write_bytes(b"rendered score source")
            audio_receipt = root / "audio-render.json"
            audio_receipt.write_bytes(b"verified audio receipt")
            sound = {
                "master_sha256": DELIVER.digest(score),
                "audio_render_receipt_sha256": DELIVER.digest(audio_receipt),
            }
            prior_digest = "0" * 64
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "repository_head": SUBMISSION_REPOSITORY_HEAD,
                        "passage_seed": DELIVER.hexseed(SPAN["seed"]),
                        "passage": SPAN["passage"],
                        "t0": SPAN["t0"],
                        "t1": SPAN["t1"],
                        "duration": SPAN["duration"],
                        "source_tree_sha256": DELIVER.delivery_source_sha256("film"),
                        "items": [{"name": "master.mov", "bytes": 23, "sha256": prior_digest}],
                    }
                )
            )
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    ["deliver.py", "--only", "master", "--out", str(root / "render"), "--package", str(package)],
                ),
                mock.patch.object(DELIVER, "query_capture_span", return_value=SPAN),
                mock.patch.object(
                    DELIVER,
                    "passage_sound",
                    return_value=(score, sound, False),
                ),
                mock.patch.object(
                    DELIVER,
                    "require_clean_repository",
                    return_value={
                        "head": SUBMISSION_REPOSITORY_HEAD,
                        "clean": True,
                        "changes": [],
                    },
                ),
                mock.patch.object(DELIVER, "AUDIO_RENDER_RECEIPT", audio_receipt),
                mock.patch.object(DELIVER.shutil, "which", return_value="/tools/ffprobe"),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(SystemExit, "producer receipt is missing"):
                    DELIVER.main()
            self.assertEqual(master.read_bytes(), b"modified after packaging")
            prior = json.loads((package / "manifest.json").read_text())
            self.assertEqual(prior["items"][0]["sha256"], prior_digest)

    def test_production_receipt_rejects_incomplete_identity_and_unsafe_destination(self) -> None:
        target = {
            "name": DELIVER.SCORE_SOURCE_ITEM,
            "bytes": 5,
            "sha256": "1" * 64,
        }
        complete = {
            "repository_head": SUBMISSION_REPOSITORY_HEAD,
            "seed": "0xAF6B7BE5",
            "passage_seed": "0xAF6B7BE5",
            "passage": 0,
            "start": 0.0,
            "t0": 0.0,
            "t1": 312.54,
            "duration": 312.54,
            "corpus_tier": "film",
            "source_tree_sha256": "0" * 64,
            "items": [target],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(SystemExit, "complete passage identity"):
                DELIVER.write_production_receipt(
                    root / "missing-passage",
                    root / "render",
                    {"items": [target]},
                    {},
                )
            without_source = {**complete}
            without_source.pop("source_tree_sha256")
            with self.assertRaisesRegex(SystemExit, "complete source-tree identity"):
                DELIVER.write_production_receipt(
                    root / "missing-source",
                    root / "render",
                    without_source,
                    {},
                )
            without_head = {**complete}
            without_head.pop("repository_head")
            with self.assertRaisesRegex(SystemExit, "repository-head identity"):
                DELIVER.write_production_receipt(
                    root / "missing-head",
                    root / "render",
                    without_head,
                    {},
                )

            package = root / "package"
            provenance = package / "provenance"
            provenance.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_bytes(b"must remain unchanged")
            production = package / DELIVER.PRODUCTION_RECEIPT
            production.symlink_to(outside)
            with self.assertRaisesRegex(SystemExit, "destination is not a regular file"):
                DELIVER.write_production_receipt(
                    package,
                    root / "render",
                    complete,
                    {},
                )
            self.assertEqual(outside.read_bytes(), b"must remain unchanged")
            self.assertFalse((package / DELIVER.PRODUCER_RECEIPTS).exists())

    def test_package_manifest_and_producer_receipt_reads_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            producer = root / "segment.receipt.json"
            with producer.open("wb") as handle:
                handle.truncate(DELIVER.PRODUCER_RECEIPT_MAX_BYTES + 1)
            with self.assertRaisesRegex(SystemExit, "safe byte limit"):
                DELIVER._read_producer_receipt(producer, "danse.render.segment.v1")

            manifest = root / "manifest.json"
            with manifest.open("wb") as handle:
                handle.truncate(DELIVER.PACKAGE_MANIFEST_MAX_BYTES + 1)
            with self.assertRaisesRegex(SystemExit, "safe byte limit"):
                DELIVER.bounded_regular_bytes(
                    manifest,
                    max_bytes=DELIVER.PACKAGE_MANIFEST_MAX_BYTES,
                    description="package manifest",
                )

    def test_producer_receipt_graph_has_count_duplicate_and_aggregate_bounds(self) -> None:
        def graph_fixture(root: Path, segments: list[dict]) -> tuple[SimpleNamespace, dict, Path]:
            out = root / "render"
            package = root / "package"
            fixture = retained_score_package(out, package)
            fixture.package = package
            current = copy.deepcopy(fixture.manifest)
            current["repository_head"] = SUBMISSION_REPOSITORY_HEAD
            current["items"].append(
                {
                    "name": "master.mov",
                    "bytes": 17,
                    "sha256": "a" * 64,
                }
            )
            concat = fixture.render_root / "passage-default.mov.receipt.json"
            concat.write_text(
                json.dumps(
                    {
                        "schema": "danse.render.concat.v1",
                        "codec": "prores",
                        "segments": segments,
                        "file_sha256": "b" * 64,
                    },
                    indent=2,
                )
                + "\n"
            )
            return fixture, current, concat

        with self.subTest("segment count"), tempfile.TemporaryDirectory() as tmp:
            segments = [
                {"name": f"passage-default-seg-{index:03d}.mov", "receipt_sha256": "c" * 64}
                for index in range(DELIVER.PRODUCER_CONCAT_SEGMENT_MAX_COUNT + 1)
            ]
            fixture, current, _concat = graph_fixture(Path(tmp), segments)
            with (
                mock.patch.object(DELIVER, "renderer_source_sha256", return_value="source"),
                self.assertRaisesRegex(SystemExit, "segment-count limit"),
            ):
                DELIVER.write_production_receipt(
                    fixture.package,
                    fixture.render_root,
                    current,
                    fixture.manifest,
                )

        with self.subTest("duplicate names"), tempfile.TemporaryDirectory() as tmp:
            name = "passage-default-seg-000.mov"
            segments = [
                {"name": name, "receipt_sha256": "c" * 64},
                {"name": name, "receipt_sha256": "c" * 64},
            ]
            fixture, current, concat = graph_fixture(Path(tmp), segments)
            reader = DELIVER._read_producer_receipt
            with (
                mock.patch.object(DELIVER, "renderer_source_sha256", return_value="source"),
                mock.patch.object(
                    DELIVER,
                    "_read_producer_receipt",
                    wraps=reader,
                ) as read_probe,
                self.assertRaisesRegex(SystemExit, "repeats a segment"),
            ):
                DELIVER.write_production_receipt(
                    fixture.package,
                    fixture.render_root,
                    current,
                    fixture.manifest,
                )
            self.assertEqual(
                [Path(call.args[0]).name for call in read_probe.call_args_list],
                [fixture.score_receipt.name, concat.name],
            )

        with self.subTest("aggregate bytes"), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            name = "passage-default-seg-000.mov"
            fixture, current, concat = graph_fixture(
                root,
                [{"name": name, "receipt_sha256": "placeholder"}],
            )
            segment_receipt = fixture.render_root / f"{name}.receipt.json"
            segment_receipt.write_text(
                json.dumps(
                    {
                        "schema": "danse.render.segment.v1",
                        "segment": 0,
                        "frames": 1,
                        "inputs": {
                            "source_tree_sha256": "source",
                            "tier": "film",
                        },
                        "file_sha256": "d" * 64,
                    },
                    indent=2,
                )
                + "\n"
            )
            concat_value = json.loads(concat.read_text())
            concat_value["segments"][0]["receipt_sha256"] = DELIVER.digest(segment_receipt)
            concat.write_text(json.dumps(concat_value, indent=2) + "\n")
            aggregate_limit = (
                fixture.score_receipt.stat().st_size
                + concat.stat().st_size
                + segment_receipt.stat().st_size
                - 1
            )
            with (
                mock.patch.object(DELIVER, "renderer_source_sha256", return_value="source"),
                mock.patch.object(
                    DELIVER,
                    "PRODUCER_RECEIPT_TOTAL_MAX_BYTES",
                    aggregate_limit,
                ),
                self.assertRaisesRegex(SystemExit, "aggregate byte limit"),
            ):
                DELIVER.write_production_receipt(
                    fixture.package,
                    fixture.render_root,
                    current,
                    fixture.manifest,
                )

    def test_package_identity_requires_a_clean_exact_git_head(self) -> None:
        head = subprocess.CompletedProcess([], 0, stdout=SUBMISSION_REPOSITORY_HEAD + "\n", stderr="")
        clean = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        dirty = subprocess.CompletedProcess([], 0, stdout=" M render/deliver.py\n", stderr="")
        with mock.patch.object(DELIVER, "sh", side_effect=[head, clean]):
            self.assertEqual(DELIVER.require_clean_repository()["head"], SUBMISSION_REPOSITORY_HEAD)
        with (
            mock.patch.object(DELIVER, "sh", side_effect=[head, dirty]),
            self.assertRaisesRegex(SystemExit, "clean tracked/untracked worktree"),
        ):
            DELIVER.require_clean_repository()
        no_head = subprocess.CompletedProcess([], 0, stdout=None, stderr="")
        with (
            mock.patch.object(DELIVER, "sh", return_value=no_head),
            self.assertRaisesRegex(SystemExit, "no Git commit identity"),
        ):
            DELIVER.repository_state()
        no_status = subprocess.CompletedProcess([], 0, stdout=None, stderr="")
        with (
            mock.patch.object(DELIVER, "sh", side_effect=[head, no_status]),
            self.assertRaisesRegex(SystemExit, "no repository worktree status"),
        ):
            DELIVER.repository_state()

    def test_production_receipt_uses_the_decimal_renderer_still_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "package"
            render_root = root / "render"
            package.mkdir()
            render_root.mkdir()
            seed = int("0x12AB", 0)
            receipt_path = render_root / f"passage-{seed}-seg-001.mov.receipt.json"
            receipt_path.write_text(
                json.dumps(
                    {
                        "schema": "danse.render.segment.v1",
                        "segment": 1,
                        "frames": 1,
                        "inputs": {
                            "source_tree_sha256": "renderer-source",
                            "tier": "film",
                        },
                        "file_sha256": "2" * 64,
                    }
                )
            )
            manifest = {
                "repository_head": SUBMISSION_REPOSITORY_HEAD,
                "seed": "0xAF6B7BE5",
                "passage_seed": "0xAF6B7BE5",
                "passage": 0,
                "start": 0.0,
                "t0": 0.0,
                "t1": 312.54,
                "duration": 312.54,
                "corpus_tier": "film",
                "source_tree_sha256": "0" * 64,
                "items": [
                    {
                        "name": "stills/seed-0x12AB.jpg",
                        "bytes": 5,
                        "sha256": "3" * 64,
                    }
                ],
            }
            with mock.patch.object(
                DELIVER,
                "renderer_source_sha256",
                return_value="renderer-source",
            ):
                reference = DELIVER.write_production_receipt(
                    package,
                    render_root,
                    manifest,
                    {},
                )
            self.assertIsNotNone(reference)
            production = json.loads((package / DELIVER.PRODUCTION_RECEIPT).read_text())
            self.assertEqual(production["outputs"][0]["name"], "stills/seed-0x12AB.jpg")
            self.assertEqual(production["producers"][0]["kind"], "render-segment")

    def test_score_receipt_is_bound_to_cached_audio_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            score = Path(tmp) / "passage-score.wav"
            score.write_bytes(b"score-audio")
            provenance = {
                "profile": "competition-classical",
                "master_sha256": DELIVER.digest(score),
                "score_file_sha256": "1" * 64,
                "choreography_file_sha256": "2" * 64,
            }
            DELIVER.write_score_receipt(score, SPAN, provenance)
            with mock.patch.object(DELIVER, "competition_audio_provenance", return_value=provenance):
                self.assertEqual(DELIVER.score_provenance(score, SPAN), provenance)
                score.write_bytes(b"changed-audio")
                self.assertIsNone(DELIVER.score_provenance(score, SPAN))

    def test_legacy_apartment_score_receipt_is_not_package_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            score = Path(tmp) / "passage-score.wav"
            score.write_bytes(b"legacy apartment score")
            DELIVER.score_receipt_path(score).write_text(
                json.dumps(
                    {
                        "schema": "danse.score.receipt.v1",
                        "sha256": DELIVER.digest(score),
                        "bank_fingerprint": "legacy-bank",
                        "sources": ["IMG_0226.MOV", "IMG_0227.MOV"],
                        "t0": SPAN["t0"],
                        "duration": SPAN["duration"],
                    }
                )
            )
            self.assertIsNone(DELIVER.score_provenance(score, SPAN))

    def test_missing_manifest_refuses_preexisting_package_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            self.assertTrue(DELIVER.package_provenance_matches(package, SPAN))
            (package / "master.mov").write_bytes(b"unowned media")
            self.assertFalse(DELIVER.package_provenance_matches(package, SPAN))

    def test_passage_independent_manifests_do_not_claim_or_require_a_span(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "manifest.json").write_text(
                json.dumps({"items": [{"name": "text/synopsis_short.txt"}]})
            )
            self.assertTrue(DELIVER.package_provenance_matches(package, SPAN, start=120.0))
            (package / "master.mov").write_bytes(b"unmanifested passage")
            self.assertFalse(DELIVER.package_provenance_matches(package, SPAN, start=120.0))

    def test_fixed_window_package_receipts_bind_the_selected_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "passage_seed": DELIVER.hexseed(SPAN["seed"]),
                        "passage": SPAN["passage"],
                        "start": 120.0,
                        "t0": SPAN["t0"],
                        "t1": SPAN["t1"],
                        "duration": SPAN["duration"],
                        "items": [{"name": "trailer.mp4"}],
                    }
                )
            )
            self.assertTrue(DELIVER.package_provenance_matches(package, SPAN, start=120.0))
            self.assertFalse(DELIVER.package_provenance_matches(package, SPAN, start=121.0))

    def test_package_receipts_bind_the_producing_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            manifest = {
                "repository_head": SUBMISSION_REPOSITORY_HEAD,
                "passage_seed": DELIVER.hexseed(SPAN["seed"]),
                "passage": SPAN["passage"],
                "t0": SPAN["t0"],
                "t1": SPAN["t1"],
                "duration": SPAN["duration"],
                "source_tree_sha256": "tree-a",
                "items": [{"name": "master.mov"}],
            }
            (package / "manifest.json").write_text(json.dumps(manifest))
            self.assertTrue(DELIVER.package_provenance_matches(package, SPAN, source_tree_sha256="tree-a"))
            self.assertFalse(DELIVER.package_provenance_matches(package, SPAN, source_tree_sha256="tree-b"))
            self.assertTrue(
                DELIVER.package_provenance_matches(
                    package,
                    SPAN,
                    source_tree_sha256="tree-a",
                    repository_head=SUBMISSION_REPOSITORY_HEAD,
                )
            )
            self.assertFalse(
                DELIVER.package_provenance_matches(
                    package,
                    SPAN,
                    source_tree_sha256="tree-a",
                    repository_head="b" * 40,
                )
            )

    def test_forced_score_rebuilds_every_selected_audio_derivative(self) -> None:
        program = json.loads((ROOT / "render/program.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            for name in DELIVER.AUDIO_ITEMS:
                (package / name).parent.mkdir(parents=True, exist_ok=True)
                (package / name).touch()
            work = DELIVER.pending(program, {"master", "derived", "reel"}, {"master"}, package)
        self.assertTrue(work["master"])
        self.assertEqual(work["derived"], set(DELIVER.DERIVED))
        self.assertTrue(work["reel"])

    def test_rebuilt_score_invalidates_every_selected_audio_artifact(self) -> None:
        work = {"master": False, "derived": set(), "reel": False, "stills": False}
        DELIVER.expand_rebuilt_score_dependents(work, {"master", "derived", "reel"})
        self.assertTrue(work["master"])
        self.assertEqual(work["derived"], set(DELIVER.DERIVED))
        self.assertTrue(work["reel"])

    def test_reel_renderer_accepts_one_segment_and_receives_the_resolved_capture_start(self) -> None:
        reel_span = {**SPAN, "capture": "reel", "t0": 140.0, "t1": 155.0, "duration": 15.0}
        passage_span = {**SPAN, "t0": 120.0, "t1": 432.54}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            out.mkdir()
            package.mkdir()
            control = package / "normal-output.mp4"
            control.write_bytes(b"normal output")

            def query(name: str, start: float = 0.0) -> dict:
                return reel_span if name == "reel" else passage_span

            def render(command: list[str], **_: object) -> subprocess.CompletedProcess:
                render_out = Path(command[command.index("--out") + 1])
                if "--concat" in command:
                    write_fake_reel_concat(render_out, b"validated concat")
                else:
                    write_fake_reel_segment(render_out)
                return subprocess.CompletedProcess(command, 0)

            def mux_reel(picture: Path, _audio: Path, dest: Path, *_: object, **__: object) -> None:
                self.assertEqual(picture.name, "reel-default.mp4")
                self.assertEqual(dest.parent.parent, package)
                self.assertEqual(dest.name, "reel.mp4")
                self.assertFalse(dest.exists())
                dest.write_bytes(b"muxed reel")

            with (
                mock.patch.object(DELIVER, "OUT", out),
                mock.patch.object(DELIVER, "PACKAGE", package),
                mock.patch.object(DELIVER, "query_capture_span", side_effect=query),
                mock.patch.object(DELIVER.subprocess, "run", side_effect=render) as run,
                mock.patch.object(DELIVER, "cut_audio"),
                mock.patch.object(DELIVER, "mux", side_effect=mux_reel),
                mock.patch.object(DELIVER, "probe_required", return_value={"seconds": 15.0, "fps": 30.0}),
            ):
                DELIVER.deliver_reel({}, root / "score.wav", "film", True, start=120.0)
            self.assertEqual(run.call_count, 2)
            first, second = (call.args[0] for call in run.call_args_list)
            self.assertEqual(first[first.index("--start") + 1], "140.0")
            self.assertNotIn("--concat", first)
            self.assertIn("--concat", second)
            final = package / "reel.mp4"
            self.assertTrue(final.is_file())
            self.assertFalse(final.is_symlink())
            self.assertEqual(final.read_bytes(), b"muxed reel")
            self.assertEqual(
                final.stat().st_mode & 0o777,
                control.stat().st_mode & 0o777,
            )
            self.assertEqual(final.stat().st_mode & 0o111, 0)
            self.assertEqual(list(package.glob(".reel-*")), [])

    def test_reel_renderer_rejects_failed_concat_recovery(self) -> None:
        reel_span = {**SPAN, "capture": "reel", "t0": 140.0, "t1": 155.0, "duration": 15.0}
        passage_span = {**SPAN, "t0": 120.0, "t1": 432.54}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            out.mkdir()
            package.mkdir()
            final = package / "reel.mp4"
            final.write_bytes(b"previous valid reel")
            prior_mode = final.stat().st_mode

            def query(name: str, start: float = 0.0) -> dict:
                return reel_span if name == "reel" else passage_span

            def render(command: list[str], **_: object) -> subprocess.CompletedProcess:
                render_out = Path(command[command.index("--out") + 1])
                if "--concat" in command:
                    return subprocess.CompletedProcess(command, 1)
                (render_out / "reel-default-seg-000.mp4").write_bytes(b"rendered reel")
                return subprocess.CompletedProcess(command, 0)

            with (
                mock.patch.object(DELIVER, "OUT", out),
                mock.patch.object(DELIVER, "PACKAGE", package),
                mock.patch.object(DELIVER, "query_capture_span", side_effect=query),
                mock.patch.object(DELIVER.subprocess, "run", side_effect=render),
                mock.patch.object(DELIVER, "cut_audio"),
                mock.patch.object(DELIVER, "mux"),
            ):
                with self.assertRaisesRegex(SystemExit, "reel would not render"):
                    DELIVER.deliver_reel({}, root / "score.wav", "film", True, start=120.0)
            self.assertEqual(final.read_bytes(), b"previous valid reel")
            self.assertEqual(final.stat().st_mode, prior_mode)
            self.assertEqual(list(package.glob(".reel-*")), [])

    def test_reel_renderer_cleans_partial_mux_without_replacing_prior_reel(self) -> None:
        reel_span = {**SPAN, "capture": "reel", "t0": 140.0, "t1": 155.0, "duration": 15.0}
        passage_span = {**SPAN, "t0": 120.0, "t1": 432.54}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            out.mkdir()
            package.mkdir()
            final = package / "reel.mp4"
            final.write_bytes(b"previous valid reel")
            prior_mode = final.stat().st_mode

            def query(name: str, start: float = 0.0) -> dict:
                return reel_span if name == "reel" else passage_span

            def render(command: list[str], **_: object) -> subprocess.CompletedProcess:
                render_out = Path(command[command.index("--out") + 1])
                write_fake_reel_concat(render_out)
                return subprocess.CompletedProcess(command, 0)

            def fail_mux(_picture: Path, _audio: Path, dest: Path, *_: object, **__: object) -> None:
                self.assertFalse(dest.exists())
                dest.write_bytes(b"partial reel")
                raise RuntimeError("mux failed")

            with (
                mock.patch.object(DELIVER, "OUT", out),
                mock.patch.object(DELIVER, "PACKAGE", package),
                mock.patch.object(DELIVER, "query_capture_span", side_effect=query),
                mock.patch.object(DELIVER.subprocess, "run", side_effect=render),
                mock.patch.object(DELIVER, "cut_audio"),
                mock.patch.object(DELIVER, "mux", side_effect=fail_mux),
            ):
                with self.assertRaisesRegex(RuntimeError, "mux failed"):
                    DELIVER.deliver_reel({}, root / "score.wav", "film", True, start=120.0)
            self.assertEqual(final.read_bytes(), b"previous valid reel")
            self.assertEqual(final.stat().st_mode, prior_mode)
            self.assertEqual(list(package.glob(".reel-*")), [])

    def test_reel_renderer_rejects_wrong_final_duration(self) -> None:
        reel_span = {**SPAN, "capture": "reel", "t0": 140.0, "t1": 155.0, "duration": 15.0}
        passage_span = {**SPAN, "t0": 120.0, "t1": 432.54}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "render"
            package = root / "package"
            out.mkdir()
            package.mkdir()
            final = package / "reel.mp4"
            final.write_bytes(b"previous valid reel")
            prior_mode = final.stat().st_mode

            def query(name: str, start: float = 0.0) -> dict:
                return reel_span if name == "reel" else passage_span

            def render(command: list[str], **_: object) -> subprocess.CompletedProcess:
                render_out = Path(command[command.index("--out") + 1])
                write_fake_reel_concat(render_out)
                return subprocess.CompletedProcess(command, 0)

            def mux_reel(_picture: Path, _audio: Path, dest: Path, *_: object, **__: object) -> None:
                dest.write_bytes(b"short reel")

            with (
                mock.patch.object(DELIVER, "OUT", out),
                mock.patch.object(DELIVER, "PACKAGE", package),
                mock.patch.object(DELIVER, "query_capture_span", side_effect=query),
                mock.patch.object(DELIVER.subprocess, "run", side_effect=render),
                mock.patch.object(DELIVER, "cut_audio"),
                mock.patch.object(DELIVER, "mux", side_effect=mux_reel),
                mock.patch.object(DELIVER, "probe_required", return_value={"seconds": 14.0, "fps": 30.0}),
            ):
                with self.assertRaisesRegex(SystemExit, "render is wrong"):
                    DELIVER.deliver_reel({}, root / "score.wav", "film", True, start=120.0)
            self.assertEqual(final.read_bytes(), b"previous valid reel")
            self.assertEqual(final.stat().st_mode, prior_mode)
            self.assertEqual(list(package.glob(".reel-*")), [])

    def test_capture_overrun_is_rejected_before_render(self) -> None:
        overrun = {**SPAN, "t0": 300.0, "t1": 470.0, "duration": 170.0, "capture": "midnight-moment"}
        with mock.patch.object(DELIVER, "query_capture_span", return_value=overrun):
            error = DELIVER.capture_span_error("midnight-moment", SPAN, 300.0)
        self.assertIn("does not fit passage", error)

    def test_bank_contract_rejects_missing_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bank_root = Path(tmp)
            grains = []
            kinds = ("bed", "sustained", "transient")
            sources = ["IMG_0226.MOV", "IMG_0227.MOV"]
            source_rows = []
            for source in sources:
                source_file = bank_root / source
                source_file.write_bytes(f"private source fixture: {source}".encode())
                source_rows.append({"name": source, "sha256": BANK_CONTRACT.sha256(source_file)})
            for i in range(24):
                grains.append(
                    {
                        "id": f"grain-{i}",
                        "source": sources[i % 2],
                        "kind": kinds[i % len(kinds)],
                        "centroid": i + 1,
                        "brightness": i + 1,
                        "flatness": i + 1,
                        "decay": i + 1,
                        "attack": i + 1,
                        "zcr": i + 1,
                        "rms": 0.125,
                        "wav_sha256": source_rows[i % 2]["sha256"],
                    }
                )
            index = bank_root / "bank.json"
            payload = {
                "schema": "danse.sound.bank.v1",
                "rate": 48_000,
                "sources": source_rows,
                "grains": grains,
            }
            payload["fingerprint"] = BANK_CONTRACT.bank_fingerprint(payload)
            index.write_text(json.dumps(payload))
            expected_source_digests = {row["name"]: row["sha256"] for row in source_rows}
            missing = DELIVER.audit_bank(index, expected_source_digests)
            self.assertEqual(len(missing.payload_errors), len(grains))
            for grain in grains:
                with wave.open(str(bank_root / f"{grain['id']}.wav"), "wb") as payload:
                    payload.setnchannels(1)
                    payload.setsampwidth(2)
                    payload.setframerate(48_000)
                    payload.writeframes(b"\0\0")
                grain["wav_sha256"] = BANK_CONTRACT.sha256(bank_root / f"{grain['id']}.wav")
            payload = {
                "schema": "danse.sound.bank.v1",
                "rate": 48_000,
                "sources": source_rows,
                "grains": grains,
            }
            payload["fingerprint"] = BANK_CONTRACT.bank_fingerprint(payload)
            index.write_text(json.dumps(payload))
            self.assertTrue(DELIVER.audit_bank(index, expected_source_digests).valid)
            stale_register = {**expected_source_digests, sources[0]: source_rows[1]["sha256"]}
            self.assertIn("do not match", DELIVER.audit_bank(index, stale_register).provenance_errors[-1])

            bad_rate = bank_root / f"{grains[0]['id']}.wav"
            with wave.open(str(bad_rate), "wb") as payload:
                payload.setnchannels(1)
                payload.setsampwidth(2)
                payload.setframerate(44_100)
                payload.writeframes(b"\0\0")
            self.assertIn("sample rate 44100", DELIVER.audit_bank(index, expected_source_digests).payload_errors[0])

            with wave.open(str(bad_rate), "wb") as payload:
                payload.setnchannels(1)
                payload.setsampwidth(2)
                payload.setframerate(48_000)
                payload.writeframes(b"\1\0")
            self.assertIn(
                f"changed {grains[0]['id']}.wav",
                DELIVER.audit_bank(index, expected_source_digests).payload_errors,
            )

    def test_malformed_cached_receipts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            score = root / "passage-score.wav"
            score.write_bytes(b"score")
            DELIVER.score_receipt_path(score).write_text("[]")
            self.assertIsNone(DELIVER.score_provenance(score, SPAN))
            DELIVER.score_receipt_path(score).write_text(
                json.dumps(
                    {
                        "schema": "danse.score.receipt.v1",
                        "sha256": DELIVER.digest(score),
                        "bank_fingerprint": "bank",
                        "sources": [],
                        "t0": "bad",
                    }
                )
            )
            self.assertIsNone(DELIVER.score_provenance(score, SPAN))

            package = root / "package"
            package.mkdir()
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "passage_seed": DELIVER.hexseed(SPAN["seed"]),
                        "passage": SPAN["passage"],
                        "t0": "bad",
                        "items": [{"name": "master.mov"}],
                    }
                )
            )
            self.assertFalse(DELIVER.package_provenance_matches(package, SPAN))

    def test_master_must_match_manifested_passage_and_digest(self) -> None:
        register = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            master = package / "master.mov"
            master.write_bytes(b"complete master")
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "duration": 312.54,
                        "items": [{"name": "master.mov", "sha256": "stale"}],
                    }
                )
            )
            info = {
                "width": 3840,
                "height": 2160,
                "fps": 30.0,
                "seconds": 4.0,
                "vcodec": "prores",
                "vprofile": "HQ",
                "acodec": "pcm_s24le",
                "channels": 2,
            }
            report = CHECK.Report()
            with mock.patch.object(CHECK, "probe", return_value=info):
                CHECK.check_master(register["package"]["master"], register, package, report)
            statuses = {name: status for _, name, status, _ in report.rows}
            self.assertEqual(statuses["master is one whole manifested passage"], CHECK.FAIL)
            self.assertEqual(statuses["master bytes match delivery manifest"], CHECK.FAIL)

            info["seconds"] = 312.54
            info["fps"] = 0.0
            with mock.patch.object(CHECK, "probe", return_value=info):
                report = CHECK.Report()
                CHECK.check_master(register["package"]["master"], register, package, report)
            statuses = {name: status for _, name, status, _ in report.rows}
            self.assertEqual(statuses["master is one whole manifested passage"], CHECK.FAIL)

    def test_screener_directly_matches_manifested_passage_and_digest(self) -> None:
        register = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            screener = package / "screener.mov"
            screener.write_bytes(b"complete screener")
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "duration": SPAN["duration"],
                        "items": [{"name": screener.name, "sha256": CHECK.sha256(screener)}],
                    }
                )
            )
            info = {
                "width": 1920,
                "height": 1080,
                "seconds": SPAN["duration"],
                "vcodec": "h264",
                "acodec": "aac",
                "channels": 2,
            }
            with mock.patch.object(CHECK, "probe", return_value=info):
                report = CHECK.Report()
                CHECK.check_screener(register["package"]["screener"], package, report)
            statuses = {name: status for _, name, status, _ in report.rows}
            self.assertEqual(statuses["screener is one whole manifested passage"], CHECK.PASS)
            self.assertEqual(statuses["screener bytes match delivery manifest"], CHECK.PASS)

            with mock.patch.object(CHECK, "probe", return_value=None):
                report = CHECK.Report()
                CHECK.check_screener(register["package"]["screener"], package, report)
            statuses = {name: status for _, name, status, _ in report.rows}
            self.assertEqual(statuses["screener bytes match delivery manifest"], CHECK.PASS)
            self.assertEqual(statuses["screener"], CHECK.OPEN)

            screener.write_bytes(b"replacement screener")
            info["seconds"] -= 1
            with mock.patch.object(CHECK, "probe", return_value=info):
                report = CHECK.Report()
                CHECK.check_screener(register["package"]["screener"], package, report)
            statuses = {name: status for _, name, status, _ in report.rows}
            self.assertEqual(statuses["screener is one whole manifested passage"], CHECK.FAIL)
            self.assertEqual(statuses["screener bytes match delivery manifest"], CHECK.FAIL)

    def test_seed_stills_match_their_manifested_bytes(self) -> None:
        register = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            stills = package / "stills"
            stills.mkdir()
            paths = []
            for i in range(6):
                path = stills / f"seed-0x{i:06X}.jpg"
                path.write_bytes(f"still {i}".encode())
                paths.append(path)
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {"name": f"stills/{path.name}", "sha256": CHECK.sha256(path)} for path in paths
                        ]
                    }
                )
            )
            with mock.patch.object(CHECK, "image_size", return_value=(3840, 2160)):
                report = CHECK.Report()
                CHECK.check_stills(register["package"]["stills"], package, report)
            statuses = {name: status for _, name, status, _ in report.rows}
            self.assertEqual(statuses["stills bytes match delivery manifest"], CHECK.PASS)

            paths[0].write_bytes(b"replacement still")
            with mock.patch.object(CHECK, "image_size", return_value=(3840, 2160)):
                report = CHECK.Report()
                CHECK.check_stills(register["package"]["stills"], package, report)
            statuses = {name: status for _, name, status, _ in report.rows}
            self.assertEqual(statuses["stills bytes match delivery manifest"], CHECK.FAIL)

    def test_audio_provenance_is_bound_to_each_artifact(self) -> None:
        register = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            for name in ("master.mov", "screener.mp4"):
                (package / name).touch()
            score = package / "provenance/passage-score.wav"
            score.parent.mkdir(parents=True)
            score.write_bytes(b"submission score fixture")
            current = submission_sound_identity(CHECK.sha256(score))
            stale = copy.deepcopy(current)
            stale["profile"] = "hybrid-apartment"
            manifest = {
                "t0": SPAN["t0"],
                "t1": SPAN["t1"],
                "duration": SPAN["duration"],
                "sound": current,
                "items": [
                    {
                        "name": "master.mov",
                        "sha256": CHECK.sha256(package / "master.mov"),
                        "sound": current,
                    },
                    {
                        "name": "screener.mp4",
                        "sha256": CHECK.sha256(package / "screener.mp4"),
                        "sound": stale,
                    },
                ],
            }
            bind_submission_score_receipt(package, manifest, current)
            (package / "manifest.json").write_text(json.dumps(manifest))
            report = CHECK.Report()
            with (
                mock.patch.object(CHECK, "loudness", return_value={"lufs": -16.0, "true_peak_dbtp": -1.1}),
                mock.patch.object(CHECK, "probe", return_value={"seconds": SPAN["duration"]}),
            ):
                CHECK.check_audio(register["package"]["audio"], package, report)
            row = next(row for row in report.rows if row[1] == "identical timed-audio sound identity")
            self.assertEqual(row[2], CHECK.FAIL)
            self.assertIn("screener.mp4 has a different sound identity", row[3])

            manifest = json.loads((package / "manifest.json").read_text())
            manifest["items"][1]["sound"] = copy.deepcopy(current)
            manifest["items"][1]["sound"]["score_contract_sha256"] = "9" * 64
            (package / "manifest.json").write_text(json.dumps(manifest))
            with (
                mock.patch.object(CHECK, "loudness", return_value={"lufs": -16.0, "true_peak_dbtp": -1.1}),
                mock.patch.object(CHECK, "probe", return_value={"seconds": SPAN["duration"]}),
            ):
                report = CHECK.Report()
                CHECK.check_audio(register["package"]["audio"], package, report)
            row = next(row for row in report.rows if row[1] == "identical timed-audio sound identity")
            self.assertEqual(row[2], CHECK.FAIL)
            self.assertIn("screener.mp4 has a different sound identity", row[3])

    def test_empty_audio_package_reports_its_actual_cause(self) -> None:
        register = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            report = CHECK.Report()
            CHECK.check_audio(register["package"]["audio"], Path(tmp), report)
        row = next(row for row in report.rows if row[1] == "identical timed-audio sound identity")
        self.assertEqual(row[2], CHECK.FAIL)
        self.assertIn("no audio artifact staged", row[3])

    def test_audio_receipts_bind_screener_bytes_and_passage_duration(self) -> None:
        register = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            master = package / "master.mxf"
            screener = package / "screener.mov"
            master.write_bytes(b"master")
            screener.write_bytes(b"screener")
            score = package / "provenance/passage-score.wav"
            score.parent.mkdir(parents=True)
            score.write_bytes(b"submission score fixture")
            sound = submission_sound_identity(CHECK.sha256(score))

            def write_manifest() -> None:
                manifest = {
                    "t0": SPAN["t0"],
                    "t1": SPAN["t1"],
                    "duration": SPAN["duration"],
                    "sound": sound,
                    "items": [
                        {"name": master.name, "sha256": CHECK.sha256(master), "sound": sound},
                        {"name": screener.name, "sha256": CHECK.sha256(screener), "sound": sound},
                    ],
                }
                bind_submission_score_receipt(package, manifest, sound)
                (package / "manifest.json").write_text(json.dumps(manifest))

            write_manifest()
            with (
                mock.patch.object(CHECK, "loudness", return_value={"lufs": -16.0, "true_peak_dbtp": -1.1}),
                mock.patch.object(CHECK, "probe", return_value={"seconds": SPAN["duration"]}),
            ):
                report = CHECK.Report()
                CHECK.check_audio(register["package"]["audio"], package, report)
            row = next(row for row in report.rows if row[1] == "identical timed-audio sound identity")
            self.assertEqual(row[2], CHECK.PASS)
            receipt_row = next(row for row in report.rows if row[1] == "copied score receipt v2 identity")
            self.assertEqual(receipt_row[2], CHECK.PASS)
            audio_render_row = next(
                row for row in report.rows if row[1] == "durable audio-render receipt identity"
            )
            self.assertEqual(audio_render_row[2], CHECK.PASS)

            audio_render_path = package / "provenance/audio-render.json"
            audio_render_path.write_text(
                '{"schema":"danse.audio.render.v1","fixture":"substituted"}\n'
            )
            with (
                mock.patch.object(CHECK, "loudness", return_value={"lufs": -16.0, "true_peak_dbtp": -1.1}),
                mock.patch.object(CHECK, "probe", return_value={"seconds": SPAN["duration"]}),
            ):
                report = CHECK.Report()
                CHECK.check_audio(register["package"]["audio"], package, report)
            audio_render_row = next(
                row for row in report.rows if row[1] == "durable audio-render receipt identity"
            )
            self.assertEqual(audio_render_row[2], CHECK.FAIL)
            self.assertIn("manifest digest is stale", audio_render_row[3])

            write_manifest()

            production_path = package / "provenance/production.json"
            production = json.loads(production_path.read_text())
            production["sound"] = copy.deepcopy(sound)
            production["sound"]["credit"] = "Incomplete music credit."
            production_path.write_text(json.dumps(production, indent=2) + "\n")
            manifest = json.loads((package / "manifest.json").read_text())
            manifest["production"]["sha256"] = CHECK.sha256(production_path)
            (package / "manifest.json").write_text(json.dumps(manifest))
            with (
                mock.patch.object(CHECK, "loudness", return_value={"lufs": -16.0, "true_peak_dbtp": -1.1}),
                mock.patch.object(CHECK, "probe", return_value={"seconds": SPAN["duration"]}),
            ):
                report = CHECK.Report()
                CHECK.check_audio(register["package"]["audio"], package, report)
            receipt_row = next(row for row in report.rows if row[1] == "copied score receipt v2 identity")
            self.assertEqual(receipt_row[2], CHECK.FAIL)
            self.assertIn("production receipt does not equal manifest.sound", receipt_row[3])

            write_manifest()

            production = json.loads(production_path.read_text())
            production["repository_head"] = "b" * 40
            production_path.write_text(json.dumps(production, indent=2) + "\n")
            manifest = json.loads((package / "manifest.json").read_text())
            manifest["production"]["sha256"] = CHECK.sha256(production_path)
            (package / "manifest.json").write_text(json.dumps(manifest))
            with (
                mock.patch.object(CHECK, "loudness", return_value={"lufs": -16.0, "true_peak_dbtp": -1.1}),
                mock.patch.object(CHECK, "probe", return_value={"seconds": SPAN["duration"]}),
            ):
                report = CHECK.Report()
                CHECK.check_audio(register["package"]["audio"], package, report)
            receipt_row = next(row for row in report.rows if row[1] == "copied score receipt v2 identity")
            self.assertEqual(receipt_row[2], CHECK.FAIL)
            self.assertIn("different repository head", receipt_row[3])

            write_manifest()

            screener.write_bytes(b"replaced screener")
            with (
                mock.patch.object(CHECK, "loudness", return_value={"lufs": -16.0, "true_peak_dbtp": -1.1}),
                mock.patch.object(CHECK, "probe", return_value={"seconds": SPAN["duration"]}),
            ):
                report = CHECK.Report()
                CHECK.check_audio(register["package"]["audio"], package, report)
            row = next(row for row in report.rows if row[1] == "identical timed-audio sound identity")
            self.assertEqual(row[2], CHECK.FAIL)
            self.assertIn("screener.mov digest is stale", row[3])

            write_manifest()
            with (
                mock.patch.object(CHECK, "loudness", return_value={"lufs": -16.0, "true_peak_dbtp": -1.1}),
                mock.patch.object(CHECK, "probe", return_value={"seconds": SPAN["duration"] - 10}),
            ):
                report = CHECK.Report()
                CHECK.check_audio(register["package"]["audio"], package, report)
            row = next(row for row in report.rows if row[1] == "identical timed-audio sound identity")
            self.assertEqual(row[2], CHECK.FAIL)
            self.assertIn("passage duration", row[3])

    def test_attestations_are_cumulative_by_owned_phase(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "attest.yaml").write_text("final-cut-only: true\n")
            expected = {"package": 3, "uploaded": 5, "submitted": 12}
            for phase, count in expected.items():
                report = CHECK.Report()
                CHECK.check_attestations(reg, root, phase, report)
                self.assertEqual(len(report.rows), count)
                self.assertEqual(report.failures, count - 1)

    def test_phase_receipts_are_cumulative_exact_and_close_elapsed_deadlines(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 9, 2, 12, tzinfo=ZoneInfo("America/New_York"))
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg)
            attested, attest_path = CHECK.read_attestations(package)
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                binding, _, identity_errors = CHECK.validate_package_identity(package)
            self.assertEqual(identity_errors, [])
            report = CHECK.Report()
            records = CHECK.check_phase_receipts(
                reg,
                package,
                "submitted",
                binding,
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            self.assertEqual(set(records), set(CHECK.PHASES))

            deadline = CHECK.Report()
            CHECK.check_deadline(reg, "submitted", deadline, now=now, receipts=records)
            self.assertEqual(deadline.failures, 0)
            self.assertIn(
                "18:00 EDT",
                next(row for row in deadline.rows if row[1] == "upload target")[3],
            )

    def test_phase_receipt_chain_cannot_predate_the_frozen_opportunity(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 9, 2, 12, tzinfo=ZoneInfo("America/New_York"))
        historical = {
            "package": "2020-08-31T20:00:00Z",
            "uploaded": "2020-08-31T21:00:00Z",
            "submitted": "2020-08-31T22:00:00Z",
        }
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg)
            for phase in CHECK.PHASES:
                path = package / CHECK.PHASE_RECEIPTS[phase]
                receipt = json.loads(path.read_text())
                receipt["recorded_at"] = historical[phase]
                if phase == "uploaded":
                    receipt["upload"]["uploaded_at"] = historical[phase]
                elif phase == "submitted":
                    receipt["submission"]["submitted_at"] = historical[phase]
                if phase != "package":
                    prior = CHECK.PHASES[CHECK.PHASES.index(phase) - 1]
                    prior_path = package / CHECK.PHASE_RECEIPTS[prior]
                    prior_receipt = json.loads(prior_path.read_text())
                    receipt["prior_receipt"] = {
                        "phase": prior,
                        "path": CHECK.PHASE_RECEIPTS[prior],
                        "sha256": CHECK.sha256(prior_path),
                        "receipt_id": prior_receipt["receipt_id"],
                    }
                path.write_text(json.dumps(receipt, indent=2) + "\n")

            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            records = CHECK.check_phase_receipts(
                reg,
                package,
                "submitted",
                submission_package_binding(package),
                attested,
                attest_path,
                report,
                now=now,
            )
            package_row = next(row for row in report.rows if row[1] == "package receipt")
            self.assertEqual(package_row[2], CHECK.FAIL)
            self.assertIn("predates the frozen opportunity snapshot", package_row[3])
            self.assertEqual(records, {})

            deadline = CHECK.Report()
            CHECK.check_deadline(reg, "submitted", deadline, now=now, receipts=records)
            self.assertEqual(deadline.failures, 2)

    def test_generated_full_attestation_binds_package_and_rejects_premature_rights_values(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        generated = CHECK.parse_attestation_document(
            DELIVER.attestation_template(),
            "generated package attestation",
        )
        contracts = CHECK.full_attestation_contracts(reg)
        self.assertEqual(set(generated), set(contracts))
        self.assertGreater(len(contracts), len(CHECK.assertion_contracts(reg, "submitted")))
        rights_only = set(contracts) - set(CHECK.assertion_contracts(reg, "submitted"))
        self.assertEqual(
            {key: contracts[key]["phase"] for key in rights_only},
            {
                "submission-copy-approved": "package",
                "dancer-release-and-credit": "package",
                "pictured-objects-reviewed": "package",
                "music-cleared": "package",
                "press-stills-cleared": "submitted",
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(
                package,
                reg,
                through="package",
                values=copy.deepcopy(generated),
            )
            attested, attest_path = CHECK.read_attestations(package)
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                binding, _, identity_errors = CHECK.validate_package_identity(package)
            self.assertEqual(identity_errors, [])
            report = CHECK.Report()
            CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                binding,
                attested,
                attest_path,
                report,
                now=datetime(2026, 8, 31, 20, tzinfo=timezone.utc),
            )
            self.assertEqual(report.failures, 0)
            self.assertEqual(
                RIGHTS.validate_attestation(RIGHTS.load_register(), attested),
                [],
            )

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            premature = copy.deepcopy(generated)
            premature["press-stills-cleared"] = True
            build_submission_receipt_chain(
                package,
                reg,
                through="package",
                values=premature,
            )
            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                submission_package_binding(package),
                attested,
                attest_path,
                report,
                now=datetime(2026, 8, 31, 20, tzinfo=timezone.utc),
            )
            row = next(item for item in report.rows if item[1] == "package receipt")
            self.assertEqual(row[2], CHECK.FAIL)
            self.assertIn(
                "prematurely asserts later-phase gate press-stills-cleared",
                row[3],
            )

    def test_score_motion_manifest_reference_is_typed_and_rights_censused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            evidence = package / "provenance/score-to-motion/score-to-motion-production.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text('{"schema":"fixture"}\n')
            manifest = {
                "schema": "danse.delivery.manifest.v1",
                "title": "THE THING WITHOUT A NAME",
                "seed": "0x0133D62C",
                "repository_head": SUBMISSION_REPOSITORY_HEAD,
                "score_motion_evidence": {
                    "path": evidence.relative_to(package).as_posix(),
                    "sha256": CHECK.sha256(evidence),
                },
                "items": [
                    {
                        "name": evidence.relative_to(package).as_posix(),
                        "bytes": evidence.stat().st_size,
                        "sha256": CHECK.sha256(evidence),
                    }
                ],
            }
            (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                _, _, errors = CHECK.validate_package_identity(package)
            self.assertEqual(errors, [])

        rights = RIGHTS.load_register()
        paths = (
            "provenance/score-to-motion/score-to-motion-production.json",
            "provenance/score-to-motion/boundary-frames/sample-000-control.png",
        )
        for path in paths:
            matched = [
                rule["id"]
                for rule in rights["package_rules"]
                if re.fullmatch(rule["pattern"], path)
            ]
            self.assertEqual(matched, ["score-motion-evidence"])
        escaped = "provenance/score-to-motion/../outside.json"
        self.assertFalse(
            any(re.fullmatch(rule["pattern"], escaped) for rule in rights["package_rules"])
        )

    def test_missing_stale_or_replayed_phase_receipts_fail_closed(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 9, 2, 12, tzinfo=ZoneInfo("America/New_York"))
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="uploaded")
            attested, attest_path = CHECK.read_attestations(package)
            binding = submission_package_binding(package)

            missing = CHECK.Report()
            CHECK.check_phase_receipts(
                reg,
                package,
                "submitted",
                binding,
                attested,
                attest_path,
                missing,
                now=now,
            )
            self.assertEqual(missing.rows[-1][2], CHECK.FAIL)
            self.assertIn("missing", missing.rows[-1][3])

            package_receipt = package / CHECK.PHASE_RECEIPTS["package"]
            package_receipt.write_text(package_receipt.read_text() + "\n")
            stale = CHECK.Report()
            CHECK.check_phase_receipts(
                reg,
                package,
                "uploaded",
                binding,
                attested,
                attest_path,
                stale,
                now=now,
            )
            self.assertEqual(stale.rows[-1][2], CHECK.FAIL)
            self.assertIn("prior-phase receipt binding", stale.rows[-1][3])

            manifest = json.loads((package / "manifest.json").read_text())
            manifest["title"] = "replayed package"
            (package / "manifest.json").write_text(json.dumps(manifest) + "\n")
            replay = CHECK.Report()
            CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                submission_package_binding(package),
                attested,
                attest_path,
                replay,
                now=now,
            )
            self.assertGreaterEqual(replay.failures, 1)
            replay_package = next(row for row in replay.rows if row[1] == "package receipt")
            self.assertIn("different package", replay_package[3])

    def test_prior_receipt_embedded_attestation_is_independently_authenticated(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 9, 2, 12, tzinfo=ZoneInfo("America/New_York"))
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="uploaded")
            prior_path = package / CHECK.PHASE_RECEIPTS["package"]
            prior = json.loads(prior_path.read_text())
            prior["attestation"]["sha256"] = "1" * 64
            prior["attestation"]["canonical_sha256"] = "2" * 64
            prior_path.write_text(json.dumps(prior, indent=2) + "\n")
            uploaded_path = package / CHECK.PHASE_RECEIPTS["uploaded"]
            uploaded = json.loads(uploaded_path.read_text())
            uploaded["prior_receipt"]["sha256"] = CHECK.sha256(prior_path)
            uploaded_path.write_text(json.dumps(uploaded, indent=2) + "\n")

            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            CHECK.check_phase_receipts(
                reg,
                package,
                "uploaded",
                submission_package_binding(package),
                attested,
                attest_path,
                report,
                now=now,
            )
            package_row = next(row for row in report.rows if row[1] == "package receipt")
            uploaded_row = next(row for row in report.rows if row[1] == "uploaded receipt")
            self.assertEqual(package_row[2], CHECK.FAIL)
            self.assertIn("digest is stale", package_row[3])
            self.assertEqual(uploaded_row[2], CHECK.FAIL)
            self.assertIn("invalid prior-phase", uploaded_row[3])

    def test_phase_receipt_rejects_an_arbitrary_signer_with_the_right_role(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="package")
            receipt_path = package / CHECK.PHASE_RECEIPTS["package"]
            receipt = json.loads(receipt_path.read_text())
            receipt["signer"]["name"] = "Mallory Impostor"
            receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                submission_package_binding(package),
                attested,
                attest_path,
                report,
                now=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )
            self.assertEqual(report.rows[0][2], CHECK.FAIL)
            self.assertIn("canonical phase authority", report.rows[0][3])

    def test_upload_receipt_binds_host_asset_and_manifested_screener_digest(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        mutations = {
            "provider": lambda upload: upload.__setitem__("provider", "example.com"),
            "asset": lambda upload: upload.__setitem__("asset_id", "unrelated.asset"),
            "digest": lambda upload: upload.__setitem__("sha256", "9" * 64),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                build_submission_receipt_chain(package, reg, through="uploaded")
                receipt_path = package / CHECK.PHASE_RECEIPTS["uploaded"]
                receipt = json.loads(receipt_path.read_text())
                mutate(receipt["upload"])
                receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
                attested, attest_path = CHECK.read_attestations(package)
                report = CHECK.Report()
                CHECK.check_phase_receipts(
                    reg,
                    package,
                    "uploaded",
                    submission_package_binding(package),
                    attested,
                    attest_path,
                    report,
                    now=datetime(2026, 9, 2, tzinfo=timezone.utc),
                )
                self.assertEqual(report.rows[-1][2], CHECK.FAIL)

    def test_receipt_deadline_events_enforce_exact_target_and_wall_boundaries(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 9, 2, 12, tzinfo=ZoneInfo("America/New_York"))
        cases = (
            ("uploaded", "2026-08-31T22:00:00Z", CHECK.PASS),
            ("uploaded", "2026-08-31T22:00:01Z", CHECK.PASS),
            ("submitted", "2026-09-01T02:00:00Z", CHECK.PASS),
            ("submitted", "2026-09-01T02:00:01Z", CHECK.PASS),
        )
        for phase, event_at, expected in cases:
            with self.subTest(phase=phase, event_at=event_at), tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                values = build_submission_receipt_chain(package, reg, through=phase)
                write_submission_phase_receipt(
                    package,
                    reg,
                    values,
                    phase,
                    event_at,
                    event_at=event_at,
                )
                attested, attest_path = CHECK.read_attestations(package)
                report = CHECK.Report()
                CHECK.check_phase_receipts(
                    reg,
                    package,
                    phase,
                    submission_package_binding(package),
                    attested,
                    attest_path,
                    report,
                    now=now,
                )
                self.assertEqual(report.rows[-1][2], expected)

                if phase == "uploaded" and event_at.endswith("01Z"):
                    deadline = CHECK.Report()
                    CHECK.check_deadline(
                        reg,
                        phase,
                        deadline,
                        now=now,
                        receipts={
                            phase: {
                                "event_at": datetime.fromisoformat(event_at.replace("Z", "+00:00"))
                            }
                        },
                    )
                    target = next(row for row in deadline.rows if row[1] == "upload target")
                    self.assertEqual(target[2], CHECK.FAIL)
                if phase == "submitted" and event_at.endswith("01Z"):
                    deadline = CHECK.Report()
                    CHECK.check_deadline(
                        reg,
                        phase,
                        deadline,
                        now=now,
                        receipts={
                            phase: {
                                "event_at": datetime.fromisoformat(event_at.replace("Z", "+00:00"))
                            }
                        },
                    )
                    wall = next(row for row in deadline.rows if row[1] == "hard wall")
                    self.assertEqual(wall[2], CHECK.FAIL)

    def test_receipt_parsers_reject_duplicate_keys_bad_urls_and_duplicate_yaml(self) -> None:
        schema = json.loads((ROOT / "submission/receipt.schema.json").read_text())
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(
            {
                phase: schema["$defs"][phase + "Receipt"]["properties"]["schema"]["const"]
                for phase in CHECK.PHASES
            },
            CHECK.PHASE_RECEIPT_SCHEMAS,
        )
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            receipt = package / "receipt.json"
            receipt.write_text('{"schema":"first","schema":"second"}\n')
            with self.assertRaisesRegex(ValueError, "repeats JSON field"):
                CHECK.read_contract_json(package, "receipt.json", "receipt")

            with (
                mock.patch.object(CHECK.json, "loads", side_effect=RecursionError),
                self.assertRaisesRegex(ValueError, "readable unique-key JSON"),
            ):
                CHECK.unique_json_bytes(b"{}", "receipt")

            (package / "attest.yaml").write_text("final-cut-only: false\nfinal-cut-only: true\n")
            with self.assertRaisesRegex(ValueError, "unique-key YAML"):
                CHECK.read_attestations(package)

            (package / "attest.yaml").write_text("? [a, b]\n: true\n")
            with self.assertRaisesRegex(ValueError, "unique-key YAML"):
                CHECK.read_attestations(package)

        for url in (
            "http://vimeo.com/123",
            "https://user:secret@vimeo.com/123",
            "https://127.0.0.1/123",
            "https://vimeo.com/123?password=secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                CHECK.https_url(url, "upload")

    def test_receipt_schema_is_executed_and_fails_closed_on_drift(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="submitted")
            receipt = json.loads((package / CHECK.PHASE_RECEIPTS["package"]).read_text())
            self.assertEqual(CHECK.receipt_schema_errors(receipt, "package receipt"), [])
            receipt["unexpected"] = True
            self.assertTrue(CHECK.receipt_schema_errors(receipt, "package receipt"))

            uploaded = json.loads((package / CHECK.PHASE_RECEIPTS["uploaded"]).read_text())
            self.assertEqual(CHECK.receipt_schema_errors(uploaded, "uploaded receipt"), [])
            uploaded["signer"]["role"] = CHECK.PHASE_SIGNER_ROLES["submitted"]
            self.assertTrue(CHECK.receipt_schema_errors(uploaded, "uploaded receipt"))
            uploaded["signer"]["role"] = CHECK.PHASE_SIGNER_ROLES["uploaded"]
            uploaded["prior_receipt"]["phase"] = "uploaded"
            uploaded["prior_receipt"]["path"] = CHECK.PHASE_RECEIPTS["uploaded"]
            self.assertTrue(CHECK.receipt_schema_errors(uploaded, "uploaded receipt"))

            submitted = json.loads((package / CHECK.PHASE_RECEIPTS["submitted"]).read_text())
            self.assertEqual(CHECK.receipt_schema_errors(submitted, "submitted receipt"), [])
            submitted["prior_receipt"]["phase"] = "package"
            submitted["prior_receipt"]["path"] = CHECK.PHASE_RECEIPTS["package"]
            self.assertTrue(CHECK.receipt_schema_errors(submitted, "submitted receipt"))

            schema = json.loads((ROOT / "submission/receipt.schema.json").read_text())
            row_validator = CHECK.jsonschema.Draft202012Validator(
                {"$ref": "#/$defs/phaseReceiptRow", "$defs": schema["$defs"]}
            )
            mismatched_row = {
                "phase": "package",
                "path": CHECK.PHASE_RECEIPTS["uploaded"],
                "sha256": "0" * 64,
                "receipt_id": "package-receipt-001",
            }
            self.assertTrue(list(row_validator.iter_errors(mismatched_row)))

            rows = [
                {
                    "phase": phase,
                    "path": CHECK.PHASE_RECEIPTS[phase],
                    "sha256": str(index) * 64,
                    "receipt_id": f"{phase}-receipt-001",
                }
                for index, phase in enumerate(CHECK.PHASES, start=1)
            ]
            validation_receipt = {
                "schema": CHECK.DONE_RECEIPT_SCHEMA,
                "scope": CHECK.DONE_RECEIPT_SCOPE,
                "phase": "submitted",
                "validated_at": "2026-08-30T05:00:00Z",
                "repository_head": SUBMISSION_REPOSITORY_HEAD,
                "package": {
                    "manifest": "manifest.json",
                    "manifest_sha256": "4" * 64,
                    "repository_head": SUBMISSION_REPOSITORY_HEAD,
                },
                "phase_receipts": rows,
                "predicates": ["python3 submission/check.py --package package --phase submitted"],
            }
            validation_validator = CHECK.jsonschema.Draft202012Validator(
                {"$ref": "#/$defs/validationReceipt", "$defs": schema["$defs"]}
            )
            self.assertEqual(list(validation_validator.iter_errors(validation_receipt)), [])
            short_chain = copy.deepcopy(validation_receipt)
            short_chain["phase_receipts"].pop()
            self.assertTrue(list(validation_validator.iter_errors(short_chain)))
            reversed_chain = copy.deepcopy(validation_receipt)
            reversed_chain["phase_receipts"].reverse()
            self.assertTrue(list(validation_validator.iter_errors(reversed_chain)))

            schema_path = package / "invalid-receipt.schema.json"
            schema_path.write_text('{"$schema":"https://json-schema.org/draft/2020-12/schema","type":5}\n')
            with mock.patch.object(CHECK, "RECEIPT_SCHEMA", schema_path):
                errors = CHECK.receipt_schema_errors(receipt, "package receipt")
            self.assertTrue(any("SchemaError" in error for error in errors))

    def test_destination_safe_https_rejects_ambiguous_or_nonpublic_authorities(self) -> None:
        for url in (
            "https://vimeo.com/123",
            "https://vimeo.com:443/123",
            "https://media.example.org:8443/upload",
        ):
            with self.subTest(valid=url):
                self.assertEqual(CHECK.destination_safe_https(url, "destination"), url)

        invalid = (
            "https://vimeo.com\\@127.0.0.1/123",
            "https://example..org/upload",
            "https://-example.org/upload",
            "https://example-.org/upload",
            "https://exa_mple.org/upload",
            "https://%65xample.org/upload",
            "https://example.org./upload",
            "https://éxample.org/upload",
            "https://xn--xample-9ua.org/upload",
            "https://[v1.fe80]/upload",
            "https://[2606:4700:4700::1111%25eth0]/upload",
            "https://example.org:/upload",
            "https://example.org:not-a-port/upload",
            "https://example.org:0/upload",
            "https://example.org:65536/upload",
            "https://vimeo.com:99999/upload",
            "https://@example.org/upload",
            "https://user@example.org/upload",
            "https://user:secret@example.org/upload",
            "https://example.org/upload?",
            "https://example.org/upload?token=secret",
            "https://example.org/upload#",
            "https://example.org/upload#confirmation",
            "https://127.0.0.1/upload",
            "https://127.1/upload",
            "https://2130706433/upload",
            "https://0177.0.0.1/upload",
            "https://0x7f.0x0.0x0.0x1/upload",
            "https://10.0.0.1/upload",
            "https://[::1]/upload",
            "https://localhost./upload",
            "https://portal.local/upload",
            "https://portal.internal/upload",
            "https://intranet/upload",
        )
        for url in invalid:
            with self.subTest(invalid=url), self.assertRaises(ValueError):
                CHECK.destination_safe_https(url, "destination")

    def test_package_identity_rejects_empty_dirty_stale_and_duplicate_manifests(self) -> None:
        cases = ("empty", "dirty", "stale")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                item = package / "item.txt"
                item.write_text("bytes\n")
                manifest = {
                    "schema": "danse.delivery.manifest.v1",
                    "title": "THE THING WITHOUT A NAME",
                    "seed": "0x0133D62C",
                    "repository_head": SUBMISSION_REPOSITORY_HEAD,
                    "items": []
                    if case == "empty"
                    else [
                        {
                            "name": item.name,
                            "bytes": item.stat().st_size,
                            "sha256": "0" * 64 if case == "stale" else CHECK.sha256(item),
                        }
                    ],
                }
                (package / "manifest.json").write_text(json.dumps(manifest))
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, case != "dirty"),
                ):
                    _, _, errors = CHECK.validate_package_identity(package)
                self.assertTrue(errors)

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "manifest.json").write_text(
                '{"schema":"danse.delivery.manifest.v1","items":[],"items":[]}\n'
            )
            _, _, errors = CHECK.validate_package_identity(package)
            self.assertIn("repeats JSON field", errors[0])

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            manifest = {
                "schema": "danse.delivery.manifest.v1",
                "title": "THE THING WITHOUT A NAME",
                "seed": "0x0133D62C",
                "repository_head": SUBMISSION_REPOSITORY_HEAD,
                "items": [{"name": "/absolute.mp4", "bytes": 1, "sha256": "0" * 64}],
            }
            (package / "manifest.json").write_text(json.dumps(manifest) + "\n")
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                _, _, errors = CHECK.validate_package_identity(package)
            self.assertTrue(any("contract root" in error for error in errors), errors)

    def test_package_identity_rejects_unmanifested_files_directories_and_symlinks(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        cases = ("file", "directory", "symlink")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                build_submission_receipt_chain(package, reg, through="package")
                if case == "file":
                    (package / "PRIVATE-UNMANIFESTED.txt").write_text("must not escape\n")
                elif case == "directory":
                    (package / "unknown-empty-directory").mkdir()
                else:
                    (package / "unmanifested-link").symlink_to(package / "screener.mp4")
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    _, _, errors = CHECK.validate_package_identity(package)
                self.assertTrue(any("package surface" in error for error in errors), errors)

    def test_package_identity_rejects_arbitrary_bytes_at_known_receipt_paths(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        for relative in (
            CHECK.PHASE_RECEIPTS["submitted"],
            CHECK.DONE_RECEIPTS["package"],
            CHECK.DONE_RECEIPTS["submitted"],
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                build_submission_receipt_chain(package, reg, through="package")
                receipt_path = package / relative
                receipt_path.parent.mkdir(exist_ok=True)
                receipt_path.write_bytes(b"PRIVATE ARBITRARY RECEIPT BYTES\n")
                if relative in CHECK.DONE_RECEIPTS.values():
                    receipt_path.chmod(0o400)
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    _, _, errors = CHECK.validate_package_identity(package)
                self.assertTrue(any("not readable unique-key JSON" in error for error in errors), errors)

    def test_selected_phase_rejects_later_receipts_and_semantically_checks_local_receipts(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="submitted")
            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                submission_package_binding(package),
                attested,
                attest_path,
                report,
                now=now,
            )
            bounded = next(row for row in report.rows if row[1] == "phase-bounded receipt surface")
            self.assertEqual(bounded[2], CHECK.FAIL)
            self.assertIn(CHECK.PHASE_RECEIPTS["submitted"], bounded[3])

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="package")
            attested, attest_path = CHECK.read_attestations(package)
            records_report = CHECK.Report()
            records = CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                submission_package_binding(package),
                attested,
                attest_path,
                records_report,
                now=now,
            )
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                done_path, _ = CHECK.write_done_receipt(
                    package,
                    "package",
                    submission_package_binding(package),
                    records,
                    now=now,
                )
            done = json.loads(done_path.read_text())
            done["package"]["manifest_sha256"] = "0" * 64
            done_path.chmod(0o600)
            done_path.write_text(json.dumps(done, indent=2) + "\n")
            done_path.chmod(0o400)
            report = CHECK.Report()
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                CHECK.check_phase_receipts(
                    reg,
                    package,
                    "package",
                    submission_package_binding(package),
                    attested,
                    attest_path,
                    report,
                    now=now,
                )
            local = next(row for row in report.rows if row[1] == "validated-package receipt")
            self.assertEqual(local[2], CHECK.FAIL)
            self.assertIn("different package", local[3])

    def test_machine_done_receipt_binds_and_rechecks_the_human_chain(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="package")
            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            records = CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                submission_package_binding(package),
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                done_path, done_digest = CHECK.write_done_receipt(
                    package,
                    "package",
                    submission_package_binding(package),
                    records,
                    now=now,
                )
            self.assertEqual(done_digest, CHECK.sha256(done_path))
            value = json.loads(done_path.read_text())
            self.assertEqual(value["scope"], CHECK.DONE_RECEIPT_SCOPE)
            self.assertEqual(len(value["predicates"]), 1)
            self.assertNotIn("check-danse", value["predicates"][0])
            self.assertNotIn("browser.py", value["predicates"][0])
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                self.assertEqual(
                    CHECK.validate_done_receipt(
                        value,
                        done_path,
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    ),
                    [],
                )
            phase_receipt = package / CHECK.PHASE_RECEIPTS["package"]
            phase_receipt.write_text(phase_receipt.read_text() + "\n")
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                errors = CHECK.validate_done_receipt(
                    value,
                    done_path,
                    package,
                    "package",
                    submission_package_binding(package),
                    records,
                    now=now,
                )
            self.assertTrue(any("changed" in error for error in errors))

    def test_ordinary_done_receipt_validation_requires_private_single_link_bytes(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)
        for case in (
            "world-writable",
            "external-hardlink",
            "unsafe-directory",
            "unsafe-package-root",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                boundary = Path(tmp)
                package = boundary / "package"
                package.mkdir()
                build_submission_receipt_chain(package, reg, through="package")
                attested, attest_path = CHECK.read_attestations(package)
                binding = submission_package_binding(package)
                initial = CHECK.Report()
                records = CHECK.check_phase_receipts(
                    reg,
                    package,
                    "package",
                    binding,
                    attested,
                    attest_path,
                    initial,
                    now=now,
                )
                self.assertEqual(initial.failures, 0)
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    done_path, _ = CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                done_value = json.loads(done_path.read_text())
                if case == "world-writable":
                    done_path.chmod(0o666)
                elif case == "external-hardlink":
                    os.link(done_path, boundary / "external-validation-receipt.json")
                elif case == "unsafe-directory":
                    done_path.parent.chmod(0o777)
                else:
                    package.chmod(0o777)

                with self.assertRaisesRegex(ValueError, "unsafe|wrong mode"):
                    CHECK.read_contract_json(
                        package,
                        CHECK.DONE_RECEIPTS["package"],
                        "validated-package receipt",
                    )
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    direct_errors = CHECK.validate_done_receipt(
                        done_value,
                        done_path,
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                self.assertTrue(
                    any("unsafe" in error or "wrong mode" in error for error in direct_errors),
                    direct_errors,
                )
                report = CHECK.Report()
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    CHECK.check_phase_receipts(
                        reg,
                        package,
                        "package",
                        binding,
                        attested,
                        attest_path,
                        report,
                        now=now,
                    )
                local = next(
                    row for row in report.rows if row[1] == "validated-package receipt"
                )
                self.assertEqual(local[2], CHECK.FAIL)
                self.assertIn("unsafe", local[3])

    def test_done_receipt_fullsyncs_on_darwin_before_atomic_publication(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)

        def ready_package(root: Path) -> dict[str, dict[str, object]]:
            build_submission_receipt_chain(root, reg, through="package")
            attested, attest_path = CHECK.read_attestations(root)
            report = CHECK.Report()
            records = CHECK.check_phase_receipts(
                reg,
                root,
                "package",
                submission_package_binding(root),
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            return records

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            records = ready_package(package)
            events: list[object] = []
            fullsync = mock.Mock()
            fullsync.fcntl.side_effect = lambda _descriptor, operation: events.append(
                ("fullsync", operation)
            )
            real_link = CHECK.exclusive_receipt_link

            def link(source, destination, directory_fd):
                events.append("link")
                return real_link(source, destination, directory_fd)

            with (
                mock.patch.object(CHECK.sys, "platform", "darwin"),
                mock.patch.object(CHECK, "fcntl", fullsync),
                mock.patch.object(CHECK, "exclusive_receipt_link", side_effect=link),
                mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ),
            ):
                CHECK.write_done_receipt(
                    package,
                    "package",
                    submission_package_binding(package),
                    records,
                    now=now,
                )
            self.assertEqual(events[0], ("fullsync", CHECK.F_FULLFSYNC))
            self.assertEqual(events[1], "link")

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            records = ready_package(package)
            blocked_fullsync = mock.Mock()
            blocked_fullsync.fcntl.side_effect = OSError("injected fullsync failure")
            with (
                mock.patch.object(CHECK.sys, "platform", "darwin"),
                mock.patch.object(CHECK, "fcntl", blocked_fullsync),
                mock.patch.object(CHECK, "exclusive_receipt_link") as link,
                mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ),
                self.assertRaisesRegex(OSError, "injected fullsync failure"),
            ):
                CHECK.write_done_receipt(
                    package,
                    "package",
                    submission_package_binding(package),
                    records,
                    now=now,
                )
            link.assert_not_called()
            self.assertFalse((package / CHECK.DONE_RECEIPTS["package"]).exists())

    def test_done_receipt_dirfd_rail_rejects_receipts_symlink_swap(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            boundary = Path(tmp)
            package = boundary / "package"
            outside = boundary / "outside"
            parked = boundary / "parked-receipts"
            package.mkdir()
            outside.mkdir()
            build_submission_receipt_chain(package, reg, through="package")
            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            records = CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                submission_package_binding(package),
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            real_sync = CHECK.sync_regular_descriptor
            swapped = False

            def swap_after_sync(descriptor):
                nonlocal swapped
                real_sync(descriptor)
                if not swapped:
                    (package / "receipts").rename(parked)
                    (package / "receipts").symlink_to(outside, target_is_directory=True)
                    swapped = True

            try:
                with (
                    mock.patch.object(CHECK, "sync_regular_descriptor", side_effect=swap_after_sync),
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(
                        (OSError, ValueError),
                        "directory changed|publication boundary|package changed",
                    ),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )
                self.assertFalse((outside / "validated-package.json").exists())
                self.assertFalse((parked / "validated-package.json").exists())
                self.assertEqual(list(outside.iterdir()), [])
                self.assertFalse(any(path.name.endswith(".tmp") for path in parked.iterdir()))
            finally:
                linked = package / "receipts"
                if linked.is_symlink():
                    linked.unlink()
                if parked.exists():
                    parked.rename(linked)

    def test_done_receipt_validates_the_pinned_root_during_a_transient_clone_swap(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            boundary = Path(tmp)
            package = boundary / "package"
            replacement = boundary / "replacement"
            parked = boundary / "parked-package"
            package.mkdir()
            build_submission_receipt_chain(package, reg, through="package")
            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            binding = submission_package_binding(package)
            records = CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                binding,
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            shutil.copytree(package, replacement)
            (package / "screener.mp4").write_bytes(b"corrupt pinned package bytes")

            real_repository_state = CHECK.repository_state
            real_descriptor_file = CHECK.descriptor_file_identity
            swapped = False
            restored = False

            def swap_to_valid_clone(*args, **kwargs):
                nonlocal swapped
                if not swapped:
                    package.rename(parked)
                    replacement.rename(package)
                    swapped = True
                return SUBMISSION_REPOSITORY_HEAD, True

            def restore_before_item_result(root_fd, relative, label, **kwargs):
                nonlocal restored
                result = real_descriptor_file(root_fd, relative, label, **kwargs)
                if relative == "screener.mp4" and swapped and not restored:
                    package.rename(replacement)
                    parked.rename(package)
                    restored = True
                return result

            try:
                with (
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        side_effect=swap_to_valid_clone,
                    ),
                    mock.patch.object(
                        CHECK,
                        "descriptor_file_identity",
                        side_effect=restore_before_item_result,
                    ),
                    self.assertRaisesRegex(ValueError, "digest is stale"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                self.assertTrue(swapped)
                self.assertTrue(restored)
                self.assertFalse((package / CHECK.DONE_RECEIPTS["package"]).exists())
                self.assertFalse((replacement / CHECK.DONE_RECEIPTS["package"]).exists())
            finally:
                CHECK.repository_state = real_repository_state
                if package.exists() and parked.exists():
                    package.rename(replacement)
                    parked.rename(package)
                elif not package.exists() and parked.exists():
                    parked.rename(package)

    def test_done_receipt_atomic_commit_is_never_simulated_as_rollback(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)

        def ready_package(root: Path) -> dict[str, dict[str, object]]:
            build_submission_receipt_chain(root, reg, through="package")
            attested, attest_path = CHECK.read_attestations(root)
            report = CHECK.Report()
            records = CHECK.check_phase_receipts(
                reg,
                root,
                "package",
                submission_package_binding(root),
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            return records

        with self.subTest("directory durability failure"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                records = ready_package(package)
                destination = package / CHECK.DONE_RECEIPTS["package"]
                real_fsync = os.fsync
                def fail_publication_sync(descriptor):
                    info = os.fstat(descriptor)
                    if stat.S_ISDIR(info.st_mode) and destination.exists():
                        raise OSError("injected post-link directory sync failure")
                    return real_fsync(descriptor)

                with (
                    mock.patch.object(CHECK.os, "fsync", side_effect=fail_publication_sync),
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(OSError, "indeterminate after atomic commit"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )
                self.assertTrue(destination.is_file())
                self.assertFalse(any(path.name.endswith(".tmp") for path in destination.parent.iterdir()))

        with self.subTest("post-link directory rebinding"):
            with tempfile.TemporaryDirectory() as tmp:
                boundary = Path(tmp)
                package = boundary / "package"
                outside = boundary / "outside"
                parked = boundary / "parked-receipts"
                package.mkdir()
                outside.mkdir()
                records = ready_package(package)
                real_link = CHECK.exclusive_receipt_link

                def swap_during_link(source, destination, directory_fd):
                    (package / "receipts").rename(parked)
                    (package / "receipts").symlink_to(outside, target_is_directory=True)
                    return real_link(source, destination, directory_fd)

                try:
                    with (
                        mock.patch.object(
                            CHECK,
                            "exclusive_receipt_link",
                            side_effect=swap_during_link,
                        ),
                        mock.patch.object(
                            CHECK,
                            "repository_state",
                            return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                        ),
                        self.assertRaisesRegex(OSError, "indeterminate after atomic commit"),
                    ):
                        CHECK.write_done_receipt(
                            package,
                            "package",
                            submission_package_binding(package),
                            records,
                            now=now,
                        )
                    self.assertTrue((parked / "validated-package.json").is_file())
                    self.assertEqual(list(outside.iterdir()), [])
                    self.assertFalse(any(path.name.endswith(".tmp") for path in parked.iterdir()))
                finally:
                    linked = package / "receipts"
                    if linked.is_symlink():
                        linked.unlink()
                    if parked.exists():
                        parked.rename(linked)

        with self.subTest("semantic validation failure"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                records = ready_package(package)
                destination = package / CHECK.DONE_RECEIPTS["package"]
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )
                prior = destination.read_bytes()
                prior_inode = destination.stat().st_ino
                with (
                    mock.patch.object(
                        CHECK,
                        "validate_done_receipt",
                        return_value=["injected pre-publication validation failure"],
                    ),
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(ValueError, "pre-publication validation failure"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )
                self.assertEqual(destination.read_bytes(), prior)
                self.assertEqual(destination.stat().st_ino, prior_inode)
                self.assertFalse(any(path.name.endswith(".tmp") for path in destination.parent.iterdir()))

        with self.subTest("staged bytes change during final package census"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                records = ready_package(package)
                destination = package / CHECK.DONE_RECEIPTS["package"]
                real_validate = CHECK.validate_pinned_publication_inputs
                calls = 0

                def mutate_during_final_census(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    result = real_validate(*args, **kwargs)
                    if calls == 2:
                        staged = [
                            path
                            for path in (package / "receipts").iterdir()
                            if path.name.endswith(".tmp")
                        ]
                        self.assertEqual(len(staged), 1)
                        staged[0].chmod(0o600)
                        staged[0].write_bytes(b"mutated staged receipt")
                    return result

                with (
                    mock.patch.object(
                        CHECK,
                        "validate_pinned_publication_inputs",
                        side_effect=mutate_during_final_census,
                    ),
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(ValueError, "staging file changed before atomic commit"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )
                self.assertFalse(destination.exists())
                self.assertFalse(any(path.name.endswith(".tmp") for path in destination.parent.iterdir()))

        with self.subTest("concurrent replacement inode is preserved"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                records = ready_package(package)
                destination = package / CHECK.DONE_RECEIPTS["package"]
                replacement = b"concurrent replacement\n"
                real_fsync = os.fsync
                def replace_after_commit(descriptor):
                    info = os.fstat(descriptor)
                    if stat.S_ISDIR(info.st_mode) and destination.exists():
                        destination.unlink()
                        destination.write_bytes(replacement)
                        raise OSError("injected failure after concurrent replacement")
                    return real_fsync(descriptor)

                with (
                    mock.patch.object(CHECK.os, "fsync", side_effect=replace_after_commit),
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(OSError, "indeterminate after atomic commit"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )
                self.assertEqual(destination.read_bytes(), replacement)

        with self.subTest("same destination inode mutates in the final binding hook"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                records = ready_package(package)
                destination = package / CHECK.DONE_RECEIPTS["package"]
                real_binding = CHECK.assert_receipt_directory_binding
                mutated = False

                def mutate_during_final_binding(*args, **kwargs):
                    nonlocal mutated
                    result = real_binding(*args, **kwargs)
                    if destination.exists() and not mutated:
                        destination.chmod(0o600)
                        destination.write_bytes(b"mutated same receipt inode\n")
                        mutated = True
                    return result

                with (
                    mock.patch.object(
                        CHECK,
                        "assert_receipt_directory_binding",
                        side_effect=mutate_during_final_binding,
                    ),
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(OSError, "indeterminate after atomic commit"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )
                self.assertTrue(mutated)
                self.assertEqual(destination.read_bytes(), b"mutated same receipt inode\n")

        with self.subTest("receipt directory permissions weaken in the final binding hook"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                records = ready_package(package)
                destination = package / CHECK.DONE_RECEIPTS["package"]
                receipt_directory = destination.parent
                real_binding = CHECK.assert_receipt_directory_binding
                weakened = False

                def weaken_during_final_binding(*args, **kwargs):
                    nonlocal weakened
                    result = real_binding(*args, **kwargs)
                    if destination.exists() and not weakened:
                        receipt_directory.chmod(0o777)
                        weakened = True
                    return result

                try:
                    with (
                        mock.patch.object(
                            CHECK,
                            "assert_receipt_directory_binding",
                            side_effect=weaken_during_final_binding,
                        ),
                        mock.patch.object(
                            CHECK,
                            "repository_state",
                            return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                        ),
                        self.assertRaisesRegex(OSError, "indeterminate after atomic commit"),
                    ):
                        CHECK.write_done_receipt(
                            package,
                            "package",
                            submission_package_binding(package),
                            records,
                            now=now,
                        )
                    self.assertTrue(weakened)
                    self.assertTrue(destination.is_file())
                finally:
                    receipt_directory.chmod(0o755)

    def test_done_receipt_publication_is_attestation_fresh_exclusive_and_idempotent(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)

        def ready_package(root: Path) -> dict[str, dict[str, object]]:
            build_submission_receipt_chain(root, reg, through="package")
            attested, attest_path = CHECK.read_attestations(root)
            report = CHECK.Report()
            records = CHECK.check_phase_receipts(
                reg,
                root,
                "package",
                submission_package_binding(root),
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            return records

        with self.subTest("live attestation drift is rejected before publication"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                records = ready_package(package)
                attestation = package / "attest.yaml"
                attestation.write_text(attestation.read_text() + "\n")
                with (
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(ValueError, "live attestation"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )
                self.assertFalse((package / CHECK.DONE_RECEIPTS["package"]).exists())

        with self.subTest("package drift in the commit hook is indeterminate"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                records = ready_package(package)
                destination = package / CHECK.DONE_RECEIPTS["package"]
                real_link = CHECK.exclusive_receipt_link

                def mutate_after_link(source, target, directory_fd):
                    result = real_link(source, target, directory_fd)
                    (package / "screener.mp4").write_bytes(b"changed during commit")
                    return result

                with (
                    mock.patch.object(
                        CHECK,
                        "exclusive_receipt_link",
                        side_effect=mutate_after_link,
                    ),
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(OSError, "indeterminate after atomic commit"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )
                self.assertTrue(destination.is_file())
                self.assertFalse(any(path.name.endswith(".tmp") for path in destination.parent.iterdir()))

        with self.subTest("a concurrent destination is never overwritten"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                records = ready_package(package)
                destination = package / CHECK.DONE_RECEIPTS["package"]
                concurrent = b'{"schema":"concurrent-winner"}\n'
                real_link = CHECK.exclusive_receipt_link

                def publish_concurrently(source, target, directory_fd):
                    destination.write_bytes(concurrent)
                    return real_link(source, target, directory_fd)

                with (
                    mock.patch.object(
                        CHECK,
                        "exclusive_receipt_link",
                        side_effect=publish_concurrently,
                    ),
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaises(ValueError),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )
                self.assertEqual(destination.read_bytes(), concurrent)
                self.assertFalse(any(path.name.endswith(".tmp") for path in destination.parent.iterdir()))

        with self.subTest("an exact existing receipt is immutable and idempotent"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                records = ready_package(package)
                binding = submission_package_binding(package)
                destination = package / CHECK.DONE_RECEIPTS["package"]
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    first_path, first_digest = CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                first_bytes = destination.read_bytes()
                first_inode = destination.stat().st_ino
                with (
                    mock.patch.object(CHECK, "exclusive_receipt_link") as link,
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                ):
                    second_path, second_digest = CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                link.assert_not_called()
                self.assertEqual((second_path, second_digest), (first_path, first_digest))
                self.assertEqual(destination.read_bytes(), first_bytes)
                self.assertEqual(destination.stat().st_ino, first_inode)

    def test_done_receipt_final_composite_census_catches_package_drift(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="package")
            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            binding = submission_package_binding(package)
            records = CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                binding,
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            destination = package / CHECK.DONE_RECEIPTS["package"]
            manifested = package / "screener.mp4"
            real_snapshot = CHECK.assert_final_receipt_snapshot
            mutated = False

            def mutate_after_former_final_hook(*args, **kwargs):
                nonlocal mutated
                result = real_snapshot(*args, **kwargs)
                if not mutated:
                    manifested.write_bytes(b"mutation after the receipt-only snapshot")
                    mutated = True
                return result

            with (
                mock.patch.object(
                    CHECK,
                    "assert_final_receipt_snapshot",
                    side_effect=mutate_after_former_final_hook,
                ),
                mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ),
                self.assertRaisesRegex(OSError, "indeterminate after atomic commit"),
            ):
                CHECK.write_done_receipt(
                    package,
                    "package",
                    binding,
                    records,
                    now=now,
                )
            self.assertTrue(mutated)
            self.assertTrue(destination.is_file())
            self.assertFalse(
                any(path.name.endswith(".tmp") for path in destination.parent.iterdir())
            )

    def test_done_receipt_recovers_only_safe_owned_crash_residue(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)

        def ready(root: Path) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
            build_submission_receipt_chain(root, reg, through="package")
            attested, attest_path = CHECK.read_attestations(root)
            report = CHECK.Report()
            binding = submission_package_binding(root)
            records = CHECK.check_phase_receipts(
                reg,
                root,
                "package",
                binding,
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            return binding, records

        residue_name = ".validated-package.json.1234-0123456789abcdef.tmp"
        with self.subTest("staging-only crash residue"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                binding, records = ready(package)
                residue = package / "receipts" / residue_name
                residue.write_bytes(b"interrupted staging bytes")
                residue.chmod(0o600)
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    destination, _ = CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                self.assertFalse(residue.exists())
                self.assertTrue(destination.is_file())
                self.assertEqual(destination.stat().st_nlink, 1)

        with self.subTest("post-link crash residue"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                binding, records = ready(package)
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    destination, first_digest = CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                first_inode = destination.stat().st_ino
                residue = destination.parent / residue_name
                os.link(destination, residue)
                self.assertEqual(destination.stat().st_nlink, 2)
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    reused, second_digest = CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                self.assertFalse(residue.exists())
                self.assertEqual(reused.stat().st_ino, first_inode)
                self.assertEqual(reused.stat().st_nlink, 1)
                self.assertEqual(second_digest, first_digest)

        with self.subTest("unsafe exact residue is preserved and rejected"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                binding, records = ready(package)
                residue = package / "receipts" / residue_name
                residue.write_bytes(b"untrusted staging bytes")
                residue.chmod(0o666)
                with (
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(ValueError, "staging residue is unsafe"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                self.assertEqual(residue.read_bytes(), b"untrusted staging bytes")

    def test_done_receipt_rejects_missing_link_dirfd_capability_before_staging(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="package")
            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            records = CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                submission_package_binding(package),
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            capabilities = set(os.supports_dir_fd)
            capabilities.discard(os.link)
            with (
                mock.patch.object(CHECK.os, "supports_dir_fd", capabilities),
                self.assertRaisesRegex(OSError, "dir-fd operations are unsupported"),
            ):
                CHECK.write_done_receipt(
                    package,
                    "package",
                    submission_package_binding(package),
                    records,
                    now=now,
                )
            destination = package / CHECK.DONE_RECEIPTS["package"]
            self.assertFalse(destination.exists())
            self.assertFalse(any(path.name.endswith(".tmp") for path in destination.parent.iterdir()))

    def test_done_receipt_lease_blocks_a_second_staging_writer(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="package")
            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            binding = submission_package_binding(package)
            records = CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                binding,
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            receipt_fd = os.open(package / "receipts", os.O_RDONLY | os.O_DIRECTORY)
            try:
                CHECK.fcntl.flock(receipt_fd, CHECK.FLOCK_EX | CHECK.FLOCK_NB)
                with (
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(OSError, "publication lease is busy"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                self.assertFalse(any(path.name.endswith(".tmp") for path in (package / "receipts").iterdir()))
                self.assertFalse((package / CHECK.DONE_RECEIPTS["package"]).exists())
            finally:
                CHECK.fcntl.flock(receipt_fd, CHECK.fcntl.LOCK_UN)
                os.close(receipt_fd)
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                done, _ = CHECK.write_done_receipt(
                    package,
                    "package",
                    binding,
                    records,
                    now=now,
                )
            self.assertTrue(done.is_file())

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="package")
            attested, attest_path = CHECK.read_attestations(package)
            report = CHECK.Report()
            records = CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                submission_package_binding(package),
                attested,
                attest_path,
                report,
                now=now,
            )
            with (
                mock.patch.object(CHECK, "fcntl", None),
                self.assertRaisesRegex(OSError, "publication lease is unsupported"),
            ):
                CHECK.write_done_receipt(
                    package,
                    "package",
                    submission_package_binding(package),
                    records,
                    now=now,
                )
            self.assertFalse(any(path.name.endswith(".tmp") for path in (package / "receipts").iterdir()))

    def test_existing_done_receipt_is_bounded_private_and_durably_rechecked(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)

        def ready(root: Path) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
            build_submission_receipt_chain(root, reg, through="package")
            attested, attest_path = CHECK.read_attestations(root)
            report = CHECK.Report()
            binding = submission_package_binding(root)
            records = CHECK.check_phase_receipts(
                reg,
                root,
                "package",
                binding,
                attested,
                attest_path,
                report,
                now=now,
            )
            self.assertEqual(report.failures, 0)
            return binding, records

        with self.subTest("oversized valid JSON is rejected before allocation"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                binding, records = ready(package)
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    destination, _ = CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                destination.chmod(0o600)
                with destination.open("ab") as handle:
                    handle.write(b" " * (CHECK.DONE_RECEIPT_MAX_BYTES + 1 - destination.stat().st_size))
                destination.chmod(0o400)
                with (
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(ValueError, "byte limit"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )

        with self.subTest("group-world-writable exact receipt is never reused"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                binding, records = ready(package)
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    destination, _ = CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                original = destination.read_bytes()
                inode = destination.stat().st_ino
                destination.chmod(0o666)
                with (
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                    self.assertRaisesRegex(ValueError, "destination is unsafe"),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                self.assertEqual(destination.read_bytes(), original)
                self.assertEqual(destination.stat().st_ino, inode)

        with self.subTest("existing exact receipt is file-then-directory synced"):
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp)
                binding, records = ready(package)
                with mock.patch.object(
                    CHECK,
                    "repository_state",
                    return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                ):
                    destination, _ = CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                receipt_directory = destination.parent.stat()
                events: list[str] = []
                real_sync = CHECK.sync_regular_descriptor
                real_fsync = CHECK.os.fsync

                def record_file_sync(descriptor):
                    if stat.S_ISREG(os.fstat(descriptor).st_mode):
                        events.append("file")
                    return real_sync(descriptor)

                def record_directory_sync(descriptor):
                    info = os.fstat(descriptor)
                    if (info.st_dev, info.st_ino) == (
                        receipt_directory.st_dev,
                        receipt_directory.st_ino,
                    ):
                        events.append("directory")
                    return real_fsync(descriptor)

                with (
                    mock.patch.object(
                        CHECK,
                        "sync_regular_descriptor",
                        side_effect=record_file_sync,
                    ),
                    mock.patch.object(CHECK.os, "fsync", side_effect=record_directory_sync),
                    mock.patch.object(
                        CHECK,
                        "repository_state",
                        return_value=(SUBMISSION_REPOSITORY_HEAD, True),
                    ),
                ):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        binding,
                        records,
                        now=now,
                    )
                self.assertEqual(events, ["file", "directory"])

    def test_descriptor_contract_caps_separate_done_phase_and_manifest_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            receipts = package / "receipts"
            receipts.mkdir()
            done = receipts / "validated-package.json"
            phase = receipts / "package.json"
            document = b"{}" + b" " * CHECK.DONE_RECEIPT_MAX_BYTES
            done.write_bytes(document)
            phase.write_bytes(document)
            root_fd = os.open(package, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaisesRegex(ValueError, "byte limit"):
                    CHECK.descriptor_file_identity(
                        root_fd,
                        CHECK.DONE_RECEIPTS["package"],
                        "validated-package receipt",
                        capture=True,
                        max_bytes=CHECK.DONE_RECEIPT_MAX_BYTES,
                    )
                captured, _, size = CHECK.descriptor_file_identity(
                    root_fd,
                    CHECK.PHASE_RECEIPTS["package"],
                    "package receipt",
                    capture=True,
                    max_bytes=CHECK.PHASE_RECEIPT_MAX_BYTES,
                )
                self.assertEqual(captured, document)
                self.assertEqual(size, len(document))

                manifest = package / "manifest.json"
                with manifest.open("wb") as handle:
                    handle.truncate(CHECK.PACKAGE_MANIFEST_MAX_BYTES + 1)
                with self.assertRaisesRegex(ValueError, "byte limit"):
                    CHECK.descriptor_file_identity(
                        root_fd,
                        "manifest.json",
                        "package manifest",
                        capture=True,
                        max_bytes=CHECK.PACKAGE_MANIFEST_MAX_BYTES,
                    )
            finally:
                os.close(root_fd)

    def test_done_receipt_revalidates_manifested_bytes_and_repository_state(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 8, 31, 20, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            build_submission_receipt_chain(package, reg, through="package")
            attested, attest_path = CHECK.read_attestations(package)
            records_report = CHECK.Report()
            records = CHECK.check_phase_receipts(
                reg,
                package,
                "package",
                submission_package_binding(package),
                attested,
                attest_path,
                records_report,
                now=now,
            )
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                done_path, _ = CHECK.write_done_receipt(
                    package,
                    "package",
                    submission_package_binding(package),
                    records,
                    now=now,
                )
            value = json.loads(done_path.read_text())
            (package / "screener.mp4").write_bytes(b"mutated after phase validation")
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, True),
            ):
                errors = CHECK.validate_done_receipt(
                    value,
                    done_path,
                    package,
                    "package",
                    submission_package_binding(package),
                    records,
                    now=now,
                )
                with self.assertRaisesRegex(ValueError, "invalid current package"):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )
            self.assertTrue(any("package identity is no longer valid" in error for error in errors))

            (package / "screener.mp4").write_text("exact package bytes\n")
            with mock.patch.object(
                CHECK,
                "repository_state",
                return_value=(SUBMISSION_REPOSITORY_HEAD, False),
            ):
                with self.assertRaisesRegex(ValueError, "repository"):
                    CHECK.write_done_receipt(
                        package,
                        "package",
                        submission_package_binding(package),
                        records,
                        now=now,
                    )

    def test_package_phase_includes_the_redacted_exact_manifest_rights_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "attest.yaml").write_text("{}\n")
            report = CHECK.Report()
            CHECK.check_rights(package, "package", report)
            self.assertEqual(len(report.rows), 1)
            self.assertEqual(report.rows[0][1], "redacted exact-manifest contract")
            self.assertEqual(report.rows[0][2], CHECK.FAIL)
            self.assertIn("blocker(s)", report.rows[0][3])
            self.assertNotIn(str(package), report.rows[0][3])

    def test_submission_rights_path_loads_pages_contract_without_scripts_on_sys_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "attest.yaml").write_text("{}\n")
            scripts = (ROOT / "scripts").resolve()
            isolated_path = [
                entry
                for entry in sys.path
                if not entry or Path(entry).resolve() != scripts
            ]
            report = CHECK.Report()
            with mock.patch.object(sys, "path", isolated_path):
                CHECK.check_rights(package, "package", report)
            self.assertEqual(len(report.rows), 1)
            self.assertEqual(report.rows[0][1], "redacted exact-manifest contract")
            self.assertEqual(report.rows[0][2], CHECK.FAIL)
            self.assertIn("blocker(s)", report.rows[0][3])
            self.assertNotIn("validation failed", report.rows[0][3])

    def test_rights_checker_exception_never_leaks_a_machine_local_path(self) -> None:
        report = CHECK.Report()
        with mock.patch.object(
            CHECK.importlib.util,
            "spec_from_file_location",
            side_effect=RuntimeError("failed at /Users/Alice/private-rights.json"),
        ):
            CHECK.check_rights(Path("/Users/Alice/private-package"), "package", report)
        self.assertEqual(report.rows[0][2], CHECK.FAIL)
        self.assertNotIn("/Users/", report.rows[0][3])
        self.assertIn("RuntimeError", report.rows[0][3])

    def test_published_terms_keep_provenance_and_explicit_archive_choice(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        self.assertEqual(
            {row["id"] for row in reg["terms"]},
            {
                "accepted-film-no-withdrawal",
                "publicity-stills-free-of-rights",
                "submission-rights-warranty",
                "festival-scheduling-discretion",
                "archive-library-choice",
                "regulations-accepted",
            },
        )
        report = CHECK.Report()
        CHECK.check_requirement_phases(reg, report)
        term_row = next(row for row in report.rows if row[1] == "published term provenance and choice contract")
        self.assertEqual(term_row[2], CHECK.PASS)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attestations = {
                item["id"]: True
                for section in CHECK.OWNED_SECTIONS
                for item in reg.get(section, [])
                if item.get("check") == "manual"
            }
            for choice in ("include", "opt-out"):
                attestations["archive-library-choice"] = choice
                (root / "attest.yaml").write_text(yaml.safe_dump(attestations))
                phase = CHECK.Report()
                CHECK.check_attestations(reg, root, "submitted", phase)
                self.assertEqual(phase.failures, 0)

            attestations["archive-library-choice"] = True
            (root / "attest.yaml").write_text(yaml.safe_dump(attestations))
            invalid = CHECK.Report()
            CHECK.check_attestations(reg, root, "submitted", invalid)
            choice_row = next(row for row in invalid.rows if row[1] == "archive-library-choice")
            self.assertEqual(choice_row[2], CHECK.FAIL)

        broken = yaml.safe_load(yaml.safe_dump(reg))
        del broken["terms"][0]["source"]
        broken_report = CHECK.Report()
        CHECK.check_requirement_phases(broken, broken_report)
        broken_term_row = next(
            row for row in broken_report.rows if row[1] == "published term provenance and choice contract"
        )
        self.assertEqual(broken_term_row[2], CHECK.FAIL)

    def test_register_preflight_binds_internal_delivery_label_and_audio_use_bytes(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        report = CHECK.Report()
        CHECK.check_requirement_phases(reg, report)
        contract = next(
            row for row in report.rows if row[1] == "internal delivery and audio-use contract"
        )
        self.assertEqual(contract[2], CHECK.PASS)

        stale = copy.deepcopy(reg)
        stale["package"]["audio"]["usage_contract"]["sha256"] = "0" * 64
        stale_report = CHECK.Report()
        CHECK.check_requirement_phases(stale, stale_report)
        stale_contract = next(
            row for row in stale_report.rows if row[1] == "internal delivery and audio-use contract"
        )
        self.assertEqual(stale_contract[2], CHECK.FAIL)
        self.assertIn("digest is missing or stale", stale_contract[3])

        mislabelled = copy.deepcopy(reg)
        mislabelled["package"]["published_requirement"] = True
        mislabelled_report = CHECK.Report()
        CHECK.check_requirement_phases(mislabelled, mislabelled_report)
        mislabelled_contract = next(
            row
            for row in mislabelled_report.rows
            if row[1] == "internal delivery and audio-use contract"
        )
        self.assertEqual(mislabelled_contract[2], CHECK.FAIL)
        self.assertIn("misrepresented as published", mislabelled_contract[3])

    def test_malformed_register_shapes_fail_cleanly_in_checks_and_cli(self) -> None:
        source = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        malformed: list[tuple[str, dict]] = []
        for section in CHECK.OWNED_SECTIONS:
            for label, value in (("null", None), ("non-list", {}), ("non-mapping row", [None])):
                register = copy.deepcopy(source)
                register[section] = value
                malformed.append((f"{section} {label}", register))

        for field in ("deadline", "opportunity_snapshot", "package"):
            register = copy.deepcopy(source)
            del register[field]
            malformed.append((f"missing {field}", register))

        register = copy.deepcopy(source)
        del register["package"]["master"]
        malformed.append(("missing package.master", register))

        for label, register in malformed:
            with self.subTest(check=label):
                report = CHECK.Report()
                CHECK.check_requirement_phases(register, report)
                self.assertGreater(report.failures, 0)

            with self.subTest(cli=label), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "register.yaml"
                path.write_text(yaml.safe_dump(register, sort_keys=False))
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    mock.patch.object(sys, "argv", ["check.py", "--register", str(path)]),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    result = CHECK.main()
                self.assertEqual(result, 1)
                output = stdout.getvalue() + stderr.getvalue()
                self.assertIn("submission register has malformed structure", output)
                self.assertNotIn("Traceback", output)

    def test_submitted_phase_name_alone_cannot_close_elapsed_targets(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 9, 2, 12, tzinfo=ZoneInfo("America/New_York"))
        report = CHECK.Report()
        CHECK.check_deadline(reg, "submitted", report, now=now)
        target = next(row for row in report.rows if row[1] == "upload target")
        self.assertEqual(target[2], CHECK.FAIL)
        self.assertIn("no timely uploaded receipt", target[3])

    def test_submitted_phase_after_the_hard_wall_requires_timed_receipts(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        now = datetime(2026, 9, 2, 12, tzinfo=ZoneInfo("America/New_York"))
        submitted = CHECK.Report()
        receipts = {
            "uploaded": {"event_at": datetime(2026, 8, 31, 21, 59, tzinfo=timezone.utc)},
            "submitted": {"event_at": datetime(2026, 9, 1, 1, 59, tzinfo=timezone.utc)},
        }
        CHECK.check_deadline(reg, "submitted", submitted, now=now, receipts=receipts)
        hard_wall = next(row for row in submitted.rows if row[1] == "hard wall")
        self.assertEqual(hard_wall[2], CHECK.PASS)
        package = CHECK.Report()
        CHECK.check_deadline(reg, "package", package, now=now)
        self.assertEqual(package.rows[0][2], CHECK.FAIL)

    def test_deadline_with_malformed_timezone_fails_closed(self) -> None:
        reg = yaml.safe_load((ROOT / "submission/screendance-2027.yaml").read_text())
        reg["opportunity_snapshot"]["timezone"] = "/UTC"
        report = CHECK.Report()
        CHECK.check_deadline(reg, "package", report)
        timezone = next(row for row in report.rows if row[1] == "timezone")
        self.assertEqual(timezone[2], CHECK.FAIL)

    def test_probe_ignores_attached_picture_streams(self) -> None:
        payload = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "width": 640,
                    "height": 480,
                    "r_frame_rate": "0/1",
                    "disposition": {"attached_pic": 1},
                },
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "r_frame_rate": "30/1",
                    "disposition": {"attached_pic": 0},
                },
            ],
            "format": {"duration": "15.0"},
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with (
            mock.patch.object(CHECK.shutil, "which", return_value="/usr/bin/ffprobe"),
            mock.patch.object(CHECK.subprocess, "run", return_value=completed),
        ):
            info = CHECK.probe(Path("screener.mp4"))
        self.assertEqual(info["vcodec"], "h264")
        self.assertEqual(info["width"], 1920)

    def test_control_rejects_non_numeric_start(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        done = subprocess.run(
            ["node", str(ROOT / "sound/control.mjs"), "--from", "not-a-number", "--rate", "0"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("--from must be a non-negative number", done.stderr)

    def test_projection_probe_returns_page_self_test_status(self) -> None:
        class Locator:
            def inner_text(self) -> str:
                return "SELF-TEST PASS\nmax Δ 0/255"

        page = mock.Mock()
        page.gl_renderer = "ANGLE Metal Renderer"
        page.evaluate.return_value = True
        page.locator.return_value = Locator()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(BROWSER.run_probe(page, "http://example.test"), 0)
        page.goto.assert_called_once_with("http://example.test/probe.html", wait_until="load")

    def test_explicit_browser_base_never_falls_back_to_local_checkout(self) -> None:
        forbidden = mock.Mock(side_effect=AssertionError("local server fallback invoked"))
        with (
            mock.patch.object(sys, "argv", ["browser.py", "--check", "--base", "https://unreachable.invalid"]),
            mock.patch.object(BROWSER, "reachable", return_value=False),
            mock.patch.object(BROWSER, "serve", forbidden),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            BROWSER.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(forbidden.called)

    def test_delivery_stages_only_the_verified_score_motion_evidence_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "evidence"
            source.mkdir()
            receipt = source / "score-to-motion-production.json"
            sample = source / "score-to-motion-samples.json"
            sample.write_text('{"sample":true}\n')
            receipt.write_text(
                json.dumps(
                    {
                        "repository_head": SUBMISSION_REPOSITORY_HEAD,
                        "span": {
                            "river_seed": 20170620,
                            "stream": 0,
                            "passage": SPAN["passage"],
                            "t0": SPAN["t0"],
                            "t1": SPAN["t1"],
                            "duration_seconds": SPAN["duration"] + 0.0000005,
                        },
                    }
                )
                + "\n"
            )
            contract = SimpleNamespace(
                production_receipt_errors=lambda path: [],
                evidence_artifact_paths=lambda path: [receipt, sample],
            )
            package = root / "package"
            package.mkdir()
            obsolete = package / DELIVER.SCORE_MOTION_EVIDENCE_DIR / "obsolete/old.json"
            obsolete.parent.mkdir(parents=True)
            obsolete.write_text("stale evidence\n")
            outside = root / "outside-shared-inode.json"
            outside.write_text("must remain unchanged\n")
            linked_sample = package / DELIVER.SCORE_MOTION_EVIDENCE_DIR / sample.name
            os.link(outside, linked_sample)
            shared_inode = outside.stat().st_ino
            with (
                mock.patch.object(DELIVER, "SCORE_MOTION_EVIDENCE", receipt),
                mock.patch.object(DELIVER, "score_motion_contract", return_value=contract),
            ):
                reference, staged = DELIVER.stage_score_motion_evidence(
                    package,
                    SPAN,
                    SUBMISSION_REPOSITORY_HEAD,
                )
            self.assertEqual(reference["path"], DELIVER.SCORE_MOTION_EVIDENCE_ITEM)
            self.assertEqual(len(staged), 2)
            self.assertEqual(
                (package / DELIVER.SCORE_MOTION_EVIDENCE_DIR / sample.name).read_bytes(),
                sample.read_bytes(),
            )
            self.assertFalse(obsolete.exists())
            self.assertFalse(obsolete.parent.exists())
            self.assertEqual(outside.read_text(), "must remain unchanged\n")
            self.assertEqual(outside.stat().st_ino, shared_inode)
            self.assertNotEqual(linked_sample.stat().st_ino, shared_inode)

            stale = json.loads(receipt.read_text())
            stale["span"]["duration_seconds"] = SPAN["duration"] + 0.000002
            receipt.write_text(json.dumps(stale) + "\n")
            with (
                mock.patch.object(DELIVER, "SCORE_MOTION_EVIDENCE", receipt),
                mock.patch.object(DELIVER, "score_motion_contract", return_value=contract),
                self.assertRaisesRegex(SystemExit, "different package span"),
            ):
                DELIVER.stage_score_motion_evidence(
                    package,
                    SPAN,
                    SUBMISSION_REPOSITORY_HEAD,
                )

    def test_absent_score_motion_wraps_boundary_removal_failure(self) -> None:
        destination = SimpleNamespace(
            rmdir=mock.Mock(side_effect=OSError("concurrent entry")),
        )
        with tempfile.TemporaryDirectory() as temporary:
            absent = Path(temporary) / "absent-production-receipt.json"
            with (
                mock.patch.object(DELIVER, "SCORE_MOTION_EVIDENCE", absent),
                mock.patch.object(
                    DELIVER,
                    "safe_score_motion_directory",
                    return_value=destination,
                ),
                mock.patch.object(DELIVER, "prune_score_motion_evidence") as prune,
                self.assertRaisesRegex(
                    SystemExit,
                    "score-to-motion evidence boundary could not be removed",
                ),
            ):
                DELIVER.stage_score_motion_evidence(
                    Path(temporary) / "package",
                    SPAN,
                    SUBMISSION_REPOSITORY_HEAD,
                )
        prune.assert_called_once_with(destination, set())

    def test_score_motion_staging_rejects_every_symlink_ancestor(self) -> None:
        for attack in ("provenance", "nested"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "evidence"
                nested = source / "boundary-frames"
                nested.mkdir(parents=True)
                receipt = source / "score-to-motion-production.json"
                frame = nested / "sample-000-control.png"
                frame.write_bytes(b"authenticated frame")
                receipt.write_text(
                    json.dumps(
                        {
                            "repository_head": SUBMISSION_REPOSITORY_HEAD,
                            "span": {
                                "river_seed": 20170620,
                                "stream": 0,
                                "passage": SPAN["passage"],
                                "t0": SPAN["t0"],
                                "t1": SPAN["t1"],
                                "duration_seconds": SPAN["duration"],
                            },
                        }
                    )
                    + "\n"
                )
                contract = SimpleNamespace(
                    production_receipt_errors=lambda path: [],
                    evidence_artifact_paths=lambda path: [receipt, frame],
                )
                package = root / "package"
                package.mkdir()
                outside = root / "outside"
                outside.mkdir()
                sentinel = outside / "sentinel"
                sentinel.write_text("unchanged\n")
                if attack == "provenance":
                    (package / "provenance").symlink_to(outside, target_is_directory=True)
                else:
                    boundary = package / DELIVER.SCORE_MOTION_EVIDENCE_DIR
                    boundary.mkdir(parents=True)
                    (boundary / "boundary-frames").symlink_to(
                        outside,
                        target_is_directory=True,
                    )
                with (
                    mock.patch.object(DELIVER, "SCORE_MOTION_EVIDENCE", receipt),
                    mock.patch.object(DELIVER, "score_motion_contract", return_value=contract),
                    self.assertRaisesRegex(SystemExit, "unsafe|symlink"),
                ):
                    DELIVER.stage_score_motion_evidence(
                        package,
                        SPAN,
                        SUBMISSION_REPOSITORY_HEAD,
                    )
                self.assertEqual(sentinel.read_text(), "unchanged\n")
                self.assertFalse((outside / frame.name).exists())

    def test_submission_score_motion_row_never_substitutes_for_human_acceptance(self) -> None:
        contract = SimpleNamespace(packaged_receipt_errors=lambda *args, **kwargs: [])
        loader = SimpleNamespace(exec_module=lambda module: None)
        spec = SimpleNamespace(loader=loader)
        with (
            mock.patch.object(CHECK.importlib.util, "spec_from_file_location", return_value=spec),
            mock.patch.object(CHECK.importlib.util, "module_from_spec", return_value=contract),
        ):
            report = CHECK.Report()
            CHECK.check_score_motion(Path("unused"), report)
        self.assertEqual(report.rows[0][2], CHECK.PASS)
        self.assertIn("machine evidence", report.rows[0][1])
        self.assertIn("human review not attested", report.rows[0][3])
        self.assertNotIn("accepted", report.rows[0][3])


if __name__ == "__main__":
    unittest.main()
