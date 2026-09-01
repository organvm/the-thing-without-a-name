#!/usr/bin/env python3
"""Adversarial portable checks for production score-to-motion evidence."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
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


def canonical_replay_source_closure(root: Path, *, mode: str = "with_score") -> str:
    """Create a tiny complete source graph with render.py's exact digest order."""

    identity_paths = [
        "film.html",
        "render/program.json",
        "render/render.py",
        "render/browser.py",
        "render/media_identity.py",
        "pipeline/corpus_contract.py",
        "corpus/manifest.json",
        "corpus/room.webp",
        "corpus/score-2017.json",
        f"corpus/tier-receipts/{AB.PRODUCTION_TIER}.json",
        "corpus/manifest.local.json",
        "engine/a.js",
        "engine/z.js",
    ]
    if mode == "with_score":
        identity_paths.extend(["music/score.json", "render/choreography.json"])
    else:
        identity_paths.append("music/score.json")
    identity_paths.extend(
        [
            f"corpus/plates/{AB.PRODUCTION_TIER}/plate-a.webp",
            f"corpus/plates/{AB.PRODUCTION_TIER}/plate-z.webp",
            f"corpus/mattes/{AB.PRODUCTION_TIER}/matte-a.webp",
            f"corpus/mattes/{AB.PRODUCTION_TIER}/matte-z.webp",
        ]
    )
    all_paths = [*identity_paths, "sound/choreography.py", "sound/music_score.py"]
    for index, relative in enumerate(all_paths):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"canonical replay fixture {index}: {relative}\n".encode())

    value = hashlib.sha256()
    for relative in identity_paths:
        value.update(relative.encode())
        value.update(bytes.fromhex(digest(root / relative)))
    return value.hexdigest()


def disappearing_process_group(_pid: int, sig: int) -> None:
    if sig == AB.signal.SIGKILL:
        return
    if sig == 0:
        raise ProcessLookupError
    raise AssertionError(f"unexpected process-group signal {sig}")


