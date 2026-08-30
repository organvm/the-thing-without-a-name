#!/usr/bin/env python3
"""Portable cross-language regressions for the deterministic room-event bus."""

from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_TMP_ROOT = ROOT / ".work" / "test-fixtures"
sys.path.insert(0, str(ROOT / "music"))
sys.path.insert(0, str(ROOT / "sound"))

from compile_score import compile_contract, output_bytes  # noqa: E402
from music_score import canonical_sha256  # noqa: E402
from room_events import (  # noqa: E402
    calibration_bus,
    layout_contract_sha256,
    load_room_layouts,
    plan_room_render,
    room_contract_sha256,
    room_events_between,
    validate_room_bus,
    validate_room_layouts,
)
from room_render import plan_control, validate_control_room  # noqa: E402
from score import room_event_plan, validate_room_event_control  # noqa: E402
from validate_repertoire import load_register  # noqa: E402

PASSAGE = {
    "river_seed": 0x12345678,
    "stream": 7,
    "index": 0,
    "seed": 0x9ABCDEF0,
    "t0": 17.25,
    "seconds": 350.896343125,
}

COMPILE_SCRIPT = f"""
  import fs from 'node:fs';
  import {{ compileRoomBus, planRoomRender, validateRoomLayouts }} from './engine/room-events.js';
  const score = JSON.parse(fs.readFileSync(process.env.DANSE_ROOM_FIXTURE_SCORE));
  const layouts = validateRoomLayouts(JSON.parse(fs.readFileSync('sound/room-layout.json')));
  const passage = {json.dumps(PASSAGE)};
  const bus = compileRoomBus(score, passage);
  const plan = planRoomRender(bus, layouts, 'reference-quad', passage.t0, passage.t0 + passage.seconds);
  console.log(JSON.stringify({{bus, plan}}));
"""


