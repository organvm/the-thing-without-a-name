#!/usr/bin/env python3
"""Adversarial portable checks for production score-to-motion evidence."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AB = load("danse_score_motion_production_test", ROOT / "scripts/score_motion_production.py")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference(path: Path, base: Path) -> dict:
    return {
        "path": path.relative_to(base).as_posix(),
        "sha256": digest(path),
        "bytes": path.stat().st_size,
    }


def write_wav(path: Path, frames: int = 48000) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(b"\x01\x00\x02\x00" * frames)


def visual(seed: str, *, pose: bool) -> dict:
    return {
        "channels": {
            "divergence": 0.1,
            "azimuth": 0.2,
            "elevation": 0.3,
            "spread": 0.4,
            "projK": 0.0,
            "turnover": 0.5,
        },
        "movement": "ONE",
        "cut": "solo",
        "material": 1,
        "cast_sha256": seed * 64,
        "cast_count": 1,
        "choreography_pose_sha256": ("e" * 64 if pose else None),
    }


class EvidenceFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.base = root / "provenance/score-to-motion"
        self.base.mkdir(parents=True)
        score_master = root / "provenance/passage-score.wav"
        score_master.parent.mkdir(parents=True, exist_ok=True)
        write_wav(score_master)
        pcm = AB.wav_pcm_identity(score_master)
        self.context = {
            "repository_head": "a" * 40,
            "source_tree_sha256": "b" * 64,
            "span": {
                "river_seed": 20170620,
                "stream": 0,
                "passage": 0,
                "t0": 0,
                "t1": 1.0,
                "duration_seconds": 1.0,
            },
            "score": {
                "path": "music/score.json",
                "file_sha256": "c" * 64,
                "contract_sha256": "d" * 64,
                "duration_seconds": 1.0,
            },
            "choreography": {
                "path": "render/choreography.json",
                "file_sha256": "e" * 64,
                "contract_sha256": "f" * 64,
            },
            "audio_render_receipt": {
                "path": ".work/music/competition/audio-render.json",
                "sha256": "1" * 64,
            },
            "audio_master": {
                "path": ".work/music/competition/delibes-master.wav",
                "sha256": digest(score_master),
                **pcm,
            },
        }
        self.rows = []
        for index, second in enumerate((0.0, 0.4, 0.8)):
            with_score = visual(str(index + 2), pose=True)
            control = visual(str(index + 5), pose=False)
            delta = {name: 0.0 for name in AB.CHANNELS}
            self.rows.append(
                {
                    "sample_id": f"sample-{index:03d}",
                    "absolute_second": second,
                    "boundaries": [{"kind": "phrase", "id": f"phrase-{index}"}],
                    "movement": "SYLVIA",
                    "phrase": f"phrase-{index}",
                    "with_score": with_score,
                    "control": control,
                    "score_delta": delta,
                    "score_delta_max": 0.0,
                    "observable_state_difference": True,
                }
            )
        self.sample = self.base / "score-to-motion-samples.json"
        self.sample.write_text(
            json.dumps(
                {
                    "schema": AB.SAMPLE_SCHEMA_ID,
                    "evidence_scope": "production-input-not-final",
                    **self.context,
                    "rows": self.rows,
                },
                indent=2,
            )
            + "\n"
        )

        frames = self.base / "boundary-frames"
        frames.mkdir()
        frame_rows = []
        self.with_paths = []
        for index, row in enumerate(self.rows):
            with_path = frames / f"sample-{index:03d}-with-score.png"
            control_path = frames / f"sample-{index:03d}-control.png"
            Image.new("RGB", (AB.PRODUCTION_WIDTH, AB.PRODUCTION_HEIGHT), (255, index, 0)).save(with_path)
            Image.new("RGB", (AB.PRODUCTION_WIDTH, AB.PRODUCTION_HEIGHT), (0, 0, index)).save(control_path)
            self.with_paths.append(with_path)
            frame_rows.append(
                {
                    "sample_id": row["sample_id"],
                    "absolute_second": row["absolute_second"],
                    "review_frame_index": AB._production_review_position(row["absolute_second"])[0],
                    "review_second": AB._production_review_position(row["absolute_second"])[1],
                    "boundaries": row["boundaries"],
                    "movement": row["movement"],
                    "phrase": row["phrase"],
                    "psnr_db": AB._image_psnr(
                        with_path,
                        control_path,
                        AB.PRODUCTION_WIDTH,
                        AB.PRODUCTION_HEIGHT,
                    ),
                    "with_score": reference(with_path, self.base),
                    "control": reference(control_path, self.base),
                }
            )
        repeat = frames / "sample-000-with-score-repeat.png"
        repeat.write_bytes(self.with_paths[0].read_bytes())
        sheet = self.base / "score-to-motion-contact-sheet.png"
        Image.new("RGB", (8, 3), "black").save(sheet)
        self.frame = self.base / "score-to-motion-frames.json"
        self.frame.write_text(
            json.dumps(
                {
                    "schema": AB.FRAME_SCHEMA_ID,
                    "evidence_scope": "production-boundary-frame-evidence",
                    **self.context,
                    "sample_receipt": reference(self.sample, self.base),
                    "capture": {
                        "tier": AB.PRODUCTION_TIER,
                        "width": AB.PRODUCTION_WIDTH,
                        "height": AB.PRODUCTION_HEIGHT,
                        "fps": AB.PRODUCTION_FPS,
                        "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M5)",
                        **AB.capture_contract_identity(ROOT),
                    },
                    "contact_sheet": reference(sheet, self.base),
                    "determinism": {
                        "absolute_second": 0.0,
                        "identical": True,
                        "first": reference(self.with_paths[0], self.base),
                        "repeat": reference(repeat, self.base),
                    },
                    "rows": frame_rows,
                },
                indent=2,
            )
            + "\n"
        )
        self.with_movie = self.base / "with-score.mov"
        self.control_movie = self.base / "control.mov"
        self.with_movie.write_bytes(b"with score review movie")
        self.control_movie.write_bytes(b"control review movie")
        self.media = {
            path: {
                "sha256": digest(path),
                "bytes": path.stat().st_size,
                "duration_seconds": 1.0,
                "fps": 30,
                "width": AB.PRODUCTION_WIDTH,
                "height": AB.PRODUCTION_HEIGHT,
                "video_frames": 30,
                "video_streams": 1,
                "audio_streams": 1,
                "audio_pcm_sha256": pcm["pcm_sha256"],
                "audio_frames": pcm["frames"],
                "audio_sample_rate": pcm["sample_rate"],
                "audio_channels": pcm["channels"],
                "video_framehash_sha256": ("7" if path == self.with_movie else "8") * 64,
                "decoded_rgb_sha256": ("3" if path == self.with_movie else "4") * 64,
                "decoded_video_frames": 30,
            }
            for path in (self.with_movie, self.control_movie)
        }
        self.anchors = {
            mode: [
                {
                    "sample_id": row["sample_id"],
                    "frame_index": row["review_frame_index"],
                    "review_second": row["review_second"],
                    "source_frame_sha256": digest(
                        self.base / row[mode]["path"]
                    ),
                    "decoded_rgb_sha256": ("9" if mode == "with_score" else "a") * 64,
                    "psnr_db": 60.0,
                }
                for row in frame_rows
            ]
            for mode in ("with_score", "control")
        }
        self.producer_paths = {}
        control_source_tree = AB.renderer_source_tree(
            AB.PRODUCTION_TIER,
            ROOT,
            with_score=False,
        )
        self.control_source_tree = control_source_tree
        for mode, movie in (("with_score", self.with_movie), ("control", self.control_movie)):
            producer_root = self.base / "producer-receipts" / mode
            producer_root.mkdir(parents=True)
            segment_name = f"{mode}-seg-000.mov"
            segment_receipt = producer_root / f"{segment_name}.receipt.json"
            inputs = {
                "window": "passage",
                "start": 0,
                "tier": AB.PRODUCTION_TIER,
                "seed": self.context["span"]["river_seed"],
                "stream": self.context["span"]["stream"],
                "codec": "prores",
                "width": AB.PRODUCTION_WIDTH,
                "height": AB.PRODUCTION_HEIGHT,
                "fps": AB.PRODUCTION_FPS,
                "segment_frames": 30,
                "source_tree_sha256": (
                    self.context["source_tree_sha256"]
                    if mode == "with_score"
                    else control_source_tree
                ),
            }
            if mode == "with_score":
                inputs["music_score"] = {
                    field: self.context["score"][field]
                    for field in ("path", "file_sha256", "contract_sha256")
                }
                inputs["choreography"] = copy.deepcopy(self.context["choreography"])
            else:
                inputs["timing_score"] = {
                    "path": self.context["score"]["path"],
                    "file_sha256": self.context["score"]["file_sha256"],
                    "contract_sha256": self.context["score"]["contract_sha256"],
                    "passage_mapping": "native-tempo",
                    "duration_seconds": self.context["span"]["duration_seconds"],
                }
                inputs["passage_timing"] = {
                    "mode": "fixed-passage",
                    "seconds": self.context["span"]["duration_seconds"],
                }
            segment_receipt.write_text(
                json.dumps(
                    {
                        "schema": "danse.render.segment.v1",
                        "segment": 0,
                        "frames": 30,
                        "inputs": inputs,
                        "capture": {
                            "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M5)",
                            "raw_rgba_sha256": "5" * 64,
                            "missing": 0,
                            "signature": f"fixture-{mode}",
                            "passage": {
                                "index": self.context["span"]["passage"],
                                "seed": AB.PRODUCTION_PASSAGE_SEED,
                                "t0": self.context["span"]["t0"],
                                "seconds": self.context["span"]["duration_seconds"],
                            },
                        },
                        "decoded_video": {
                            "algorithm": "rgb24-stream-sha256-v1",
                            "sha256": self.media[movie]["decoded_rgb_sha256"],
                            "frames": 30,
                            "width": AB.PRODUCTION_WIDTH,
                            "height": AB.PRODUCTION_HEIGHT,
                        },
                        "file_sha256": "6" * 64,
                    },
                    indent=2,
                )
                + "\n"
            )
            concat_receipt = producer_root / f"{mode}.mov.receipt.json"
            concat_receipt.write_text(
                json.dumps(
                    {
                        "schema": "danse.render.concat.v1",
                        "codec": "prores",
                        "segments": [
                            {
                                "name": segment_name,
                                "receipt_sha256": digest(segment_receipt),
                            }
                        ],
                        "file_sha256": "7" * 64,
                        "decoded_video": {
                            "algorithm": "rgb24-stream-sha256-v1",
                            "sha256": self.media[movie]["decoded_rgb_sha256"],
                            "frames": 30,
                            "width": AB.PRODUCTION_WIDTH,
                            "height": AB.PRODUCTION_HEIGHT,
                            "fps": AB.PRODUCTION_FPS,
                        },
                    },
                    indent=2,
                )
                + "\n"
            )
            self.producer_paths[mode] = concat_receipt
        self.receipt = self.base / "score-to-motion-production.json"
        self.write_receipt()
        self.score_master = score_master

    def write_receipt(self, transform=None) -> None:
        document = {
            "schema": AB.PRODUCTION_SCHEMA_ID,
            "evidence_scope": "production-machine-evidence-only",
            **copy.deepcopy(self.context),
            "sample_receipt": reference(self.sample, self.base),
            "frame_receipt": reference(self.frame, self.base),
            "review_media": {
                "with_score": {
                    "path": self.with_movie.relative_to(self.base).as_posix(),
                    "mode": "with_score",
                    **self.media[self.with_movie],
                    "producer_receipt": reference(
                        self.producer_paths["with_score"], self.base
                    ),
                    "anchors": self.anchors["with_score"],
                },
                "control": {
                    "path": self.control_movie.relative_to(self.base).as_posix(),
                    "mode": "control",
                    **self.media[self.control_movie],
                    "producer_receipt": reference(
                        self.producer_paths["control"], self.base
                    ),
                    "anchors": self.anchors["control"],
                },
            },
            "human_review": {"status": "not-attested"},
        }
        if transform:
            transform(document)
        self.receipt.write_text(json.dumps(document, indent=2) + "\n")

    def probe(self, path: Path) -> dict:
        return copy.deepcopy(self.media[path])

    def anchor_probe(self, path: Path, *, frame_path: Path, frame: dict, mode: str) -> list[dict]:
        return copy.deepcopy(self.anchors[mode])

    def package_manifest(self) -> dict:
        producer = self.root / "provenance/producer-receipts/render.json"
        producer.parent.mkdir(parents=True, exist_ok=True)
        producer.write_text(
            json.dumps(
                {
                    "schema": "danse.render.segment.v1",
                    "inputs": {
                        "source_tree_sha256": self.context["source_tree_sha256"],
                        "tier": AB.PRODUCTION_TIER,
                    },
                }
            )
        )
        production = self.root / "provenance/production.json"
        production.write_text(
            json.dumps(
                {
                    "schema": "danse.delivery.production.v1",
                    "repository_head": self.context["repository_head"],
                    "source_tree_sha256": self.context["source_tree_sha256"],
                    "producers": [
                        {
                            "kind": "render-segment",
                            "receipt": {
                                "path": producer.relative_to(self.root).as_posix(),
                                "sha256": digest(producer),
                            },
                        }
                    ],
                }
            )
        )
        sound = {
            "score_file_sha256": self.context["score"]["file_sha256"],
            "score_contract_sha256": self.context["score"]["contract_sha256"],
            "choreography_file_sha256": self.context["choreography"]["file_sha256"],
            "choreography_contract_sha256": self.context["choreography"]["contract_sha256"],
            "audio_render_receipt_sha256": self.context["audio_render_receipt"]["sha256"],
            "master_sha256": self.context["audio_master"]["sha256"],
        }
        owned = AB.evidence_artifact_paths(self.receipt)
        items = [
            {
                "name": path.relative_to(self.root).as_posix(),
                "sha256": digest(path),
                "bytes": path.stat().st_size,
            }
            for path in owned
        ]
        return {
            "repository_head": self.context["repository_head"],
            "seed": "0x133C77C",
            "passage": 0,
            "t0": 0,
            "t1": 1.0,
            "duration": 1.0,
            "corpus_tier": AB.PRODUCTION_TIER,
            "source_tree_sha256": self.context["source_tree_sha256"],
            "sound": sound,
            "production": {"path": "provenance/production.json", "sha256": digest(production)},
            "score_motion_evidence": {
                "path": self.receipt.relative_to(self.root).as_posix(),
                "sha256": digest(self.receipt),
            },
            "items": items,
        }


class ProductionScoreMotionTest(unittest.TestCase):
    def test_portable_probe_uses_current_production_score_and_choreography(self) -> None:
        rows = AB.generate_sample_rows(ROOT)
        self.assertGreaterEqual(len(rows), 40)
        self.assertEqual(rows[0]["sample_id"], "sample-000")
        self.assertEqual(rows[0]["absolute_second"], 0)
        self.assertTrue(all(row["observable_state_difference"] for row in rows))
        kinds = {boundary["kind"] for row in rows for boundary in row["boundaries"]}
        self.assertEqual(kinds, {"origin", "movement", "phrase", "cue"})
        self.assertTrue(all(row["with_score"]["choreography_pose_sha256"] for row in rows))
        self.assertTrue(all(row["control"]["choreography_pose_sha256"] is None for row in rows))

    def test_boundary_capture_control_uses_and_proves_the_selected_passage_clock(self) -> None:
        duration = json.loads((ROOT / "music/score.json").read_text())["time"][
            "duration_seconds"
        ]
        common = {"tier": "film", "width": 1920, "height": 1080}
        control = AB._capture_query(
            **common,
            with_score=False,
            passage_seconds=duration,
        )
        scored = AB._capture_query(
            **common,
            with_score=True,
            passage_seconds=duration,
        )
        self.assertEqual(control["passage-seconds"], str(duration))
        self.assertNotIn("score", control)
        self.assertNotIn("choreography", control)
        self.assertNotIn("passage-seconds", scored)
        self.assertEqual(scored["score"], "music/score.json")
        self.assertEqual(scored["choreography"], "render/choreography.json")

        rendered = {
            "hasMusic": False,
            "hasChoreography": False,
            "passage": 0,
            "passageSeed": AB.PRODUCTION_PASSAGE_SEED,
            "passageT0": 0,
            "passageSeconds": duration,
        }
        AB._assert_control_frame_identity(rendered, duration)
        for field, value in (
            ("hasMusic", True),
            ("hasChoreography", True),
            ("passage", 1),
            ("passage", False),
            ("passageSeed", 1),
            ("passageT0", 1),
            ("passageSeconds", duration - 1),
        ):
            with self.subTest(field=field, value=value):
                stale = {**rendered, field: value}
                with self.assertRaisesRegex(AB.EvidenceError, "score-free passage identity"):
                    AB._assert_control_frame_identity(stale, duration)
        for invalid in (False, 0, float("nan")):
            with self.subTest(invalid_passage_seconds=invalid):
                with self.assertRaisesRegex(AB.EvidenceError, "finite positive passage clock"):
                    AB._capture_query(
                        **common,
                        with_score=False,
                        passage_seconds=invalid,
                    )

    def test_historical_tracked_receipts_are_explicit_and_never_production(self) -> None:
        for relative in (
            "docs/evidence/score-to-motion-ab.json",
            "docs/evidence/score-to-motion-frames.json",
        ):
            path = ROOT / relative
            document = json.loads(path.read_text())
            self.assertEqual(document["evidence_scope"], "historical-fixture-only")
            errors = AB.production_receipt_errors(path, expected={}, recompute_samples=False)
            self.assertEqual(errors, ["historical fixture evidence cannot satisfy the production A/B gate"])

    def test_complete_receipt_binds_every_artifact_and_never_human_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertEqual(errors, [])
            document = json.loads(fixture.receipt.read_text())
            self.assertEqual(document["human_review"], {"status": "not-attested"})
            self.assertNotIn("accepted", fixture.receipt.read_text())

            fixture.write_receipt(lambda value: value.__setitem__("repository_head", "9" * 40))
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertIn("production A/B receipt has stale repository_head", errors)

            fixture.write_receipt(lambda value: value.__setitem__("human_review", {"status": "accepted"}))
            errors = AB.production_receipt_errors(
                fixture.receipt,
                expected=fixture.context,
                recompute_samples=False,
            )
            self.assertIn("production A/B receipt schema failed", errors[0])

    def test_non_anchor_splice_fails_full_frame_producer_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            fixture.with_movie.write_bytes(b"spliced non-anchor review frames")
            fixture.media[fixture.with_movie] |= {
                "sha256": digest(fixture.with_movie),
                "bytes": fixture.with_movie.stat().st_size,
                "video_framehash_sha256": "d" * 64,
                "decoded_rgb_sha256": "e" * 64,
            }
            fixture.write_receipt()
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertIn(
                "with_score review media full decoded video differs from its canonical producer",
                errors,
            )

    def test_producer_modes_and_segment_chain_fail_closed(self) -> None:
        cases = (
            "scored-control",
            "missing-control-timing",
            "stale-control-timing",
            "length-only-control",
            "crossed-control-passage",
            "stale-control-passage-seed",
            "falsy-control-passage",
            "timing-on-with-score",
            "missing-choreography",
            "short-segment",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                fixture = EvidenceFixture(Path(temporary))
                mode = (
                    "control"
                    if case in {
                        "scored-control",
                        "missing-control-timing",
                        "stale-control-timing",
                        "length-only-control",
                        "crossed-control-passage",
                        "stale-control-passage-seed",
                        "falsy-control-passage",
                    }
                    else "with_score"
                )
                concat_path = fixture.producer_paths[mode]
                concat = json.loads(concat_path.read_text())
                segment_path = concat_path.parent / f"{concat['segments'][0]['name']}.receipt.json"
                segment = json.loads(segment_path.read_text())
                if case == "scored-control":
                    segment["inputs"]["music_score"] = {
                        field: fixture.context["score"][field]
                        for field in ("path", "file_sha256", "contract_sha256")
                    }
                elif case == "missing-control-timing":
                    segment["inputs"].pop("passage_timing")
                elif case == "stale-control-timing":
                    segment["inputs"]["timing_score"]["duration_seconds"] = 312.540051998
                elif case == "length-only-control":
                    segment["inputs"].pop("timing_score")
                    segment["inputs"].pop("passage_timing")
                    segment["inputs"]["duration_seconds"] = fixture.context["span"][
                        "duration_seconds"
                    ]
                elif case == "crossed-control-passage":
                    segment["capture"]["passage"]["index"] = 1
                elif case == "stale-control-passage-seed":
                    segment["capture"]["passage"]["seed"] = 1
                elif case == "falsy-control-passage":
                    segment["capture"]["passage"]["index"] = False
                    segment["capture"]["passage"]["t0"] = False
                elif case == "timing-on-with-score":
                    segment["inputs"]["timing_score"] = {
                        "path": fixture.context["score"]["path"],
                        "file_sha256": fixture.context["score"]["file_sha256"],
                        "contract_sha256": fixture.context["score"]["contract_sha256"],
                        "passage_mapping": "native-tempo",
                        "duration_seconds": fixture.context["span"]["duration_seconds"],
                    }
                elif case == "missing-choreography":
                    segment["inputs"].pop("choreography")
                else:
                    segment["frames"] = 29
                    segment["decoded_video"]["frames"] = 29
                segment_path.write_text(json.dumps(segment, indent=2) + "\n")
                concat["segments"][0]["receipt_sha256"] = digest(segment_path)
                concat_path.write_text(json.dumps(concat, indent=2) + "\n")
                fixture.write_receipt()
                with (
                    mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                    mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
                ):
                    errors = AB.production_receipt_errors(
                        fixture.receipt,
                        expected=fixture.context,
                        recompute_samples=False,
                    )
                expected_error = {
                    "scored-control": "control render segment 0 is not score-free",
                    "missing-control-timing": (
                        "control render segment 0 does not bind the selected production span"
                    ),
                    "stale-control-timing": (
                        "control render segment 0 has stale timing-score identity"
                    ),
                    "length-only-control": (
                        "control render segment 0 uses the obsolete length-only override"
                    ),
                    "crossed-control-passage": (
                        "control render segment 0 left the selected production passage"
                    ),
                    "stale-control-passage-seed": (
                        "control render segment 0 left the selected production passage"
                    ),
                    "falsy-control-passage": (
                        "control render segment 0 left the selected production passage"
                    ),
                    "timing-on-with-score": (
                        "with_score render segment timing is not owned solely by its score"
                    ),
                    "missing-choreography": "with_score render segment 0 has stale choreography",
                    "short-segment": "with_score render segment 0 does not own its exact frame range",
                }[case]
                self.assertIn(expected_error, errors)

    def test_evidence_graph_owns_every_render_producer_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            owned = set(AB.evidence_artifact_paths(fixture.receipt))
            for producer in fixture.producer_paths.values():
                self.assertIn(producer.resolve(), owned)
                concat = json.loads(producer.read_text())
                for row in concat["segments"]:
                    self.assertIn(
                        (producer.parent / f"{row['name']}.receipt.json").resolve(),
                        owned,
                    )

    def test_pcm_or_frame_substitution_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            changed = copy.deepcopy(fixture.media[fixture.control_movie])
            changed["audio_pcm_sha256"] = "9" * 64
            with mock.patch.object(
                AB,
                "ffprobe_media",
                side_effect=lambda path: changed if path == fixture.control_movie else fixture.probe(path),
            ), mock.patch.object(
                AB, "review_frame_anchors", side_effect=fixture.anchor_probe
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertTrue(any("control review media audio differs" in error for error in errors), errors)

            Image.new(
                "RGB", (AB.PRODUCTION_WIDTH, AB.PRODUCTION_HEIGHT), "white"
            ).save(fixture.with_paths[1])
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertTrue(any("sample-001 with-score frame digest is stale" in error for error in errors), errors)

    def test_packaged_copy_binds_manifest_span_sound_head_and_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            manifest = fixture.package_manifest()
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
                mock.patch.object(AB, "git_identity", return_value=fixture.context["repository_head"]),
                mock.patch.object(
                    AB,
                    "renderer_source_tree",
                    side_effect=lambda *_args, with_score=True, **_kwargs: (
                        fixture.context["source_tree_sha256"]
                        if with_score
                        else fixture.control_source_tree
                    ),
                ),
                mock.patch.object(AB, "generate_sample_rows", return_value=fixture.rows),
                mock.patch.object(AB, "require_production_tier", return_value=None),
            ):
                errors = AB.packaged_receipt_errors(fixture.root, manifest)
            self.assertEqual(errors, [])

            stale = copy.deepcopy(manifest)
            stale["t1"] = 0.9
            stale["duration"] = 0.9
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
                mock.patch.object(AB, "git_identity", return_value=fixture.context["repository_head"]),
                mock.patch.object(
                    AB,
                    "renderer_source_tree",
                    side_effect=lambda *_args, with_score=True, **_kwargs: (
                        fixture.context["source_tree_sha256"]
                        if with_score
                        else fixture.control_source_tree
                    ),
                ),
                mock.patch.object(AB, "generate_sample_rows", return_value=fixture.rows),
                mock.patch.object(AB, "require_production_tier", return_value=None),
            ):
                errors = AB.packaged_receipt_errors(fixture.root, stale)
            self.assertTrue(any("stale span" in error for error in errors), errors)

            stale = copy.deepcopy(manifest)
            stale["sound"]["master_sha256"] = "8" * 64
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
                mock.patch.object(AB, "git_identity", return_value=fixture.context["repository_head"]),
                mock.patch.object(
                    AB,
                    "renderer_source_tree",
                    side_effect=lambda *_args, with_score=True, **_kwargs: (
                        fixture.context["source_tree_sha256"]
                        if with_score
                        else fixture.control_source_tree
                    ),
                ),
                mock.patch.object(AB, "generate_sample_rows", return_value=fixture.rows),
                mock.patch.object(AB, "require_production_tier", return_value=None),
            ):
                errors = AB.packaged_receipt_errors(fixture.root, stale)
            self.assertTrue(any("score master differs" in error or "stale audio_master" in error for error in errors), errors)

            screen = copy.deepcopy(manifest)
            production_path = fixture.root / screen["production"]["path"]
            production = json.loads(production_path.read_text())
            producer_ref = production["producers"][0]["receipt"]
            producer_path = fixture.root / producer_ref["path"]
            producer = json.loads(producer_path.read_text())
            producer["inputs"]["tier"] = "screen"
            producer_path.write_text(json.dumps(producer, indent=2) + "\n")
            producer_ref["sha256"] = digest(producer_path)
            production_path.write_text(json.dumps(production, indent=2) + "\n")
            screen["production"]["sha256"] = digest(production_path)
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
                mock.patch.object(AB, "git_identity", return_value=fixture.context["repository_head"]),
                mock.patch.object(
                    AB,
                    "renderer_source_tree",
                    side_effect=lambda *_args, with_score=True, **_kwargs: (
                        fixture.context["source_tree_sha256"]
                        if with_score
                        else fixture.control_source_tree
                    ),
                ),
                mock.patch.object(AB, "generate_sample_rows", return_value=fixture.rows),
                mock.patch.object(AB, "require_production_tier", return_value=None),
            ):
                errors = AB.packaged_receipt_errors(fixture.root, screen)
            self.assertIn(
                "render producer receipt does not use the production film tier",
                errors,
            )

    def test_identical_synthetic_movies_and_fixture_gpu_claim_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            frame = json.loads(fixture.frame.read_text())
            frame["capture"] |= {
                "width": 4,
                "height": 3,
                "renderer": "fixture",
            }
            fixture.frame.write_text(json.dumps(frame, indent=2) + "\n")

            def exploit(document: dict) -> None:
                document["frame_receipt"] = reference(fixture.frame, fixture.base)
                document["review_media"]["control"] = copy.deepcopy(
                    document["review_media"]["with_score"]
                )

            fixture.write_receipt(exploit)
            identical = copy.deepcopy(fixture.media[fixture.with_movie])
            with (
                mock.patch.object(AB, "ffprobe_media", return_value=identical),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertTrue(any("frame receipt schema failed" in error for error in errors), errors)
            self.assertIn("with-score and control review movies are byte-identical", errors)
            self.assertIn("with-score and control review movies have identical decoded video", errors)


if __name__ == "__main__":
    unittest.main()
