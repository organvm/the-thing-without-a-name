#!/usr/bin/env python3
"""Cross-runtime acceptance tests for score-led photographic choreography."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sound"))
sys.path.insert(0, str(ROOT / "render"))

from choreography import (  # noqa: E402
    contract_sha256 as choreography_sha256,
    load_choreography,
    pose_at,
    validate as validate_choreography,
)
from music_score import (  # noqa: E402
    contract_sha256 as score_sha256,
    load_score,
    score_at,
    validate as validate_score,
)


SEED = 20170620
SCORE_PATH = ROOT / "music/score.json"
CHOREOGRAPHY_PATH = ROOT / "render/choreography.json"
MANIFEST_PATH = ROOT / "corpus/manifest.json"
CORPUS_SCORE_PATH = ROOT / "corpus/score-2017.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*command: str, timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def node_json(script: str, timeout: float = 180):
    result = run("node", "--input-type=module", "--eval", script, timeout=timeout)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compact_pose(pose: dict) -> list:
    return [
        pose["movement_id"],
        pose["next_movement_id"],
        pose["cut_mode"],
        pose["current_cut_mode"],
        pose["next_cut_mode"],
        pose["motif"],
        pose["pose_index"],
        pose["current_source_frame_id"],
        pose["next_source_frame_id"],
        pose["blend"],
        pose["current_geometry_frame_id"],
        pose["next_geometry_frame_id"],
        pose["phrase"]["index"],
        pose["phrase"]["bars_elapsed"],
        pose["beat"]["index"],
        pose["beat"]["phase"],
        pose["transition"]["kind"],
        pose["transition"]["progress"],
        pose["transition"]["fragment_change_fraction"],
        pose["panel_counterpoint"],
    ]


NODE_SETUP = r"""
  import fs from 'node:fs';
  import crypto from 'node:crypto';
  import { fromData } from './engine/corpus.js';
  import { validate as validateChoreography, poseAt } from './engine/choreography.js';
  import { state } from './engine/clock.js';
  import { step } from './engine/engine.js';
  import { captureSpan, fixedPassageTiming, validate as validateProgram } from './engine/program.js';
  import { validate as validateScore } from './engine/score.js';

  const scoreBytes = fs.readFileSync('music/score.json');
  const score = validateScore(JSON.parse(scoreBytes));
  Object.defineProperty(score, 'fileSha256', {
    value: crypto.createHash('sha256').update(scoreBytes).digest('hex'),
    enumerable: false,
  });
  const manifestBytes = fs.readFileSync('corpus/manifest.json');
  const manifest = JSON.parse(manifestBytes);
  const solvedBytes = fs.readFileSync('corpus/score-2017.json');
  const solved = JSON.parse(solvedBytes);
  const corpus = fromData('corpus/', manifest, solved, {
    manifest_sha256: crypto.createHash('sha256').update(manifestBytes).digest('hex'),
    score_sha256: crypto.createHash('sha256').update(solvedBytes).digest('hex'),
  });
  const choreography = validateChoreography(
    JSON.parse(fs.readFileSync('render/choreography.json')),
    { score, corpus },
  );
  const program = validateProgram(JSON.parse(fs.readFileSync('render/program.json')));
  const seed = 20170620;