def exited_waitid(pid: int, status: int = 0):
    return SimpleNamespace(
        si_pid=pid,
        si_code=AB.os.CLD_EXITED,
        si_status=status,
    )


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
                        "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M5, Unspecified Version)",
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
        self.producer_rgb_by_payload = {}
        self.producer_rgb_by_mode = {
            mode: f"{mode} canonical decoded RGB frames".encode()
            for mode in ("with_score", "control")
        }
        for mode, movie in (("with_score", self.with_movie), ("control", self.control_movie)):
            decoded_sha256 = hashlib.sha256(self.producer_rgb_by_mode[mode]).hexdigest()
            self.media[movie]["video_framehash_sha256"] = decoded_sha256
            self.media[movie]["decoded_rgb_sha256"] = decoded_sha256
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
        self.producer_media_paths = {}
        self.producer_decoded_by_payload = {}
        self.replay_by_mode = {}
        self.replay_calls = []
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
            segment_media = producer_root / segment_name
            segment_payload = f"{mode} canonical segment media".encode()
            segment_media.write_bytes(segment_payload)
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
                            "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M5, Unspecified Version)",
                            "raw_rgba_sha256": (
                                "5" if mode == "with_score" else "6"
                            )
                            * 64,
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
                        "file_sha256": digest(segment_media),
                        "file_bytes": segment_media.stat().st_size,
                    },
                    indent=2,
                )
                + "\n"
            )
            segment_document = json.loads(segment_receipt.read_text())
            self.replay_by_mode[mode] = (
                copy.deepcopy(segment_document["capture"]),
                copy.deepcopy(segment_document["decoded_video"]),
            )
            concat_media = producer_root / f"{mode}.mov"
            concat_payload = f"{mode} canonical concat media".encode()
            concat_media.write_bytes(concat_payload)
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
                        "file_sha256": digest(concat_media),
                        "file_bytes": concat_media.stat().st_size,
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
            decoded = {
                "algorithm": "rgb24-stream-sha256-v1",
                "sha256": self.media[movie]["decoded_rgb_sha256"],
                "frames": 30,
                "width": AB.PRODUCTION_WIDTH,
                "height": AB.PRODUCTION_HEIGHT,
            }
            self.producer_decoded_by_payload[segment_payload] = decoded
            self.producer_decoded_by_payload[concat_payload] = decoded
            self.producer_rgb_by_payload[segment_payload] = self.producer_rgb_by_mode[mode]
            self.producer_rgb_by_payload[concat_payload] = self.producer_rgb_by_mode[mode]
            self.producer_paths[mode] = concat_receipt
            self.producer_media_paths[mode] = {
                "concat": concat_media,
                "segments": [segment_media],
            }
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

    def probe(self, path: Path, **_kwargs) -> dict:
        return copy.deepcopy(self.media[path])

    def anchor_probe(
        self,
        path: Path,
        *,
        frame_path: Path,
        frame: dict,
        mode: str,
        expected_frames: int | None = None,
        source_fd: int | None = None,
    ) -> list[dict]:
        return copy.deepcopy(self.anchors[mode])

    def producer_probe(
        self,
        path: Path,
        *,
        expected_frames: int,
        include_fps: bool,
        label: str,
        source_fd: int | None = None,
        aggregate_digest=None,
    ) -> dict:
        try:
            payload = (
                path.read_bytes()
                if source_fd is None
                else AB.os.pread(source_fd, AB.os.fstat(source_fd).st_size, 0)
            )
            identity = copy.deepcopy(self.producer_decoded_by_payload[payload])
        except (KeyError, OSError) as exc:
            raise AB.EvidenceError(f"{label} media cannot be authenticated") from exc
        if identity["frames"] != expected_frames:
            raise AB.EvidenceError(
                f"{label} media cannot be authenticated: decoded frame count is stale"
            )
        if include_fps:
            identity["fps"] = AB.PRODUCTION_FPS
        if aggregate_digest is not None:
            aggregate_digest.update(self.producer_rgb_by_payload[payload])
        return identity

    def replay_probe(self, **kwargs):
        mode = kwargs["mode"]
        self.replay_calls.append(copy.deepcopy(kwargs))
        return copy.deepcopy(self.replay_by_mode[mode])

    @contextmanager
    def producer_patch(self):
        with mock.patch.object(
            AB,
            "_producer_decoded_video_identity",
            side_effect=self.producer_probe,
        ), mock.patch.object(
            AB,
            "_canonical_segment_replay",
            side_effect=self.replay_probe,
        ):
            yield

    def production_errors(self) -> list[str]:
        with (
            mock.patch.object(AB, "ffprobe_media", side_effect=self.probe),
            mock.patch.object(AB, "review_frame_anchors", side_effect=self.anchor_probe),
            self.producer_patch(),
        ):
            return AB.production_receipt_errors(
                self.receipt,
                expected=self.context,
                recompute_samples=False,
            )

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
                mock.patch.object(
                    AB, "review_frame_anchors", side_effect=fixture.anchor_probe
                ),
                fixture.producer_patch(),
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

            fixture.write_receipt(
                lambda value: value["review_media"]["with_score"].__setitem__(
                    "decoded_rgb_sha256", "0" * 64
                )
            )
            errors = fixture.production_errors()
            self.assertIn("with_score review media has stale decoded_rgb_sha256", errors)

            fixture.write_receipt(lambda value: value.__setitem__("repository_head", "9" * 40))
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(
                    AB, "review_frame_anchors", side_effect=fixture.anchor_probe
                ),
                fixture.producer_patch(),
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertIn("production A/B receipt has stale repository_head", errors)

            fixture.write_receipt(lambda value: value.__setitem__("human_review", {"status": "accepted"}))
            with fixture.producer_patch(), mock.patch.object(
                AB, "ffprobe_media", side_effect=fixture.probe
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertIn("production A/B receipt schema failed", errors[0])

    def test_write_receipt_decodes_each_full_media_graph_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe) as review_probe,
                mock.patch.object(
                    AB, "review_frame_anchors", side_effect=fixture.anchor_probe
                ) as anchor_probe,
                mock.patch.object(
                    AB,
                    "_producer_decoded_video_identity",
                    side_effect=fixture.producer_probe,
                ) as producer_probe,
                mock.patch.object(
                    AB,
                    "_canonical_segment_replay",
                    side_effect=fixture.replay_probe,
                ) as replay_probe,
                mock.patch.object(AB, "generate_sample_rows", return_value=fixture.rows),
            ):
                document = AB.write_receipt(
                    destination=fixture.receipt,
                    context=fixture.context,
                    sample_path=fixture.sample,
                    frame_path=fixture.frame,
                    with_score=fixture.with_movie,
                    control=fixture.control_movie,
                    with_score_producer=fixture.producer_paths["with_score"],
                    control_producer=fixture.producer_paths["control"],
                    root=ROOT,
                )
            self.assertEqual(document["human_review"], {"status": "not-attested"})
            self.assertEqual(review_probe.call_count, 2)
            self.assertEqual(anchor_probe.call_count, 2)
            self.assertEqual(producer_probe.call_count, 4)
            self.assertEqual(replay_probe.call_count, 2)
            self.assertEqual(
                [(call.kwargs["mode"], call.kwargs["ordinal"]) for call in replay_probe.call_args_list],
                [("with_score", 0), ("control", 0)],
            )
            for probe_call, anchor_call in zip(
                review_probe.call_args_list,
                anchor_probe.call_args_list,
                strict=True,
            ):
                self.assertEqual(
                    probe_call.kwargs["source_fd"],
                    anchor_call.kwargs["source_fd"],
                )
            labels = [call.kwargs["label"] for call in producer_probe.call_args_list]
            self.assertEqual(
                labels,
                [
                    "with_score render concat",
                    "with_score render segment 0",
                    "control render concat",
                    "control render segment 0",
                ],
            )

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
                fixture.producer_patch(),
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

    def test_gpu_capture_and_decoded_pixels_require_canonical_replay(self) -> None:
        with self.subTest("forged raw GPU digest"), tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            concat_path = fixture.producer_paths["with_score"]
            concat = json.loads(concat_path.read_text())
            segment_path = concat_path.parent / f"{concat['segments'][0]['name']}.receipt.json"
            segment = json.loads(segment_path.read_text())
            segment["capture"]["raw_rgba_sha256"] = "f" * 64
            segment_path.write_text(json.dumps(segment, indent=2) + "\n")
            concat["segments"][0]["receipt_sha256"] = digest(segment_path)
            concat_path.write_text(json.dumps(concat, indent=2) + "\n")
            fixture.write_receipt()

            errors = fixture.production_errors()
            self.assertIn(
                "with_score render segment 0 GPU-frame sequence digest differs from canonical replay",
                errors,
            )

        with self.subTest("forged full decoded stream"), tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            mode = "with_score"
            concat_path = fixture.producer_paths[mode]
            concat_media = fixture.producer_media_paths[mode]["concat"]
            segment_media = fixture.producer_media_paths[mode]["segments"][0]
            concat = json.loads(concat_path.read_text())
            segment_path = segment_media.with_name(segment_media.name + ".receipt.json")
            segment = json.loads(segment_path.read_text())

            forged_rgb = b"forged full decoded producer stream"
            forged = {
                "algorithm": "rgb24-stream-sha256-v1",
                "sha256": hashlib.sha256(forged_rgb).hexdigest(),
                "frames": 30,
                "width": AB.PRODUCTION_WIDTH,
                "height": AB.PRODUCTION_HEIGHT,
            }
            segment_payload = segment_media.read_bytes()
            concat_payload = concat_media.read_bytes()
            fixture.producer_decoded_by_payload[segment_payload] = copy.deepcopy(forged)
            fixture.producer_decoded_by_payload[concat_payload] = copy.deepcopy(forged)
            fixture.producer_rgb_by_payload[segment_payload] = forged_rgb
            fixture.media[fixture.with_movie]["video_framehash_sha256"] = forged["sha256"]
            fixture.media[fixture.with_movie]["decoded_rgb_sha256"] = forged["sha256"]
            segment["decoded_video"] = copy.deepcopy(forged)
            segment_path.write_text(json.dumps(segment, indent=2) + "\n")
            concat["segments"][0]["receipt_sha256"] = digest(segment_path)
            concat["decoded_video"] = {**forged, "fps": AB.PRODUCTION_FPS}
            concat_path.write_text(json.dumps(concat, indent=2) + "\n")
            fixture.write_receipt()

            errors = fixture.production_errors()
            self.assertIn(
                "with_score render segment 0 decoded pixels differ from canonical Apple-Metal replay",
                errors,
            )

    def test_canonical_replay_has_no_portable_acceptance_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            segment_media = fixture.producer_media_paths["with_score"]["segments"][0]
            segment = json.loads(
                segment_media.with_name(segment_media.name + ".receipt.json").read_text()
            )
            with mock.patch.object(AB.sys, "platform", "linux"), self.assertRaisesRegex(
                AB.EvidenceError,
                "authorized macOS Apple-Metal host",
            ):
                AB._canonical_segment_replay(
                    mode="with_score",
                    ordinal=0,
                    frames=30,
                    segment_frames=30,
                    expected=fixture.context,
                    original_inputs=segment["inputs"],
                    root=ROOT,
                )

    def test_canonical_replay_source_snapshot_rejects_restored_mutations(self) -> None:
        def replay_inputs(fixture, source_root):
            segment_media = fixture.producer_media_paths["with_score"]["segments"][0]
            segment = json.loads(
                segment_media.with_name(segment_media.name + ".receipt.json").read_text()
            )
            inputs = copy.deepcopy(segment["inputs"])
            inputs["source_tree_sha256"] = canonical_replay_source_closure(source_root)
            return inputs

        with (
            self.subTest("source bytes overwritten then restored"),
            tempfile.TemporaryDirectory() as evidence_temporary,
            tempfile.TemporaryDirectory(dir=ROOT.parent) as source_temporary,
        ):
            fixture = EvidenceFixture(Path(evidence_temporary))
            source_root = Path(source_temporary)
            inputs = replay_inputs(fixture, source_root)
            target = source_root / "engine/a.js"
            original = target.read_bytes()
            original_mode = target.stat().st_mode & 0o777

            class MutatingReplay:
                pid = 4545

                @staticmethod
                def wait(timeout=None):
                    return 0

                @staticmethod
                def poll():
                    return 0

            def launch_mutating(*_args, **_kwargs):
                # Exercise the gap inside Popen itself: the source is restored
                # before the caller receives a process handle.
                target.write_bytes(b"substituted renderer source\n")
                target.write_bytes(original)
                # Make the ctime transition explicit even on a coarse test FS.
                target.chmod(0o600)
                target.chmod(original_mode)
                return MutatingReplay()

            with (
                mock.patch.object(AB.sys, "platform", "darwin"),
                mock.patch.object(AB, "require_production_tier", return_value=None),
                mock.patch.object(AB.subprocess, "Popen", side_effect=launch_mutating),
                mock.patch.object(
                    AB.os,
                    "waitid",
                    side_effect=lambda _kind, pid, _flags: exited_waitid(pid),
                ),
                mock.patch.object(
                    AB.os,
                    "killpg",
                    side_effect=disappearing_process_group,
                ) as kill_group,
                self.assertRaisesRegex(
                    AB.EvidenceError,
                    r"canonical replay source engine/a\.js changed during authentication",
                ),
            ):
                AB._canonical_segment_replay(
                    mode="with_score",
                    ordinal=0,
                    frames=30,
                    segment_frames=30,
                    expected=fixture.context,
                    original_inputs=inputs,
                    root=source_root,
                )
            self.assertEqual(
                kill_group.call_args_list,
                [
                    mock.call(MutatingReplay.pid, AB.signal.SIGKILL),
                    mock.call(MutatingReplay.pid, 0),
                ],
            )
            self.assertEqual(target.read_bytes(), original)

        with (
            self.subTest("ancestor directory swapped then restored"),
            tempfile.TemporaryDirectory() as evidence_temporary,
            tempfile.TemporaryDirectory(dir=ROOT.parent) as source_temporary,
        ):
            fixture = EvidenceFixture(Path(evidence_temporary))
            source_root = Path(source_temporary)
            inputs = replay_inputs(fixture, source_root)
            alternate = source_root / "render-alternate"
            alternate.mkdir()
            for path in (source_root / "render").iterdir():
                if path.is_file():
                    (alternate / path.name).write_bytes(path.read_bytes())

            class SwappingReplay:
                pid = 4646

                @staticmethod
                def wait(timeout=None):
                    original = source_root / "render"
                    parked = source_root / "render-authenticated-original"
                    original.rename(parked)
                    alternate.rename(original)
                    original.rename(alternate)
                    parked.rename(original)
                    return 0

                @staticmethod
                def poll():
                    return 0

            with (
                mock.patch.object(AB.sys, "platform", "darwin"),
                mock.patch.object(AB, "require_production_tier", return_value=None),
                mock.patch.object(AB.subprocess, "Popen", return_value=SwappingReplay()),
                mock.patch.object(
                    AB.os,
                    "waitid",
                    side_effect=lambda _kind, pid, _flags: exited_waitid(pid),
                ),
                mock.patch.object(
                    AB.os,
                    "killpg",
                    side_effect=disappearing_process_group,
                ) as kill_group,
                self.assertRaisesRegex(
                    AB.EvidenceError,
                    r"canonical replay source ancestor .* changed during authentication",
                ),
            ):
                AB._canonical_segment_replay(
                    mode="with_score",
                    ordinal=0,
                    frames=30,
                    segment_frames=30,
                    expected=fixture.context,
                    original_inputs=inputs,
                    root=source_root,
                )
            self.assertEqual(
                kill_group.call_args_list,
                [
                    mock.call(SwappingReplay.pid, AB.signal.SIGKILL),
                    mock.call(SwappingReplay.pid, 0),
                ],
            )
            self.assertTrue((source_root / "render/render.py").is_file())

    def test_canonical_replay_source_snapshot_normal_lifecycle(self) -> None:
        with (
            tempfile.TemporaryDirectory() as evidence_temporary,
            # Source and replay output deliberately share the platform temp
            # ancestor; replay-temp creation must precede its ctime snapshot.
            tempfile.TemporaryDirectory() as source_temporary,
        ):
            fixture = EvidenceFixture(Path(evidence_temporary))
            source_root = Path(source_temporary)
            source_digest = canonical_replay_source_closure(source_root)
            segment_media = fixture.producer_media_paths["with_score"]["segments"][0]
            segment = json.loads(
                segment_media.with_name(segment_media.name + ".receipt.json").read_text()
            )
            inputs = copy.deepcopy(segment["inputs"])
            inputs["source_tree_sha256"] = source_digest
            capture, decoded = copy.deepcopy(fixture.replay_by_mode["with_score"])
            snapshots = []
            lifecycle = []
            real_snapshot = AB._pinned_canonical_replay_source_snapshot

            @contextmanager
            def observing_snapshot(*args, **kwargs):
                with real_snapshot(*args, **kwargs) as snapshot:
                    snapshots.append(snapshot)
                    yield snapshot

            class SuccessfulReplay:
                pid = 4747

                @staticmethod
                def wait(timeout=None):
                    lifecycle.append("wait")
                    self.assertEqual(len(snapshots), 1)
                    for record in snapshots[0]._directories:
                        self.assertTrue(AB.stat.S_ISDIR(AB.os.fstat(record["fd"]).st_mode))
                    return 0

                @staticmethod
                def poll():
                    return 0

            def launch(command, **kwargs):
                output_root = Path(command[command.index("--out") + 1])
                self.assertEqual(
                    command[:6],
                    [
                        AB.sys.executable,
                        "-I",
                        "-B",
                        "-X",
                        f"pycache_prefix={AB.os.devnull}",
                        str(source_root / "render/render.py"),
                    ],
                )
                self.assertEqual(kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
                self.assertEqual(kwargs["env"]["PYTHONPYCACHEPREFIX"], AB.os.devnull)
                self.assertEqual(kwargs["stdout"].name, AB.os.devnull)
                self.assertEqual(kwargs["stderr"], AB.subprocess.STDOUT)
                self.assertEqual(kwargs["stdout"].write(b"noisy renderer\n" * 1024), 15 * 1024)
                self.assertFalse((output_root / ".pycache").exists())
                media = output_root / "passage-20170620-seg-000.mov"
                media.write_bytes(b"canonical replay media")
                media.with_name(media.name + ".receipt.json").write_text(
                    json.dumps(
                        {
                            "schema": "danse.render.segment.v1",
                            "segment": 0,
                            "frames": 30,
                            "inputs": inputs,
                            "capture": capture,
                            "decoded_video": decoded,
                            "file_sha256": digest(media),
                            "file_bytes": media.stat().st_size,
                        },
                        indent=2,
                    )
                    + "\n"
                )
                return SuccessfulReplay()

            def observe_waitid(kind, pid, flags):
                lifecycle.append("waitid")
                self.assertEqual(kind, AB.os.P_PID)
                self.assertEqual(flags, AB.os.WEXITED | AB.os.WNOWAIT | AB.os.WNOHANG)
                return exited_waitid(pid)

            def kill_group(pid, sig):
                lifecycle.append(f"killpg:{sig}")
                return disappearing_process_group(pid, sig)

            with (
                mock.patch.object(AB.sys, "platform", "darwin"),
                mock.patch.object(AB, "require_production_tier", return_value=None),
                mock.patch.object(
                    AB,
                    "_pinned_canonical_replay_source_snapshot",
                    side_effect=observing_snapshot,
                ),
                mock.patch.object(AB.subprocess, "Popen", side_effect=launch),
                mock.patch.object(AB.os, "waitid", side_effect=observe_waitid),
                mock.patch.object(
                    AB.os,
                    "killpg",
                    side_effect=kill_group,
                ) as kill_group,
                mock.patch.object(
                    AB,
                    "_producer_media_identity_errors",
                    return_value=([], decoded),
                ),
            ):
                replay_capture, replay_decoded = AB._canonical_segment_replay(
                    mode="with_score",
                    ordinal=0,
                    frames=30,
                    segment_frames=30,
                    expected=fixture.context,
                    original_inputs=inputs,
                    root=source_root,
                )
            self.assertEqual(replay_capture, capture)
            self.assertEqual(replay_decoded, decoded)
            self.assertEqual(
                kill_group.call_args_list,
                [
                    mock.call(SuccessfulReplay.pid, AB.signal.SIGKILL),
                    mock.call(SuccessfulReplay.pid, 0),
                ],
            )
            self.assertEqual(
                lifecycle,
                ["waitid", f"killpg:{AB.signal.SIGKILL}", "wait", "killpg:0"],
            )
            self.assertTrue(snapshots[0]._closed)
            for record in snapshots[0]._directories:
                with self.assertRaises(OSError):
                    AB.os.fstat(record["fd"])

            stale_inputs = copy.deepcopy(inputs)
            stale_inputs["source_tree_sha256"] = "0" * 64
            with (
                mock.patch.object(AB.sys, "platform", "darwin"),
                mock.patch.object(AB, "require_production_tier", return_value=None),
                mock.patch.object(AB.subprocess, "Popen") as popen,
                self.assertRaisesRegex(
                    AB.EvidenceError,
                    "source tree differs from its producer inputs",
                ),
            ):
                AB._canonical_segment_replay(
                    mode="with_score",
                    ordinal=0,
                    frames=30,
                    segment_frames=30,
                    expected=fixture.context,
                    original_inputs=stale_inputs,
                    root=source_root,
                )
            popen.assert_not_called()

    def test_failed_canonical_replay_terminates_its_worker_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            segment_media = fixture.producer_media_paths["with_score"]["segments"][0]
            segment = json.loads(
                segment_media.with_name(segment_media.name + ".receipt.json").read_text()
            )

            class StableSourceSnapshot:
                def __init__(self, source_tree_sha256):
                    self.source_tree_sha256 = source_tree_sha256

                @staticmethod
                def revalidate():
                    return None

            @contextmanager
            def stable_source_snapshot(_root, *, original_inputs, **_kwargs):
                yield StableSourceSnapshot(original_inputs["source_tree_sha256"])

            class FailedReplay:
                pid = 4242

                @staticmethod
                def wait(timeout=None):
                    return 1

                @staticmethod
                def poll():
                    return 1

            with (
                mock.patch.object(AB.sys, "platform", "darwin"),
                mock.patch.object(AB, "require_production_tier", return_value=None),
                mock.patch.object(
                    AB,
                    "repository_file",
                    return_value=ROOT / "render/render.py",
                ),
                mock.patch.object(
                    AB,
                    "_pinned_canonical_replay_source_snapshot",
                    side_effect=stable_source_snapshot,
                ),
                mock.patch.object(AB.subprocess, "Popen", return_value=FailedReplay()),
                mock.patch.object(
                    AB.os,
                    "waitid",
                    side_effect=lambda _kind, pid, _flags: exited_waitid(pid, 1),
                ),
                mock.patch.object(
                    AB.os,
                    "killpg",
                    side_effect=disappearing_process_group,
                ) as kill_group,
                self.assertRaisesRegex(AB.EvidenceError, "canonical with_score segment 0 replay failed"),
            ):
                AB._canonical_segment_replay(
                    mode="with_score",
                    ordinal=0,
                    frames=30,
                    segment_frames=30,
                    expected=fixture.context,
                    original_inputs=segment["inputs"],
                    root=ROOT,
                )
            self.assertEqual(
                kill_group.call_args_list,
                [
                    mock.call(FailedReplay.pid, AB.signal.SIGKILL),
                    mock.call(FailedReplay.pid, 0),
                ],
            )

            class KillErrorReplay:
                pid = 4262

                def __init__(self):
                    self.wait_timeouts = []

                def wait(self, timeout=None):
                    self.wait_timeouts.append(timeout)
                    return 0

            kill_error = KillErrorReplay()
            with (
                mock.patch.object(AB.sys, "platform", "darwin"),
                mock.patch.object(AB, "require_production_tier", return_value=None),
                mock.patch.object(
                    AB,
                    "repository_file",
                    return_value=ROOT / "render/render.py",
                ),
                mock.patch.object(
                    AB,
                    "_pinned_canonical_replay_source_snapshot",
                    side_effect=stable_source_snapshot,
                ),
                mock.patch.object(AB.subprocess, "Popen", return_value=kill_error),
                mock.patch.object(
                    AB.os,
                    "waitid",
                    side_effect=lambda _kind, pid, _flags: exited_waitid(pid),
                ),
                mock.patch.object(
                    AB.os,
                    "killpg",
                    side_effect=PermissionError("process-group signal denied"),
                ),
                self.assertRaisesRegex(
                    AB.EvidenceError,
                    "cannot terminate canonical with_score segment 0 replay workers",
                ),
            ):
                AB._canonical_segment_replay(
                    mode="with_score",
                    ordinal=0,
                    frames=30,
                    segment_frames=30,
                    expected=fixture.context,
                    original_inputs=segment["inputs"],
                    root=ROOT,
                )
            self.assertEqual(
                kill_error.wait_timeouts,
                [AB.CANONICAL_REPLAY_CLEANUP_SECONDS],
            )

            class TimedOutReplay:
                pid = 4272

                def __init__(self):
                    self.wait_timeouts = []

                def wait(self, timeout=None):
                    self.wait_timeouts.append(timeout)
                    return -AB.signal.SIGKILL

            timed_out = TimedOutReplay()
            with (
                mock.patch.object(AB.sys, "platform", "darwin"),
                mock.patch.object(AB, "require_production_tier", return_value=None),
                mock.patch.object(
                    AB,
                    "repository_file",
                    return_value=ROOT / "render/render.py",
                ),
                mock.patch.object(
                    AB,
                    "_pinned_canonical_replay_source_snapshot",
                    side_effect=stable_source_snapshot,
                ),
                mock.patch.object(AB.subprocess, "Popen", return_value=timed_out),
                mock.patch.object(AB.os, "waitid", return_value=None),
                mock.patch.object(
                    AB.os,
                    "killpg",
                    side_effect=disappearing_process_group,
                ) as kill_group,
                mock.patch.object(
                    AB.time,
                    "monotonic",
                    side_effect=[0.0, 901.0, 902.0],
                ),
                mock.patch.object(AB.time, "sleep") as sleep,
                self.assertRaisesRegex(
                    AB.EvidenceError,
                    "canonical with_score segment 0 replay timed out",
                ),
            ):
                AB._canonical_segment_replay(
                    mode="with_score",
                    ordinal=0,
                    frames=30,
                    segment_frames=30,
                    expected=fixture.context,
                    original_inputs=segment["inputs"],
                    root=ROOT,
                )
            self.assertEqual(
                kill_group.call_args_list,
                [
                    mock.call(TimedOutReplay.pid, AB.signal.SIGKILL),
                    mock.call(TimedOutReplay.pid, 0),
                ],
            )
            self.assertEqual(
                timed_out.wait_timeouts,
                [AB.CANONICAL_REPLAY_CLEANUP_SECONDS],
            )
            sleep.assert_not_called()

            class LingeringGroupReplay:
                pid = 4292

                @staticmethod
                def wait(timeout=None):
                    return 0

                @staticmethod
                def poll():
                    return 0

            with (
                mock.patch.object(AB.sys, "platform", "darwin"),
                mock.patch.object(AB, "require_production_tier", return_value=None),
                mock.patch.object(
                    AB,
                    "repository_file",
                    return_value=ROOT / "render/render.py",
                ),
                mock.patch.object(
                    AB,
                    "_pinned_canonical_replay_source_snapshot",
                    side_effect=stable_source_snapshot,
                ),
                mock.patch.object(
                    AB.subprocess,
                    "Popen",
                    return_value=LingeringGroupReplay(),
                ),
                mock.patch.object(
                    AB.os,
                    "waitid",
                    side_effect=lambda _kind, pid, _flags: exited_waitid(pid),
                ),
                mock.patch.object(AB.os, "killpg", return_value=None) as kill_group,
                mock.patch.object(
                    AB.time,
                    "monotonic",
                    side_effect=[0.0, 100.0, 111.0],
                ),
                mock.patch.object(AB.time, "sleep") as sleep,
                self.assertRaisesRegex(
                    AB.EvidenceError,
                    "replay process group did not terminate",
                ),
            ):
                AB._canonical_segment_replay(
                    mode="with_score",
                    ordinal=0,
                    frames=30,
                    segment_frames=30,
                    expected=fixture.context,
                    original_inputs=segment["inputs"],
                    root=ROOT,
                )
            self.assertEqual(
                kill_group.call_args_list,
                [
                    mock.call(LingeringGroupReplay.pid, AB.signal.SIGKILL),
                    mock.call(LingeringGroupReplay.pid, 0),
                ],
            )
            sleep.assert_not_called()

            class StuckReplay:
                pid = 4343

                def __init__(self):
                    self.wait_timeouts = []

                def wait(self, timeout=None):
                    self.wait_timeouts.append(timeout)
                    raise AB.subprocess.TimeoutExpired("canonical replay", timeout)

                @staticmethod
                def poll():
                    return None

            stuck = StuckReplay()
            with (
                mock.patch.object(AB.sys, "platform", "darwin"),
                mock.patch.object(AB, "require_production_tier", return_value=None),
                mock.patch.object(
                    AB,
                    "repository_file",
                    return_value=ROOT / "render/render.py",
                ),
                mock.patch.object(
                    AB,
                    "_pinned_canonical_replay_source_snapshot",
                    side_effect=stable_source_snapshot,
                ),
                mock.patch.object(AB.subprocess, "Popen", return_value=stuck),
                mock.patch.object(
                    AB.os,
                    "waitid",
                    side_effect=lambda _kind, pid, _flags: exited_waitid(pid),
                ),
                mock.patch.object(
                    AB.os,
                    "killpg",
                    side_effect=disappearing_process_group,
                ) as kill_group,
                self.assertRaisesRegex(AB.EvidenceError, "workers did not terminate"),
            ):
                AB._canonical_segment_replay(
                    mode="with_score",
                    ordinal=0,
                    frames=30,
                    segment_frames=30,
                    expected=fixture.context,
                    original_inputs=segment["inputs"],
                    root=ROOT,
                )
            self.assertEqual(
                stuck.wait_timeouts,
                [AB.CANONICAL_REPLAY_CLEANUP_SECONDS],
            )
            kill_group.assert_called_once_with(StuckReplay.pid, AB.signal.SIGKILL)

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
                    fixture.producer_patch(),
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

    def test_producer_media_files_and_encoded_identities_fail_closed(self) -> None:
        cases = (
            "missing-concat",
            "missing-segment",
            "missing-segment-receipt",
            "stale-segment-receipt",
            "symlink-segment",
            "stale-concat-bytes",
            "stale-segment-digest",
            "boolean-concat-byte-count",
            "zero-segment-byte-count",
            "duplicate-segment",
            "out-of-order-segment-name",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                fixture = EvidenceFixture(Path(temporary))
                mode = "with_score"
                concat_path = fixture.producer_paths[mode]
                concat_media = fixture.producer_media_paths[mode]["concat"]
                segment_media = fixture.producer_media_paths[mode]["segments"][0]
                concat = json.loads(concat_path.read_text())
                segment_path = segment_media.with_name(segment_media.name + ".receipt.json")
                segment = json.loads(segment_path.read_text())
                rewrite_concat = False
                rewrite_segment = False
                if case == "missing-concat":
                    concat_media.unlink()
                elif case == "missing-segment":
                    segment_media.unlink()
                elif case == "missing-segment-receipt":
                    segment_path.unlink()
                elif case == "stale-segment-receipt":
                    segment_path.write_text(segment_path.read_text() + "\n")
                elif case == "symlink-segment":
                    segment_media.unlink()
                    segment_media.symlink_to(fixture.producer_media_paths["control"]["segments"][0])
                elif case == "stale-concat-bytes":
                    concat_media.write_bytes(b"changed concat bytes")
                elif case == "stale-segment-digest":
                    segment_media.write_bytes(b"changed segment bytes")
                elif case == "boolean-concat-byte-count":
                    concat["file_bytes"] = False
                    rewrite_concat = True
                elif case == "zero-segment-byte-count":
                    segment["file_bytes"] = 0
                    rewrite_segment = True
                elif case == "duplicate-segment":
                    concat["segments"].append(copy.deepcopy(concat["segments"][0]))
                    rewrite_concat = True
                elif case == "out-of-order-segment-name":
                    concat["segments"][0]["name"] = "with_score-seg-001.mov"
                    rewrite_concat = True

                if rewrite_segment:
                    segment_path.write_text(json.dumps(segment, indent=2) + "\n")
                    concat["segments"][0]["receipt_sha256"] = digest(segment_path)
                    rewrite_concat = True
                if rewrite_concat:
                    concat_path.write_text(json.dumps(concat, indent=2) + "\n")
                    fixture.write_receipt()
                errors = fixture.production_errors()
                expected = {
                    "missing-concat": "with_score render concat media is missing",
                    "missing-segment": "review-media render segment 0 media is missing",
                    "missing-segment-receipt": "review-media render segment 0 receipt is missing",
                    "stale-segment-receipt": "render segment 0 receipt digest is stale",
                    "symlink-segment": "review-media render segment 0 media traverses a symlink",
                    "stale-concat-bytes": "with_score render concat encoded output",
                    "stale-segment-digest": "with_score render segment 0 encoded output",
                    "boolean-concat-byte-count": (
                        "with_score render concat has no exact encoded output byte count"
                    ),
                    "zero-segment-byte-count": (
                        "with_score render segment 0 has no exact encoded output byte count"
                    ),
                    "duplicate-segment": "review-media render segment 1 reuses media",
                    "out-of-order-segment-name": "is not the canonical with_score-seg-000.mov",
                }[case]
                self.assertTrue(any(expected in error for error in errors), errors)

    def test_rehashed_swapped_producer_media_still_fails_decoded_binding(self) -> None:
        for target in ("concat", "segment"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                fixture = EvidenceFixture(Path(temporary))
                left = fixture.producer_media_paths["with_score"]
                right = fixture.producer_media_paths["control"]
                left_path = left["concat"] if target == "concat" else left["segments"][0]
                right_path = right["concat"] if target == "concat" else right["segments"][0]
                left_bytes, right_bytes = left_path.read_bytes(), right_path.read_bytes()
                left_path.write_bytes(right_bytes)
                right_path.write_bytes(left_bytes)
                for mode, media_path in (("with_score", left_path), ("control", right_path)):
                    concat_path = fixture.producer_paths[mode]
                    concat = json.loads(concat_path.read_text())
                    receipt_path = (
                        concat_path
                        if target == "concat"
                        else media_path.with_name(media_path.name + ".receipt.json")
                    )
                    receipt = json.loads(receipt_path.read_text())
                    receipt["file_sha256"] = digest(media_path)
                    receipt["file_bytes"] = media_path.stat().st_size
                    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
                    if target == "segment":
                        concat["segments"][0]["receipt_sha256"] = digest(receipt_path)
                        concat_path.write_text(json.dumps(concat, indent=2) + "\n")
                fixture.write_receipt()
                errors = fixture.production_errors()
                if target == "concat":
                    self.assertTrue(
                        any("render concat decoded video identity is stale" in error for error in errors),
                        errors,
                    )
                else:
                    self.assertTrue(
                        any("render segment 0 decoded video identity is stale" in error for error in errors),
                        errors,
                    )
                self.assertTrue(
                    any("segment decoded chain differs from its actual concat" in error for error in errors),
                    errors,
                )

    def test_segment_chain_aggregate_must_equal_the_actual_concat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            mode = "with_score"
            concat_path = fixture.producer_paths[mode]
            concat = json.loads(concat_path.read_text())
            first_media = fixture.producer_media_paths[mode]["segments"][0]
            first_receipt_path = first_media.with_name(first_media.name + ".receipt.json")
            first = json.loads(first_receipt_path.read_text())
            first["frames"] = 15
            first["inputs"]["segment_frames"] = 15
            first["decoded_video"]["frames"] = 15
            first["decoded_video"]["sha256"] = "1" * 64
            first_receipt_path.write_text(json.dumps(first, indent=2) + "\n")

            second_media = concat_path.parent / "with_score-seg-001.mov"
            second_media.write_bytes(b"with_score second canonical segment")
            second = copy.deepcopy(first)
            second["segment"] = 1
            second["file_sha256"] = digest(second_media)
            second["file_bytes"] = second_media.stat().st_size
            second["decoded_video"]["sha256"] = "2" * 64
            second_receipt_path = second_media.with_name(second_media.name + ".receipt.json")
            second_receipt_path.write_text(json.dumps(second, indent=2) + "\n")
            concat["segments"] = [
                {"name": first_media.name, "receipt_sha256": digest(first_receipt_path)},
                {"name": second_media.name, "receipt_sha256": digest(second_receipt_path)},
            ]
            concat_path.write_text(json.dumps(concat, indent=2) + "\n")
            fixture.write_receipt()

            individual = [first["decoded_video"], second["decoded_video"]]

            def producer_probe(path, *, expected_frames, include_fps, label, source_fd=None,
                               aggregate_digest=None):
                if label.startswith("with_score render segment"):
                    ordinal = int(label.rsplit(" ", 1)[1])
                    identity = copy.deepcopy(individual[ordinal])
                    self.assertEqual(identity["frames"], expected_frames)
                    if aggregate_digest is not None:
                        aggregate_digest.update(f"different decoded segment {ordinal}".encode())
                    return identity
                return fixture.producer_probe(
                    path,
                    expected_frames=expected_frames,
                    include_fps=include_fps,
                    label=label,
                    source_fd=source_fd,
                    aggregate_digest=aggregate_digest,
                )

            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
                mock.patch.object(
                    AB,
                    "_producer_decoded_video_identity",
                    side_effect=producer_probe,
                ),
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertIn(
                "with_score render segment decoded chain differs from its actual concat",
                errors,
            )

    def test_producer_stream_contract_rejects_wrong_encoding_or_extra_streams(self) -> None:
        exact = {
            "width": AB.PRODUCTION_WIDTH,
            "height": AB.PRODUCTION_HEIGHT,
            "fps": AB.PRODUCTION_FPS,
            "codec_name": "prores",
            "profile": "HQ",
            "pix_fmt": "yuv422p10le",
            "stream_count": 1,
            "video_streams": 1,
            "audio_streams": 0,
            "subtitle_streams": 0,
            "data_streams": 0,
        }
        cases = {
            "shape": {"width": 1280},
            "fps": {"fps": 24},
            "codec": {"codec_name": "h264"},
            "profile": {"profile": "Standard"},
            "pixel-format": {"pix_fmt": "yuv422p"},
            "audio": {"stream_count": 2, "audio_streams": 1},
            "attachment": {"stream_count": 2},
        }
        for case, changes in cases.items():
            with self.subTest(case=case), mock.patch.object(
                AB,
                "video_stream_info",
                return_value={**exact, **changes},
            ):
                with self.assertRaisesRegex(AB.EvidenceError, "video-only ProRes HQ"):
                    AB._producer_stream_info(Path("unused.mov"), "producer")

    def test_boolean_producer_receipt_numbers_fail_closed(self) -> None:
        cases = {
            "ordinal": (lambda row: row.__setitem__("segment", False), "not ordered and contiguous"),
            "missing": (
                lambda row: row["capture"].__setitem__("missing", False),
                "missing photographic plates",
            ),
            "stream": (
                lambda row: row["inputs"].__setitem__("stream", False),
                "has stale stream",
            ),
            "start": (
                lambda row: row["inputs"].__setitem__("start", False),
                "has stale start",
            ),
            "decoded-frames": (
                lambda row: row["decoded_video"].__setitem__("frames", True),
                "has no full decoded-frame identity",
            ),
        }
        for case, (mutate, expected) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                fixture = EvidenceFixture(Path(temporary))
                concat_path = fixture.producer_paths["control"]
                concat = json.loads(concat_path.read_text())
                segment_media = fixture.producer_media_paths["control"]["segments"][0]
                segment_path = segment_media.with_name(segment_media.name + ".receipt.json")
                segment = json.loads(segment_path.read_text())
                mutate(segment)
                segment_path.write_text(json.dumps(segment, indent=2) + "\n")
                concat["segments"][0]["receipt_sha256"] = digest(segment_path)
                concat_path.write_text(json.dumps(concat, indent=2) + "\n")
                fixture.write_receipt()
                errors = fixture.production_errors()
                self.assertTrue(any(expected in error for error in errors), errors)

    def test_producer_receipt_byte_and_segment_count_caps_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "oversized.json"
            path.write_bytes(b" " * 9)
            with self.assertRaisesRegex(AB.EvidenceError, "8-byte limit"):
                AB.read_json(path, "producer receipt", max_bytes=8)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            concat_path = fixture.producer_paths["with_score"]
            concat = json.loads(concat_path.read_text())
            concat["segments"].append(copy.deepcopy(concat["segments"][0]))
            concat_path.write_text(json.dumps(concat, indent=2) + "\n")
            fixture.write_receipt()
            with mock.patch.object(AB, "PRODUCER_SEGMENT_MAX_COUNT", 1):
                errors = fixture.production_errors()
            self.assertTrue(
                any("exceeds its segment-count limit" in error for error in errors),
                errors,
            )

        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            segment_receipts = [
                paths["segments"][0].with_name(paths["segments"][0].name + ".receipt.json")
                for paths in fixture.producer_media_paths.values()
            ]
            with (
                mock.patch.object(
                    AB,
                    "PRODUCER_SEGMENT_RECEIPT_TOTAL_MAX_BYTES",
                    min(path.stat().st_size for path in segment_receipts) - 1,
                ),
                mock.patch.object(
                    AB,
                    "_producer_decoded_video_identity",
                    side_effect=AssertionError("receipt cap must precede every producer decoder"),
                ),
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(
                    AB, "review_frame_anchors", side_effect=fixture.anchor_probe
                ),
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertTrue(any("aggregate byte limit" in error for error in errors), errors)

    def test_pinned_media_and_receipt_snapshots_reject_path_swaps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            target = fixture.producer_media_paths["with_score"]["segments"][0]
            swapped = False

            def swapping_replay(**kwargs):
                nonlocal swapped
                identity = fixture.replay_probe(**kwargs)
                if not swapped and kwargs["mode"] == "with_score":
                    swapped = True
                    target.rename(target.with_name(target.name + ".authenticated-original"))
                    target.write_bytes(b"replaced after decode during canonical replay")
                return identity

            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
                mock.patch.object(
                    AB,
                    "_producer_decoded_video_identity",
                    side_effect=fixture.producer_probe,
                ),
                mock.patch.object(
                    AB,
                    "_canonical_segment_replay",
                    side_effect=swapping_replay,
                ),
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertIn(
                "with_score render segment 0 changed during authentication",
                errors,
            )

        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            target = fixture.producer_media_paths["with_score"]["concat"]
            replacement = fixture.producer_media_paths["control"]["concat"].read_bytes()
            original_probe = fixture.producer_probe
            swapped = False

            def swapping_probe(path, **kwargs):
                nonlocal swapped
                identity = original_probe(path, **kwargs)
                if not swapped and kwargs["label"] == "with_score render concat":
                    swapped = True
                    target.rename(target.with_name(target.name + ".pinned-original"))
                    target.write_bytes(replacement)
                return identity

            with mock.patch.object(
                AB,
                "_producer_decoded_video_identity",
                side_effect=swapping_probe,
            ), mock.patch.object(
                AB, "ffprobe_media", side_effect=fixture.probe
            ), mock.patch.object(
                AB, "review_frame_anchors", side_effect=fixture.anchor_probe
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertTrue(
                any("with_score render concat changed during authentication" in error for error in errors),
                errors,
            )

        for kind in ("concat", "segment"):
            with self.subTest(receipt=kind), tempfile.TemporaryDirectory() as temporary:
                fixture = EvidenceFixture(Path(temporary))
                if kind == "concat":
                    target = fixture.producer_paths["with_score"]
                    trigger = "with_score render concat"
                    expected = "with_score review-media producer receipt changed during authentication"
                else:
                    segment = fixture.producer_media_paths["with_score"]["segments"][0]
                    target = segment.with_name(segment.name + ".receipt.json")
                    trigger = "with_score render segment 0"
                    expected = "review-media render segment 0 receipt changed during authentication"
                payload = target.read_bytes()
                original_probe = fixture.producer_probe
                swapped = False

                def swapping_receipt_probe(path, **kwargs):
                    nonlocal swapped
                    identity = original_probe(path, **kwargs)
                    if not swapped and kwargs["label"] == trigger:
                        swapped = True
                        target.rename(target.with_name(target.name + ".pinned-original"))
                        target.write_bytes(payload)
                    return identity

                with mock.patch.object(
                    AB,
                    "_producer_decoded_video_identity",
                    side_effect=swapping_receipt_probe,
                ), mock.patch.object(
                    AB, "ffprobe_media", side_effect=fixture.probe
                ), mock.patch.object(
                    AB, "review_frame_anchors", side_effect=fixture.anchor_probe
                ):
                    errors = AB.production_receipt_errors(
                        fixture.receipt,
                        expected=fixture.context,
                        recompute_samples=False,
                    )
                self.assertTrue(any(expected in error for error in errors), errors)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "producer.mov.receipt.json"
            path.write_text('{"schema":"danse.render.concat.v1"}\n')
            original_reader = AB._read_pinned_bytes
            swapped = False

            def swapping_reader(file_fd, *, maximum, label):
                nonlocal swapped
                payload = original_reader(file_fd, maximum=maximum, label=label)
                if not swapped:
                    swapped = True
                    path.rename(path.with_name(path.name + ".pinned-original"))
                    path.write_bytes(payload)
                return payload

            with mock.patch.object(AB, "_read_pinned_bytes", side_effect=swapping_reader):
                with self.assertRaisesRegex(AB.EvidenceError, "changed during authentication"):
                    AB._bounded_json_snapshot(path, "producer receipt", max_bytes=1024)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            original_loader = AB._load_frame_receipt
            swapped = False

            def swapping_frame_loader(*args, **kwargs):
                nonlocal swapped
                result = original_loader(*args, **kwargs)
                if not swapped:
                    swapped = True
                    payload = fixture.frame.read_bytes()
                    fixture.frame.rename(
                        fixture.frame.with_name(fixture.frame.name + ".pinned-original")
                    )
                    fixture.frame.write_bytes(payload)
                return result

            with mock.patch.object(
                AB, "_load_frame_receipt", side_effect=swapping_frame_loader
            ), mock.patch.object(
                AB, "ffprobe_media", side_effect=fixture.probe
            ), mock.patch.object(
                AB, "review_frame_anchors", side_effect=fixture.anchor_probe
            ), fixture.producer_patch():
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertTrue(any("changed during authentication" in error for error in errors), errors)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "frame-receipt.json"
            receipt.write_text("{}\n")
            image = root / "frame.png"
            image.write_bytes(b"one exact image snapshot")
            image_reference = reference(image, root)
            original_reader = AB._read_pinned_bytes
            swapped = False

            def swapping_image_reader(file_fd, *, maximum, label):
                nonlocal swapped
                payload = original_reader(file_fd, maximum=maximum, label=label)
                if not swapped:
                    swapped = True
                    image.rename(image.with_name(image.name + ".pinned-original"))
                    image.write_bytes(payload)
                return payload

            with mock.patch.object(AB, "_read_pinned_bytes", side_effect=swapping_image_reader):
                errors, path, payload = AB._artifact_binary_snapshot(
                    receipt,
                    image_reference,
                    "anchor frame",
                    max_bytes=1024,
                )
            self.assertIsNone(path)
            self.assertIsNone(payload)
            self.assertTrue(any("changed during authentication" in error for error in errors), errors)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            target = fixture.with_movie
            replacement = fixture.control_movie.read_bytes()
            source_fds: list[int] = []
            swapped = False

            def review_probe(path, **kwargs):
                source_fds.append(kwargs["source_fd"])
                return fixture.probe(path, **kwargs)

            def swapping_anchor(path, **kwargs):
                nonlocal swapped
                source_fds.append(kwargs["source_fd"])
                if path == target and not swapped:
                    swapped = True
                    target.rename(target.with_name(target.name + ".pinned-original"))
                    target.write_bytes(replacement)
                return fixture.anchor_probe(path, **kwargs)

            with mock.patch.object(
                AB, "ffprobe_media", side_effect=review_probe
            ), mock.patch.object(
                AB, "review_frame_anchors", side_effect=swapping_anchor
            ), fixture.producer_patch():
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertTrue(any("changed during authentication" in error for error in errors), errors)
            self.assertEqual(source_fds[0], source_fds[1])

    def test_caps_and_malformed_large_integers_fail_before_expensive_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oversized_json = root / "oversized.json"
            oversized_json.write_bytes(b" " * 9)
            reference_value = {"path": oversized_json.name, "sha256": "0" * 64, "bytes": 9}
            receipt = root / "receipt.json"
            receipt.write_text("{}\n")
            with mock.patch.object(AB.hashlib, "sha256", side_effect=AssertionError) as sha:
                errors, path, document = AB._artifact_json_snapshot(
                    receipt,
                    reference_value,
                    "producer receipt",
                    max_bytes=8,
                )
            self.assertIsNone(path)
            self.assertIsNone(document)
            self.assertTrue(any("8-byte limit" in error for error in errors), errors)
            sha.assert_not_called()

            oversized_media = root / "producer.mov"
            with oversized_media.open("wb") as handle:
                handle.truncate(AB.PRODUCER_MEDIA_MAX_BYTES_PER_FRAME + 1)
            with mock.patch.object(AB, "_fd_sha256", side_effect=AssertionError) as digest_probe, \
                 mock.patch.object(
                     AB, "_producer_decoded_video_identity", side_effect=AssertionError
                 ) as decoded_probe:
                errors, decoded = AB._producer_media_identity_errors(
                    oversized_media,
                    {"file_bytes": 1, "file_sha256": "0" * 64},
                    expected_frames=1,
                    include_fps=False,
                    label="producer",
                )
            self.assertIsNone(decoded)
            self.assertTrue(any("media limit" in error for error in errors), errors)
            digest_probe.assert_not_called()
            decoded_probe.assert_not_called()

            long_integer = root / "long-integer.json"
            long_integer.write_bytes(b'{"value":' + (b"1" * 5000) + b"}\n")
            with self.assertRaisesRegex(AB.EvidenceError, "cannot read producer receipt"):
                AB.read_json(long_integer, "producer receipt", max_bytes=6000)

            self.assertEqual(
                AB.production_frame_count(AB.PRODUCTION_FRAME_MAX / AB.PRODUCTION_FPS),
                AB.PRODUCTION_FRAME_MAX,
            )
            invalid_durations = {
                "bool": True,
                "string": "1",
                "zero": 0,
                "negative": -1,
                "nan": float("nan"),
                "infinity": float("inf"),
                "overflowing-product": 1e308,
                "over-production-limit": (
                    AB.PRODUCTION_FRAME_MAX + 1
                ) / AB.PRODUCTION_FPS,
            }
            fixture = EvidenceFixture(root / "duration-fixture")
            for case, duration in invalid_durations.items():
                with self.subTest(duration=case), self.assertRaises(AB.EvidenceError):
                    AB.production_frame_count(duration)

                expected = copy.deepcopy(fixture.context)
                expected["span"]["duration_seconds"] = duration
                with mock.patch.object(
                    AB,
                    "ffprobe_media",
                    side_effect=AssertionError("invalid duration must precede media probes"),
                ):
                    errors = AB.production_receipt_errors(
                        fixture.receipt,
                        expected=expected,
                        recompute_samples=False,
                    )
                self.assertTrue(
                    any("expected production duration" in error for error in errors),
                    errors,
                )

                with mock.patch.object(
                    AB,
                    "_load_sample_receipt",
                    side_effect=AssertionError("invalid duration must precede receipt loads"),
                ), mock.patch.object(
                    AB,
                    "ffprobe_media",
                    side_effect=AssertionError("invalid duration must precede media probes"),
                ), self.assertRaisesRegex(AB.EvidenceError, "production receipt duration"):
                    AB.write_receipt(
                        destination=fixture.base / f"invalid-duration-{case}.json",
                        context=expected,
                        sample_path=fixture.sample,
                        frame_path=fixture.frame,
                        with_score=fixture.with_movie,
                        control=fixture.control_movie,
                        with_score_producer=fixture.producer_paths["with_score"],
                        control_producer=fixture.producer_paths["control"],
                    )

            self.assertEqual(AB._production_review_position(0), (0, 0.0))
            self.assertEqual(AB.production_audio_frame_count(1.0, 48000), 48000)
            self.assertEqual(AB.production_audio_frame_count(0.02084375, 48000), 1001)
            for case, value in {
                "bool": True,
                "negative": -1,
                "nan": float("nan"),
                "infinity": float("inf"),
                "overflowing-product": 1e308,
                "over-production-limit": (
                    AB.PRODUCTION_FRAME_MAX + 1
                ) / AB.PRODUCTION_FPS,
            }.items():
                with self.subTest(review_second=case), self.assertRaises(AB.EvidenceError):
                    AB._production_review_position(value)
                with self.subTest(audio_duration=case), self.assertRaises(AB.EvidenceError):
                    AB.production_audio_frame_count(value, 48000)

    def test_invalid_absurd_segment_plan_never_launches_segment_decoder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            concat_path = fixture.producer_paths["with_score"]
            concat = json.loads(concat_path.read_text())
            segment_path = concat_path.parent / f"{concat['segments'][0]['name']}.receipt.json"
            segment = json.loads(segment_path.read_text())
            segment["frames"] = 10**18
            segment["inputs"]["segment_frames"] = 10**18
            segment["decoded_video"]["frames"] = 10**18
            segment_path.write_text(json.dumps(segment, indent=2) + "\n")
            concat["segments"][0]["receipt_sha256"] = digest(segment_path)
            concat_path.write_text(json.dumps(concat, indent=2) + "\n")
            fixture.write_receipt()

            def bounded_probe(path, **kwargs):
                if kwargs["label"].startswith("with_score render segment"):
                    raise AssertionError("invalid segment frame plans must not launch a decoder")
                return fixture.producer_probe(path, **kwargs)

            with mock.patch.object(
                AB, "_producer_decoded_video_identity", side_effect=bounded_probe
            ), mock.patch.object(
                AB, "ffprobe_media", side_effect=fixture.probe
            ), mock.patch.object(
                AB, "review_frame_anchors", side_effect=fixture.anchor_probe
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertTrue(any("does not own its exact frame range" in error for error in errors), errors)

    def test_media_decoder_lifecycle_is_bounded_and_pcm_stderr_is_file_backed(self) -> None:
        media_subprocess = AB.decoded_video_identity.__globals__["subprocess"]

        class FakeVideoProcess:
            def __init__(self, payload: bytes, returncode: int = 0) -> None:
                self.stdout = io.BytesIO(payload)
                self.returncode = returncode
                self.killed = False
                self.waited = 0

            def poll(self):
                return None if not self.killed else self.returncode

            def kill(self):
                self.killed = True

            def wait(self):
                self.waited += 1
                return self.returncode

        with tempfile.TemporaryDirectory() as temporary:
            movie = Path(temporary) / "video.mov"
            movie.write_bytes(b"container")
            for name, payload in (("surplus", b"abcdef"), ("partial", b"abcd")):
                with self.subTest(case=name):
                    process = FakeVideoProcess(payload)
                    with mock.patch.object(media_subprocess, "Popen", return_value=process):
                        with self.assertRaises(AB.MediaIdentityError):
                            AB.decoded_video_identity(
                                movie,
                                width=1,
                                height=1,
                                expected_frames=1,
                                ffmpeg="ffmpeg",
                            )
                    self.assertTrue(process.killed)
                    self.assertGreaterEqual(process.waited, 1)

            failed = FakeVideoProcess(b"abc", returncode=9)
            with mock.patch.object(media_subprocess, "Popen", return_value=failed):
                with self.assertRaisesRegex(AB.MediaIdentityError, "cannot decode renderer video"):
                    AB.decoded_video_identity(
                        movie,
                        width=1,
                        height=1,
                        expected_frames=1,
                        ffmpeg="ffmpeg",
                    )
            self.assertEqual(failed.waited, 1)

            class FakeAudioProcess:
                def __init__(self, _command, *, stdout, stderr, **_kwargs) -> None:
                    self.stdout = io.BytesIO(b"\x00\x00\x00\x00")
                    stderr.write(b"x" * (2 << 20))

                def poll(self):
                    return 0

                def kill(self):
                    return None

                def wait(self):
                    return 0

            with mock.patch.object(AB.shutil, "which", return_value="ffmpeg"), mock.patch.object(
                AB.subprocess, "Popen", side_effect=FakeAudioProcess
            ) as popen:
                identity = AB.media_pcm_identity(movie)
            self.assertEqual(identity["audio_frames"], 1)
            self.assertIsNot(popen.call_args.kwargs["stderr"], AB.subprocess.PIPE)

            surplus_audio = FakeVideoProcess(b"\x00" * 8)
            with mock.patch.object(AB.shutil, "which", return_value="ffmpeg"), mock.patch.object(
                AB.subprocess, "Popen", return_value=surplus_audio
            ):
                with self.assertRaisesRegex(AB.EvidenceError, "more than the expected 1 PCM frames"):
                    AB.media_pcm_identity(movie, expected_frames=1)
            self.assertTrue(surplus_audio.killed)
            self.assertGreaterEqual(surplus_audio.waited, 1)

    def test_descriptor_walk_and_pread_faults_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            artifact = real / "artifact.json"
            artifact.write_text("{}\n")
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(AB.EvidenceError, "unsafe|cannot be pinned"):
                with AB._pinned_regular_file(linked / artifact.name, "artifact"):
                    self.fail("an ancestor symlink must not be descriptor-walked")

            file_fd = AB.os.open(artifact, AB.os.O_RDONLY)
            try:
                with mock.patch.object(AB.os, "pread", side_effect=OSError("read fault")):
                    with self.assertRaisesRegex(AB.EvidenceError, "encoded digest"):
                        AB._fd_sha256(file_fd, "artifact")
                    with self.assertRaisesRegex(AB.EvidenceError, "cannot read artifact"):
                        AB._read_pinned_bytes(file_fd, maximum=16, label="artifact")
            finally:
                AB.os.close(file_fd)

    def test_malformed_frame_images_and_capture_shapes_report_gate_errors(self) -> None:
        import struct
        import zlib
        from PIL import Image as PillowImage

        ihdr = struct.pack(">IIBBBBB", 100_000, 100_000, 8, 2, 0, 0, 0)
        chunk = b"IHDR" + ihdr
        bomb = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", len(ihdr))
            + chunk
            + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
        )
        with self.assertRaisesRegex(AB.EvidenceError, "cannot decode review anchor"):
            AB._raw_frame_psnr(bomb, b"\x00\x00\x00", 1, 1)
        with self.assertRaisesRegex(AB.EvidenceError, "cannot decode production boundary"):
            AB._image_psnr(bomb, bomb, 1, 1)

        stale_image = io.BytesIO()
        PillowImage.new("RGB", (2, 2), "black").save(stale_image, format="PNG")
        with mock.patch.object(
            PillowImage.Image,
            "convert",
            side_effect=AssertionError("stale dimensions must be rejected before conversion"),
        ):
            with self.assertRaisesRegex(AB.EvidenceError, "dimensions are stale"):
                AB._raw_frame_psnr(stale_image.getvalue(), b"\x00\x00\x00", 1, 1)
            with self.assertRaisesRegex(AB.EvidenceError, "dimensions are stale"):
                AB._image_psnr(stale_image.getvalue(), stale_image.getvalue(), 1, 1)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            frame = json.loads(fixture.frame.read_text())
            frame["capture"]["width"] = "not-an-integer"
            fixture.frame.write_text(json.dumps(frame, indent=2) + "\n")
            fixture.write_receipt()
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(
                    AB,
                    "review_frame_anchors",
                    side_effect=AssertionError("invalid frame receipts must not launch anchor decoding"),
                ),
                fixture.producer_patch(),
            ):
                errors = AB.production_receipt_errors(
                    fixture.receipt,
                    expected=fixture.context,
                    recompute_samples=False,
                )
            self.assertTrue(any("frame receipt schema failed" in error for error in errors), errors)
            self.assertTrue(any("no authenticated Metal frame receipt" in error for error in errors), errors)

    def test_review_anchor_decoder_uses_pinned_media_and_file_backed_stderr(self) -> None:
        class AnchorProcess:
            def __init__(self, payload: bytes, *, stderr, running: bool = False) -> None:
                self.stdout = io.BytesIO(payload)
                self.running = running
                self.killed = False
                self.waited = 0
                stderr.write(b"diagnostic" * (1 << 18))

            def poll(self):
                return None if self.running and not self.killed else 0

            def kill(self):
                self.killed = True

            def wait(self):
                self.waited += 1
                return 0

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            movie = root / "review.mov"
            movie.write_bytes(b"review container")
            source = root / "source.png"
            source.write_bytes(b"source frame")
            frame_path = root / "frames.json"
            frame = {
                "capture": {"width": 1, "height": 1},
                "rows": [
                    {
                        "sample_id": "sample-000",
                        "review_frame_index": 0,
                        "review_second": 0,
                        "with_score": reference(source, root),
                    }
                ],
            }
            process = None

            def popen(_command, *, stdout, stderr, **_kwargs):
                nonlocal process
                process = AnchorProcess(b"abc", stderr=stderr)
                return process

            with mock.patch.object(AB.shutil, "which", return_value="ffmpeg"), mock.patch.object(
                AB.subprocess, "Popen", side_effect=popen
            ) as launched, mock.patch.object(
                AB, "_raw_frame_psnr", return_value=120.0
            ), mock.patch.object(
                AB, "PRODUCTION_WIDTH", 1
            ), mock.patch.object(
                AB, "PRODUCTION_HEIGHT", 1
            ):
                anchors = AB.review_frame_anchors(
                    movie,
                    frame_path=frame_path,
                    frame=frame,
                    mode="with_score",
                    expected_frames=1,
                )
            self.assertEqual(len(anchors), 1)
            command = launched.call_args.args[0]
            self.assertIn("-nostdin", command)
            self.assertIn("/dev/fd/", command[command.index("-i") + 1])
            self.assertIsNot(launched.call_args.kwargs["stderr"], AB.subprocess.PIPE)
            self.assertIsNotNone(process)
            self.assertEqual(process.waited, 1)

            short = None

            def short_popen(_command, *, stdout, stderr, **_kwargs):
                nonlocal short
                short = AnchorProcess(b"", stderr=stderr, running=True)
                return short

            with mock.patch.object(AB.shutil, "which", return_value="ffmpeg"), mock.patch.object(
                AB.subprocess, "Popen", side_effect=short_popen
            ), mock.patch.object(AB, "PRODUCTION_WIDTH", 1), mock.patch.object(
                AB, "PRODUCTION_HEIGHT", 1
            ):
                with self.assertRaisesRegex(AB.EvidenceError, "missing boundary frame"):
                    AB.review_frame_anchors(
                        movie,
                        frame_path=frame_path,
                        frame=frame,
                        mode="with_score",
                        expected_frames=1,
                    )
            self.assertIsNotNone(short)
            self.assertTrue(short.killed)
            self.assertGreaterEqual(short.waited, 1)

    def test_review_probe_counts_decoded_frames_not_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            movie = Path(temporary) / "review.mov"
            movie.write_bytes(b"review")
            document = {
                "format": {"duration": "1.0"},
                "streams": [
                    {
                        "codec_type": "video",
                        "avg_frame_rate": "30/1",
                        "nb_read_frames": "not authoritative",
                        "nb_read_packets": "999",
                        "width": AB.PRODUCTION_WIDTH,
                        "height": AB.PRODUCTION_HEIGHT,
                    },
                    {"codec_type": "audio"},
                ],
            }
            result = mock.Mock(returncode=0, stdout=json.dumps(document), stderr="")
            with (
                mock.patch.object(AB.shutil, "which", return_value="ffprobe"),
                mock.patch.object(AB.subprocess, "run", return_value=result) as run,
                mock.patch.object(
                    AB,
                    "media_pcm_identity",
                    return_value={
                        "audio_pcm_sha256": "1" * 64,
                        "audio_frames": 48_000,
                        "audio_sample_rate": 48_000,
                        "audio_channels": 2,
                    },
                ),
                mock.patch.object(
                    AB,
                    "media_video_identity",
                    return_value={
                        "video_framehash_sha256": "2" * 64,
                        "decoded_rgb_sha256": "2" * 64,
                        "decoded_video_frames": 30,
                    },
                ) as video_identity,
            ):
                identity = AB.ffprobe_media(movie, expected_video_frames=30)
            self.assertEqual(identity["video_frames"], 30)
            command = run.call_args.args[0]
            self.assertNotIn("-count_frames", command)
            self.assertNotIn("nb_read_frames", command[command.index("-show_entries") + 1])
            self.assertNotIn("-count_packets", command)
            video_call = video_identity.call_args
            self.assertEqual(video_call.kwargs["expected_frames"], 30)

    def test_decoded_segment_chain_hash_is_ordered_rgb_bytes(self) -> None:
        class FakeProcess:
            def __init__(self, payload: bytes) -> None:
                self.stdout = io.BytesIO(payload)

            def poll(self):
                return 0

            def kill(self):
                return None

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.mov"
            second = Path(temporary) / "second.mov"
            first.write_bytes(b"first container")
            second.write_bytes(b"second container")
            frames = {first: b"\x01\x02\x03", second: b"\x04\x05\x06"}

            def popen(command, **_kwargs):
                source = Path(command[command.index("-i") + 1])
                return FakeProcess(frames[source])

            media_subprocess = AB.decoded_video_chain_identities.__globals__["subprocess"]
            with mock.patch.object(media_subprocess, "Popen", side_effect=popen):
                individual, chain = AB.decoded_video_chain_identities(
                    [first, second],
                    width=1,
                    height=1,
                    expected_frames=[1, 1],
                    ffmpeg="ffmpeg",
                )
                _, reversed_chain = AB.decoded_video_chain_identities(
                    [second, first],
                    width=1,
                    height=1,
                    expected_frames=[1, 1],
                    ffmpeg="ffmpeg",
                )
            self.assertEqual(individual[0]["sha256"], hashlib.sha256(frames[first]).hexdigest())
            self.assertEqual(
                chain["sha256"],
                hashlib.sha256(frames[first] + frames[second]).hexdigest(),
            )
            self.assertEqual(
                reversed_chain["sha256"],
                hashlib.sha256(frames[second] + frames[first]).hexdigest(),
            )
            self.assertNotEqual(chain["sha256"], reversed_chain["sha256"])

    def test_evidence_graph_owns_every_render_producer_receipt_and_movie(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            owned = set(AB.evidence_artifact_paths(fixture.receipt))
            for mode, producer in fixture.producer_paths.items():
                self.assertIn(producer.resolve(), owned)
                self.assertIn(
                    fixture.producer_media_paths[mode]["concat"].resolve(),
                    owned,
                )
                concat = json.loads(producer.read_text())
                for index, row in enumerate(concat["segments"]):
                    self.assertIn(
                        (producer.parent / f"{row['name']}.receipt.json").resolve(),
                        owned,
                    )
                    self.assertIn(
                        fixture.producer_media_paths[mode]["segments"][index].resolve(),
                        owned,
                    )
            surplus_media = fixture.producer_paths["with_score"].parent / "with_score-seg-999.mov"
            surplus_receipt = surplus_media.with_name(surplus_media.name + ".receipt.json")
            surplus_media.write_bytes(b"unowned prior render")
            surplus_receipt.write_text("{}\n")
            owned_with_surplus = set(AB.evidence_artifact_paths(fixture.receipt))
            self.assertNotIn(surplus_media.resolve(), owned_with_surplus)
            self.assertNotIn(surplus_receipt.resolve(), owned_with_surplus)
            fixture.producer_media_paths["control"]["segments"][0].unlink()
            with self.assertRaisesRegex(AB.EvidenceError, "segment 0 media is missing"):
                AB.evidence_artifact_paths(fixture.receipt)

    def test_pcm_or_frame_substitution_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = EvidenceFixture(Path(temporary))
            changed = copy.deepcopy(fixture.media[fixture.control_movie])
            changed["audio_pcm_sha256"] = "9" * 64
            with mock.patch.object(
                AB,
                "ffprobe_media",
                side_effect=lambda path, **kwargs: (
                    changed if path == fixture.control_movie else fixture.probe(path, **kwargs)
                ),
            ), mock.patch.object(
                AB, "review_frame_anchors", side_effect=fixture.anchor_probe
            ), fixture.producer_patch():
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
                fixture.producer_patch(),
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
                fixture.producer_patch(),
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

            for case, duration in {
                "bool": True,
                "zero": 0,
                "nan": float("nan"),
                "infinity": float("inf"),
                "overflowing-product": 1e308,
            }.items():
                invalid_duration = copy.deepcopy(manifest)
                invalid_duration["duration"] = duration
                invalid_duration["t1"] = duration
                with self.subTest(package_duration=case), (
                    mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe)
                ), mock.patch.object(
                    AB, "review_frame_anchors", side_effect=fixture.anchor_probe
                ), fixture.producer_patch(), mock.patch.object(
                    AB, "git_identity", return_value=fixture.context["repository_head"]
                ), mock.patch.object(
                    AB,
                    "renderer_source_tree",
                    side_effect=lambda *_args, with_score=True, **_kwargs: (
                        fixture.context["source_tree_sha256"]
                        if with_score
                        else fixture.control_source_tree
                    ),
                ), mock.patch.object(
                    AB, "generate_sample_rows", return_value=fixture.rows
                ), mock.patch.object(
                    AB, "require_production_tier", return_value=None
                ):
                    errors = AB.packaged_receipt_errors(fixture.root, invalid_duration)
                self.assertTrue(
                    any("expected production duration" in error for error in errors),
                    errors,
                )

            stale = copy.deepcopy(manifest)
            stale["t1"] = 0.9
            stale["duration"] = 0.9
            with (
                mock.patch.object(AB, "ffprobe_media", side_effect=fixture.probe),
                mock.patch.object(AB, "review_frame_anchors", side_effect=fixture.anchor_probe),
                fixture.producer_patch(),
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
                fixture.producer_patch(),
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
                fixture.producer_patch(),
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
                fixture.producer_patch(),
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
