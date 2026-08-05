#!/usr/bin/env python3
"""A/B capture evidence for Music II — the score clock drives the image.

For one deterministic (seed, stream, passage) we sample, at every declared
musical boundary (movement start, phrase start, cue/accent start, downbeat),
the visual state of the piece WITH the score and WITHOUT it, at the same
absolute time. The delta is exactly the choreography the score contributes:
the answer to the #7 human correction ("the visuals do not move to the music")
is made numerically and audibly inspectable. The audible half is the note plan
`planWebAudio` schedules in the same window.

Everything is a pure function of (seed, t), so the receipt is byte-reproducible:
the test suite regenerates it into a temp path and requires it to equal the
tracked `docs/evidence/score-to-motion-ab.json` + `.md`.

Usage:
    python3 scripts/capture-score-motion-ab.py [--passage 7] [--seed 0x12345678]
                                               [--out docs/evidence]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUFFIX = {".json": ".md"}


def run(*command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def emit(seed: int, stream: int, passage_index: int) -> dict:
    script = """
      import fs from 'node:fs';
      import { passageAt } from './engine/program.js';
      import { state } from './engine/clock.js';
      import { scoreAt } from './engine/score.js';
      import { planWebAudio } from './sound/web_audio.mjs';
      const score = JSON.parse(fs.readFileSync('music/score.json'));
      const program = JSON.parse(fs.readFileSync('render/program.json'));
      const seed = SEED, stream = STREAM, index = PASSAGE;
      const passage = passageAt(program, seed, 0, stream);
      const scale = passage.seconds / score.time.duration_seconds;
      const window = { t0: passage.t0, seconds: passage.seconds };
      const boundaries = [];
      for (const m of score.movements) boundaries.push({ kind: 'movement', id: m.id, at: passage.t0 + m.start_second * scale });
      for (const p of score.phrases) boundaries.push({ kind: 'phrase', id: p.id, at: passage.t0 + p.start_second * scale });
      for (const c of score.cues) boundaries.push({ kind: 'cue', id: c.id, at: passage.t0 + c.second * scale });
      for (const b of score.beats) {
        if (b.downbeat) boundaries.push({ kind: 'downbeat', id: `bar-${b.bar}`, at: passage.t0 + b.second * scale });
      }
      boundaries.sort((a, b) => a.at - b.at);
      const keep = new Set();
      for (const row of boundaries) keep.add(row.kind + '@' + row.id);
      const unique = boundaries.filter((row) => {
        const key = row.kind + '@' + row.id;
        if (keep.has(key)) { keep.delete(key); return true; }
        return false;
      });
      const rows = [];
      for (const b of unique) {
        const at = b.at;
        const eps = 0.01;
        const beforeScore = state(seed, Math.max(0, at - eps), program, stream, score);
        const withScore = state(seed, at, program, stream, score);
        const afterScore = state(seed, at + eps, program, stream, score);
        const without = state(seed, at, program, stream, null);
        const mus = scoreAt(score, at, window);
        const audio = planWebAudio(score, at, at + 0.25, window);
        const chan = (s) => ({
          divergence: +s.divergence.toFixed(6),
          azimuth: +s.azimuth.toFixed(6),
          elevation: +s.elevation.toFixed(6),
          spread: +s.spread.toFixed(6),
          projK: +s.projK.toFixed(6),
          turnover: +s.turnover.toFixed(6),
        });
        const a = chan(without);
        const z = chan(withScore);
        const delta = {};
        for (const k of Object.keys(a)) delta[k] = +(z[k] - a[k]).toFixed(6);
        const movement = {};
        for (const k of Object.keys(a)) movement[k] = +(afterScore[k] - beforeScore[k]).toFixed(6);
        rows.push({
          kind: b.kind,
          id: b.id,
          absolute_second: +at.toFixed(6),
          source_second: +mus.source_second.toFixed(6),
          movement: mus.movement.id,
          phrase: mus.phrase.id,
          beat: mus.beat.index,
          downbeat: mus.beat.downbeat,
          dynamic: mus.dynamic.midi_expression,
          visual: {
            without_score: a,
            with_score: z,
            score_delta: delta,
            score_transition: movement,
            recast: withScore.music?.visual.recast ?? null,
            hold: withScore.music?.visual.hold ?? false,
            material: withScore.material,
          },
          audio: {
            notes: audio.map((n) => ({ at: +n.at.toFixed(6), stem: n.stem, pitch: n.pitch, velocity: n.velocity })),
          },
        });
      }
      const payload = {
        contract: score.identity.contract_sha256,
        seed: `0x${seed.toString(16)}`,
        stream,
        passage: { index: passage.index, t0: +passage.t0.toFixed(6), seconds: +passage.seconds.toFixed(6) },
        source_seconds: score.time.duration_seconds,
        rows,
      };
      const text = JSON.stringify(payload, null, 2);
      process.stdout.write(text + '\\n');
    """
    script = script.replace("SEED", str(seed)).replace("STREAM", str(stream)).replace("PASSAGE", str(passage_index))
    result = run("node", "--input-type=module", "--eval", script)
    if result.returncode != 0:
        raise SystemExit(f"score-to-motion A/B probe failed:\n{result.stderr}")
    return json.loads(result.stdout)


def markdown(payload: dict) -> str:
    lines = [
        "# Score → motion A/B capture evidence (2026-08-05)",
        "",
        "For one fixed (seed, stream, passage) the same absolute time is sampled with and",
        "without the score clock. `score_delta` is exactly the choreography the score",
        "contributes; the image alone (`without_score`) is the control. `audio.notes` is",
        "what `planWebAudio` schedules in the same 250 ms window.",
        "",
        f"- score contract: `{payload['contract']}`",
        f"- seed: `{payload['seed']}`, stream: `{payload['stream']}`",
        f"- passage: `{payload['passage']['index']}` (t0={payload['passage']['t0']}s, "
        f"{payload['passage']['seconds']}s) over {payload['source_seconds']} source seconds",
        "",
    ]

    structural = [row for row in payload["rows"] if row["kind"] != "downbeat"]
    changed = [row for row in structural if any(abs(v) > 0.0005 for v in row["visual"]["score_transition"].values())]
    audible = [row for row in payload["rows"] if row["audio"]["notes"]]

    lines += [
        f"**{len(payload['rows'])} boundaries sampled** ({len(structural)} structural, "
        f"{len(changed)} where the score moves the image across the boundary, "
        f"{len(audible)} with an audible note in-window). "
        "The full machine receipt is `score-to-motion-ab.json`; downbeats are in the JSON.",
        "",
        "## Declared structural boundaries that move the image",
        "",
        "`score_delta` = visual state WITH the score minus WITHOUT it at the same time "
        "(the choreography the score contributes). `score_transition` = the image motion "
        "the boundary itself causes under the score (just-after minus just-before, ±10 ms).",
        "",
        "| t (s) | boundary | movement | beat | dynamic | score_transition (div, azi, ele, spread, projK, turn) | recast | hold | audio notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in changed:
        d = row["visual"]["score_transition"]
        delta = ", ".join(f"{k[0]}{v:+.3f}" for k, v in d.items() if abs(v) > 0.0005) or "0"
        notes = "; ".join(f"{n['stem']} {n['pitch']}@{n['at']}" for n in row["audio"]["notes"]) or "—"
        lines.append(
            f"| {row['absolute_second']:.3f} | {row['kind']} {row['id']} | {row['movement']} | "
            f"{row['beat']}{' ↓' if row['downbeat'] else ''} | {row['dynamic']} | {delta} | "
            f"{'y' if row['visual']['recast'] is not None else ''} | {'y' if row['visual']['hold'] else ''} | {notes} |"
        )

    silent = [row for row in structural if row not in changed]
    if silent:
        lines += [
            "",
            "## Declared structural boundaries that do not perturb the image",
            "",
            "These land exactly on their declared time without a measurable image delta —",
            "the choreography only moves what each movement declares.",
            "",
            "| t (s) | boundary | movement | beat | dynamic | audio notes |",
            "|---|---|---|---|---|---|",
        ]
        for row in silent:
            notes = "; ".join(f"{n['stem']} {n['pitch']}@{n['at']}" for n in row["audio"]["notes"]) or "—"
            lines.append(
                f"| {row['absolute_second']:.3f} | {row['kind']} {row['id']} | {row['movement']} | "
                f"{row['beat']}{' ↓' if row['downbeat'] else ''} | {row['dynamic']} | {notes} |"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=lambda raw: int(raw, 0), default=0x12345678)
    ap.add_argument("--stream", type=int, default=0)
    ap.add_argument("--passage", type=int, default=0)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "evidence")
    args = ap.parse_args()

    payload = emit(args.seed, args.stream, args.passage)
    args.out.mkdir(parents=True, exist_ok=True)
    json_path = args.out / "score-to-motion-ab.json"
    md_path = args.out / "score-to-motion-ab.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n")
    md_path.write_text(markdown(payload))

    digest = hashlib.sha256(json_path.read_bytes()).hexdigest()
    print(f"wrote {json_path} ({json_path.stat().st_size} bytes) sha256 {digest[:16]}")
    print(f"wrote {md_path} ({md_path.stat().st_size} bytes)")
    print(f"{len(payload['rows'])} boundaries sampled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
