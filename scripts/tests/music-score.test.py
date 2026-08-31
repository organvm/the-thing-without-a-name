#!/usr/bin/env python3
"""Portable fixture-level regressions for Danse Music I/II contracts."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_TMP_ROOT = ROOT / ".work" / "test-fixtures"
sys.path.insert(0, str(ROOT / "music"))
sys.path.insert(0, str(ROOT / "sound"))
sys.path.insert(0, str(ROOT / "render"))
sys.path.insert(0, str(ROOT / "pipeline"))

from compile_score import (  # noqa: E402
    Event,
    Timeline,
    canonical_sha256,
    compile_contract,
    cue_rows,
    dynamics_rows,
    meter_rows,
    note_and_orchestration_rows,
    output_bytes,
    parse_track,
)
from music_score import events_between, load_score, score_at, validate as validate_score  # noqa: E402
from record_recording_custody import (  # noqa: E402
    CANONICAL_STEMS,
    _atomic_write_many,
    _canonical_repository_output,
    authenticate_production_outputs,
    build_receipt,
    hydrated_receipt_errors,
    transition_recording_custody,
    validate_production_input_graph,
)
from validate_repertoire import (  # noqa: E402
    load_register,
    sha256,
    validate_document,
    validate_recording_custody_schema,
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*command: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def compact(state: dict) -> dict:
    return {
        "source": round(state["source_second"], 8),
        "scale": round(state["scale"], 8),
        "tempo": round(state["tempo"]["effective_bpm"], 8),
        "beat": state["beat"]["index"],
        "downbeat": state["beat"]["downbeat"],
        "beat_phase": round(state["beat"]["phase"], 8),
        "phrase": state["phrase"]["id"],
        "dynamic": state["dynamic"]["midi_expression"],
        "movement": state["movement"]["id"],
        "movement_u": round(state["movement"]["u"], 8),
        "cues": [cue["id"] for cue in state["cues"]],
        "visual": state["visual"],
    }


def compact_events(events: list[dict]) -> list[dict]:
    return [
        {
            "type": event["type"],
            "index": event["index"],
            "name": event.get("id", event.get("stem")),
            "at": round(event["at"], 8),
            "end": round(event["end"], 8),
        }
        for event in events
    ]


def current_recording_custody_receipt(master_digest: str = "a" * 64) -> dict:
    score_path = ROOT / "music/score.json"
    choreography_path = ROOT / "render/choreography.json"
    midi_path = ROOT / "music/delibes-screendance-suite.mid"
    adaptation_path = ROOT / "music/adaptation.json"
    toolchain_path = ROOT / "music/audio-toolchain.json"
    mix_path = ROOT / "music/delibes-mix.json"
    uses_path = ROOT / "sound/audio-uses.json"
    score = json.loads(score_path.read_text())
    choreography = json.loads(choreography_path.read_text())
    toolchain = json.loads(toolchain_path.read_text())

    def artifact(path: str, digest: str, *, polyphonic: bool = False) -> dict:
        row = {
            "path": path,
            "sha256": digest,
            "bytes": 67_372_140,
            "frames": 16_843_024,
            "sample_rate": 48_000,
            "channels": 2,
            "sample_format": "signed-16-bit-little-endian-pcm",
            "duration_seconds": 350.896333333,
            "peak_sample": 20_000,
            "rms_sample": 2_000.0,
            "non_silent": True,
        }
        if polyphonic:
            row["polyphonic_frames"] = 1_000_000
        return row

    return {
        "schema": "danse.music.recording-custody.v1",
        "status": "custody-only",
        "profile": "competition-classical",
        "work_id": "delibes-screendance-suite",
        "recorded_on": "2026-08-30",
        "audio_render": {
            "path": ".work/music/competition/audio-render.json",
            "sha256": "b" * 64,
            "bytes": 4096,
        },
        "generator": {
            "path": "music/record_recording_custody.py",
            "sha256": sha256(ROOT / "music/record_recording_custody.py"),
        },
        "source_schema": {
            "path": "music/audio-render.schema.json",
            "sha256": sha256(ROOT / "music/audio-render.schema.json"),
        },
        "pre_normalized_master": artifact(
            ".work/music/competition/delibes-pre-normalized.wav",
            "d" * 64,
            polyphonic=True,
        ),
        "master": artifact(
            ".work/music/competition/delibes-master.wav",
            master_digest,
            polyphonic=True,
        ),
        "stems": [
            {
                "id": stem_id,
                **artifact(
                    f".work/music/competition/stems/{stem_id}.wav",
                    hashlib.sha256(stem_id.encode()).hexdigest(),
                ),
            }
            for stem_id in CANONICAL_STEMS
        ],
        "contracts": {
            "score": {
                "path": score_path.relative_to(ROOT).as_posix(),
                "sha256": sha256(score_path),
                "contract_sha256": score["identity"]["contract_sha256"],
            },
            "choreography": {
                "path": choreography_path.relative_to(ROOT).as_posix(),
                "sha256": sha256(choreography_path),
                "contract_sha256": choreography["identity"]["contract_sha256"],
            },
            "midi": {"path": midi_path.relative_to(ROOT).as_posix(), "sha256": sha256(midi_path)},
            "adaptation": {
                "path": adaptation_path.relative_to(ROOT).as_posix(),
                "sha256": sha256(adaptation_path),
            },
            "toolchain": {
                "path": toolchain_path.relative_to(ROOT).as_posix(),
                "sha256": sha256(toolchain_path),
            },
            "mix": {"path": mix_path.relative_to(ROOT).as_posix(), "sha256": sha256(mix_path)},
            "audio_uses": {"path": uses_path.relative_to(ROOT).as_posix(), "sha256": sha256(uses_path)},
            "soundfont": {
                "path": toolchain["soundfont"]["path"],
                "sha256": toolchain["soundfont"]["sha256"],
            },
        },
        "executables": {
            name: {
                "sha256": toolchain[name]["executable_sha256"],
                "version": toolchain[name]["version"],
            }
            for name in ("fluidsynth", "ffmpeg")
        },
        "normalization": {
            "method": "ffmpeg-loudnorm-two-pass",
            "targets": {
                "integrated_lufs": -16.0,
                "tolerance_lu": 0.5,
                "target_true_peak_dbtp": -1.1,
                "max_true_peak_dbtp": -1.0,
                "lra_lu": 11.0,
            },
            "output": {
                "integrated_lufs": -16.0,
                "true_peak_dbtp": -1.1,
                "lra_lu": 8.0,
                "threshold_lufs": -26.0,
            },
        },
        "verification": {
            "repeat_master_sha256": master_digest,
            "deterministic": True,
            "non_silent": True,
            "stems_non_silent": True,
            "polyphonic": True,
            "normalization_deterministic": True,
            "loudness_in_target": True,
            "true_peak_in_target": True,
            "duration_matches_score": True,
            "seek_safe": True,
            "seek_probes": [
                {
                    "start_frame": 0,
                    "frames": 12_000,
                    "sha256": "e" * 64,
                    "repeat_sha256": "e" * 64,
                    "equal": True,
                }
            ],
        },
        "clearance": {
            "gate": "music-cleared",
            "state": "pending",
            "note": (
                "This receipt binds deterministic recording custody only; it does not approve "
                "music rights, credit, final cut, upload, or submission."
            ),
        },
    }


class MusicScoreContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.program = json.loads((ROOT / "render/program.json").read_text())
        cls.production_score = load_score()
        cls.production_register = load_register()
        cls.register = load_register(ROOT / "music/fixtures/repertoire.yaml")
        cls.score = compile_contract(copy.deepcopy(cls.register), cls.program, "generated-contract-study")
        # Keep ephemeral score bytes under the ignored work root. Workspace
        # synchronizers may briefly resurrect deleted paths under tracked source
        # directories, which would dirty the repository after a successful run.
        FIXTURE_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls._fixture_directory = tempfile.TemporaryDirectory(dir=FIXTURE_TMP_ROOT)
        cls.fixture_score_path = Path(cls._fixture_directory.name) / "score.json"
        cls.fixture_score_path.write_bytes(output_bytes(cls.score))
        cls._prior_fixture_env = os.environ.get("DANSE_FIXTURE_SCORE")
        os.environ["DANSE_FIXTURE_SCORE"] = str(cls.fixture_score_path)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._prior_fixture_env is None:
            os.environ.pop("DANSE_FIXTURE_SCORE", None)
        else:
            os.environ["DANSE_FIXTURE_SCORE"] = cls._prior_fixture_env
        cls._fixture_directory.cleanup()

    def test_fixture_and_production_registers_compile_with_separate_release_semantics(self) -> None:
        commands = (
            (sys.executable, "music/generate_fixture_midi.py", "--check"),
            (sys.executable, "music/compile_score.py", "--check"),
            (sys.executable, "music/validate_repertoire.py"),
            (sys.executable, "music/validate_repertoire.py", "music/fixtures/repertoire.yaml", "--allow-stale-derived"),
        )
        for command in commands:
            with self.subTest(command=command):
                result = run(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.score["release_status"], "fixture-only")
        self.assertEqual(self.score["artistic_gate"]["status"], "pending")
        self.assertEqual(self.register["works"][0]["selection"]["status"], "not-selected")
        self.assertEqual(self.score["identity"]["midi_sha256"], sha256(ROOT / "music/fixtures/generated-study.mid"))
        self.assertEqual(self.score["time"]["passage_mapping"], "restart-and-affine-stretch")
        self.assertEqual(self.production_score["release_status"], "production-selected")
        self.assertEqual(self.production_score["time"]["passage_mapping"], "native-tempo")
        self.assertEqual(self.production_score["identity"]["midi_sha256"], sha256(ROOT / "music/delibes-screendance-suite.mid"))
        identity_source = copy.deepcopy(self.score)
        declared_contract = identity_source["identity"].pop("contract_sha256")
        self.assertEqual(declared_contract, canonical_sha256(identity_source))
        schema = json.loads((ROOT / "music/score.schema.json").read_text())
        self.assertEqual(schema["properties"]["schema"]["const"], "danse.music.score.v1")
        self.assertTrue(all(type(beat["tick"]) is int for beat in self.score["beats"]))
        self.assertTrue(all(type(beat["tick"]) is int for beat in self.production_score["beats"]))

    def test_cli_success_diagnostics_accept_external_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            score = directory / "score.json"
            score.write_bytes((ROOT / "music/score.json").read_bytes())
            checked = run(
                sys.executable,
                "music/compile_score.py",
                "--check",
                "--out",
                str(score),
            )
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn(str(score), checked.stdout)

            register = directory / "repertoire.yaml"
            register.write_bytes((ROOT / "music/repertoire.yaml").read_bytes())
            validated = run(sys.executable, "music/validate_repertoire.py", str(register))
            self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
            self.assertIn(str(register), validated.stdout)

    def test_compilation_is_byte_deterministic(self) -> None:
        first = output_bytes(compile_contract(copy.deepcopy(self.register), self.program, "generated-contract-study"))
        second = output_bytes(compile_contract(copy.deepcopy(self.register), self.program, "generated-contract-study"))
        self.assertEqual(first, second)
        self.assertEqual(first, self.fixture_score_path.read_bytes())

    def test_python_validator_reports_missing_and_malformed_lookup_as_value_errors(self) -> None:
        malformed = []
        for value in (None, [], {}, {"quantum_seconds": 1}, {"quantum_seconds": 1, "buckets": None}):
            candidate = copy.deepcopy(self.score)
            if value is None:
                candidate.pop("lookup")
            else:
                candidate["lookup"] = value
            malformed.append(candidate)
        bad_bucket = copy.deepcopy(self.score)
        bad_bucket["lookup"]["buckets"][128]["note_start"] = [3, "4"]
        malformed.append(bad_bucket)

        for candidate in malformed:
            with self.subTest(lookup=candidate.get("lookup")):
                with self.assertRaisesRegex(ValueError, r"^music score: lookup"):
                    validate_score(candidate)

    def test_public_domain_composition_does_not_clear_a_nonfree_recording(self) -> None:
        false_equivalence = copy.deepcopy(self.register)
        false_equivalence["artistic_gate"] |= {"status": "accepted", "evidence": "validator fixture"}
        work = false_equivalence["works"][0]
        work["role"] = "repertoire"
        work["selection"] |= {"status": "selected", "evidence": "validator fixture"}
        work["composition"]["status"] = "public-domain"
        work["edition"]["status"] = "not-applicable"
        work["arrangement_midi"]["status"] = "project-authored"
        work["performance"]["status"] = "project-authored"
        work["recording"]["status"] = "restricted"
        errors = validate_document(false_equivalence, check_derived=False)
        self.assertTrue(
            any("public-domain composition status does not clear" in error for error in errors),
            errors,
        )
        self.assertTrue(any("selected repertoire requires" in error and "recording" in error for error in errors), errors)

    def test_declared_schema_and_evidence_source_bytes_are_enforced(self) -> None:
        unknown = copy.deepcopy(self.register)
        unknown["works"][0]["undeclared"] = True
        self.assertTrue(
            any("works[0].undeclared: additional property" in error for error in validate_document(unknown, check_derived=False))
        )

        missing = copy.deepcopy(self.register)
        missing["works"][0]["edition"].pop("editor")
        self.assertTrue(
            any("works[0].edition.editor: is required" in error for error in validate_document(missing, check_derived=False))
        )

        wrong_type = copy.deepcopy(self.register)
        wrong_type["works"][0]["selection"]["evidence"] = []
        self.assertTrue(
            any("works[0].selection.evidence: must have JSON type" in error for error in validate_document(wrong_type, check_derived=False))
        )

        stale_evidence = copy.deepcopy(self.register)
        stale_evidence["works"][0]["composition"]["evidence"][0]["source"] = {
            "path": "music/generate_fixture_midi.py",
            "sha256": "0" * 64,
        }
        self.assertTrue(
            any("composition.evidence[0].source.sha256" in error and "actual" in error for error in validate_document(stale_evidence, check_derived=False))
        )

        with tempfile.TemporaryDirectory(dir=ROOT / "music") as temporary:
            private = Path(temporary) / "private-source.bin"
            private.write_bytes(b"private custody must not enter a tracked score")
            untracked = copy.deepcopy(self.register)
            untracked["works"][0]["composition"]["source"] = {
                "path": private.relative_to(ROOT).as_posix(),
                "sha256": sha256(private),
            }
            errors = validate_document(untracked, check_derived=False)
        self.assertTrue(any("composition.source.path" in error and "tracked by Git" in error for error in errors))

        cleared_without_bytes = copy.deepcopy(self.register)
        recording = cleared_without_bytes["works"][0]["recording"]
        recording |= {"status": "project-authored", "owner": "fixture owner", "source": None}
        self.assertTrue(
            any("recording.source: is required" in error for error in validate_document(cleared_without_bytes, check_derived=False))
        )

        licensed_without_id = copy.deepcopy(self.register)
        arrangement = licensed_without_id["works"][0]["arrangement_midi"]
        arrangement |= {"status": "licensed", "license": None}
        self.assertTrue(
            any("arrangement_midi.license" in error for error in validate_document(licensed_without_id, check_derived=False))
        )

    def test_rendered_recording_uses_ignored_bytes_and_a_tracked_custody_receipt(self) -> None:
        digest = "a" * 64
        with tempfile.TemporaryDirectory(dir=ROOT / "music") as temporary:
            directory = Path(temporary)
            receipt_path = directory / "recording-custody.json"
            relative_receipt = receipt_path.relative_to(ROOT).as_posix()
            receipt = current_recording_custody_receipt(digest)
            receipt_path.write_text(json.dumps(receipt))
            tracked = subprocess.run(
                ["git", "-C", str(ROOT), "ls-files", "-z"],
                capture_output=True,
                check=True,
            ).stdout + relative_receipt.encode() + b"\0"
            listed = subprocess.CompletedProcess([], 0, stdout=tracked, stderr=b"")

            candidate = copy.deepcopy(self.production_register)
            recording = candidate["works"][0]["recording"]
            recording |= {
                "status": "project-authored",
                "source": {
                    "path": receipt["master"]["path"],
                    "sha256": digest,
                    "custody": "hydrated-derived",
                    "receipt": {"path": relative_receipt, "sha256": sha256(receipt_path)},
                },
            }
            recording["evidence"].append(
                {
                    "kind": "recording-custody-receipt",
                    "citation": "Downstream receipt evidence must not feed back into its source score.",
                    "source": {"path": relative_receipt, "sha256": sha256(receipt_path)},
                }
            )
            with mock.patch("validate_repertoire.subprocess.run", return_value=listed):
                self.assertEqual(validate_document(candidate, check_derived=False), [])
                hydrated_errors = validate_document(
                    candidate,
                    check_derived=False,
                    require_hydrated=True,
                )
                transitioned_score = compile_contract(
                    candidate,
                    self.program,
                    "delibes-screendance-suite",
                )
            self.assertEqual(
                output_bytes(transitioned_score),
                (ROOT / "music/score.json").read_bytes(),
                "adding downstream recording custody must not change its upstream score",
            )
            self.assertTrue(any("required hydrated bytes are absent" in error for error in hydrated_errors))
            self.assertTrue(any("requires hydrated audio render bytes" in error for error in hydrated_errors))

            missing_render_contract = copy.deepcopy(candidate)
            missing_render_contract["works"][0]["recording"].pop("render_contract")
            with mock.patch("validate_repertoire.subprocess.run", return_value=listed):
                errors = validate_document(missing_render_contract, check_derived=False)
            self.assertTrue(any("recording.render_contract: is required" in error for error in errors), errors)

            wrong_master = copy.deepcopy(candidate)
            wrong_master["works"][0]["recording"]["source"]["sha256"] = "c" * 64
            with mock.patch("validate_repertoire.subprocess.run", return_value=listed):
                errors = validate_document(wrong_master, check_derived=False)
            self.assertTrue(any("must equal the master identity" in error for error in errors), errors)

            wrong_layer = copy.deepcopy(candidate)
            wrong_layer["works"][0]["performance"]["source"] = copy.deepcopy(recording["source"])
            with mock.patch("validate_repertoire.subprocess.run", return_value=listed):
                errors = validate_document(wrong_layer, check_derived=False)
            self.assertTrue(any("hydrated-derived is allowed only" in error for error in errors), errors)

            malformed = copy.deepcopy(receipt)
            malformed["stems"][0]["id"] = []
            malformed["master"]["path"] = []
            malformed_errors = validate_recording_custody_schema(malformed)
            self.assertTrue(any("must have JSON type string" in error for error in malformed_errors))

            def validate_variant(document: dict, *, status: str = "project-authored") -> list[str]:
                receipt_path.write_text(json.dumps(document))
                variant = copy.deepcopy(candidate)
                variant_recording = variant["works"][0]["recording"]
                variant_recording["status"] = status
                if isinstance(document.get("master"), dict):
                    variant_recording["source"]["path"] = document["master"].get("path")
                    variant_recording["source"]["sha256"] = document["master"].get("sha256")
                variant_recording["source"]["receipt"]["sha256"] = sha256(receipt_path)
                if status == "pending-render":
                    variant_recording["render_contract"] = copy.deepcopy(
                        self.production_register["works"][0]["recording"]["render_contract"]
                    )
                with mock.patch("validate_repertoire.subprocess.run", return_value=listed):
                    return validate_document(variant, check_derived=False)

            traversal = copy.deepcopy(receipt)
            traversal["contracts"]["adaptation"]["path"] = "../foreign/adaptation.json"
            errors = validate_variant(traversal)
            self.assertTrue(
                any("contracts.adaptation" in error and "does not match" in error for error in errors),
                errors,
            )

            alternate_score = copy.deepcopy(receipt)
            alternate_score_path = ROOT / "music/fixtures/score-affine.json"
            alternate_score["contracts"]["score"] |= {
                "path": alternate_score_path.relative_to(ROOT).as_posix(),
                "sha256": sha256(alternate_score_path),
            }
            errors = validate_variant(alternate_score)
            self.assertTrue(
                any("contracts.score.path" in error and "canonical current artifact" in error for error in errors),
                errors,
            )

            stale = copy.deepcopy(receipt)
            stale["contracts"]["adaptation"]["sha256"] = "f" * 64
            errors = validate_variant(stale)
            self.assertTrue(any("contracts.adaptation" in error and "actual" in error for error in errors), errors)

            wrong_shape = copy.deepcopy(receipt)
            wrong_shape["stems"][0]["frames"] -= 1
            errors = validate_variant(wrong_shape)
            self.assertTrue(any("one exact PCM shape" in error for error in errors), errors)
            self.assertTrue(any("current score duration" in error for error in errors), errors)

            wrong_duration = copy.deepcopy(receipt)
            wrong_duration["stems"][0]["duration_seconds"] = 1.0
            errors = validate_variant(wrong_duration)
            self.assertTrue(any("duration_seconds" in error and "frames / sample_rate" in error for error in errors), errors)

            for alias in (".work//music/competition/delibes-master.wav", ".work/./music/competition/delibes-master.wav"):
                with self.subTest(alias=alias):
                    noncanonical = copy.deepcopy(receipt)
                    noncanonical["master"]["path"] = alias
                    errors = validate_variant(noncanonical)
                    self.assertTrue(any("canonical POSIX" in error for error in errors), errors)

            newline_digest = copy.deepcopy(receipt)
            newline_digest["stems"][0]["sha256"] += "\n"
            newline_digest["verification"]["seek_probes"][0]["sha256"] += "\n"
            newline_digest["verification"]["seek_probes"][0]["repeat_sha256"] += "\n"
            errors = validate_variant(newline_digest)
            self.assertTrue(any("exactly one lowercase SHA-256" in error for error in errors), errors)

            out_of_range_probe = copy.deepcopy(receipt)
            out_of_range_probe["verification"]["seek_probes"][0] |= {
                "start_frame": out_of_range_probe["master"]["frames"] - 1,
                "frames": 2,
            }
            errors = validate_variant(out_of_range_probe)
            self.assertTrue(any("range must be" in error and "final master" in error for error in errors), errors)

            impossible_polyphony = copy.deepcopy(receipt)
            impossible_polyphony["master"]["polyphonic_frames"] = impossible_polyphony["master"]["frames"] + 1
            errors = validate_variant(impossible_polyphony)
            self.assertTrue(any("polyphonic_frames" in error and "no greater" in error for error in errors), errors)

            mismatched_polyphony = copy.deepcopy(receipt)
            mismatched_polyphony["pre_normalized_master"]["polyphonic_frames"] -= 1
            errors = validate_variant(mismatched_polyphony)
            self.assertTrue(any("same polyphonic frame count" in error for error in errors), errors)

            wrong_order = copy.deepcopy(receipt)
            wrong_order["stems"][0]["id"], wrong_order["stems"][1]["id"] = (
                wrong_order["stems"][1]["id"],
                wrong_order["stems"][0]["id"],
            )
            errors = validate_variant(wrong_order)
            self.assertTrue(any("canonical competition mix order" in error for error in errors), errors)

            wrong_pcm = copy.deepcopy(receipt)
            wrong_pcm["master"]["sample_format"] = "unsigned-8-bit-pcm"
            errors = validate_variant(wrong_pcm)
            self.assertTrue(any("sample_format" in error and "must equal" in error for error in errors), errors)

            stale_executable = copy.deepcopy(receipt)
            stale_executable["executables"]["ffmpeg"]["sha256"] = "f" * 64
            errors = validate_variant(stale_executable)
            self.assertTrue(
                any("executables.ffmpeg" in error and "current pinned" in error for error in errors),
                errors,
            )

            stale_probe = copy.deepcopy(receipt)
            stale_probe["verification"]["seek_probes"][0]["repeat_sha256"] = "f" * 64
            errors = validate_variant(stale_probe)
            self.assertTrue(any("repeat digest must equal" in error for error in errors), errors)

            wrong_normalization = copy.deepcopy(receipt)
            wrong_normalization["normalization"]["targets"]["integrated_lufs"] = -18.0
            errors = validate_variant(wrong_normalization)
            self.assertTrue(any("normalization.targets" in error and "current mix" in error for error in errors), errors)

            impossible_output = copy.deepcopy(receipt)
            impossible_output["normalization"]["output"]["integrated_lufs"] = -50.0
            impossible_output["normalization"]["output"]["true_peak_dbtp"] = 0.0
            errors = validate_variant(impossible_output)
            self.assertTrue(any("integrated_lufs" in error and "mix tolerance" in error for error in errors), errors)
            self.assertTrue(any("true_peak_dbtp" in error and "mix ceiling" in error for error in errors), errors)

            false_clearance = copy.deepcopy(receipt)
            false_clearance["clearance"]["note"] = (
                "Music rights approved; final cut approved; uploaded and submitted."
            )
            errors = validate_variant(false_clearance)
            self.assertTrue(any("clearance.note" in error and "must equal" in error for error in errors), errors)

            errors = validate_variant(receipt, status="pending-render")
            self.assertTrue(any("hydrated-derived custody is valid only" in error for error in errors), errors)

    def test_recording_custody_emitter_is_exact_redacted_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir=FIXTURE_TMP_ROOT) as temporary:
            directory = Path(temporary)
            master = directory / "master.wav"
            pre_normalized = directory / "pre-normalized.wav"
            stem_ids = (
                "violin-i",
                "violin-ii",
                "viola",
                "cello",
                "contrabass",
                "triangle",
                "timpani",
            )
            stem_paths = [directory / f"{stem_id}.wav" for stem_id in stem_ids]
            targets = [(master, (100, -100, 200, -200)), (pre_normalized, (90, -90, 180, -180))]
            targets.extend((path, (50, -50, 75, -75)) for path in stem_paths)
            for target, samples in targets:
                with wave.open(str(target), "wb") as writer:
                    writer.setnchannels(2)
                    writer.setsampwidth(2)
                    writer.setframerate(48_000)
                    writer.writeframes(b"".join(int(value).to_bytes(2, "little", signed=True) for value in samples))

            from render_music import inspect_wav, pcm_slice_hash

            def output(path: Path, *, stem_id: str | None = None) -> dict:
                row = inspect_wav(path, sample_rate=48_000, frames=2)
                if stem_id is None:
                    row["polyphonic_frames"] = 1
                return {"id": stem_id, **row} if stem_id else row

            audio_path = directory / "audio-render.json"
            contracts = {
                name: {"path": f"music/{name}.json", "sha256": hashlib.sha256(name.encode()).hexdigest()}
                for name in (
                    "score",
                    "choreography",
                    "midi",
                    "adaptation",
                    "toolchain",
                    "mix",
                    "audio_uses",
                    "soundfont",
                )
            }
            contracts["score"] |= {"contract_sha256": "1" * 64, "duration_seconds": 2 / 48_000}
            contracts["choreography"]["contract_sha256"] = "2" * 64
            contracts["fluidsynth_executable"] = {
                "path": "/opt/homebrew/bin/fluidsynth",
                "sha256": "3" * 64,
                "version": "2.6.0",
            }
            contracts["ffmpeg_executable"] = {
                "path": "/opt/homebrew/bin/ffmpeg",
                "sha256": "4" * 64,
                "version": "9.0.1",
            }
            loudness = {
                "integrated_lufs": -16.0,
                "true_peak_dbtp": -1.1,
                "lra_lu": 8.0,
                "threshold_lufs": -26.0,
            }
            audio = {
                "schema": "danse.audio.render.v1",
                "profile": "competition-classical",
                "inputs": contracts,
                "outputs": {
                    "pre_normalized_master": output(pre_normalized),
                    "master": output(master),
                    "stems": [
                        output(path, stem_id=stem_id)
                        for stem_id, path in zip(stem_ids, stem_paths, strict=True)
                    ],
                },
                "normalization": {
                    "schema": "danse.audio.normalization.v1",
                    "method": "ffmpeg-loudnorm-two-pass",
                    "limiter": "ffmpeg-loudnorm-dynamic-true-peak",
                    "ffmpeg": {"version": "9.0.1", "executable_sha256": "4" * 64},
                    "targets": {
                        "integrated_lufs": -16.0,
                        "tolerance_lu": 0.5,
                        "target_true_peak_dbtp": -1.1,
                        "max_true_peak_dbtp": -1.0,
                        "lra_lu": 11.0,
                    },
                    "first_pass": loudness,
                    "application_filter": "loudnorm=I=-16:LRA=11:TP=-1.1",
                    "normalization_type": "dynamic",
                    "output": loudness,
                },
                "verification": {
                    "repeat_master_sha256": sha256(master),
                    "deterministic": True,
                    "non_silent": True,
                    "stems_non_silent": True,
                    "polyphonic": True,
                    "normalization_deterministic": True,
                    "loudness_in_target": True,
                    "true_peak_in_target": True,
                    "duration_matches_score": True,
                    "seek_safe": True,
                    "seek_probes": [
                        {
                            "start_frame": 0,
                            "frames": 2,
                            "sha256": pcm_slice_hash(master, 0, 2),
                            "repeat_sha256": pcm_slice_hash(master, 0, 2),
                            "equal": True,
                        }
                    ],
                },
                "runtime": {"python": "3.13.7", "platform": "test"},
            }
            audio_path.write_text(json.dumps(audio))
            with mock.patch(
                "record_recording_custody.validate_production_input_graph",
                return_value=CANONICAL_STEMS,
            ) as production_validation:
                receipt = build_receipt(
                    audio_path.relative_to(ROOT),
                    work_id="delibes-screendance-suite",
                    recorded_on="2026-08-30",
                )
            production_validation.assert_called_once()
            self.assertEqual(receipt["status"], "custody-only")
            self.assertEqual(receipt["clearance"]["state"], "pending")
            self.assertEqual(receipt["pre_normalized_master"]["sha256"], sha256(pre_normalized))
            self.assertEqual(receipt["master"]["sha256"], sha256(master))
            self.assertNotIn(str(ROOT), json.dumps(receipt))
            self.assertEqual(validate_recording_custody_schema(receipt), [])
            with mock.patch(
                "record_recording_custody.validate_production_input_graph",
                return_value=CANONICAL_STEMS,
            ):
                self.assertEqual(hydrated_receipt_errors(receipt, require_hydrated=True), [])

            missing_schema_fields = copy.deepcopy(audio)
            missing_schema_fields.pop("normalization")
            audio_path.write_text(json.dumps(missing_schema_fields))
            with mock.patch(
                "record_recording_custody.validate_production_input_graph",
                return_value=CANONICAL_STEMS,
            ) as production_validation:
                with self.assertRaisesRegex(ValueError, "audio render receipt schema"):
                    build_receipt(
                        audio_path,
                        work_id="delibes-screendance-suite",
                        recorded_on="2026-08-30",
                    )
            production_validation.assert_not_called()

            wrong_stems = copy.deepcopy(audio)
            for index, row in enumerate(wrong_stems["outputs"]["stems"]):
                row["id"] = f"arbitrary-{index}"
            audio_path.write_text(json.dumps(wrong_stems))
            with mock.patch(
                "record_recording_custody.validate_production_input_graph",
                return_value=CANONICAL_STEMS,
            ):
                with self.assertRaisesRegex(ValueError, "canonical competition mix"):
                    build_receipt(
                        audio_path,
                        work_id="delibes-screendance-suite",
                        recorded_on="2026-08-30",
                    )

            audio_path.write_text(json.dumps(audio))
            with mock.patch(
                "record_recording_custody.validate_production_input_graph",
                side_effect=ValueError("current production graph is stale"),
            ):
                with self.assertRaisesRegex(ValueError, "current production graph is stale"):
                    build_receipt(
                        audio_path,
                        work_id="delibes-screendance-suite",
                        recorded_on="2026-08-30",
                    )

            pcm8 = directory / "pcm8.wav"
            with wave.open(str(pcm8), "wb") as writer:
                writer.setnchannels(2)
                writer.setsampwidth(1)
                writer.setframerate(48_000)
                writer.writeframes(b"\x80\x80\x81\x7f")
            wrong_pcm = copy.deepcopy(audio)
            wrong_pcm["outputs"]["stems"][0] |= {
                "path": pcm8.relative_to(ROOT).as_posix(),
                "sha256": sha256(pcm8),
                "frames": 2,
            }
            audio_path.write_text(json.dumps(wrong_pcm))
            with mock.patch(
                "record_recording_custody.validate_production_input_graph",
                return_value=CANONICAL_STEMS,
            ):
                with self.assertRaisesRegex(ValueError, "signed 16-bit PCM"):
                    build_receipt(
                        audio_path,
                        work_id="delibes-screendance-suite",
                        recorded_on="2026-08-30",
                    )

            master.write_bytes(master.read_bytes() + b"replacement")
            audio_path.write_text(json.dumps(audio))
            with mock.patch(
                "record_recording_custody.validate_production_input_graph",
                return_value=CANONICAL_STEMS,
            ):
                errors = hydrated_receipt_errors(receipt, require_hydrated=True)
            self.assertTrue(any("declares" in error and "actual" in error for error in errors), errors)

            audio["verification"]["loudness_in_target"] = False
            audio_path.write_text(json.dumps(audio))
            with self.assertRaisesRegex(ValueError, "audio render receipt schema"):
                build_receipt(
                    audio_path,
                    work_id="delibes-screendance-suite",
                    recorded_on="2026-08-30",
                )

    def test_custody_transition_rebinds_both_rights_identities_and_is_portable(self) -> None:
        receipt = current_recording_custody_receipt("a" * 64)
        with tempfile.TemporaryDirectory(dir=ROOT / "music") as temporary:
            receipt_path = Path(temporary) / "recording-custody.json"
            receipt_payload = json.dumps(receipt, indent=2) + "\n"
            receipt_path.write_text(receipt_payload)
            relative_receipt = receipt_path.relative_to(ROOT).as_posix()
            transitioned, repertoire_payload, rights = transition_recording_custody(
                copy.deepcopy(self.production_register),
                json.loads((ROOT / "rights/register.json").read_text()),
                receipt,
                receipt_path=relative_receipt,
                receipt_sha256=sha256(receipt_path),
            )
            repertoire_digest = hashlib.sha256(repertoire_payload).hexdigest()
            references: list[dict] = []

            def collect(value: object) -> None:
                if isinstance(value, dict):
                    if value.get("path") == "music/repertoire.yaml":
                        references.append(value)
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            collect(rights)
            self.assertEqual(len(references), 2)
            self.assertTrue(all(row.get("sha256") == repertoire_digest for row in references))

            tracked = subprocess.run(
                ["git", "-C", str(ROOT), "ls-files", "-z"],
                capture_output=True,
                check=True,
            ).stdout + relative_receipt.encode() + b"\0"
            listed = subprocess.CompletedProcess([], 0, stdout=tracked, stderr=b"")
            with mock.patch("validate_repertoire.subprocess.run", return_value=listed):
                self.assertEqual(validate_document(transitioned, check_derived=False), [])
                transitioned_score = compile_contract(
                    transitioned,
                    self.program,
                    "delibes-screendance-suite",
                )
            self.assertEqual(output_bytes(transitioned_score), (ROOT / "music/score.json").read_bytes())

    def test_custody_transition_rejects_symlink_outputs_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "music") as temporary:
            directory = Path(temporary)
            target = directory / "target.json"
            target.write_text("target")
            alias = directory / "receipt.json"
            alias.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                _canonical_repository_output(alias, "custody receipt output")

            first = directory / "first.json"
            second = directory / "second.json"
            first.write_bytes(b"first-old")
            second.write_bytes(b"second-old")
            real_replace = os.replace

            def fail_second(source: object, destination: object) -> None:
                if Path(destination) == second:
                    raise OSError("simulated second replacement failure")
                real_replace(source, destination)

            with mock.patch("record_recording_custody.os.replace", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "simulated"):
                    _atomic_write_many([(first, b"first-new"), (second, b"second-new")])
            self.assertEqual(first.read_bytes(), b"first-old")
            self.assertEqual(second.read_bytes(), b"second-old")

    def test_recording_custody_rehashes_every_current_render_input(self) -> None:
        score_path = ROOT / "music/score.json"
        choreography_path = ROOT / "render/choreography.json"
        midi_path = ROOT / "music/delibes-screendance-suite.mid"
        adaptation_path = ROOT / "music/adaptation.json"
        toolchain_path = ROOT / "music/audio-toolchain.json"
        mix_path = ROOT / "music/delibes-mix.json"
        uses_path = ROOT / "sound/audio-uses.json"
        soundfont_path = ROOT / "music/licenses/MuseScore_General_License.md"
        score = json.loads(score_path.read_text())
        choreography = json.loads(choreography_path.read_text())
        mix = json.loads(mix_path.read_text())
        env_name = shutil.which("env")
        true_name = shutil.which("true")
        self.assertIsNotNone(env_name, "portable test fixture requires an env executable")
        self.assertIsNotNone(true_name, "portable test fixture requires a true executable")
        env_executable = Path(str(env_name)).resolve(strict=True)
        true_executable = Path(str(true_name)).resolve(strict=True)
        self.assertTrue(env_executable.is_file() and not env_executable.is_symlink())
        self.assertTrue(true_executable.is_file() and not true_executable.is_symlink())
        inputs = {
            "score": {
                "path": score_path.relative_to(ROOT).as_posix(),
                "sha256": sha256(score_path),
                "contract_sha256": score["identity"]["contract_sha256"],
                "duration_seconds": score["time"]["duration_seconds"],
            },
            "choreography": {
                "path": choreography_path.relative_to(ROOT).as_posix(),
                "sha256": sha256(choreography_path),
                "contract_sha256": choreography["identity"]["contract_sha256"],
            },
            "midi": {"path": midi_path.relative_to(ROOT).as_posix(), "sha256": sha256(midi_path)},
            "adaptation": {
                "path": adaptation_path.relative_to(ROOT).as_posix(),
                "sha256": sha256(adaptation_path),
            },
            "toolchain": {
                "path": toolchain_path.relative_to(ROOT).as_posix(),
                "sha256": sha256(toolchain_path),
            },
            "mix": {"path": mix_path.relative_to(ROOT).as_posix(), "sha256": sha256(mix_path)},
            "audio_uses": {"path": uses_path.relative_to(ROOT).as_posix(), "sha256": sha256(uses_path)},
            "soundfont": {
                "path": soundfont_path.relative_to(ROOT).as_posix(),
                "sha256": sha256(soundfont_path),
            },
            "fluidsynth_executable": {
                "path": str(env_executable),
                "sha256": sha256(env_executable),
                "version": "2.6.0",
            },
            "ffmpeg_executable": {
                "path": str(true_executable),
                "sha256": sha256(true_executable),
                "version": "9.0.1",
            },
        }
        audio = {"profile": "competition-classical", "inputs": inputs}
        validated = {
            "score": score,
            "choreography": choreography,
            "mix": mix,
        }
        with mock.patch("render_music.validate_inputs", return_value=validated), mock.patch(
            "record_recording_custody.authenticate_production_outputs"
        ) as output_authentication:
            self.assertEqual(validate_production_input_graph(audio), CANONICAL_STEMS)
        output_authentication.assert_called_once()

        stale = copy.deepcopy(audio)
        stale["inputs"]["midi"]["sha256"] = "f" * 64
        with mock.patch("render_music.validate_inputs", return_value=validated) as production_validation:
            with self.assertRaisesRegex(ValueError, "midi.sha256 declares"):
                validate_production_input_graph(stale)
        production_validation.assert_not_called()

        alternate = copy.deepcopy(audio)
        alternate_score = ROOT / "music/fixtures/score-affine.json"
        alternate["inputs"]["score"] |= {
            "path": alternate_score.relative_to(ROOT).as_posix(),
            "sha256": sha256(alternate_score),
        }
        with mock.patch("render_music.validate_inputs", return_value=validated) as production_validation:
            with self.assertRaisesRegex(ValueError, "score.path must equal the canonical current artifact"):
                validate_production_input_graph(alternate)
        production_validation.assert_not_called()

    def test_recording_custody_authenticates_outputs_against_two_reproductions(self) -> None:
        def row(digest: str, *, stem_id: str | None = None, polyphonic: bool = False) -> dict:
            value = {
                "sha256": digest,
                "frames": 2,
                "sample_rate": 48_000,
                "channels": 2,
                "duration_seconds": round(2 / 48_000, 9),
                "peak_sample": 100,
                "rms_sample": 50.0,
                "non_silent": True,
            }
            if polyphonic:
                value["polyphonic_frames"] = 1
            if stem_id is not None:
                value = {"id": stem_id, **value}
            return value

        rendered = {
            "pre_normalized_master": row("a" * 64, polyphonic=True),
            "master": row("b" * 64, polyphonic=True),
            "stems": [
                row(hashlib.sha256(stem_id.encode()).hexdigest(), stem_id=stem_id)
                for stem_id in CANONICAL_STEMS
            ],
            "normalization": {"schema": "test-normalization"},
        }
        audio = {
            "outputs": copy.deepcopy(
                {
                    "pre_normalized_master": rendered["pre_normalized_master"],
                    "master": rendered["master"],
                    "stems": rendered["stems"],
                }
            ),
            "normalization": copy.deepcopy(rendered["normalization"]),
        }
        contracts = {
            "score": {"time": {"duration_seconds": 2 / 48_000}},
            "mix": {"sample_rate": 48_000},
        }
        with mock.patch(
            "render_music.render_once",
            side_effect=[copy.deepcopy(rendered), copy.deepcopy(rendered)],
        ) as reproduce:
            authenticate_production_outputs(audio, args=SimpleNamespace(), contracts=contracts)
        self.assertEqual(reproduce.call_count, 2)

        forged = copy.deepcopy(audio)
        forged["outputs"]["master"]["sha256"] = "f" * 64
        with mock.patch(
            "render_music.render_once",
            side_effect=[copy.deepcopy(rendered), copy.deepcopy(rendered)],
        ):
            with self.assertRaisesRegex(ValueError, "reproduction 1 master"):
                authenticate_production_outputs(forged, args=SimpleNamespace(), contracts=contracts)

    def test_compiler_boundaries_global_dynamics_and_authored_note_order(self) -> None:
        division = 480
        tempo = Event(0, 0, 0, "tempo", (500_000,))
        timeline = Timeline(division, [tempo])

        meters = [
            Event(0, 0, 1, "meter", (4, 2, 24, 8)),
            Event(1920, 0, 2, "meter", (3, 2, 24, 8)),
        ]
        _meter_rows, beats = meter_rows(meters, 3360, division, timeline)
        self.assertEqual(
            [(beat["tick"], beat["bar"]) for beat in beats if beat["downbeat"]],
            [(0, 1), (1920, 2)],
        )

        tail = Event(1000, 0, 1, "marker", ("cue:tail:cue:100",))
        with self.assertRaisesRegex(ValueError, "beyond MIDI duration"):
            cue_rows([tail], {"tail": {"window_beats": 1}}, 1200, division, timeline)

        expression = [
            Event(0, 0, 1, "control", (0, 11, 25)),
            Event(0, 1, 1, "control", (0, 11, 99)),
            Event(480, 0, 2, "control", (0, 11, 50)),
        ]
        dynamics = dynamics_rows(expression, division, timeline, {"track": 0, "channel": 0})
        self.assertEqual(
            [(row["track"], row["channel"], row["midi_expression"]) for row in dynamics],
            [(0, 0, 25), (0, 0, 50)],
        )
        with self.assertRaisesRegex(ValueError, "no CC11 expression"):
            dynamics_rows(expression, division, timeline, {"track": 3, "channel": 0})

        notes, stems = note_and_orchestration_rows(
            [
                Event(0, 0, 0, "program", (0, 41)),
                Event(0, 0, 1, "control", (0, 64, 127)),
                Event(0, 1, 0, "note_on", (0, 72, 100)),
                Event(0, 1, 1, "note_on", (0, 60, 100)),
                Event(480, 1, 2, "note_off", (0, 72, 0)),
                Event(480, 1, 3, "note_off", (0, 60, 0)),
                Event(960, 0, 2, "control", (0, 64, 0)),
            ],
            {1: "authored-order"},
            1200,
            division,
            timeline,
            "a" * 64,
        )
        self.assertEqual(
            [(note["pitch"], note["source_order"], note["program"], note["end_tick"]) for note in notes],
            [(72, 0, 41, 960), (60, 1, 41, 960)],
        )
        self.assertEqual(stems[0]["program"], 41)

    def test_meta_events_clear_running_status(self) -> None:
        channel_then_meta_then_data = bytes(
            [
                0x00,
                0x90,
                60,
                100,
                0x00,
                0xFF,
                0x06,
                0x01,
                ord("x"),
                0x00,
                61,
                100,
            ]
        )
        with self.assertRaisesRegex(ValueError, "data byte without running status"):
            parse_track(channel_then_meta_then_data, 0)

    def test_compiler_rechecks_declared_midi_identity_before_parsing(self) -> None:
        stale = copy.deepcopy(self.register)
        stale["works"][0]["score"]["source_midi"]["sha256"] = "0" * 64
        with mock.patch("compile_score.validate_document", return_value=[]):
            with self.assertRaisesRegex(ValueError, "source_midi.sha256.*does not match actual"):
                compile_contract(stale, self.program, "generated-contract-study")

    def test_score_contract_digest_rejects_content_with_a_stale_identity(self) -> None:
        tampered = copy.deepcopy(self.score)
        tampered["notes"][0]["pitch"] += 1
        with self.assertRaisesRegex(ValueError, "contract_sha256 does not match"):
            validate_score(tampered)

        script = """
          import fs from 'node:fs';
          import { contractSha256, validate } from './engine/score.js';
          const score = JSON.parse(fs.readFileSync(process.env.DANSE_FIXTURE_SCORE));
          const declared = score.identity.contract_sha256;
          const actual = contractSha256(score);
          score.notes[0].pitch += 1;
          let rejected = null;
          try { validate(score); } catch (error) { rejected = error.message; }
          console.log(JSON.stringify({declared, actual, rejected}));
        """
        result = run("node", "--input-type=module", "--eval", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["declared"], payload["actual"])
        self.assertIn("contract_sha256 does not match", payload["rejected"])

    def test_js_and_python_queries_are_value_identical(self) -> None:
        window = {"t0": 17.25, "seconds": 312.54}
        times = [17.25, 49.304, 113.4, 119.85, 120.1, 145.45, 248.0, 329.79]
        expected = [compact(score_at(self.score, at, window)) for at in times]
        script = f"""
          import fs from 'node:fs';
          import {{ scoreAt, validate }} from './engine/score.js';
          const score = validate(JSON.parse(fs.readFileSync(process.env.DANSE_FIXTURE_SCORE)));
          const window = {json.dumps(window)};
          const compact = (state) => ({{
            source: Number(state.source_second.toFixed(8)),
            scale: Number(state.scale.toFixed(8)),
            tempo: Number(state.tempo.effective_bpm.toFixed(8)),
            beat: state.beat.index,
            downbeat: state.beat.downbeat,
            beat_phase: Number(state.beat.phase.toFixed(8)),
            phrase: state.phrase.id,
            dynamic: state.dynamic.midi_expression,
            movement: state.movement.id,
            movement_u: Number(state.movement.u.toFixed(8)),
            cues: state.cues.map((cue) => cue.id),
            visual: state.visual,
          }});
          console.log(JSON.stringify({json.dumps(times)}.map((at) => compact(scoreAt(score, at, window)))));
        """
        result = run("node", "--input-type=module", "--eval", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

    def test_zero_beat_span_uses_the_tempo_fallback_in_both_consumers(self) -> None:
        score = copy.deepcopy(self.score)
        score["beats"][8]["index"] = 7
        expected = score_at(score, 4.25)["beat"]["phase"]
        script = """
          import fs from 'node:fs';
          import { scoreAt } from './engine/score.js';
          const score = JSON.parse(fs.readFileSync(process.env.DANSE_FIXTURE_SCORE));
          score.beats[8].index = 7;
          console.log(JSON.stringify(scoreAt(score, 4.25).beat.phase));
        """
        result = run("node", "--input-type=module", "--eval", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(expected, 0.5)

    def test_bucket_event_queries_are_scaled_half_open_deduplicated_and_value_identical(self) -> None:
        window = {"t0": 51.25, "seconds": 312.54}
        scale = window["seconds"] / self.score["time"]["duration_seconds"]
        at = lambda source: window["t0"] + source * scale
        queries = [
            [at(120.0), at(136.0)],
            [at(128.0), at(132.0)],
            [at(225.0), at(228.0)],
            [at(226.0), at(226.5)],
            [at(120.25), at(120.4)],
            [at(128.0), at(128.0)],
        ]
        expected = [compact_events(events_between(self.score, start, end, window)) for start, end in queries]

        self.assertIn(6, self.score["lookup"]["buckets"][225]["active_cues"])
        self.assertIn(6, self.score["lookup"]["buckets"][226]["active_cues"])
        self.assertEqual([(row["type"], row["index"]) for row in expected[0]], [
            ("cue", 2), ("note", 2),
            ("cue", 3), ("note", 3),
            ("cue", 4), ("note", 4),
        ])
        self.assertEqual([(row["type"], row["index"]) for row in expected[1]], [("cue", 3), ("note", 3)])
        self.assertEqual([(row["type"], row["index"]) for row in expected[2]], [("cue", 6), ("note", 6)])
        self.assertEqual(expected[3], [], "an already-active cue is not a new authored start")
        self.assertEqual(expected[4], [], "an already-active note is not a second note-on event")
        self.assertEqual(expected[5], [])

        script = f"""
          import fs from 'node:fs';
          import {{ eventsBetween }} from './engine/score.js';
          const score = JSON.parse(fs.readFileSync(process.env.DANSE_FIXTURE_SCORE));
          const window = {json.dumps(window)};
          const compact = (events) => events.map((event) => ({{
            type: event.type,
            index: event.index,
            name: event.id ?? event.stem,
            at: Number(event.at.toFixed(8)),
            end: Number(event.end.toFixed(8)),
          }}));
          const queries = {json.dumps(queries)};
          console.log(JSON.stringify(queries.map(([start, end]) => compact(eventsBetween(score, start, end, window)))));
        """
        result = run("node", "--input-type=module", "--eval", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

    def test_subsecond_recast_remains_cumulative_after_its_cue_window(self) -> None:
        score = copy.deepcopy(self.score)
        cue = score["cues"][3]
        cue["second"] = 128.25
        cue["end_second"] = 128.5
        score["lookup"]["buckets"][128]["recast"] = 1
        expected = [score_at(score, at)["visual"]["recast"] for at in (128.2, 128.3, 128.6)]
        self.assertEqual(expected, [1, 2, 2])

        script = """
          import fs from 'node:fs';
          import { scoreAt } from './engine/score.js';
          const score = JSON.parse(fs.readFileSync(process.env.DANSE_FIXTURE_SCORE));
          score.cues[3].second = 128.25;
          score.cues[3].end_second = 128.5;
          score.lookup.buckets[128].recast = 1;
          console.log(JSON.stringify([128.2, 128.3, 128.6].map((at) => scoreAt(score, at).visual.recast)));
        """
        result = run("node", "--input-type=module", "--eval", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

    def test_event_queries_use_lookup_indices_without_scanning_event_arrays(self) -> None:
        class IndexedOnly(list):
            def __init__(self, rows: list[dict]) -> None:
                super().__init__(rows)
                self.reads = 0

            def __iter__(self):
                raise AssertionError("event query attempted a full-array scan")

            def __getitem__(self, index):
                if isinstance(index, int):
                    self.reads += 1
                return super().__getitem__(index)

        guarded = copy.deepcopy(self.score)
        guarded["cues"] = IndexedOnly(guarded["cues"])
        guarded["notes"] = IndexedOnly(guarded["notes"])
        events = events_between(guarded, 128.0, 128.1)
        self.assertEqual([(event["type"], event["index"]) for event in events], [("cue", 3), ("note", 3)])
        self.assertEqual((guarded["cues"].reads, guarded["notes"].reads), (1, 1))

        script = """
          import fs from 'node:fs';
          import { eventsBetween } from './engine/score.js';
          const score = JSON.parse(fs.readFileSync(process.env.DANSE_FIXTURE_SCORE));
          const guard = (rows) => {
            let reads = 0;
            const proxy = new Proxy(rows, {
              get(target, property, receiver) {
                if (property === Symbol.iterator) throw new Error('event query attempted a full-array scan');
                if (typeof property === 'string' && /^\\d+$/.test(property)) reads += 1;
                return Reflect.get(target, property, receiver);
              },
            });
            return { proxy, reads: () => reads };
          };
          const cues = guard(score.cues);
          const notes = guard(score.notes);
          score.cues = cues.proxy;
          score.notes = notes.proxy;
          const events = eventsBetween(score, 128, 128.1);
          console.log(JSON.stringify({
            events: events.map((event) => [event.type, event.index]),
            reads: [cues.reads(), notes.reads()],
          }));
        """
        result = run("node", "--input-type=module", "--eval", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"events": [["cue", 3], ["note", 3]], "reads": [1, 1]})

    def test_optional_live_loader_recovers_while_strict_loader_still_fails_closed(self) -> None:
        script = """
          import { load, loadOptional } from './engine/score.js';
          globalThis.fetch = async () => ({ ok: false, status: 404 });
          let reported = null;
          const optional = await loadOptional('missing-score.json', (error) => { reported = error.message; });
          let strict = null;
          try { await load('missing-score.json'); } catch (error) { strict = error.message; }
          console.log(JSON.stringify({ optional, reported, strict }));
        """
        result = run("node", "--input-type=module", "--eval", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "optional": None,
                "reported": "music score 404 at missing-score.json",
                "strict": "music score 404 at missing-score.json",
            },
        )

    def test_score_boundaries_beats_accents_and_visual_transitions_land_exactly(self) -> None:
        movements = [(row["id"], row["start_second"], row["end_second"]) for row in self.score["movements"]]
        self.assertEqual(
            movements,
            [
                ("ONE", 0.0, 40.0),
                ("ASSEMBLY", 40.0, 65.0),
                ("DIVISION", 65.0, 120.0),
                ("PHRASE", 120.0, 225.0),
                ("STILLNESS", 225.0, 285.0),
                ("RESEED", 285.0, 386.0),
                ("SIGNATURE", 386.0, 390.0),
            ],
        )
        beat = score_at(self.score, 4.0)["beat"]
        self.assertEqual((beat["index"], beat["bar"], beat["beat_in_bar"], beat["downbeat"]), (8, 3, 1, True))
        before = score_at(self.score, 127.999)
        accent = score_at(self.score, 128.0)
        after = score_at(self.score, 128.251)
        self.assertEqual(accent["cues"][0]["id"], "phrase-accent-a")
        self.assertEqual(accent["visual"]["recast"], before["visual"]["recast"] + 1)
        self.assertGreater(accent["visual"]["channel_offsets"]["turnover"], 0)
        self.assertFalse(after["cues"])
        self.assertEqual(after["visual"]["recast"], accent["visual"]["recast"])

    def test_seek_segment_concat_restart_and_audio_disabled_paths_align(self) -> None:
        window = {"t0": 51.25, "seconds": 312.54}
        full = events_between(self.score, window["t0"], window["t0"] + window["seconds"], window)
        edges = [window["t0"], 100.0, 177.0, 250.0, window["t0"] + window["seconds"]]
        segmented = [
            event
            for start, end in zip(edges, edges[1:])
            for event in events_between(self.score, start, end, window)
        ]
        self.assertEqual(segmented, full)
        phase = 0.615
        first = score_at(self.score, window["t0"] + phase * window["seconds"], window)
        restarted_window = {"t0": 900.0, "seconds": 425.0}
        restarted = score_at(
            self.score,
            restarted_window["t0"] + phase * restarted_window["seconds"],
            restarted_window,
        )
        self.assertEqual(
            (first["movement"]["id"], first["phrase"]["id"], first["beat"]["index"]),
            (restarted["movement"]["id"], restarted["phrase"]["id"], restarted["beat"]["index"]),
        )

        script = """
          import fs from 'node:fs';
          import { state } from './engine/clock.js';
          import { passageAt } from './engine/program.js';
          import { validate } from './engine/score.js';
          import { scheduleWebAudio } from './sound/web_audio.mjs';
          const score = validate(JSON.parse(fs.readFileSync(process.env.DANSE_FIXTURE_SCORE)));
          const program = JSON.parse(fs.readFileSync('render/program.json'));
          const passage = passageAt(program, 0x12345678, 0, 7);
          const t = passage.t0 + (128 / 390) * passage.seconds;
          const before = state(0x12345678, t, program, 7, score);
          const audio = scheduleWebAudio({currentTime:0}, score, {}, 120, 140, {window:{t0:0,seconds:390}});
          const after = state(0x12345678, t, program, 7, score);
          console.log(JSON.stringify({
            identical: JSON.stringify(before) === JSON.stringify(after),
            planned: audio.plan.length,
            scheduled: audio.scheduled.length,
            missing: audio.missing.length,
            blocked: audio.blocked.length,
            movement: before.movement,
            cue: before.music.cues.map((row) => row.id),
          }));
        """
        result = run("node", "--input-type=module", "--eval", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "identical": True,
                "planned": 4,
                "scheduled": 0,
                "missing": 0,
                "blocked": 4,
                "movement": "PHRASE",
                "cue": ["phrase-accent-a"],
            },
        )

    def test_hold_freezes_the_authored_turnover_state_instead_of_resetting_epoch_zero(self) -> None:
        script = """
          import fs from 'node:fs';
          import { state, turnover } from './engine/clock.js';
          import { passageAt } from './engine/program.js';
          const program = JSON.parse(fs.readFileSync('render/program.json'));
          const baselineScore = JSON.parse(fs.readFileSync(process.env.DANSE_FIXTURE_SCORE));
          const heldScore = JSON.parse(JSON.stringify(baselineScore));
          heldScore.cues[3].visual.hold = true;
          const seed = 0x12345678;
          const stream = 7;
          const passage = passageAt(program, seed, 0, stream);
          const absolute = (source) => passage.t0 + (source / 390) * passage.seconds;
          const startAt = absolute(128);
          const laterAt = absolute(128.125);
          const baseline = state(seed, startAt, program, stream, baselineScore);
          const heldStart = state(seed, startAt, program, stream, heldScore);
          const heldLater = state(seed, laterAt, program, stream, heldScore);
          const photoState = (snapshot, at) => turnover(
            23,
            snapshot.material,
            snapshot.turnoverAt ?? at,
            snapshot.turnover,
          );
          console.log(JSON.stringify({
            start: photoState(heldStart, startAt),
            baseline: photoState(baseline, startAt),
            later: photoState(heldLater, laterAt),
            rates: [baseline.turnover, heldStart.turnover, heldLater.turnover],
            anchors: [heldStart.turnoverAt, heldLater.turnoverAt],
            material: [baseline.material, heldStart.material, heldLater.material],
          }));
        """
        result = run("node", "--input-type=module", "--eval", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["start"], payload["baseline"])
        self.assertEqual(payload["later"], payload["baseline"])
        self.assertEqual(payload["rates"][0], payload["rates"][1])
        self.assertEqual(payload["rates"][1], payload["rates"][2])
        self.assertGreater(payload["rates"][0], 0)
        self.assertEqual(payload["anchors"][0], payload["anchors"][1])
        self.assertEqual(payload["material"][0], payload["material"][1])
        self.assertEqual(payload["material"][1], payload["material"][2])

    def test_control_and_segment_receipts_emit_score_and_source_identity_without_local_paths(self) -> None:
        result = run("node", "sound/control.mjs", "--rate", "0", "--score", str(self.fixture_score_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        control = json.loads(result.stdout)
        self.assertEqual(control["music"]["identity"], self.score["identity"])
        self.assertEqual(control["music"]["score_file_sha256"], sha256(self.fixture_score_path))
        self.assertEqual(len(control["music"]["events"]), len(self.score["cues"]) + len(self.score["notes"]))
        self.assertTrue(all(stem["midi_source_sha256"] == self.score["identity"]["midi_sha256"] for stem in control["music"]["stems"]))

        offline = load_module("danse_music_receipt_test", ROOT / "render/render.py")
        args = SimpleNamespace(
            window="passage",
            start=0.0,
            tier="screen",
            seed=0,
            stream=7,
            codec="preview",
            width=320,
            height=180,
            fps=30,
            segment_frames=60,
            score=str(self.fixture_score_path),
        )
        with mock.patch.object(offline, "source_tree_sha256", return_value="fixture-tree"):
            receipt = offline.segment_identity(args, 0, 60)
        identity = receipt["inputs"]["music_score"]
        self.assertEqual(identity["path"], self.fixture_score_path.relative_to(ROOT).as_posix())
        self.assertEqual(identity["contract_sha256"], self.score["identity"]["contract_sha256"])
        self.assertNotIn(str(ROOT), json.dumps(identity))
        self.assertTrue(all(set(stem) == {"id", "midi_source_sha256", "audio_source_sha256"} for stem in identity["stems"]))

    def test_offline_receipt_rejects_structurally_invalid_or_stale_scores(self) -> None:
        offline = load_module("danse_music_bad_receipt_test", ROOT / "render/render.py")
        tampered = copy.deepcopy(self.score)
        tampered["notes"][0]["pitch"] += 1
        cases = [
            [],
            {"schema": "danse.music.score.v1"},
            tampered,
        ]
        with tempfile.TemporaryDirectory(dir=ROOT / "render") as temporary:
            directory = Path(temporary)
            for index, malformed in enumerate(cases):
                with self.subTest(index=index):
                    candidate = directory / f"bad-{index}.json"
                    candidate.write_text(json.dumps(malformed))
                    args = SimpleNamespace(score=str(candidate.relative_to(ROOT)))
                    with self.assertRaisesRegex(SystemExit, "invalid --score contract"):
                        offline.music_score_identity(args)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_control_rejects_a_repository_score_symlink_to_external_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory(dir=ROOT / "music") as inside:
            external = Path(outside) / "external-score.json"
            external.write_bytes((ROOT / "music/score.json").read_bytes())
            link = Path(inside) / "score-link.json"
            link.symlink_to(external)
            result = run(
                "node",
                "sound/control.mjs",
                "--rate",
                "0",
                "--score",
                str(link.relative_to(ROOT)),
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a symlink", result.stderr)

    def test_webaudio_requires_matching_cleared_source_identity_before_scheduling(self) -> None:
        script = """
          import fs from 'node:fs';
          import { scheduleWebAudio } from './sound/web_audio.mjs';
          const fixture = JSON.parse(fs.readFileSync(process.env.DANSE_FIXTURE_SCORE));
          let created = 0;
          const context = {
            currentTime: 0,
            destination: {},
            createBufferSource() {
              created += 1;
              return {
                playbackRate: {value: 1},
                connect() {},
                start() {},
                stop() {},
              };
            },
            createGain() {
              return {gain: {value: 1}, connect() {}};
            },
          };
          const stem = fixture.orchestration[0].id;
          const uncleared = scheduleWebAudio(context, fixture, {[stem]: {}}, 120, 121);

          const cleared = JSON.parse(JSON.stringify(fixture));
          const digest = 'a'.repeat(64);
          cleared.orchestration[0].audio_source_sha256 = digest;
          const mismatch = scheduleWebAudio(
            context,
            cleared,
            {[stem]: {buffer: {}, audio_source_sha256: 'b'.repeat(64)}},
            120,
            121,
          );
          const absent = scheduleWebAudio(context, cleared, {}, 120, 121);
          const admitted = scheduleWebAudio(
            context,
            cleared,
            {[stem]: {buffer: {}, audio_source_sha256: digest}},
            120,
            121,
          );
          console.log(JSON.stringify({
            uncleared: [uncleared.scheduled.length, uncleared.blocked.length],
            mismatch: [mismatch.scheduled.length, mismatch.blocked.length],
            absent: [absent.scheduled.length, absent.missing.length],
            admitted: [admitted.scheduled.length, admitted.blocked.length],
            created,
            planDigest: admitted.plan[0].audio_source_sha256,
          }));
        """
        result = run("node", "--input-type=module", "--eval", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "uncleared": [0, 1],
                "mismatch": [0, 1],
                "absent": [0, 1],
                "admitted": [1, 0],
                "created": 1,
                "planDigest": "a" * 64,
            },
        )

    def test_python_renderer_exposes_the_same_event_plan_and_fails_closed_on_uncleared_stems(self) -> None:
        score_renderer = load_module("danse_fixture_score_renderer_test", ROOT / "sound/score.py")
        control_result = run("node", "sound/control.mjs", "--rate", "0", "--score", str(self.fixture_score_path))
        self.assertEqual(control_result.returncode, 0, control_result.stderr)
        control = json.loads(control_result.stdout)
        plan = score_renderer.music_event_plan(control)
        self.assertEqual(plan, control["music"]["events"])
        self.assertTrue(all(stem["audio_source_sha256"] is None for stem in control["music"]["stems"]))

    def test_ab_capture_evidence_regenerates_byte_identically_and_is_current(self) -> None:
        """Predicate 4 of #9: score-to-motion is deterministically and audibly inspectable.

        The tracked `docs/evidence/score-to-motion-ab.*` receipt must regenerate
        byte-identically from the pure f(seed, t) engine, and its content must
        prove the declared boundaries move the image under the score while the
        image alone is the control.
        """
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            result = run(sys.executable, "scripts/capture-score-motion-ab.py", "--stream", "7", "--out", str(out))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            fresh_json = (out / "score-to-motion-ab.json").read_bytes()
            tracked_json = (ROOT / "docs/evidence/score-to-motion-ab.json").read_bytes()
            self.assertEqual(fresh_json, tracked_json, "A/B evidence JSON drifted — regenerate it")
            fresh_md = (out / "score-to-motion-ab.md").read_bytes()
            tracked_md = (ROOT / "docs/evidence/score-to-motion-ab.md").read_bytes()
            self.assertEqual(fresh_md, tracked_md, "A/B evidence markdown drifted — regenerate it")

        evidence = json.loads(tracked_json)
        self.assertEqual(evidence["schema"], "danse.evidence.score-to-motion-ab.fixture.v1")
        self.assertEqual(evidence["evidence_scope"], "historical-fixture-only")
        self.assertEqual(evidence["contract"], self.score["identity"]["contract_sha256"])
        structural = [row for row in evidence["rows"] if row["kind"] != "downbeat"]
        moved = [row for row in structural if any(abs(v) > 0.0005 for v in row["visual"]["score_transition"].values())]
        self.assertTrue(len(structural) >= 7, "one row per declared movement is expected")
        self.assertTrue(moved, "no declared boundary measurably moves the image")
        by_id = {row["id"]: row for row in structural}
        self.assertIn("stillness-entry", by_id)
        self.assertTrue(by_id["stillness-entry"]["visual"]["hold"], "stillness must declare the visual hold")
        accented = [row for row in structural if row["kind"] == "cue" and "accent" in row["id"]]
        self.assertTrue(all(row["visual"]["recast"] is not None for row in accented), "accents must recast the material")
        audible = [row for row in evidence["rows"] if row["audio"]["notes"]]
        self.assertTrue(audible, "the fixture must schedule audible notes at declared boundaries")

    def test_frames_capture_receipt_is_self_consistent_and_covers_every_boundary(self) -> None:
        """Predicate 4 of #9: WITH-score vs WITHOUT-score frames evidence must be lookable.

        The tracked `docs/evidence/score-to-motion-frames.*` receipt cannot be
        regenerated on CI (it needs the Metal GPU and the corpus), so this test
        proves the tracked receipt is self-consistent: determinism byte-identical,
        the contact sheet matches its recorded digest, and one row exists for every
        structural boundary the numeric A/B receipt already declared to move the
        image — at an observable PSNR in the direction the score changes the frame.
        """
        evidence_dir = ROOT / "docs/evidence"
        frames = json.loads((evidence_dir / "score-to-motion-frames.json").read_text())
        ab = json.loads((evidence_dir / "score-to-motion-ab.json").read_text())
        self.assertEqual(frames["schema"], "danse.evidence.score-to-motion-frames.v1")
        self.assertEqual(frames["evidence_scope"], "historical-fixture-only")
        self.assertEqual(frames["contract"], ab["contract"])
        self.assertEqual(frames["source"], "score-to-motion-ab.json")
        self.assertEqual(frames["determinism"]["identical"], True)
        self.assertEqual(
            frames["determinism"]["with_sha256"],
            frames["determinism"]["redraw_sha256"],
            "the two fresh-process renders must be byte-identical",
        )
        sheet = evidence_dir / frames["contact_sheet"]
        self.assertTrue(sheet.is_file(), f"contact sheet {sheet} is missing")
        self.assertEqual(
            sha256(sheet),
            frames["contact_sheet_sha256"],
            "contact sheet digest drifted — regenerate it",
        )

        declared = {
            round(row["absolute_second"], 3): row
            for row in ab["rows"]
            if row["kind"] != "downbeat"
            and any(abs(v) > 0.0005 for v in row["visual"]["score_transition"].values())
        }
        self.assertTrue(len(declared) >= 7, "one row per declared movement is expected")
        captured = {row["id"]: row["absolute_second"] for row in frames["rows"]}
        captured_times = [round(row["absolute_second"], 3) for row in frames["rows"]]
        for absolute_second, row in declared.items():
            self.assertIn(
                absolute_second,
                captured_times,
                f"boundary {row['id']} at {absolute_second}s missing from frames evidence",
            )
        accent_ids = {row["id"] for row in ab["rows"] if row["kind"] == "cue" and "accent" in row["id"]}
        self.assertTrue(accent_ids.issubset(set(captured)), "accent cues must be captured as their own rows")
        self.assertEqual(any(row["kind"] == "origin" for row in frames["rows"]), True)
        for row in frames["rows"]:
            self.assertGreaterEqual(row["psnr_db"], 0.0)
            self.assertLess(row["psnr_db"], 40.0, "an observable difference must not look identical")
            self.assertTrue(row["with_sha256"] and row["without_sha256"])

    def test_production_score_motion_contract_fails_closed_portably(self) -> None:
        result = run(sys.executable, "scripts/tests/score-motion-production.test.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
