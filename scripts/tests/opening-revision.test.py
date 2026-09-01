#!/usr/bin/env python3
"""Portable invariants for the phrase-native ScreenDance opening revision."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "submission" / "revise_screendance_opening.py"
SPEC = importlib.util.spec_from_file_location("danse_opening_revision", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import {SCRIPT}")
OPENING = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OPENING)


class OpeningRevisionContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.score = json.loads((ROOT / "music" / "score.json").read_text())
        cls.choreography = json.loads((ROOT / "render" / "choreography.json").read_text())
        cls.timing = OPENING.opening_timing(cls.score, cls.choreography)

    def test_score_phrases_replace_arbitrary_thirty_second_blocks(self) -> None:
        self.assertEqual(
            self.timing["phrases"],
            {
                "assembly_resolve": "sylvia-03",
                "division_entry": "sylvia-04",
                "canonical_handoff": "sylvia-05",
            },
        )
        self.assertEqual(
            self.timing["frames"],
            {
                "fixed_opening_end": 900,
                "assembly_resolve": 1036,
                "division_entry": 1379,
                "canonical_handoff": 1722,
                "tail_unchanged": 1800,
            },
        )
        self.assertAlmostEqual(self.timing["score_seconds"]["assembly_resolve"], 34.527651875)
        self.assertAlmostEqual(self.timing["score_seconds"]["division_entry"], 45.956211875)
        self.assertAlmostEqual(self.timing["score_seconds"]["canonical_handoff"], 57.384771875)

    def test_five_segments_tile_exactly_one_minute(self) -> None:
        frames = self.timing["frames"]
        segment_frames = [
            frames["fixed_opening_end"],
            frames["assembly_resolve"] - frames["fixed_opening_end"],
            frames["division_entry"] - frames["assembly_resolve"],
            frames["canonical_handoff"] - frames["division_entry"],
            frames["tail_unchanged"] - frames["canonical_handoff"],
        ]
        self.assertEqual(segment_frames, [900, 136, 343, 343, 78])
        self.assertEqual(sum(segment_frames), 1800)

    def test_accepted_first_thirty_seconds_still_visits_all_161_frames(self) -> None:
        pulses = OPENING.pulse_times(
            self.score,
            self.timing["frame_seconds"]["assembly_resolve"],
        )
        seen = set()
        for output_frame in range(self.timing["frames"]["fixed_opening_end"]):
            second = output_frame / OPENING.FPS
            pulse = max(0, OPENING.bisect.bisect_right(pulses, second) - 1)
            seen.add(OPENING.opening_source_index(pulse, 161))
        self.assertEqual(seen, set(range(161)))


if __name__ == "__main__":
    unittest.main()
