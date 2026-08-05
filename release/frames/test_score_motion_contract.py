#!/usr/bin/env python3
"""Fail-closed CI contract for score→motion A/B evidence — no GPU required."""

import json
import pathlib
import shutil
import sys
import unittest


REPO = pathlib.Path(__file__).resolve().parents[2]
RECEIPT = REPO / "release" / "frames" / "score-to-motion-frames.json"


class ScoreMotionContract(unittest.TestCase):
    def test_receipt_exists(self):
        self.assertTrue(RECEIPT.exists(), "receipt.json missing")

    def test_receipt_schema(self):
        r = json.loads(RECEIPT.read_text())
        self.assertEqual(r["schema"], "danse.evidence.score-to-motion-frames.v1")
        self.assertIn("contract", r)
        self.assertIn("seed", r)
        self.assertIn("stream", r)
        self.assertIn("passage", r)
        self.assertIn("rows", r)
        self.assertGreater(len(r["rows"]), 0, "must have boundary rows")

    def test_determinism_byte_identical(self):
        r = json.loads(RECEIPT.read_text())
        self.assertIn("determinism", r)
        self.assertTrue(r["determinism"]["identical"], "determinism check failed: renders not byte-identical")

    def test_psnr_bounds(self):
        r = json.loads(RECEIPT.read_text())
        for row in r["rows"]:
            psnr = row.get("psnr_db")
            self.assertIsNotNone(psnr, f"missing psnr_db at t={row.get('absolute_second')}")
            # PSNR should be within plausible range (identical=None handled in script, but we check values)
            if psnr is not None:
                self.assertLessEqual(psnr, 60, f"PSNR implausibly high at t={row.get('absolute_second')}: {psnr}")
                self.assertGreaterEqual(psnr, 10, f"PSNR implausibly low at t={row.get('absolute_second')}: {psnr}")

    def test_boundary_coverage(self):
        r = json.loads(RECEIPT.read_text())
        kinds = {row["kind"] for row in r["rows"]}
        self.assertIn("origin", kinds, "must include origin boundary")
        self.assertIn("movement", kinds, "must include movement boundaries")
        self.assertIn("cue", kinds, "must include cue/accent boundaries")

    @unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg not present")
    def test_contact_sheet_rendered(self):
        sheet = REPO / "release" / "frames" / "score-to-motion-frames.png"
        self.assertTrue(sheet.exists(), "contact sheet not generated")


if __name__ == "__main__":
    sys.exit(unittest.main())