def run(*command: str, input_text: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=ROOT,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def node_json(script: str) -> object:
    result = run("node", "--input-type=module", "--eval", script)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


class RoomEventContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Keep ephemeral buses under the ignored work root so filesystem sync
        # cannot resurrect deleted test artifacts inside tracked source trees.
        FIXTURE_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls._temporary = tempfile.TemporaryDirectory(dir=FIXTURE_TMP_ROOT)
        temporary = Path(cls._temporary.name)
        cls.fixture_score_path = temporary / "score.json"
        cls.production_bus_path = temporary / "production-room-bus.json"
        program = json.loads((ROOT / "render/program.json").read_text())
        fixture_register = load_register(ROOT / "music/fixtures/repertoire.yaml")
        fixture_score = compile_contract(fixture_register, program, "generated-contract-study")
        cls.fixture_score_path.write_bytes(output_bytes(fixture_score))
        cls._prior_fixture = os.environ.get("DANSE_ROOM_FIXTURE_SCORE")
        cls._prior_production_bus = os.environ.get("DANSE_PRODUCTION_ROOM_BUS")
        os.environ["DANSE_ROOM_FIXTURE_SCORE"] = str(cls.fixture_score_path)
        os.environ["DANSE_PRODUCTION_ROOM_BUS"] = str(cls.production_bus_path)
        compiled = node_json(COMPILE_SCRIPT)
        cls.bus = validate_room_bus(compiled["bus"])
        cls.node_plan = compiled["plan"]
        cls.layouts = load_room_layouts()
        production = run(
            "node",
            "--input-type=module",
            "--eval",
            f"""
              import fs from 'node:fs';
              import {{ compileRoomBus }} from './engine/room-events.js';
              const score = JSON.parse(fs.readFileSync('music/score.json'));
              const bus = compileRoomBus(score, {json.dumps(PASSAGE)});
              fs.writeFileSync(process.env.DANSE_PRODUCTION_ROOM_BUS, JSON.stringify(bus));
              console.log(JSON.stringify({{events: bus.events.length, contract: bus.identity.contract_sha256}}));
            """,
        )
        if production.returncode:
            raise AssertionError(production.stdout + production.stderr)
        cls.production_summary = json.loads(production.stdout)
        cls.production_bus = validate_room_bus(json.loads(cls.production_bus_path.read_text()))

    @classmethod
    def tearDownClass(cls) -> None:
        for name, prior in (
            ("DANSE_ROOM_FIXTURE_SCORE", cls._prior_fixture),
            ("DANSE_PRODUCTION_ROOM_BUS", cls._prior_production_bus),
        ):
            if prior is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prior
        cls._temporary.cleanup()

    def test_declared_schemas_and_cross_language_contract_identities_hold(self) -> None:
        layout_schema = json.loads((ROOT / "sound/room-layout.schema.json").read_text())
        event_schema = json.loads((ROOT / "sound/room-events.schema.json").read_text())
        self.assertEqual(layout_schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(layout_schema["properties"]["schema"]["const"], "danse.room.layouts.v1")
        self.assertFalse(layout_schema["additionalProperties"])
        self.assertEqual(event_schema["properties"]["schema"]["const"], "danse.room.events.v1")
        self.assertEqual(event_schema["properties"]["semantics"]["const"], "authored-start-events")
        self.assertFalse(event_schema["additionalProperties"])
        self.assertEqual(
            self.layouts["identity"]["contract_sha256"],
            layout_contract_sha256(self.layouts),
        )
        self.assertEqual(
            self.bus["identity"]["contract_sha256"],
            room_contract_sha256(self.bus),
        )
        canonical_zero = node_json(
            """
              import { canonicalSha256 } from './engine/score.js';
              console.log(JSON.stringify({negative: canonicalSha256(-0), positive: canonicalSha256(0)}));
            """
        )
        self.assertEqual(canonical_zero["negative"], canonical_zero["positive"])
        self.assertEqual(canonical_zero["positive"], canonical_sha256(0))
        self.assertEqual(canonical_sha256(-0.0), canonical_sha256(0))
        self.assertEqual(self.node_plan, plan_room_render(
            self.bus,
            self.layouts,
            "reference-quad",
            PASSAGE["t0"],
            PASSAGE["t0"] + PASSAGE["seconds"],
        ))
        # Full production scale is file-backed so 5,000+ events never enter a
        # command argument. JavaScript compiles/validates the bus and Python
        # verifies the same immutable digest and indexed interval values.
        self.assertEqual(self.production_summary["events"], len(self.production_bus["events"]))
        self.assertGreater(len(self.production_bus["events"]), 5000)
        self.assertEqual(self.production_bus["release_status"], "production-selected")
        self.assertEqual(
            self.production_bus["identity"]["contract_sha256"],
            room_contract_sha256(self.production_bus),
        )
        production_intervals = [
            [PASSAGE["t0"], PASSAGE["t0"] + 1],
            [PASSAGE["t0"] + 149, PASSAGE["t0"] + 150],
            [PASSAGE["t0"] + PASSAGE["seconds"] - 1, PASSAGE["t0"] + PASSAGE["seconds"]],
        ]
        observed = node_json(
            f"""
              import fs from 'node:fs';
              import {{ roomContractSha256, roomEventsBetween, validateRoomBus }} from './engine/room-events.js';
              import {{ canonicalSha256 }} from './engine/score.js';
              const bus = validateRoomBus(JSON.parse(fs.readFileSync(process.env.DANSE_PRODUCTION_ROOM_BUS)));
              const intervals = {json.dumps(production_intervals)};
              console.log(JSON.stringify({{
                contract: roomContractSha256(bus),
                query_digests: intervals.map(([start, end]) => canonicalSha256(roomEventsBetween(bus, start, end))),
              }}));
            """
        )
        self.assertEqual(observed["contract"], room_contract_sha256(self.production_bus))
        self.assertEqual(
            observed["query_digests"],
            [canonical_sha256(room_events_between(self.production_bus, start, end)) for start, end in production_intervals],
        )

    def test_production_selected_release_status_is_mirrored_and_unknown_status_fails_closed(self) -> None:
        production = copy.deepcopy(self.bus)
        production["release_status"] = "production-selected"
        production["identity"]["contract_sha256"] = room_contract_sha256(production)
        self.assertEqual(validate_room_bus(production)["release_status"], "production-selected")

        unknown = copy.deepcopy(production)
        unknown["release_status"] = "published-by-implication"
        unknown["identity"]["contract_sha256"] = room_contract_sha256(unknown)
        with self.assertRaisesRegex(ValueError, "release_status is invalid"):
            validate_room_bus(unknown)

        observed = node_json(
            f"""
              import {{ roomContractSha256, validateRoomBus }} from './engine/room-events.js';
              const production = {json.dumps(production)};
              const unknown = JSON.parse(JSON.stringify(production));
              unknown.release_status = 'published-by-implication';
              unknown.identity.contract_sha256 = roomContractSha256(unknown);
              let rejected = false;
              try {{ validateRoomBus(unknown); }} catch (error) {{ rejected = /release_status is invalid/.test(error.message); }}
              console.log(JSON.stringify({{accepted: validateRoomBus(production).release_status, rejected}}));
            """
        )
        self.assertEqual(observed, {"accepted": "production-selected", "rejected": True})

    def test_compilation_is_repeatable_typed_bounded_and_provenance_safe(self) -> None:
        repeated = node_json(COMPILE_SCRIPT)
        self.assertEqual(repeated["bus"], self.bus)
        self.assertEqual(len(self.bus["lookup"]["buckets"]), math.ceil(PASSAGE["seconds"]))
        self.assertEqual(len(self.bus["events"]), 38)
        self.assertEqual(len({event["id"] for event in self.bus["events"]}), len(self.bus["events"]))
        types = {event["type"] for event in self.bus["events"]}
        self.assertTrue({"passage.start", "movement.start", "plane.assembly", "score.cue", "plane.recast", "score.note"} <= types)
        self.assertTrue(all(-1 <= event["position"][axis] <= 1 for event in self.bus["events"] for axis in ("x", "y", "z")))
        self.assertTrue(all(0 <= event["depth"] <= 1 and 0 <= event["intensity"] <= 1 for event in self.bus["events"]))
        notes = [event for event in self.bus["events"] if event["type"] == "score.note"]
        self.assertTrue(notes)
        self.assertTrue(all(event["audio"]["pitch"] == event["source"]["pitch"] for event in notes))
        self.assertTrue(all(event["audio"]["pitch"] is None for event in self.bus["events"] if event["type"] != "score.note"))
        serialized = json.dumps(self.bus)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn(".work", serialized)
        self.assertEqual(self.bus["provenance"]["policy"], "declared-source-bytes-only")

    def test_js_and_python_interval_queries_and_routes_are_value_identical(self) -> None:
        intervals = [
            [PASSAGE["t0"], PASSAGE["t0"] + 1],
            [PASSAGE["t0"] + 95.25, PASSAGE["t0"] + 128.75],
            [PASSAGE["t0"] + PASSAGE["seconds"] - 5, PASSAGE["t0"] + PASSAGE["seconds"]],
            [PASSAGE["t0"] + 30, PASSAGE["t0"] + 30],
        ]
        script = f"""
          import fs from 'node:fs';
          import {{ compileRoomBus, planRoomRender, roomEventsBetween, validateRoomLayouts }} from './engine/room-events.js';
          const score = JSON.parse(fs.readFileSync(process.env.DANSE_ROOM_FIXTURE_SCORE));
          const layouts = validateRoomLayouts(JSON.parse(fs.readFileSync('sound/room-layout.json')));
          const passage = {json.dumps(PASSAGE)};
          const bus = compileRoomBus(score, passage);
          const intervals = {json.dumps(intervals)};
          console.log(JSON.stringify(intervals.map(([start, end]) => ({{
            events: roomEventsBetween(bus, start, end),
            plan: planRoomRender(bus, layouts, 'reference-quad', start, end),
          }}))));
        """
        observed = node_json(script)
        expected = [
            {
                "events": room_events_between(self.bus, start, end),
                "plan": plan_room_render(self.bus, self.layouts, "reference-quad", start, end),
            }
            for start, end in intervals
        ]
        self.assertEqual(observed, expected)

    def test_seek_segment_concat_restart_and_start_only_semantics_hold(self) -> None:
        start = PASSAGE["t0"]
        end = start + PASSAGE["seconds"]
        full = room_events_between(self.bus, start, end)
        edges = [start, start + 61.75, start + 128.0, start + 230.2, end]
        concatenated = [
            event
            for left, right in zip(edges, edges[1:])
            for event in room_events_between(self.bus, left, right)
        ]
        self.assertEqual(concatenated, full)
        seek = start + 128.125
        self.assertEqual(room_events_between(self.bus, seek, end), [event for event in full if event["at"] >= seek])

        note = next(event for event in full if event["type"] == "score.note" and event["end"] > event["at"] + 0.01)
        overlap_only = room_events_between(self.bus, note["at"] + 0.001, min(note["end"], note["at"] + 0.01))
        self.assertNotIn(note["id"], {event["id"] for event in overlap_only})

        shifted = {**PASSAGE, "t0": 900.0}
        script = f"""
          import fs from 'node:fs';
          import {{ compileRoomBus }} from './engine/room-events.js';
          const score = JSON.parse(fs.readFileSync(process.env.DANSE_ROOM_FIXTURE_SCORE));
          const first = compileRoomBus(score, {json.dumps(PASSAGE)});
          const repeated = compileRoomBus(score, {json.dumps(PASSAGE)});
          const shifted = compileRoomBus(score, {json.dumps(shifted)});
          console.log(JSON.stringify({{first, repeated, shifted}}));
        """
        buses = node_json(script)
        self.assertEqual(buses["first"], buses["repeated"])
        compact = lambda bus: [
            (event["type"], event["source_second"], event["position"], event["intensity"])
            for event in bus["events"]
        ]
        self.assertEqual(compact(buses["first"]), compact(buses["shifted"]))

    def test_queries_use_bucket_indices_without_scanning_complete_event_arrays(self) -> None:
        class IndexedOnly(list):
            def __iter__(self):
                raise AssertionError("room event query scanned the complete event array")

        guarded = copy.deepcopy(self.bus)
        guarded["events"] = IndexedOnly(guarded["events"])
        target = next(event for event in self.bus["events"] if event["type"] == "score.cue")
        query_start = target["at"]
        query_end = target["at"] + 0.001
        got = room_events_between(guarded, query_start, query_end)
        self.assertTrue(got)

        script = f"""
          import fs from 'node:fs';
          import {{ compileRoomBus, roomEventsBetween }} from './engine/room-events.js';
          const score = JSON.parse(fs.readFileSync(process.env.DANSE_ROOM_FIXTURE_SCORE));
          const bus = compileRoomBus(score, {json.dumps(PASSAGE)});
          let reads = 0;
          bus.events = new Proxy(bus.events, {{
            get(target, property, receiver) {{
              if (property === Symbol.iterator) throw new Error('room event query scanned the complete event array');
              if (typeof property === 'string' && /^\\d+$/.test(property)) reads += 1;
              return Reflect.get(target, property, receiver);
            }},
          }});
          const events = roomEventsBetween(bus, {target['at']}, {target['at'] + 0.001});
          console.log(JSON.stringify({{ids: events.map((event) => event.id), reads}}));
        """
        observed = node_json(script)
        self.assertEqual(observed["ids"], [event["id"] for event in got])
        self.assertEqual(observed["reads"], len(got))

    def test_speaker_map_fold_down_gain_latency_and_malformed_maps_fail_closed(self) -> None:
        plan = plan_room_render(
            self.bus,
            self.layouts,
            "reference-quad",
            PASSAGE["t0"],
            PASSAGE["t0"] + PASSAGE["seconds"],
        )
        layout = next(row for row in self.layouts["layouts"] if row["id"] == "reference-quad")
        for event in plan["events"]:
            self.assertTrue(all(0 <= tap["delay_ms"] <= self.layouts["safety"]["latency_budget_ms"] for tap in event["multichannel"]))
            self.assertLessEqual(math_hypot(tap["gain"] for tap in event["multichannel"]), self.layouts["safety"]["max_event_gain"] + 1e-9)
            expected = []
            for tap in event["multichannel"]:
                speaker_index = next(
                    index for index, speaker in enumerate(layout["speakers"]) if speaker["id"] == tap["speaker"]
                )
                for channel, coefficient in enumerate(layout["stereo_fold_down"]["matrix"][speaker_index]):
                    if coefficient:
                        expected.append((channel, tap["speaker"], round_js(tap["gain"] * coefficient, 12), tap["delay_ms"]))
            actual = [(tap["channel"], tap["source_speaker"], tap["gain"], tap["delay_ms"]) for tap in event["stereo"]]
            self.assertEqual(actual, expected)

        stale = copy.deepcopy(self.layouts)
        stale["safety"]["max_event_gain"] = 0.5
        with self.assertRaisesRegex(ValueError, "contract_sha256 is stale"):
            validate_room_layouts(stale)

        duplicate = copy.deepcopy(self.layouts)
        duplicate["layouts"][1]["speakers"][1]["channel"] = 0
        duplicate["identity"]["contract_sha256"] = layout_contract_sha256(duplicate)
        with self.assertRaisesRegex(ValueError, "channels must be unique"):
            validate_room_layouts(duplicate)

        empty_speaker = copy.deepcopy(self.layouts)
        empty_speaker["layouts"][1]["speakers"][0]["id"] = ""
        empty_speaker["identity"]["contract_sha256"] = layout_contract_sha256(empty_speaker)
        with self.assertRaisesRegex(ValueError, "speaker ids are invalid"):
            validate_room_layouts(empty_speaker)

        bad_axes = copy.deepcopy(self.layouts)
        bad_axes["coordinate_system"]["axes"]["z"] = "near-to-far"
        bad_axes["identity"]["contract_sha256"] = layout_contract_sha256(bad_axes)
        with self.assertRaisesRegex(ValueError, "coordinate system is invalid"):
            validate_room_layouts(bad_axes)

        amplified_fold = copy.deepcopy(self.layouts)
        amplified_fold["layouts"][1]["stereo_fold_down"]["matrix"][0] = [1, 1]
        amplified_fold["identity"]["contract_sha256"] = layout_contract_sha256(amplified_fold)
        with self.assertRaisesRegex(ValueError, "stereo fold-down is invalid"):
            validate_room_layouts(amplified_fold)

        unordered = copy.deepcopy(self.layouts)
        stereo_layout = next(row for row in unordered["layouts"] if row["id"] == "stereo")
        stereo_layout["speakers"].reverse()
        stereo_layout["stereo_fold_down"]["matrix"].reverse()
        unordered["identity"]["contract_sha256"] = layout_contract_sha256(unordered)
        validate_room_layouts(unordered)
        unordered_python = plan_room_render(self.bus, unordered, "stereo", PASSAGE["t0"], PASSAGE["t0"] + 1)
        unordered_script = f"""
          import {{ planRoomRender, validateRoomLayouts }} from './engine/room-events.js';
          const registry = validateRoomLayouts({json.dumps(unordered)});
          console.log(JSON.stringify(planRoomRender({json.dumps(self.bus)}, registry, 'stereo', {PASSAGE['t0']}, {PASSAGE['t0'] + 1})));
        """
        self.assertEqual(node_json(unordered_script), unordered_python)
        for event in unordered_python["events"]:
            self.assertTrue(all(tap["output"] == tap["source_speaker"] for tap in event["stereo"]))

        anchored = copy.deepcopy(self.layouts)
        anchored["coordinate_system"]["listener"] = [0.1, -0.05, 0.1]
        anchored["identity"]["contract_sha256"] = layout_contract_sha256(anchored)
        validate_room_layouts(anchored)
        anchored_python = plan_room_render(self.bus, anchored, "reference-quad", PASSAGE["t0"], PASSAGE["t0"] + 1)
        anchored_script = f"""
          import {{ planRoomRender, validateRoomLayouts }} from './engine/room-events.js';
          const registry = validateRoomLayouts({json.dumps(anchored)});
          console.log(JSON.stringify(planRoomRender({json.dumps(self.bus)}, registry, 'reference-quad', {PASSAGE['t0']}, {PASSAGE['t0'] + 1})));
        """
        self.assertEqual(node_json(anchored_script), anchored_python)
        self.assertNotEqual(anchored_python["events"], plan["events"][: len(anchored_python["events"])])

        excessive = copy.deepcopy(self.layouts)
        excessive["coordinate_system"]["meters_per_unit"] = 100
        excessive["identity"]["contract_sha256"] = layout_contract_sha256(excessive)
        validate_room_layouts(excessive)
        with self.assertRaisesRegex(ValueError, "exceeds latency budget"):
            plan_room_render(self.bus, excessive, "reference-quad", PASSAGE["t0"], PASSAGE["t0"] + 1)

    def test_calibration_is_diagnostic_direct_and_cross_language_identical(self) -> None:
        python_bus = calibration_bus(self.layouts, "reference-quad")
        script = """
          import fs from 'node:fs';
          import { calibrationBus, planRoomRender, validateRoomLayouts } from './engine/room-events.js';
          const layouts = validateRoomLayouts(JSON.parse(fs.readFileSync('sound/room-layout.json')));
          const bus = calibrationBus(layouts, 'reference-quad');
          console.log(JSON.stringify({bus, plan: planRoomRender(bus, layouts, 'reference-quad', 0, 1)}));
        """
        observed = node_json(script)
        self.assertEqual(observed["bus"], python_bus)
        self.assertEqual(observed["plan"], plan_room_render(python_bus, self.layouts, "reference-quad", 0, 1))
        self.assertEqual(python_bus["release_status"], "diagnostic-only")
        self.assertEqual(len(python_bus["events"]), 4)
        for event in observed["plan"]["events"]:
            self.assertEqual(len(event["multichannel"]), 1)
            target = python_bus["events"][event["event_index"]]["target_speaker"]
            self.assertEqual(event["multichannel"][0]["speaker"], target)
            self.assertEqual(event["multichannel"][0]["delay_ms"], 0)

        precise = copy.deepcopy(self.layouts)
        precise_layout = next(row for row in precise["layouts"] if row["id"] == "reference-quad")
        precise_layout["speakers"][0]["position"] = [-0.0000004, 0.21234567, 0.81234567]
        precise["identity"]["contract_sha256"] = layout_contract_sha256(precise)
        validate_room_layouts(precise)
        precise_python = calibration_bus(precise, "reference-quad")
        precise_script = f"""
          import {{ calibrationBus, validateRoomLayouts }} from './engine/room-events.js';
          const registry = validateRoomLayouts({json.dumps(precise)});
          console.log(JSON.stringify(calibrationBus(registry, 'reference-quad')));
        """
        precise_node = node_json(precise_script)
        validate_room_bus(precise_node)
        self.assertEqual(precise_node, precise_python)
        self.assertEqual(precise_node["events"][0]["position"], {"x": 0, "y": 0.212346, "z": 0.812346})

    def test_webaudio_disabled_and_uncleared_paths_do_not_touch_audio_nodes(self) -> None:
        script = f"""
          import fs from 'node:fs';
          import {{ compileRoomBus, planRoomRender, roomContractSha256, validateRoomLayouts }} from './engine/room-events.js';
          import {{ scheduleRoomWebAudio }} from './sound/web_audio.mjs';
          const score = JSON.parse(fs.readFileSync(process.env.DANSE_ROOM_FIXTURE_SCORE));
          const layouts = validateRoomLayouts(JSON.parse(fs.readFileSync('sound/room-layout.json')));
          const bus = compileRoomBus(score, {json.dumps(PASSAGE)});
          const start = bus.time.t0;
          const end = bus.time.t1;
          const forbidden = new Proxy({{}}, {{ get() {{ throw new Error('audio-disabled path touched context'); }} }});
          const disabled = scheduleRoomWebAudio(forbidden, bus, layouts, 'reference-quad', {{}}, start, end, {{enabled:false}});
          const direct = planRoomRender(bus, layouts, 'reference-quad', start, end);

          const staleBus = JSON.parse(JSON.stringify(bus));
          staleBus.events[0].intensity = 0.123;
          let staleBusRejected = false;
          try {{ scheduleRoomWebAudio(forbidden, staleBus, layouts, 'reference-quad', {{}}, start, end, {{enabled:false}}); }}
          catch (error) {{ staleBusRejected = /contract_sha256 is stale/.test(error.message); }}
          const staleLayouts = JSON.parse(JSON.stringify(layouts));
          staleLayouts.safety.max_event_gain = 0.5;
          let staleLayoutsRejected = false;
          try {{ scheduleRoomWebAudio(forbidden, bus, staleLayouts, 'reference-quad', {{}}, start, end, {{enabled:false}}); }}
          catch (error) {{ staleLayoutsRejected = /contract_sha256 is stale/.test(error.message); }}

          let nodeCalls = 0;
          let disconnectCalls = 0;
          const destination = {{kind:'destination'}};
          let directDestination = false;
          let limiterNode = null;
          let sourceNode = null;
          const node = (kind) => ({{
            kind,
            connect(target) {{
              nodeCalls += 1;
              if (kind === 'merger' && target === destination) directDestination = true;
            }},
            disconnect() {{ disconnectCalls += 1; }},
          }});
          const context = {{
            currentTime: 0,
            destination,
            createChannelMerger() {{ nodeCalls += 1; return node('merger'); }},
            createWaveShaper() {{
              nodeCalls += 1;
              limiterNode = {{...node('limiter'), curve:null, oversample:'none'}};
              return limiterNode;
            }},
            createBufferSource() {{
              nodeCalls += 1;
              sourceNode = {{...node('source'), buffer:null, playbackRate:{{value:1}}, start() {{}}, stop() {{}}}};
              return sourceNode;
            }},
            createDelay() {{ nodeCalls += 1; return {{...node('delay'), delayTime:{{value:0}}}}; }},
            createGain() {{ nodeCalls += 1; return {{...node('gain'), gain:{{value:0}}}}; }},
          }};
          const blocked = scheduleRoomWebAudio(context, bus, layouts, 'reference-quad', {{}}, start, end);
          const callsAfterBlocked = nodeCalls;

          const admittedBus = JSON.parse(JSON.stringify(bus));
          const admittedEvent = admittedBus.events.find((event) => event.type === 'score.note' && event.audio.pitch > 60);
          const digest = 'a'.repeat(64);
          admittedEvent.audio.source_sha256 = digest;
          admittedBus.identity.contract_sha256 = roomContractSha256(admittedBus);
          const admitted = scheduleRoomWebAudio(
            context,
            admittedBus,
            layouts,
            'reference-quad',
            {{[admittedEvent.audio.role]: {{buffer: {{duration: 1}}, audio_source_sha256: digest}}}},
            admittedEvent.at,
            admittedEvent.at + 0.0001,
          );
          const admittedStop = admitted.stop(0);
          const admittedSecondStop = admitted.stop(0);
          console.log(JSON.stringify({{
            disabledPlanMatches: JSON.stringify(disabled.plan) === JSON.stringify(direct),
            disabledEvents: disabled.disabled.length,
            staleBusRejected,
            staleLayoutsRejected,
            blocked: blocked.blocked.length,
            silent: blocked.silent.length,
            callsAfterBlocked,
            scheduled: admitted.scheduled.length,
            nodeCalls,
            playbackRate: sourceNode.playbackRate.value,
            expectedPlaybackRate: 2 ** ((admittedEvent.audio.pitch - 60) / 12),
            limiterMax: Math.max(...limiterNode.curve),
            limiterCeiling: 10 ** (layouts.safety.limiter_ceiling_dbfs / 20),
            limiterOversample: limiterNode.oversample,
            directDestination,
            limiterReceipt: admitted.limiter,
            disabledStop: disabled.stop(),
            blockedStop: blocked.stop(),
            admittedStop,
            admittedSecondStop,
            admittedDisposed: admitted.disposed,
            disconnectCalls,
          }}));
        """
        observed = node_json(script)
        self.assertTrue(observed["disabledPlanMatches"])
        self.assertEqual(observed["disabledEvents"], len(self.bus["events"]))
        self.assertTrue(observed["staleBusRejected"])
        self.assertTrue(observed["staleLayoutsRejected"])
        self.assertGreater(observed["blocked"], 0)
        self.assertGreater(observed["silent"], 0)
        self.assertEqual(observed["callsAfterBlocked"], 0)
        self.assertEqual(observed["scheduled"], 1)
        self.assertGreater(observed["nodeCalls"], 0)
        self.assertAlmostEqual(observed["playbackRate"], observed["expectedPlaybackRate"], places=12)
        self.assertLessEqual(observed["limiterMax"], observed["limiterCeiling"] + 1e-7)
        self.assertEqual(observed["limiterOversample"], "4x")
        self.assertFalse(observed["directDestination"])
        self.assertEqual(observed["limiterReceipt"]["ceiling_dbfs"], self.layouts["safety"]["limiter_ceiling_dbfs"])
        self.assertFalse(observed["disabledStop"])
        self.assertFalse(observed["blockedStop"])
        self.assertTrue(observed["admittedStop"])
        self.assertFalse(observed["admittedSecondStop"])
        self.assertTrue(observed["admittedDisposed"])
        self.assertGreater(observed["disconnectCalls"], 0)

    def test_room_layout_loading_is_bounded_outside_the_pure_engine(self) -> None:
        script = f"""
          import {{ loadRoomLayouts }} from './sound/web_audio.mjs';
          const registry = {json.dumps(self.layouts)};
          const loaded = await loadRoomLayouts('memory:room-layouts', {{
            timeoutMs: 50,
            fetchImpl: async () => ({{ok: true, status: 200, json: async () => registry}}),
          }});
          let timedOut = false;
          try {{
            await loadRoomLayouts('memory:never', {{
              timeoutMs: 5,
              fetchImpl: () => new Promise(() => {{}}),
            }});
          }} catch (error) {{
            timedOut = /did not load within 5ms/.test(error.message);
          }}
          let unboundedRejected = false;
          try {{ await loadRoomLayouts('memory:invalid', {{timeoutMs: 30001}}); }}
          catch (error) {{ unboundedRejected = /timeout must be/.test(error.message); }}
          console.log(JSON.stringify({{
            identity: loaded.identity.contract_sha256,
            timedOut,
            unboundedRejected,
          }}));
        """
        observed = node_json(script)
        self.assertEqual(observed["identity"], self.layouts["identity"]["contract_sha256"])
        self.assertTrue(observed["timedOut"])
        self.assertTrue(observed["unboundedRejected"])
        engine_source = (ROOT / "engine/room-events.js").read_text()
        self.assertNotIn("setTimeout", engine_source)
        self.assertNotIn("fetch(", engine_source)

    def test_webaudio_teardown_waits_for_delayed_tail_and_future_stop(self) -> None:
        script = f"""
          import fs from 'node:fs';
          import {{ compileRoomBus, roomContractSha256, validateRoomLayouts }} from './engine/room-events.js';
          import {{ scheduleRoomWebAudio }} from './sound/web_audio.mjs';

          const score = JSON.parse(fs.readFileSync(process.env.DANSE_ROOM_FIXTURE_SCORE));
          const layouts = validateRoomLayouts(JSON.parse(fs.readFileSync('sound/room-layout.json')));
          const bus = compileRoomBus(score, {json.dumps(PASSAGE)});
          const event = bus.events.find((candidate) => candidate.type === 'score.note');
          const digest = 'd'.repeat(64);
          event.audio.source_sha256 = digest;
          bus.identity.contract_sha256 = roomContractSha256(bus);
          const buffers = {{[event.audio.role]: {{buffer: {{duration: 1}}, audio_source_sha256: digest}}}};

          const timers = [];
          globalThis.setTimeout = (callback, delay) => {{
            const timer = {{callback, delay, cleared: false}};
            timers.push(timer);
            return timer;
          }};
          globalThis.clearTimeout = (timer) => {{ if (timer) timer.cleared = true; }};
          function nextTimer() {{
            while (timers.length) {{
              const timer = timers.shift();
              if (!timer.cleared) return timer;
            }}
            throw new Error('expected a pending teardown timer');
          }}

          function harness(currentTime) {{
            const state = {{sources: [], disconnected: []}};
            const destination = {{kind: 'destination'}};
            const node = (kind) => ({{
              kind,
              connect() {{}},
              disconnect() {{ state.disconnected.push(kind); }},
            }});
            const context = {{
              currentTime,
              destination,
              createChannelMerger() {{ return node('merger'); }},
              createWaveShaper() {{ return {{...node('limiter'), curve: null, oversample: 'none'}}; }},
              createBufferSource() {{
                const source = {{
                  ...node('source'),
                  buffer: null,
                  playbackRate: {{value: 1}},
                  stops: [],
                  start() {{}},
                  stop(at) {{ this.stops.push(at); }},
                }};
                state.sources.push(source);
                return source;
              }},
              createDelay() {{ return {{...node('delay'), delayTime: {{value: 0}}}}; }},
              createGain() {{ return {{...node('gain'), gain: {{value: 0}}}}; }},
            }};
            return {{context, state}};
          }}

          const tailHarness = harness(10);
          const tail = scheduleRoomWebAudio(
            tailHarness.context,
            bus,
            layouts,
            'reference-quad',
            buffers,
            event.at,
            event.at + 0.0001,
          );
          const plannedEvent = tail.plan.events.find((candidate) => candidate.id === event.id);
          const maximumDelayMs = Math.max(...plannedEvent.stereo.map((tap) => tap.delay_ms));
          tailHarness.state.sources[0].onended();
          const tailTimer = nextTimer();
          const tailPending = !tail.disposed && tailHarness.state.disconnected.length === 0;
          tailTimer.callback();
          const suspendedPending = !tail.disposed && tailHarness.state.disconnected.length === 0;
          const resumedTailTimer = nextTimer();
          tailHarness.context.currentTime += maximumDelayMs / 1000 + 0.001;
          resumedTailTimer.callback();

          const futureHarness = harness(20);
          const future = scheduleRoomWebAudio(
            futureHarness.context,
            bus,
            layouts,
            'reference-quad',
            buffers,
            event.at,
            event.at + 0.0001,
          );
          const stopAt = futureHarness.context.currentTime + 0.01;
          const futureStop = future.stop(stopAt);
          const futureSecondStop = future.stop(stopAt);
          const futureTimer = nextTimer();
          const futurePending = !future.disposed && futureHarness.state.disconnected.length === 0;
          futureHarness.context.currentTime = stopAt + maximumDelayMs / 1000 + 0.001;
          futureTimer.callback();

          console.log(JSON.stringify({{
            maximumDelayMs,
            tailTimerDelay: tailTimer.delay,
            tailPending,
            suspendedPending,
            tailDisposed: tail.disposed,
            tailDisconnected: tailHarness.state.disconnected,
            futureStop,
            futureSecondStop,
            futurePending,
            futureTimerDelay: futureTimer.delay,
            futureSourceStop: futureHarness.state.sources[0].stops.at(-1),
            stopAt,
            futureDisposed: future.disposed,
            futureDisconnected: futureHarness.state.disconnected,
          }}));
        """
        observed = node_json(script)
        self.assertGreater(observed["maximumDelayMs"], 0)
        self.assertGreaterEqual(observed["tailTimerDelay"], observed["maximumDelayMs"])
        self.assertTrue(observed["tailPending"])
        self.assertTrue(observed["suspendedPending"])
        self.assertTrue(observed["tailDisposed"])
        self.assertIn("delay", observed["tailDisconnected"])
        self.assertIn("limiter", observed["tailDisconnected"])
        self.assertTrue(observed["futureStop"])
        self.assertFalse(observed["futureSecondStop"])
        self.assertTrue(observed["futurePending"])
        self.assertGreaterEqual(observed["futureTimerDelay"], 10 + observed["maximumDelayMs"])
        self.assertAlmostEqual(observed["futureSourceStop"], observed["stopAt"], places=12)
        self.assertTrue(observed["futureDisposed"])
        self.assertIn("delay", observed["futureDisconnected"])
        self.assertIn("limiter", observed["futureDisconnected"])

    def test_zero_latency_and_discrete_multichannel_webaudio_are_admitted_safely(self) -> None:
        script = f"""
          import fs from 'node:fs';
          import {{ compileRoomBus, layoutContractSha256, roomContractSha256, validateRoomLayouts }} from './engine/room-events.js';
          import {{ scheduleRoomWebAudio }} from './sound/web_audio.mjs';
          const score = JSON.parse(fs.readFileSync(process.env.DANSE_ROOM_FIXTURE_SCORE));
          const layouts = JSON.parse(fs.readFileSync('sound/room-layout.json'));
          layouts.safety.latency_budget_ms = 0;
          layouts.identity.contract_sha256 = layoutContractSha256(layouts);
          validateRoomLayouts(layouts);

          const bus = compileRoomBus(score, {json.dumps(PASSAGE)});
          const event = bus.events.find((candidate) => candidate.type === 'score.note');
          const layout = layouts.layouts.find((candidate) => candidate.id === 'reference-quad');
          const digest = 'b'.repeat(64);
          for (const coincident of bus.events.filter((candidate) => candidate.at === event.at)) {{
            coincident.target_speaker = layout.speakers[0].id;
          }}
          event.audio.source_sha256 = digest;
          bus.identity.contract_sha256 = roomContractSha256(bus);

          function harness(maxChannelCount, channelCount = 2) {{
            const state = {{
              nodeCalls: 0,
              delayCalls: 0,
              mergerChannels: null,
              limiterReachedDestination: false,
              createdKinds: [],
              disconnectedKinds: [],
              sources: [],
            }};
            const destination = {{
              kind: 'destination',
              maxChannelCount,
              channelCount,
              channelCountMode: 'max',
              channelInterpretation: 'speakers',
            }};
            const node = (kind) => {{
              state.createdKinds.push(kind);
              return {{
                kind,
                connect(target) {{
                  state.nodeCalls += 1;
                  if (kind === 'limiter' && target === destination) state.limiterReachedDestination = true;
                }},
                disconnect() {{ state.disconnectedKinds.push(kind); }},
              }};
            }};
            const context = {{
              currentTime: 0,
              destination,
              createChannelMerger(channels) {{
                state.nodeCalls += 1;
                state.mergerChannels = channels;
                return node('merger');
              }},
              createWaveShaper() {{
                state.nodeCalls += 1;
                return {{...node('limiter'), curve: null, oversample: 'none'}};
              }},
              createBufferSource() {{
                state.nodeCalls += 1;
                const source = {{...node('source'), buffer: null, playbackRate: {{value: 1}}, start() {{}}, stop() {{}}}};
                state.sources.push(source);
                return source;
              }},
              createDelay(maxDelayTime) {{
                state.nodeCalls += 1;
                state.delayCalls += 1;
                if (!(maxDelayTime > 0)) throw new Error('createDelay requires a positive maximum');
                return {{...node('delay'), delayTime: {{value: 0}}}};
              }},
              createGain() {{
                state.nodeCalls += 1;
                return {{...node('gain'), gain: {{value: 0}}}};
              }},
            }};
            return {{context, destination, state}};
          }}

          const admittedHarness = harness(4);
          const admitted = scheduleRoomWebAudio(
            admittedHarness.context,
            bus,
            layouts,
            'reference-quad',
            {{[event.audio.role]: {{buffer: {{duration: 1}}, audio_source_sha256: digest}}}},
            event.at,
            event.at + 0.0001,
            {{output: 'multichannel'}},
          );
          const admittedStop = admitted.stop(0);
          const admittedSecondStop = admitted.stop(0);

          const limitedHarness = harness(2);
          const blockedOnly = scheduleRoomWebAudio(
            limitedHarness.context,
            bus,
            layouts,
            'reference-quad',
            {{}},
            event.at,
            event.at + 0.0001,
            {{output: 'multichannel'}},
          );
          const blockedDestination = {{...limitedHarness.destination}};
          const callsAfterBlocked = limitedHarness.state.nodeCalls;
          let insufficientRejected = false;
          try {{
            scheduleRoomWebAudio(
              limitedHarness.context,
              bus,
              layouts,
              'reference-quad',
              {{[event.audio.role]: {{buffer: {{duration: 1}}, audio_source_sha256: digest}}}},
              event.at,
              event.at + 0.0001,
              {{output: 'multichannel'}},
            );
          }} catch (error) {{
            insufficientRejected = /requires 4 destination channels/.test(error.message);
          }}

          const fixedOfflineHarness = harness(0, 4);
          const fixedOffline = scheduleRoomWebAudio(
            fixedOfflineHarness.context,
            bus,
            layouts,
            'reference-quad',
            {{[event.audio.role]: {{buffer: {{duration: 1}}, audio_source_sha256: digest}}}},
            event.at,
            event.at + 0.0001,
            {{output: 'multichannel'}},
          );
          fixedOfflineHarness.state.sources[0].onended();

          console.log(JSON.stringify({{
            scheduled: admitted.scheduled.length,
            delayCalls: admittedHarness.state.delayCalls,
            mergerChannels: admittedHarness.state.mergerChannels,
            destination: admittedHarness.destination,
            limiterReachedDestination: admittedHarness.state.limiterReachedDestination,
            admittedStop,
            admittedSecondStop,
            admittedDisposed: admitted.disposed,
            admittedCreated: admittedHarness.state.createdKinds,
            admittedDisconnected: admittedHarness.state.disconnectedKinds,
            insufficientRejected,
            limitedNodeCalls: limitedHarness.state.nodeCalls,
            blockedMissing: blockedOnly.missing.length,
            blockedDisposed: blockedOnly.disposed,
            blockedDestination,
            callsAfterBlocked,
            fixedOfflineScheduled: fixedOffline.scheduled.length,
            fixedOfflineDestination: fixedOfflineHarness.destination,
            fixedOfflineDisposed: fixedOffline.disposed,
            fixedOfflineDisconnected: fixedOfflineHarness.state.disconnectedKinds,
          }}));
        """
        observed = node_json(script)
        self.assertEqual(observed["scheduled"], 1)
        self.assertEqual(observed["delayCalls"], 0)
        self.assertEqual(observed["mergerChannels"], 4)
        self.assertEqual(observed["destination"]["channelCount"], 4)
        self.assertEqual(observed["destination"]["channelCountMode"], "explicit")
        self.assertEqual(observed["destination"]["channelInterpretation"], "discrete")
        self.assertTrue(observed["limiterReachedDestination"])
        self.assertTrue(observed["admittedStop"])
        self.assertFalse(observed["admittedSecondStop"])
        self.assertTrue(observed["admittedDisposed"])
        self.assertEqual(sorted(observed["admittedCreated"]), sorted(observed["admittedDisconnected"]))
        self.assertIn("merger", observed["admittedDisconnected"])
        self.assertIn("limiter", observed["admittedDisconnected"])
        self.assertTrue(observed["insufficientRejected"])
        self.assertGreater(observed["blockedMissing"], 0)
        self.assertTrue(observed["blockedDisposed"])
        self.assertEqual(observed["blockedDestination"]["channelCount"], 2)
        self.assertEqual(observed["blockedDestination"]["channelCountMode"], "max")
        self.assertEqual(observed["blockedDestination"]["channelInterpretation"], "speakers")
        self.assertEqual(observed["callsAfterBlocked"], 0)
        self.assertEqual(observed["limitedNodeCalls"], 0)
        self.assertEqual(observed["fixedOfflineScheduled"], 1)
        self.assertEqual(observed["fixedOfflineDestination"]["channelCount"], 4)
        self.assertEqual(observed["fixedOfflineDestination"]["channelCountMode"], "explicit")
        self.assertEqual(observed["fixedOfflineDestination"]["channelInterpretation"], "discrete")
        self.assertTrue(observed["fixedOfflineDisposed"])
        self.assertIn("merger", observed["fixedOfflineDisconnected"])
        self.assertIn("limiter", observed["fixedOfflineDisconnected"])

    def test_control_live_stereo_offline_and_multichannel_share_exact_bus_events(self) -> None:
        result = run(
            "node", "sound/control.mjs", "--rate", "0",
            "--score", "music/score.json",
            "--choreography", "render/choreography.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        control = json.loads(result.stdout)
        for invalid_seed in ("1.5", "0x100000000", "-1"):
            invalid = run(
                "node",
                "sound/control.mjs",
                "--rate",
                "0",
                "--score",
                "music/score.json",
                "--choreography",
                "render/choreography.json",
                "--seed",
                invalid_seed,
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("32-bit unsigned integer", invalid.stderr)
        buses = validate_control_room(control, self.layouts)
        self.assertEqual(control["room"]["semantics"], "authored-start-events")
        self.assertEqual(control["room"]["layout_registry_path"], "sound/room-layout.json")
        self.assertEqual(control["music"]["score_path"], "music/score.json")
        self.assertNotIn("\\", control["room"]["layout_registry_path"])
        self.assertNotIn("\\", control["music"]["score_path"])
        self.assertNotIn(str(ROOT), json.dumps(control["room"]))
        stereo = plan_control(control, self.layouts, "reference-quad", "stereo")
        multichannel = plan_control(control, self.layouts, "reference-quad", "multichannel")
        self.assertEqual([event["id"] for event in stereo["events"]], [event["id"] for event in multichannel["events"]])
        intensities = {
            event["id"]: event["intensity"]
            for bus in buses
            for event in bus["events"]
        }
        for event in stereo["events"]:
            self.assertEqual(bool(event["taps"]), intensities[event["id"]] > 0)
        self.assertTrue(all(event["taps"] for event in multichannel["events"]))
        self.assertEqual(stereo["bus_contract_sha256"], [bus["identity"]["contract_sha256"] for bus in buses])
        self.assertEqual(room_event_plan(control, "reference-quad", "multichannel"), multichannel)
        with self.assertRaisesRegex(ValueError, "undeclared audio source bytes"):
            plan_control(control, self.layouts, "reference-quad", "stereo", require_cleared=True)

        later_result = run(
            "node",
            "sound/control.mjs",
            "--rate",
            "0",
            "--score",
            "music/score.json",
            "--choreography",
            "render/choreography.json",
            "--window",
            "midnight-moment",
            "--from",
            "350",
        )
        self.assertEqual(later_result.returncode, 0, later_result.stderr)
        later_control = json.loads(later_result.stdout)
        later_buses = validate_control_room(later_control, self.layouts)
        self.assertTrue(later_buses)
        for bus in later_buses:
            starts = [event for event in bus["events"] if event["type"] == "passage.start"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["at"], bus["time"]["t0"])

        with tempfile.TemporaryDirectory(dir=ROOT / "sound") as temporary:
            alternate_path = Path(temporary) / "alternate-room-layout.json"
            alternate = copy.deepcopy(self.layouts)
            alternate["identity"]["id"] = "alternate-test-room-layouts"
            alternate["safety"]["max_event_gain"] = 0.5
            alternate["identity"]["contract_sha256"] = layout_contract_sha256(alternate)
            alternate_path.write_text(json.dumps(alternate))
            alternate_control = copy.deepcopy(control)
            alternate_control["room"]["layout_registry_path"] = alternate_path.relative_to(ROOT).as_posix()
            alternate_control["room"]["layout_identity"] = alternate["identity"]
            alternate_plan = room_event_plan(alternate_control, "reference-quad", "multichannel")
            self.assertEqual(alternate_plan["layout_contract_sha256"], alternate["identity"]["contract_sha256"])
            self.assertEqual(alternate_plan["safety"]["max_event_gain"], 0.5)
            self.assertEqual(
                [bus["identity"]["contract_sha256"] for bus in validate_room_event_control(alternate_control)],
                alternate_plan["bus_contract_sha256"],
            )

            custom_default_path = Path(temporary) / "custom-default-room-layout.json"
            custom_default = copy.deepcopy(self.layouts)
            portable = next(row for row in custom_default["layouts"] if row["id"] == "stereo")
            portable["id"] = "portable-default"
            custom_default["default_layout"] = portable["id"]
            custom_default["identity"]["id"] = "custom-default-test-layouts"
            custom_default["identity"]["contract_sha256"] = layout_contract_sha256(custom_default)
            custom_default_path.write_text(json.dumps(custom_default))
            custom_control = copy.deepcopy(control)
            custom_control["room"]["layout_registry_path"] = custom_default_path.relative_to(ROOT).as_posix()
            custom_control["room"]["layout_identity"] = custom_default["identity"]
            self.assertEqual(room_event_plan(custom_control)["layout"], "portable-default")

            custom_control_path = Path(temporary) / "custom-default-control.json"
            custom_control_path.write_text(json.dumps(custom_control))
            cli_default = run(
                "python3",
                "sound/room_render.py",
                str(custom_control_path),
                "--layouts",
                str(custom_default_path),
                "--output",
                "stereo",
            )
            self.assertEqual(cli_default.returncode, 0, cli_default.stderr)
            self.assertEqual(json.loads(cli_default.stdout)["layout"], "portable-default")

            validation_path = Path(temporary) / "validation-only-room-layout.json"
            validation_only = copy.deepcopy(self.layouts)
            validation_only["coordinate_system"]["meters_per_unit"] = 100
            validation_only["identity"]["id"] = "validation-only-test-layouts"
            validation_only["identity"]["contract_sha256"] = layout_contract_sha256(validation_only)
            validation_path.write_text(json.dumps(validation_only))
            validation_control = copy.deepcopy(control)
            validation_control["room"]["layout_registry_path"] = validation_path.relative_to(ROOT).as_posix()
            validation_control["room"]["layout_identity"] = validation_only["identity"]
            self.assertEqual(len(validate_room_event_control(validation_control)), len(buses))
            with self.assertRaisesRegex(ValueError, "exceeds latency budget"):
                room_event_plan(validation_control, "reference-quad", "stereo")

            link = Path(temporary) / "linked-room-layout.json"
            link.symlink_to(ROOT / "sound/room-layout.json")
            linked_control = copy.deepcopy(control)
            linked_control["room"]["layout_registry_path"] = link.relative_to(ROOT).as_posix()
            with self.assertRaisesRegex(ValueError, "regular file, not a symlink"):
                room_event_plan(linked_control)

        escaped_control = copy.deepcopy(control)
        escaped_control["room"]["layout_registry_path"] = "../outside-room-layout.json"
        with self.assertRaisesRegex(ValueError, "outside the Danse repository"):
            room_event_plan(escaped_control)

    def test_control_buses_are_identity_bound_contiguous_and_complete(self) -> None:
        result = run(
            "node", "sound/control.mjs", "--rate", "0",
            "--score", "music/score.json",
            "--choreography", "render/choreography.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        control = json.loads(result.stdout)
        original = control["room"]["buses"][0]

        foreign = copy.deepcopy(control)
        foreign_bus = foreign["room"]["buses"][0]
        foreign_seed = (foreign["seed"] + 1) & 0xFFFFFFFF
        foreign_bus["identity"]["passage"]["river_seed"] = foreign_seed
        for event in foreign_bus["events"]:
            event["passage"]["river_seed"] = foreign_seed
        foreign_bus["identity"]["contract_sha256"] = room_contract_sha256(foreign_bus)
        with self.assertRaisesRegex(ValueError, "seed or stream"):
            validate_control_room(foreign, self.layouts)

        foreign_score = copy.deepcopy(control)
        foreign_score["music"]["identity"]["contract_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "score identity"):
            validate_control_room(foreign_score, self.layouts)

        def shifted_bus(source: dict, index: int, t0: float) -> dict:
            shifted = copy.deepcopy(source)
            old_t0 = shifted["time"]["t0"]
            delta = t0 - old_t0
            passage = shifted["identity"]["passage"]
            passage["index"] = index
            passage["seed"] = (passage["seed"] + index) & 0xFFFFFFFF
            passage["t0"] = t0
            for event in shifted["events"]:
                event["id"] = f"{index}:" + event["id"].split(":", 1)[1]
                event["at"] += delta
                if "end" in event:
                    event["end"] += delta
                event["passage"] = copy.deepcopy(passage)
            shifted["time"]["t0"] = t0
            shifted["time"]["t1"] = t0 + shifted["time"]["seconds"]
            shifted["identity"]["contract_sha256"] = room_contract_sha256(shifted)
            return validate_room_bus(shifted)

        second = shifted_bus(original, original["identity"]["passage"]["index"] + 1, original["time"]["t1"])
        third = shifted_bus(original, original["identity"]["passage"]["index"] + 2, second["time"]["t1"])
        continuous = copy.deepcopy(control)
        continuous["t0"] = original["time"]["t0"]
        continuous["t1"] = third["time"]["t1"]
        continuous["room"]["buses"] = [original, second, third]
        self.assertEqual(len(validate_control_room(continuous, self.layouts)), 3)

        tolerated_seam = copy.deepcopy(continuous)
        tolerated_seam["room"]["buses"][0]["time"]["t1"] -= 0.5e-6
        tolerated_seam["room"]["buses"][0]["identity"]["contract_sha256"] = room_contract_sha256(
            tolerated_seam["room"]["buses"][0]
        )
        self.assertEqual(len(validate_control_room(tolerated_seam, self.layouts)), 3)

        missing_middle = copy.deepcopy(continuous)
        del missing_middle["room"]["buses"][1]
        with self.assertRaisesRegex(ValueError, "ordered and contiguous"):
            validate_control_room(missing_middle, self.layouts)

        missing_first = copy.deepcopy(continuous)
        del missing_first["room"]["buses"][0]
        with self.assertRaisesRegex(ValueError, "cover the complete control interval"):
            validate_control_room(missing_first, self.layouts)

        missing_last = copy.deepcopy(continuous)
        missing_last["room"]["buses"].pop()
        with self.assertRaisesRegex(ValueError, "cover the complete control interval"):
            validate_control_room(missing_last, self.layouts)

    def test_tampered_bus_and_control_identity_fail_before_rendering(self) -> None:
        stale = copy.deepcopy(self.bus)
        stale["events"][0]["intensity"] = 0.1
        with self.assertRaisesRegex(ValueError, "contract_sha256 is stale"):
            validate_room_bus(stale)

        malformed = copy.deepcopy(self.bus)
        malformed["events"][0]["position"]["x"] = 2
        malformed["identity"]["contract_sha256"] = room_contract_sha256(malformed)
        with self.assertRaisesRegex(ValueError, "position is invalid"):
            validate_room_bus(malformed)

        bool_coercion = copy.deepcopy(self.bus)
        bool_coercion["identity"]["passage"]["river_seed"] = 1
        for event in bool_coercion["events"]:
            event["passage"]["river_seed"] = 1
        bool_coercion["events"][0]["passage"]["river_seed"] = True
        bool_coercion["identity"]["contract_sha256"] = room_contract_sha256(bool_coercion)
        with self.assertRaisesRegex(ValueError, "seed and stream fields must be uint32"):
            validate_room_bus(bool_coercion)

        passage_script = f"""
          import fs from 'node:fs';
          import {{ compileRoomBus, validateRoomBus }} from './engine/room-events.js';
          const score = JSON.parse(fs.readFileSync(process.env.DANSE_ROOM_FIXTURE_SCORE));
          const reordered = compileRoomBus(score, {json.dumps(PASSAGE)});
          reordered.events[0].passage = Object.fromEntries(Object.entries(reordered.events[0].passage).reverse());
          validateRoomBus(reordered);
          const extra = JSON.parse(JSON.stringify(reordered));
          extra.events[0].passage.untrusted = true;
          let rejected = false;
          try {{ validateRoomBus(extra); }} catch (error) {{ rejected = /passage does not match identity/.test(error.message); }}
          console.log(JSON.stringify({{reordered: true, rejected}}));
        """
        self.assertEqual(node_json(passage_script), {"reordered": True, "rejected": True})

        control = {
            "t0": PASSAGE["t0"],
            "t1": PASSAGE["t0"] + PASSAGE["seconds"],
            "seed": PASSAGE["river_seed"],
            "stream": PASSAGE["stream"],
            "room": {
                "schema": "danse.room.control.v1",
                "semantics": "authored-start-events",
                "layout_identity": {"contract_sha256": "0" * 64},
                "buses": [self.bus],
            },
        }
        with self.assertRaisesRegex(ValueError, "layout identity"):
            validate_control_room(control, self.layouts)
        malformed_identity = copy.deepcopy(control)
        malformed_identity["room"]["layout_identity"] = "not-an-identity-object"
        with self.assertRaisesRegex(ValueError, "layout identity"):
            validate_control_room(malformed_identity, self.layouts)


def round_js(value: float, places: int) -> float:
    import math

    scale = 10**places
    return math.floor(value * scale + 0.5) / scale


def math_hypot(values) -> float:
    import math

    return math.hypot(*values)


if __name__ == "__main__":
    unittest.main()