"""


class ChoreographyContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.score = load_score(SCORE_PATH)
        cls.manifest = json.loads(MANIFEST_PATH.read_text())
        cls.choreography = load_choreography(
            CHOREOGRAPHY_PATH,
            score=cls.score,
            score_path=SCORE_PATH,
            corpus_manifest=cls.manifest,
            corpus_manifest_path=MANIFEST_PATH,
            corpus_score_path=CORPUS_SCORE_PATH,
        )
        cls.duration = float(cls.score["time"]["duration_seconds"])
        cls.frame_times = [index / 30 for index in range(math.ceil(cls.duration * 30)) if index / 30 < cls.duration]
        cls.frame_poses = [pose_at(cls.score, cls.choreography, SEED, at) for at in cls.frame_times]

    def reidentify(self, candidate: dict) -> dict:
        candidate["identity"]["contract_sha256"] = choreography_sha256(candidate)
        return candidate

    @staticmethod
    def bar_coordinate(pose: dict) -> float:
        beat = pose["beat"]
        return (beat["bar"] - 1) + ((beat["beat_in_bar"] - 1) + float(beat["phase"])) / 3

    def test_schema_and_all_bound_identities_are_exact(self) -> None:
        schema = json.loads((ROOT / "render/choreography.schema.json").read_text())
        jsonschema.Draft202012Validator(schema).validate(self.choreography)
        identity = self.choreography["identity"]
        self.assertEqual(identity["contract_sha256"], choreography_sha256(self.choreography))
        self.assertEqual(identity["score_contract_sha256"], self.score["identity"]["contract_sha256"])
        self.assertEqual(identity["score_file_sha256"], sha256(SCORE_PATH))
        self.assertEqual(identity["corpus_manifest_sha256"], sha256(MANIFEST_PATH))
        self.assertEqual(identity["corpus_score_sha256"], sha256(CORPUS_SCORE_PATH))
        self.assertEqual(self.score["identity"]["contract_sha256"], score_sha256(self.score))
        self.assertEqual(self.score["identity"]["midi_sha256"], sha256(ROOT / "music/delibes-screendance-suite.mid"))
        self.assertEqual(self.score["release_status"], "production-selected")
        self.assertEqual(self.score["time"]["passage_mapping"], "native-tempo")
        self.assertEqual(self.duration, 350.896343125)
        self.assertEqual(
            [(row["id"], row["start_second"], row["end_second"]) for row in self.score["movements"]],
            [
                ("SYLVIA", 0.0, 149.152297125),
                ("COPPELIA", 149.152297125, 346.896343125),
                ("SIGNATURE", 346.896343125, 350.896343125),
            ],
        )

    def test_assignments_tile_the_score_and_make_hold_transition_ownership_truthful(self) -> None:
        assignments = self.choreography["phrase_assignments"]
        self.assertEqual([row["phrase_id"] for row in assignments], [row["id"] for row in self.score["phrases"]])
        self.assertEqual(self.score["phrases"][0]["start_second"], 0)
        self.assertEqual(self.score["phrases"][-1]["end_second"], self.duration)
        blocks = []
        for assignment in assignments:
            if not blocks or blocks[-1] != assignment["movement_id"]:
                blocks.append(assignment["movement_id"])
        self.assertEqual(blocks, ["ONE", "ASSEMBLY", "DIVISION", "PHRASE", "STILLNESS", "RESEED", "SIGNATURE"])

        by_movement = {
            movement: [row for row in assignments if row["movement_id"] == movement]
            for movement in blocks
        }
        for movement in ("ONE", "ASSEMBLY", "STILLNESS"):
            self.assertTrue(any(row["hold_complete_phrase"] for row in by_movement[movement]))
        self.assertTrue(all(row["hold_complete_phrase"] for row in by_movement["SIGNATURE"]))
        for current, following in zip(assignments, assignments[1:]):
            changed = (
                current["movement_id"] != following["movement_id"]
                or current["cut_mode"] != following["cut_mode"]
                or current["motif_id"] != following["motif_id"]
            )
            if changed:
                self.assertFalse(current["hold_complete_phrase"], current["phrase_id"])

        for index, assignment in enumerate(assignments):
            if not assignment["hold_complete_phrase"]:
                continue
            start = float(self.score["phrases"][index]["start_second"])
            end = float(self.score["phrases"][index]["end_second"])
            samples = [pose_at(self.score, self.choreography, SEED, start + (end - start) * part) for part in (0.1, 0.5, 0.9)]
            self.assertTrue(all(not sample["transition"]["active"] for sample in samples), assignment["phrase_id"])
            self.assertTrue(all(sample["blend"] == 0 for sample in samples), assignment["phrase_id"])

        signature = self.score["phrases"][-1]
        self.assertEqual(signature["start_second"], 346.896343125)
        self.assertEqual(signature["end_second"] - signature["start_second"], 4)

    def test_validator_rejects_unregistered_frames_stale_bindings_and_false_holds(self) -> None:
        unregistered = copy.deepcopy(self.choreography)
        unregistered["motifs"][0]["source_frame_ids"] = ["IMG_1926"]
        unregistered["motifs"][0]["geometry_frame_id"] = "IMG_1926"
        self.reidentify(unregistered)
        with self.assertRaisesRegex(ValueError, "unregistered source frame IMG_1926"):
            validate_choreography(unregistered, corpus_manifest=self.manifest)

        stale_score = copy.deepcopy(self.choreography)
        stale_score["identity"]["score_contract_sha256"] = "0" * 64
        self.reidentify(stale_score)
        with self.assertRaisesRegex(ValueError, "score contract digest does not match"):
            validate_choreography(stale_score, score=self.score)

        short = copy.deepcopy(self.choreography)
        short["phrase_assignments"][4]["pose_dwell_bars"] = 1
        self.reidentify(short)
        with self.assertRaisesRegex(ValueError, "pose dwell is too short"):
            validate_choreography(short)

        eroded = copy.deepcopy(self.choreography)
        eroded["phrase_assignments"][1]["hold_complete_phrase"] = True
        self.reidentify(eroded)
        with self.assertRaisesRegex(ValueError, "cannot own an outgoing transition"):
            validate_choreography(eroded)

        stale_content = copy.deepcopy(self.choreography)
        stale_content["motifs"][0]["id"] = "changed-without-new-digest"
        with self.assertRaisesRegex(ValueError, "contract_sha256 does not match"):
            validate_choreography(stale_content)

    def test_js_and_python_pose_queries_are_identical_at_every_30fps_frame(self) -> None:
        expected = [compact_pose(pose) for pose in self.frame_poses]
        compact_js = r"""
          const compact = (pose) => [
            pose.movement_id, pose.next_movement_id, pose.cut_mode,
            pose.current_cut_mode, pose.next_cut_mode, pose.motif, pose.pose_index,
            pose.current_source_frame_id, pose.next_source_frame_id, pose.blend,
            pose.current_geometry_frame_id, pose.next_geometry_frame_id,
            pose.phrase.index, pose.phrase.bars_elapsed, pose.beat.index, pose.beat.phase,
            pose.transition.kind, pose.transition.progress, pose.transition.fragment_change_fraction,
            pose.panel_counterpoint,
          ];
          const out = [];
          for (let index = 0; index / 30 < score.time.duration_seconds; index++) {
            out.push(compact(poseAt(score, choreography, seed, index / 30)));
          }
          process.stdout.write(JSON.stringify(out));
        """
        observed = node_json(NODE_SETUP + compact_js, timeout=240)
        self.assertEqual(observed, expected)

    def test_full_frame_scan_proves_dwell_dissolve_budget_and_no_hard_recast(self) -> None:
        intervals: list[list[int]] = []
        for index, pose in enumerate(self.frame_poses):
            self.assertLessEqual(
                pose["transition"]["fragment_change_fraction"],
                self.choreography["legibility"]["maximum_fragment_change_area_per_bar"],
            )
            active = bool(pose["transition"]["active"])
            if active and (not intervals or intervals[-1][-1] != index - 1):
                intervals.append([index])
            elif active:
                intervals[-1].append(index)

        self.assertTrue(intervals)
        for interval in intervals:
            start_pose = self.frame_poses[interval[0]]
            end_pose = self.frame_poses[interval[-1]]
            span = self.bar_coordinate(end_pose) - self.bar_coordinate(start_pose)
            self.assertGreaterEqual(span, 0.88, (start_pose["phrase"]["id"], span))
            self.assertLessEqual(span, 1.02, (start_pose["phrase"]["id"], span))
        for previous, following in zip(intervals, intervals[1:]):
            end_pose = self.frame_poses[previous[-1]]
            start_pose = self.frame_poses[following[0]]
            dwell = self.bar_coordinate(start_pose) - self.bar_coordinate(end_pose)
            self.assertGreaterEqual(dwell, 1.88, (end_pose["phrase"]["id"], start_pose["phrase"]["id"], dwell))

        intermediate = set()
        for previous, current in zip(self.frame_poses, self.frame_poses[1:]):
            if 0 < current["blend"] < 1:
                intermediate.add(current["blend"])
            previous_pair = (previous["current_source_frame_id"], previous["next_source_frame_id"])
            current_pair = (current["current_source_frame_id"], current["next_source_frame_id"])
            if previous_pair != current_pair and previous_pair != (None, None) and current_pair != (None, None):
                same_visible_source = previous["current_source_frame_id"] == current["current_source_frame_id"]
                completed_dissolve = (
                    previous["next_source_frame_id"] == current["current_source_frame_id"]
                    and previous["blend"] > 0.98
                    and current["blend"] == 0
                )
                self.assertTrue(same_visible_source or completed_dissolve, (previous, current))
            if previous["current_cut_mode"] != current["current_cut_mode"]:
                self.assertEqual(previous["transition"]["kind"], "topology")
                self.assertGreater(previous["transition"]["progress"], 0.98)
                self.assertEqual(previous["next_cut_mode"], current["current_cut_mode"])
        self.assertGreater(len(intermediate), 300, "dissolves were quantised instead of sampled continuously")

    def test_quantisation_never_quantises_pose_blend_and_casts_use_one_authored_pair(self) -> None:
        pair = None
        for left, right in zip(range(len(self.frame_times) - 1), range(1, len(self.frame_times))):
            a_t, b_t = self.frame_times[left], self.frame_times[right]
            a, b = self.frame_poses[left], self.frame_poses[right]
            if (
                math.floor(a_t * 8) == math.floor(b_t * 8)
                and 0 < a["blend"] < b["blend"] < 1
                and a["current_source_frame_id"] == b["current_source_frame_id"]
                and a["next_source_frame_id"] == b["next_source_frame_id"]
            ):
                pair = [a_t, b_t]
                break
        self.assertIsNotNone(pair)
        topology_midpoints = []
        active = []
        for index, pose in enumerate(self.frame_poses):
            if pose["transition"]["kind"] == "topology":
                active.append(index)
            elif active:
                topology_midpoints.append(self.frame_times[active[len(active) // 2]])
                active = []
        counterpoint_midpoints = []
        active = []
        for index, pose in enumerate(self.frame_poses):
            if pose["panel_counterpoint"] and pose["panel_counterpoint"]["active_group"] is not None:
                active.append(index)
            elif active:
                counterpoint_midpoints.append(self.frame_times[active[len(active) // 2]])
                active = []
        sample_times = pair + topology_midpoints + counterpoint_midpoints
        script = NODE_SETUP + f"""
          const samples = {json.dumps(sample_times)};
          const failures = [];
          const blends = [];
          for (const at of samples) {{
            const exact = step(corpus, seed, at, program, {{ quantise: 0, score, choreography }});
            const cached = step(corpus, seed, at, program, {{ quantise: 0.125, score, choreography }});
            blends.push([exact.pose.blend, cached.pose.blend]);
            if (exact.pose.blend !== cached.pose.blend) failures.push(['quantised-blend', at]);
            // The exact recovered composite is the one explicit source-pair
            // exception: its 256 solved cells retain their authored layers while
            // its stable topology crossfades in or out as a whole.
            if (exact.pose.movement_id === 'ASSEMBLY'
                || exact.pose.next_movement_id === 'ASSEMBLY'
                || exact.pose.movement_id === 'SIGNATURE') continue;
            const allowed = new Set([exact.pose.current_source_frame_id, exact.pose.next_source_frame_id]);
            for (const panel of exact.pose.panel_counterpoint?.groups ?? []) {{
              allowed.add(panel.current_source_frame_id);
              allowed.add(panel.next_source_frame_id);
            }}
            for (const cell of exact.cast) {{
              for (const layer of cell.layers ?? []) {{
                if (!allowed.has(layer.frame)) failures.push(['undeclared-frame', at, layer.frame]);
              }}
            }}
            const counterpointCells = exact.cast.filter((cell) => cell.counterpoint_group !== undefined);
            if (exact.pose.panel_counterpoint?.active_group !== null && counterpointCells.length) {{
              const changing = counterpointCells.filter((cell) => (cell.layers ?? []).length > 1);
              const area = changing.reduce((sum, cell) => sum + (cell.rect[2] - cell.rect[0]) * (cell.rect[3] - cell.rect[1]), 0);
              if (!changing.every((cell) => cell.counterpoint_group === exact.pose.panel_counterpoint.active_group)) {{
                failures.push(['counterpoint-outside-active-cohort', at]);
              }}
              if (area > 0.250001) failures.push(['counterpoint-area-over-budget', at, area]);
            }}
            if (exact.pose.transition.kind !== 'topology' && !exact.pose.panel_counterpoint) {{
              const pairs = new Set(exact.cast.map((cell) => JSON.stringify((cell.layers ?? []).map((layer) => [layer.frame, layer.weight]))));
              if (pairs.size > 1) failures.push(['independent-cell-pairs', at, pairs.size]);
            }}
          }}
          console.log(JSON.stringify({{failures, blends}}));
        """
        observed = node_json(script)
        self.assertEqual(observed["failures"], [])
        self.assertNotEqual(observed["blends"][0][0], observed["blends"][1][0])
        self.assertEqual(observed["blends"][0][0], observed["blends"][0][1])
        self.assertEqual(observed["blends"][1][0], observed["blends"][1][1])

    def test_panel_selection_follows_the_conducted_waltz_grid(self) -> None:
        # The panel score is not an arbitrary refresh timer.  It is the written
        # 3/4 pulse, 1-&-2-&-3-&: lower body on 1, pelvis on 2, torso on 3,
        # and the upper-string/light cohort answering the intervening eighths.
        phrase = next(row for row in self.score["phrases"] if row["id"] == "sylvia-09")
        first = score_at(self.score, float(phrase["start_second"]))
        beat_index = int(first["beat"]["index"])
        samples = []
        for beat_offset in range(3):
            beat = self.score["beats"][beat_index + beat_offset]
            following = self.score["beats"][beat_index + beat_offset + 1]
            for phase in (0.10, 0.60):
                at = float(beat["second"]) + (float(following["second"]) - float(beat["second"])) * phase
                samples.append(pose_at(self.score, self.choreography, SEED, at))
        self.assertEqual([row["panel_counterpoint"]["active_group"] for row in samples], [0, 3, 1, 3, 2, 3])
        self.assertTrue(all(row["transition"]["fragment_change_fraction"] == 0.25 for row in samples))

    def test_free_river_uses_the_same_score_led_panel_contract(self) -> None:
        script = NODE_SETUP + """
          const at = score.phrases.find((row) => row.id === 'sylvia-09').start_second + 0.12;
          const free = step(corpus, seed, at, null, {score, choreography});
          const posed = poseAt(score, choreography, seed, at);
          console.log(JSON.stringify({
            pose: free.pose,
            statePose: free.state.choreography,
            groups: [...new Set(free.cast.map((cell) => cell.counterpoint_group))].sort(),
            expected: posed,
          }));
        """
        observed = node_json(script)
        self.assertEqual(observed["pose"], observed["expected"])
        self.assertEqual(observed["statePose"], observed["expected"])
        self.assertEqual(observed["groups"], [0, 1, 2, 3])

    def test_live_conductor_overrides_are_deterministic_and_do_not_mutate_the_score(self) -> None:
        script = NODE_SETUP + """
          const at = score.phrases.find((row) => row.id === 'sylvia-09').start_second + 0.4;
          const conductor = {model: 'common', meter: '4/4', tempo: 100};
          const native = poseAt(score, choreography, seed, at);
          const first = poseAt(score, choreography, seed, at, null, conductor);
          const second = poseAt(score, choreography, seed, at, null, conductor);
          console.log(JSON.stringify({native, first, second, scoreIdentity: score.identity.contract_sha256}));
        """
        observed = node_json(script)
        self.assertEqual(observed["first"], observed["second"])
        self.assertEqual(observed["first"]["panel_counterpoint"]["conductor"], {
            "model": "common", "meter": "4/4", "tempo": 100,
            "effective_model": "common", "numerator": 4,
        })
        self.assertNotEqual(observed["native"]["panel_counterpoint"], observed["first"]["panel_counterpoint"])
        self.assertEqual(observed["scoreIdentity"], self.score["identity"]["contract_sha256"])

    def test_exact_assembly_four_second_black_and_all_boundaries_are_continuous(self) -> None:
        critical = sorted(
            {
                *(float(row["second"]) for row in self.score["cues"]),
                *(float(row["start_second"]) for row in self.score["phrases"][1:]),
            }
        )
        script = NODE_SETUP + f"""
          const first = step(corpus, seed, 25, program, {{ score, choreography }});
          const second = step(corpus, seed, 32, program, {{ score, choreography }});
          const critical = {json.dumps(critical)};
          const epsilon = 1e-6;
          const channels = ['divergence', 'spread', 'azimuth', 'elevation', 'projK'];
          let maxDelta = 0;
          const deltas = [];
          for (const edge of critical) {{
            const left = step(corpus, seed, Math.max(0, edge - epsilon), program, {{score, choreography}}).state;
            const right = step(corpus, seed, Math.min(score.time.duration_seconds - epsilon, edge + epsilon), program, {{score, choreography}}).state;
            const delta = Math.max(...channels.map((name) => Math.abs(left[name] - right[name])));
            maxDelta = Math.max(maxDelta, delta);
            deltas.push([edge, delta]);
          }}
          const offsetFree = [];
          for (const cue of score.cues.filter((row) => Object.keys(row.visual.channel_offsets).length)) {{
            const plain = JSON.parse(JSON.stringify(score));
            for (const row of plain.cues) row.visual.channel_offsets = {{}};
            const pose = poseAt(score, choreography, seed, cue.second);
            const actual = step(corpus, seed, cue.second, program, {{score, choreography}}).state;
            const without = step(corpus, seed, cue.second, program, {{score: plain, choreography}}).state;
            offsetFree.push([cue.id, channels.every((name) => actual[name] === without[name]), pose.transition.kind]);
          }}
          const signatureStart = score.phrases.at(-1).start_second;
          const before = step(corpus, seed, signatureStart - epsilon, program, {{score, choreography}});
          const signature = [signatureStart, signatureStart + 2, score.time.duration_seconds - epsilon]
            .map((at) => step(corpus, seed, at, program, {{score, choreography}}));
          const visibleBefore = before.cast.reduce((sum, cell) => sum + (cell.opacity ?? 1) * before.state.sceneOpacity, 0);
          console.log(JSON.stringify({{
            assembly: {{
              count: first.cast.length,
              allSolved: first.cast.every((cell) => cell.solved),
              stable: JSON.stringify(first.cast) === JSON.stringify(second.cast),
              scoreCount: corpus.score.tiles.length,
            }},
            maxDelta,
            deltas,
            offsetFree,
            visibleBefore,
            signature: signature.map((row) => [row.pose.movement_id, row.pose.cut_mode, row.cast.length]),
          }}));
        """
        observed = node_json(script)
        self.assertEqual(
            observed["assembly"],
            {"count": 256, "allSolved": True, "stable": True, "scoreCount": 256},
        )
        self.assertLess(observed["maxDelta"], 0.0001, observed["deltas"])
        self.assertTrue(all(row[1] for row in observed["offsetFree"]), observed["offsetFree"])
        self.assertLess(observed["visibleBefore"], 0.0001)
        self.assertEqual(observed["signature"], [["SIGNATURE", "black", 0]] * 3)

    def test_native_score_rejects_affine_delivery_while_fixture_mode_and_passage_restart_survive(self) -> None:
        with self.assertRaisesRegex(ValueError, "native-tempo score window must be exactly"):
            score_at(self.score, 10, {"t0": 0, "seconds": 100})

        fixture = copy.deepcopy(self.score)
        fixture["release_status"] = "fixture-only"
        fixture["time"]["passage_mapping"] = "restart-and-affine-stretch"
        fixture["identity"]["contract_sha256"] = score_sha256(fixture)
        validate_score(fixture)
        affine = score_at(fixture, 50, {"t0": 0, "seconds": 100})
        self.assertNotEqual(affine["scale"], 1)

        affine_choreography = copy.deepcopy(self.choreography)
        affine_choreography["identity"]["score_contract_sha256"] = fixture["identity"]["contract_sha256"]
        self.reidentify(affine_choreography)
        with self.assertRaisesRegex(ValueError, "requires a native-tempo score"):
            validate_choreography(affine_choreography, score=fixture)

        script = NODE_SETUP + """
          const duration = score.time.duration_seconds;
          let rejected = false;
          try { poseAt(score, choreography, seed, 10, {t0: 0, seconds: 100}); }
          catch (error) { rejected = /native-tempo score window/.test(error.message); }
          const first = step(corpus, seed, 10, program, {score, choreography});
          const repeated = step(corpus, seed, duration + 10, program, {score, choreography});
          console.log(JSON.stringify({
            rejected,
            first: [first.state.passage, first.pose.phrase.id, first.pose.blend, first.cast[0].layers[0].frame],
            repeated: [repeated.state.passage, repeated.pose.phrase.id, repeated.pose.blend, repeated.cast[0].layers[0].frame],
          }));
        """
        observed = node_json(script)
        self.assertTrue(observed["rejected"])
        self.assertEqual(observed["first"][0], 0)
        self.assertEqual(observed["repeated"][0], 1)
        self.assertEqual(observed["first"][1:], observed["repeated"][1:])

    def test_control_and_renderer_receipts_bind_score_choreography_and_exact_span(self) -> None:
        missing = run(
            "node",
            "sound/control.mjs",
            "--score",
            "music/score.json",
            "--rate",
            "0",
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("production --score requires --choreography", missing.stderr)

        result = run(
            "node",
            "sound/control.mjs",
            "--score",
            "music/score.json",
            "--choreography",
            "render/choreography.json",
            "--seed",
            str(SEED),
            "--rate",
            "0",
            timeout=240,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["duration"], self.duration)
        self.assertEqual(payload["origin"], "IMG_1702")
        self.assertEqual(payload["music"]["score_file_sha256"], sha256(SCORE_PATH))
        self.assertEqual(payload["choreography"]["file_sha256"], sha256(CHOREOGRAPHY_PATH))
        self.assertEqual(payload["choreography"]["identity"], self.choreography["identity"])
        self.assertEqual(payload["room"]["buses"][0]["identity"]["passage"]["seconds"], self.duration)

        renderer = load_module("danse_render_choreography_test", ROOT / "render/render.py")
        args = SimpleNamespace(
            score="music/score.json",
            choreography="render/choreography.json",
            tier="screen",
            window="passage",
            start=0,
            seed=SEED,
            stream=0,
            codec="prores",
            width=3840,
            height=2160,
            fps=30,
            segment_frames=600,
        )
        receipt = renderer.segment_identity(args, 0, 600)
        self.assertEqual(receipt["inputs"]["music_score"]["file_sha256"], sha256(SCORE_PATH))
        self.assertEqual(receipt["inputs"]["choreography"], {
            "path": "render/choreography.json",
            "file_sha256": sha256(CHOREOGRAPHY_PATH),
            "contract_sha256": self.choreography["identity"]["contract_sha256"],
        })
        url = renderer.film_url("http://127.0.0.1:8000", args)
        self.assertIn("score=music%2Fscore.json", url)
        self.assertIn("choreography=render%2Fchoreography.json", url)

    def test_timing_only_control_keeps_one_selected_passage_without_score_motion(self) -> None:
        observed = node_json(
            NODE_SETUP
            + """
          const timing = fixedPassageTiming(score.time.duration_seconds);
          const naturalEdge = 312.54005199847745;
          const moments = [naturalEdge + 1 / 30, score.time.duration_seconds - 1 / 30];
          const states = moments.map((at) => step(corpus, seed, at, program, {timing}).state);
          const boundary = step(corpus, seed, score.time.duration_seconds, program, {timing}).state;
          let mixedRejected = false;
          try { step(corpus, seed, 1, program, {score, timing}); }
          catch (error) { mixedRejected = /mutually exclusive/.test(error.message); }
          let poseRejected = false;
          try { state(seed, 1, program, 0, null, {}, timing); }
          catch (error) { poseRejected = /cannot admit choreography poses/.test(error.message); }
          let falsyTimingRejected = false;
          try { state(seed, 1, program, 0, null, null, ''); }
          catch (error) { falsyTimingRejected = /invalid fixed passage timing/.test(error.message); }
          let falsyScoreRejected = false;
          try { state(seed, 1, program, 0, ''); }
          catch (error) { falsyScoreRejected = /invalid music score contract/.test(error.message); }
          const tiny = fixedPassageTiming(5e-7);
          const tinySpan = captureSpan(
            program, seed, 0, {seconds: 0, passages: 1}, 0, null, tiny,
          );
          const tinyBoundary = step(corpus, seed, tinySpan.t1, program, {timing: tiny}).state;
          console.log(JSON.stringify({
            mixedRejected,
            poseRejected,
            falsyTimingRejected,
            falsyScoreRejected,
            tinySpan,
            tinyBoundary: {passage: tinyBoundary.passage},
            boundary: {passage: boundary.passage, passageSeed: boundary.passageSeed},
            states: states.map((state) => ({
              passage: state.passage,
              passageSeed: state.passageSeed,
              passageSeconds: state.passageSeconds,
              movement: state.movement,
              hasMusic: Object.hasOwn(state, 'music'),
              hasChoreography: Object.hasOwn(state, 'choreography'),
            })),
          }));
        """
        )
        self.assertTrue(observed["mixedRejected"])
        self.assertTrue(observed["poseRejected"])
        self.assertTrue(observed["falsyTimingRejected"])
        self.assertTrue(observed["falsyScoreRejected"])
        self.assertEqual(observed["tinySpan"], {"t0": 0, "t1": 5e-7})
        self.assertEqual(observed["tinyBoundary"]["passage"], 1)
        self.assertEqual(observed["boundary"]["passage"], 1)
        for state in observed["states"]:
            self.assertEqual(state["passage"], 0)
            self.assertEqual(state["passageSeed"], 2943173797)
            self.assertEqual(state["passageSeconds"], self.duration)
            self.assertFalse(state["hasMusic"])
            self.assertFalse(state["hasChoreography"])
        self.assertEqual(observed["states"][-1]["movement"], "SIGNATURE")


if __name__ == "__main__":
    unittest.main()
