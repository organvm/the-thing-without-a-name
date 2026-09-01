#!/usr/bin/env python3
"""danse: the invariants the engine is built on, checked without a GPU.

Three claims carry the piece. Two of them are checkable with arithmetic alone,
and this is where they bind — a plan may record a decision, it may never be its
only home.

  1. PROJECTIVE TEXTURING, NOT PER-PLANE UVs. Every fragment addresses its pixel
     through one shared projector matrix, which only works because the 2017 score
     partitions the frame exactly: no gaps, no overlaps, every tile inside the
     frustum. A hole in that partition is a hole in the room. Checked here.
     (The GPU half — that continuity survives arbitrary geometry — is probe.html.)

  2. THE FLATTENING IS THE CAMERA, NOT `projK`. Corrected from the original plan,
     which had `projK` as the film's spine. At divergence 0 the render is the 2017
     composite no matter how the planes are arranged, so the reveal is a MOVE, not
     a uniform sweep. Checked here as `divergence(seed, 0) == 0` exactly, for many
     seeds, and as the return: the same is true again one PERIOD later.

  3. THE ENGINE IS A PURE f(seed, t). No accumulated state anywhere in engine/.
     Checked here by evaluating the clock twice, out of order, and requiring
     bit-identical output — and by grepping engine/ for the state that would break
     it.

  4. EVERY PASSAGE PARTITIONS ITS OWN TIME. `render/program.json` declares a
     PHRASE, not a film — there is no duration and no end. The engine traverses
     the phrase forever, and each traversal draws its own length and its own
     material. Every passage's movements must still tile that passage end to end,
     no gaps and no overlaps, for exactly the reason the score must tile the
     frame. Same arithmetic, one axis down, now checked over 400 passages rather
     than once. And the claim the work actually makes — that it never repeats —
     is checked as a claim: over 20,000 passages, no seed and no length recurs.

The fifth thing this guards is delivery: every frame the score names must exist
as a plate at every SHIPPED tier, or the flat state renders with holes on a
machine that is not this one — and every frame that is not registered to the 2017
camera must be withheld from generated cuts.

    scripts/check-danse.py            # exit 0 iff all five hold
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT
CORPUS = APP / "corpus"
ENGINE = APP / "engine"
PROGRAM = APP / "render" / "program.json"
sys.path.insert(0, str(APP / "sound"))
from bank_contract import audit_bank  # noqa: E402

FAIL: list[str] = []
NOTE: list[str] = []
RUN: list[tuple[str, str | None]] = []

# How many invariants must still be here. A gate that can be quietly hollowed out
# is not a gate: delete half these checks and the remainder still exits 0, and the
# next agent to touch the engine is verified by nothing. So the count ratchets —
# ADDING invariants is free, and removing one is red until someone lowers this
# number in a diff a reviewer can see.
#
# The floor counts only the PORTABLE invariants: the ones that run on any machine
# with python3 and node. That distinction is not bookkeeping, it is the whole point.
# Some checks need a local artifact derived from 2.8 GB of originals that never
# enter git, so on CI they cannot run — and before this floor existed they did not
# merely skip, they SHRANK THE TOTAL SILENTLY. The first CI run of this gate
# reported 39 where this machine reported 42, which is precisely the shape of thing
# an agent trusts and should not: a number that quietly means less depending on
# where it ran.
#
# So conditional checks are declared, counted separately, and named when they are
# absent. Raise FLOOR when you add a portable check; raise the group's count when
# you add a conditional one. Never lower either to make a machine agree.
FLOOR = 53
CONDITIONAL = {"grain bank": 3}

GROUP: str | None = None


@contextlib.contextmanager
def conditional(name: str):
    """Tag every check inside as needing a local artifact CI cannot have.

    Absent is not a failure — but it must be VISIBLE, and it must never be
    mistaken for the portable floor having been met.
    """
    global GROUP
    GROUP, prev = name, GROUP
    try:
        yield
    finally:
        GROUP = prev


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    RUN.append((name, GROUP))
    if not ok:
        FAIL.append(name)


def load(path: Path):
    return json.loads(path.read_text())


def check_music_contract() -> None:
    """Run the fixture score's cross-language, provenance, and seek regressions."""
    test = ROOT / "scripts/tests/music-score.test.py"
    done = subprocess.run([sys.executable, str(test)], cwd=ROOT, capture_output=True, text=True, check=False)
    detail = "fixture register, compiler, JS/Python/WebAudio parity, receipts, and human gate"
    if done.returncode != 0:
        lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
        detail = lines[-1] if lines else f"exit {done.returncode}"
    check("the musical score is one immutable absolute-time contract", done.returncode == 0, detail)


def check_rights_contract() -> None:
    """Run the redacted rights inventory and fail-closed phase regressions."""
    test = ROOT / "scripts/tests/rights.test.py"
    done = subprocess.run([sys.executable, str(test)], cwd=ROOT, capture_output=True, text=True, check=False)
    detail = "redacted exact-source inventory, human gates, package/release binding, and private-custody boundary"
    if done.returncode != 0:
        lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
        detail = lines[-1] if lines else f"exit {done.returncode}"
    check("rights and attribution fail closed on every uncleared use", done.returncode == 0, detail)


def check_interface_contract() -> None:
    """Run pure progressive-control state, shortcut, and action-bus checks."""
    test = ROOT / "scripts/tests/interface-controls.test.mjs"
    done = subprocess.run(["node", str(test)], cwd=ROOT, capture_output=True, text=True, check=False)
    detail = "shared named actions, typed states, editable-control shortcuts, and share-state boundary"
    if done.returncode != 0:
        lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
        detail = lines[-1] if lines else f"exit {done.returncode}"
    check("progressive controls share one typed action contract", done.returncode == 0, detail)


def check_room_event_contract() -> None:
    """Run the typed room bus, routing, safety, provenance, and parity regressions."""
    test = ROOT / "scripts/tests/room-events.test.py"
    try:
        done = subprocess.run(
            [sys.executable, str(test)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        check("one room-event bus serves every sound renderer", False, "room-event regressions exceeded 120s")
        return
    detail = "typed passage buses, bucket seeks, JS/Python parity, fold-down, safety, and source gates"
    if done.returncode != 0:
        lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
        detail = lines[-1] if lines else f"exit {done.returncode}"
    check("one room-event bus serves every sound renderer", done.returncode == 0, detail)


def check_installation_contract() -> None:
    """Prove the reference twin and the fail-closed physical boundary together."""
    test = ROOT / "scripts/tests/installation.test.py"
    checker = ROOT / "scripts/check-installation.py"
    try:
        done = subprocess.run(
            [sys.executable, str(test)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        check("one deterministic twin binds every installation subsystem", False, "installation regressions exceeded 120s")
        check("physical installation claims require venue-owned evidence", False, "installation regressions unavailable")
        return
    detail = "reference geometry, frame tickets, calibration, runtime, recovery, restore, and archive disposition"
    if done.returncode != 0:
        lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
        detail = lines[-1] if lines else f"exit {done.returncode}"
    check("one deterministic twin binds every installation subsystem", done.returncode == 0, detail)

    blocked = subprocess.run(
        [sys.executable, str(checker), "--phase", "complete"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    fail_closed = blocked.returncode != 0 and "BLOCKED: physical phase complete requires" in blocked.stderr
    check(
        "physical installation claims require venue-owned evidence",
        fail_closed,
        "8 gates blocked · venue/hardware/calibration/3 wall-plug/restore receipts absent"
        if fail_closed
        else "the terminal installation phase did not fail closed",
    )


# ── 1. the score partitions the frame exactly ──────────────────────────────────


def check_partition(score: dict) -> None:
    w, h = score["target"]["w"], score["target"]["h"]
    cover = bytearray(w * h)
    overlap = 0
    outside = 0

    for tile in score["tiles"]:
        x0, y0, x1, y1 = tile["px"]
        if x0 < 0 or y0 < 0 or x1 > w or y1 > h or x1 <= x0 or y1 <= y0:
            outside += 1
            continue
        for y in range(y0, y1):
            row = y * w
            for x in range(x0, x1):
                if cover[row + x]:
                    overlap += 1
                cover[row + x] = 1

    gaps = len(cover) - sum(cover)
    check("score covers every pixel", gaps == 0, f"{gaps} uncovered of {w * h}")
    check("score tiles never overlap", overlap == 0, f"{overlap} doubly-covered pixels")
    check("every tile inside the frame", outside == 0, f"{outside} degenerate or out of bounds")

    # The rect form is what the engine actually places; it must agree with px.
    worst = 0.0
    for tile in score["tiles"]:
        px = tile["px"]
        want = [px[0] / w, px[1] / h, px[2] / w, px[3] / h]
        worst = max(worst, max(abs(a - b) for a, b in zip(tile["rect"], want)))
    check("rect agrees with px", worst < 1e-3, f"worst disagreement {worst:.2e}")


# ── 4. the program partitions time ─────────────────────────────────────────────


def check_program(program: dict, grammar_cuts: set[str], river: dict) -> None:
    """The phrase, and the river that traverses it.

    The old form of this checked one partition once, because there was one film
    with one set of boundaries. There is no such thing now — every passage lays
    the movements out differently — so the partition is checked over MANY
    passages, and the arithmetic that must hold is the same arithmetic the 2017
    score obeys over the picture plane.
    """
    moves = program["movements"]

    named = {m["cut"] for m in moves}
    unknown = sorted(named - grammar_cuts)
    check(
        "every movement names a cut the grammar serves",
        not unknown,
        f"unknown: {', '.join(unknown)}" if unknown else ", ".join(sorted(named)),
    )

    if not river:
        return

    check(
        "every passage tiles its own time exactly",
        river["badPartitions"] == 0,
        f"{river['badPartitions']} of {river['passages']} passages had a gap or an overlap"
        if river["badPartitions"]
        else f"{river['passages']} passages, no dead air in any of them",
    )

    # The piece is a river or it is a loop, and the difference is measurable: if
    # passage lengths repeat, a viewer can anchor to the phrase and what they are
    # watching is a loop with the fill changed.
    # Lengths must be SPREAD, not unique. Two passages can share a length and
    # still be entirely different passages — they differ in seed, so they differ
    # in every photograph. What this catches is `vary` collapsing toward zero,
    # which would turn the phrase back into a clock a viewer can anchor to.
    spread = river["distinctLengths"] / river["passages"]
    check(
        "passage lengths do not settle onto a clock",
        spread > 0.99,
        f"{river['distinctLengths']} distinct lengths across {river['passages']} passages ({spread:.3%})",
    )
    check(
        "no passage recurs",
        river["repeatedSeeds"] == 0,
        f"{river['repeatedSeeds']} repeated over {river['passages']} passages"
        if river["repeatedSeeds"]
        else f"{river['passages']} passages, {river['days']:.0f} days, none repeated",
    )

    # It still has to be a PHRASE, not noise: a passage that can run twenty
    # seconds or twenty minutes has no shape a viewer could learn.
    lo, hi = river["minSeconds"], river["maxSeconds"]
    check(
        "a passage still runs 5–8 minutes",
        300 <= lo and hi <= 480,
        f"{lo / 60:.2f}–{hi / 60:.2f} min (mean {river['meanSeconds'] / 60:.2f})",
    )

    # Times Square Arts' Midnight Moment is not "about three minutes". It is 170
    # seconds, and a submission that is 171 is rejected without a conversation.
    mm = (program.get("captures") or {}).get("midnight-moment")
    if mm:
        check("the midnight-moment capture is exactly 170s", mm.get("seconds") == 170, f"{mm.get('seconds')}s")


# ── 2/3. the clock, evaluated by node ──────────────────────────────────────────

CLOCK_PROBE = """
import { readFileSync } from "node:fs";
import { state, PERIOD } from "%(clock)s";
import { movementsIn, passageAt, passageSeconds, passageSeed, validate } from "%(program)s";
import { CUTS } from "%(grammar)s";

const seeds = [20170620, 1, 2, 7919, 2147483647, 305419896];
let flatAtZero = 0, flatAtPeriod = 0, impure = 0, everLeaves = 0;
for (const s of seeds) {
  if (state(s, 0).divergence === 0) flatAtZero++;
  if (state(s, PERIOD).divergence === 0) flatAtPeriod++;
  // Out of order on purpose: a stateful clock gives a different answer the
  // second time, and evaluating t ascending would hide exactly that.
  const late = state(s, 37.25), early = state(s, 3.5), lateAgain = state(s, 37.25);
  if (JSON.stringify(late) !== JSON.stringify(lateAgain)) impure++;
  if (early.divergence >= 0 && late.divergence > 0) everLeaves++;
}

// ── the programmed clock ───────────────────────────────────────────────────────
const program = JSON.parse(readFileSync("%(programJson)s", "utf8"));
let programError = null;
try { validate(program); } catch (e) { programError = e.message; }

const seed = program.seed;

// ── the river ──────────────────────────────────────────────────────────────────
// The piece has no duration, so "the whole film" is not a thing that can be
// sampled. What is checked instead is the claim it actually makes: that the
// phrase recurs and the water never does.
const PASSAGES = 20000;
let badPartitions = 0, repeatedSeeds = 0;
const lengths = [], seen = new Set();
for (let n = 0; n < PASSAGES; n++) {
  const sd = passageSeed(seed, n);
  if (seen.has(sd)) repeatedSeeds++;
  seen.add(sd);
  const secs = passageSeconds(program, seed, n);
  lengths.push(secs);
  if (n < 400) {
    const laid = movementsIn(program, seed, n);
    let cursor = 0, ok = true;
    for (const m of laid) {
      if (Math.abs(m.t0 - cursor) > 1e-6 || !(m.t1 > m.t0)) ok = false;
      cursor = m.t1;
    }
    if (!ok || Math.abs(cursor - secs) > 1e-6) badPartitions++;
  }
}
const totalSeconds = lengths.reduce((a, b) => a + b, 0);
const river = {
  passages: PASSAGES,
  badPartitions,
  repeatedSeeds,
  distinctLengths: new Set(lengths.map((x) => x.toFixed(6))).size,
  minSeconds: Math.min(...lengths),
  maxSeconds: Math.max(...lengths),
  meanSeconds: totalSeconds / PASSAGES,
  days: totalSeconds / 86400,
};

// Sample deep into the river, not just its first passage.
const SPAN = passageSeconds(program, seed, 0) * 12;
const N = 1560;
let impureProgram = 0, outOfRange = 0, assemblyFlat = true, assemblySamples = 0;
const epochs = new Set();
for (let i = 0; i <= N; i++) {
  const t = (i / N) * SPAN;
  const a = state(seed, t, program);
  // Out of order again: sample t, then a far-away t, then t once more. The edge
  // cache that finds a passage is memoisation; if it ever became state, this is
  // where it would show.
  state(seed, SPAN - t, program);
  const b = state(seed, t, program);
  if (JSON.stringify(a) !== JSON.stringify(b)) impureProgram++;
  const ok =
    a.divergence >= -1e-9 && a.divergence <= 1 &&
    a.spread >= -1e-9 && a.spread <= 1 &&
    a.projK >= -1e-9 && a.projK <= 1 &&
    a.turnover >= -1e-9 &&
    Math.abs(a.azimuth) <= 1.5 && Math.abs(a.elevation) <= 1;
  if (!ok) outOfRange++;
  // The 2017 composite is only a reproduction while the camera is exactly on
  // axis. If ASSEMBLY drifts off zero at all, what the film shows is a homage.
  if (a.movement === "ASSEMBLY") {
    assemblySamples++;
    if (a.divergence !== 0 || a.spread !== 0) assemblyFlat = false;
  }
  if (a.movement === "RESEED") epochs.add(a.epoch);
}
const reseedMovement = program.movements.find((m) => m.id === "RESEED");
const declaredEpochs = (reseedMovement?.reseeds ?? []).length;

// Two passages far apart must not be the same picture. This is the claim.
const far = passageAt(program, seed, 0), later = passageAt(program, seed, SPAN * 40);
const sameRiver = far.seed !== later.seed && Math.abs(far.seconds - later.seconds) > 1e-9;

console.log(JSON.stringify({
  seeds: seeds.length, flatAtZero, flatAtPeriod, impure, everLeaves, PERIOD,
  programError, impureProgram, outOfRange, assemblyFlat, assemblySamples,
  epochs: epochs.size, declaredEpochs, cuts: CUTS, river, sameRiver, span: SPAN,
}));
"""


def check_clock() -> dict:
    # A real file, not stdin: node resolves the module's relative imports against
    # the script's own path. In a worktree `.git` is a file, so it cannot go there.
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(
            CLOCK_PROBE
            % {
                "clock": ENGINE / "clock.js",
                "program": ENGINE / "program.js",
                "grammar": ENGINE / "grammar.js",
                "programJson": PROGRAM,
            }
        )
        probe = Path(fh.name)
    try:
        out = subprocess.run([node(), probe], capture_output=True, text=True, check=False)
        if out.returncode != 0:
            check("clock evaluates", False, out.stderr.strip().splitlines()[-1] if out.stderr else "node failed")
            return {}
        r = json.loads(out.stdout)
    finally:
        probe.unlink(missing_ok=True)

    check("flat at t=0 for every seed", r["flatAtZero"] == r["seeds"], f"{r['flatAtZero']}/{r['seeds']}")
    check(
        "flat again one period later",
        r["flatAtPeriod"] == r["seeds"],
        f"{r['flatAtPeriod']}/{r['seeds']} at t={r['PERIOD']}s",
    )
    check("the room does open", r["everLeaves"] == r["seeds"], f"{r['everLeaves']}/{r['seeds']}")
    check("clock is pure — same t, same state", r["impure"] == 0, f"{r['impure']} seeds disagreed with themselves")
    return r


def check_film(r: dict) -> None:
    """The programmed clock: the same purity, held to the film's declared arc."""
    check("the program validates", not r["programError"], r["programError"] or "danse.program.v2")
    check(
        "programmed clock is pure anywhere in the river",
        r["impureProgram"] == 0,
        f"{r['impureProgram']} of 1561 samples across {r['span'] / 60:.0f} minutes of river disagreed with themselves",
    )
    check(
        "every channel stays in range across twelve passages",
        r["outOfRange"] == 0,
        f"{r['outOfRange']} samples out of range",
    )
    check(
        "ASSEMBLY holds the camera exactly on axis",
        r["assemblyFlat"] and r["assemblySamples"] > 0,
        f"{r['assemblySamples']} samples at divergence 0 — the composite is reproduced, not evoked",
    )
    check(
        "RESEED restarts as many times as it declares",
        r["epochs"] == r["declaredEpochs"],
        f"{r['epochs']} epochs observed, {r['declaredEpochs']} declared",
    )
    check(
        "the river does not return",
        r["sameRiver"],
        "passages far apart differ in both seed and length",
    )


def node() -> str:
    return "node"


# ── 2b. arrival: the one impure boundary ───────────────────────────────────────

ARRIVAL = APP / "arrival.js"

# A visitor's river is (seed, epoch): a draw made when they show up, and the
# millisecond it was drawn. `arrival.js` is the only place in the app allowed to
# touch a clock or an entropy source, and it exposes `platform` precisely so this
# probe can replace both — with them fixed, arrival is an ordinary pure function
# and can be held to the same standard as the engine.
ARRIVAL_PROBE = """
import { readFileSync } from "node:fs";
import { arrive, hasCitedSeed, hasSelfContainedRiver, href, mint, modeOf, now, platform, recall, remember, rememberedRiverForUndo, riverOf, streamOf, withMode } from "%(arrival)s";
import { passageAt } from "%(program)s";
import { state } from "%(clock)s";

const program = JSON.parse(readFileSync("%(programJson)s", "utf8"));

let CLOCK = 1780000000000;
let NEXT = 0;
platform.now = () => CLOCK;
platform.draw = () => NEXT;
const stored = new Map();
globalThis.localStorage = {
  getItem: (key) => stored.get(key) ?? null,
  setItem: (key, value) => stored.set(key, value),
};

// ── the precedence table, exactly as arrival.js documents it ──────────────────
NEXT = 0xabcdef01;
const shared = arrive("#s=42&e=99");
const cited = arrive("#s=42&t=10");
const bare = arrive("#s=42");
const fresh = arrive("");
const citedAt = 10.1234567890123;
const citedHref = href(fresh, {at: citedAt});
const citedFresh = arrive(citedHref.split("#")[1]);
const citedSerialized = Number(new URLSearchParams(citedHref.split("#")[1]).get("t"));
const links =
  shared.seed === 42 && shared.stream === streamOf(99) && shared.epoch === 99 && !shared.shifted &&
  cited.seed === 42 && Math.abs(now(cited) - 10) < 1e-9 && cited.shifted &&
  bare.seed === 42 && Math.abs(now(bare)) < 1e-9 &&
  fresh.minted && fresh.seed === riverOf(0xabcdef01, CLOCK) &&
  citedFresh.stream === fresh.stream && citedSerialized === citedAt && Math.abs(now(citedFresh) - citedAt) < 1e-6 &&
  modeOf(href(fresh, {mode: "free"})) === "free";

// ── a t-only undo restores the backing river, not just the visible URL ────────
const backing = { seed: 0x10203040, stream: 0x50607080, epoch: CLOCK - 90000 };
remember(backing);
const tOnly = arrive("#t=30");
const undoBacking = rememberedRiverForUndo(tOnly, "#t=30", recall());
NEXT = 0x99887766;
mint();
if (undoBacking) remember(undoBacking);
const tOnlyReloaded = arrive("#t=30");
const tOnlyUndoDurable =
  undoBacking?.epoch === backing.epoch &&
  tOnlyReloaded.seed === tOnly.seed &&
  tOnlyReloaded.stream === tOnly.stream &&
  Math.abs(now(tOnlyReloaded) - 30) < 1e-9 &&
  rememberedRiverForUndo(tOnly, "#s=1&t=30", backing) === null &&
  rememberedRiverForUndo(tOnly, "#s=not-a-seed&t=30", backing) === backing &&
  rememberedRiverForUndo({...tOnly, seed: tOnly.seed + 1}, "#t=30", backing) === null;

const rememberedMatrix = [
  ["bare/recalled", backing, "", backing, backing],
  ["s-only/recalled", backing, "#s=270544960", backing, backing],
  ["wrong epoch", {...backing, epoch: backing.epoch + 1}, "", backing, null],
  ["t-only", tOnly, "#t=30", backing, backing],
  ["cited s+t", tOnly, "#s=270544960&t=30", backing, null],
  ["malformed s+t", tOnly, "#s=not-a-seed&t=30", backing, backing],
  ["Project without carried provenance", tOnly, "#evidence", backing, null],
  ["foreign river", {...tOnly, stream: tOnly.stream + 1}, "#t=30", backing, null],
  ["no remembered river", tOnly, "#t=30", null, null],
];
const rememberedMatrixPasses = rememberedMatrix.every(
  ([, candidate, fragment, storedRiver, expected]) =>
    rememberedRiverForUndo(candidate, fragment, storedRiver) === expected,
);

// A Project-only hash navigation keeps the already-proven backing in UI state.
// After New/Undo restores that stored value, reloading the Project fragment must
// still open the pre-mint river rather than the minted replacement.
const backingAcrossProject = rememberedRiverForUndo(tOnly, "#t=30", backing);
NEXT = 0xaabbccdd;
mint();
if (backingAcrossProject) remember(backingAcrossProject);
const projectReload = arrive("#evidence");
const projectUndoDurable =
  backingAcrossProject === backing &&
  projectReload.seed === backing.seed &&
  projectReload.stream === backing.stream &&
  projectReload.epoch === backing.epoch &&
  !projectReload.shifted;

// A cited/shared river is self-contained even after a Project-only fragment
// temporarily hides its hash. Preserve that old cited URL for Undo/reload.
const foreignFragment = "#s=324508639&t=30&u=610839776";
const foreign = arrive(foreignFragment);
NEXT = 0x13579bdf;
mint();
const foreignReload = arrive(foreignFragment);
const foreignProjectDurable =
  hasCitedSeed(foreignFragment) &&
  hasSelfContainedRiver(foreignFragment) &&
  hasSelfContainedRiver("#s=42&e=1780000000000") &&
  !hasSelfContainedRiver("#s=42") &&
  !hasSelfContainedRiver("#s=42&e=not-an-epoch") &&
  !hasCitedSeed("#s=not-a-seed&t=30") &&
  !hasCitedSeed("#evidence") &&
  foreignReload.seed === foreign.seed &&
  foreignReload.stream === foreign.stream &&
  Math.abs(now(foreignReload) - 30) < 1e-9;

// An s-only URL names a seed but not a stable epoch. Once Project navigation
// hides it, Undo must synthesize an s+e URL from the live river rather than
// reusing the incomplete old fragment.
const sOnly = arrive("#s=42");
const sOnlyExplicit = href(sOnly);
NEXT = 0x2468ace0;
mint();
const sOnlyReload = arrive(sOnlyExplicit.split("#")[1]);
const sOnlyProjectDurable =
  !hasSelfContainedRiver("#s=42") &&
  sOnlyReload.seed === sOnly.seed &&
  sOnlyReload.stream === sOnly.stream &&
  sOnlyReload.epoch === sOnly.epoch;

const restoredScoreUrl = withMode(
  "https://danse.pages.dev/#s=42&t=30&u=7&p=free",
  "program",
);
const restoredFreeUrl = withMode(restoredScoreUrl, "free");
const undoModeDurable =
  modeOf(restoredScoreUrl.split("#")[1]) === "program" &&
  modeOf(restoredFreeUrl.split("#")[1]) === "free" &&
  new URLSearchParams(restoredFreeUrl.split("#")[1]).get("t") === "30";

// A carried backing is a claim about current local storage, not a lease. If
// another tab changes storage, reject the stale carry and use an explicit href.
remember(backing);
const carriedBeforeStorageChange = rememberedRiverForUndo(tOnly, "#t=30", recall());
remember({seed: 9, stream: 10, epoch: 11});
const staleCarry = rememberedRiverForUndo(carriedBeforeStorageChange, "", recall());
const explicitReload = arrive(href(tOnly).split("#")[1]);
const staleBackingRejected =
  staleCarry === null &&
  explicitReload.seed === tOnly.seed &&
  explicitReload.stream === tOnly.stream &&
  explicitReload.epoch === tOnly.epoch;

// ── a named river ─────────────────────────────────────────────────────────────
// `#name=` is the cheap slice of "put yourself into it": the seed comes from
// what you typed rather than from a coin. It must still be YOURS — two people
// typing the same word a millisecond apart cannot be handed the same river —
// while staying reproducible within one instant, which is what makes it a name
// and not just more entropy.
CLOCK = 1780000000000;
const nameA = arrive("#name=madison");
const nameSame = arrive("#name=madison");
CLOCK += 1;
const nameLater = arrive("#name=madison");
CLOCK -= 1;
const nameOther = arrive("#name=chris");
const named =
  nameA.seed === nameSame.seed &&
  nameA.seed !== nameLater.seed &&
  nameA.seed !== nameOther.seed &&
  nameA.minted;

// ── deterministic in its inputs ───────────────────────────────────────────────
const a = mint(), b = mint();
const deterministic = a.seed === b.seed && a.epoch === b.epoch;

// ── both inputs are actually mixed ────────────────────────────────────────────
// The failure this catches is specific: drop the draw and everyone arriving in
// the same millisecond shares a river; drop the epoch and two visitors who type
// the same name into mint() share one.
const N = 4096;
const byDraw = new Set(), byEpoch = new Set();
CLOCK = 1780000000000;
for (let i = 0; i < N; i++) { NEXT = i; byDraw.add(mint().seed); }
NEXT = 0x12345678;
for (let i = 0; i < N; i++) { CLOCK = 1780000000000 + i; byEpoch.add(mint().seed); }

// ── the adversarial arrival: ten thousand people click in the SAME millisecond ─
// Identical epoch means identical t, so every pair occupies the same moment and
// the only thing separating them is the seed. This is the tightest form of the
// claim — a link goes round and everyone opens it at once.
const V = 10000;
CLOCK = 1780000000000;
const seeds = [];
for (let i = 0; i < V; i++) { NEXT = (0x51ed0000 ^ i) >>> 0; seeds.push(mint().seed); }
const distinct = new Set(seeds).size;

// A seed collision must still produce different passage material when the two
// arrivals happened at different epochs.
const collisionSeed = 0x12345678;
const collisionA = state(collisionSeed, 0, program, streamOf(1780000000000));
const collisionB = state(collisionSeed, 0, program, streamOf(1780000000001));
const disambiguated = collisionA.passageSeed !== collisionB.passageSeed;

// ── time only moves forward ───────────────────────────────────────────────────
// The regression this guards is the one that was there until now: a page that
// writes `t` into the address bar and resumes from it is a loop wearing a river's
// clothes. Over a year of wall clocks, t must strictly rise and the passage
// ordinal must never go back.
const river = { seed: 0x1234abcd, epoch: 1780000000000 };
let backwards = 0, rewound = 0, lastT = -Infinity, lastP = -1;
for (let i = 0; i <= 2000; i++) {
  CLOCK = river.epoch + Math.round((i / 2000) * 365 * 86400 * 1000);
  const t = now(river);
  const p = passageAt(program, river.seed, t).index;
  if (t <= lastT) backwards++;
  if (p < lastP) rewound++;
  lastT = t; lastP = p;
}

// ── a shared link is the same water ───────────────────────────────────────────
// Two clients resolving the same link at the same instant must be in the same
// frame, having exchanged nothing. This is the multi-projector claim clock.js
// has asserted in a comment since the beginning; here it is measured.
CLOCK = 1780000000000 + 987654321;
const one = arrive("#s=305419896&e=1780000000000");
const two = arrive("#s=305419896&e=1780000000000");
const synced =
  JSON.stringify(state(one.seed, now(one), program)) ===
  JSON.stringify(state(two.seed, now(two), program));

// ── an aged river still resolves ──────────────────────────────────────────────
// Passage starts cannot be divided out in closed form, so `edgesTo` walks. A
// visitor returning to a river seeded years ago pays that walk once at load, and
// this pins the cost so a change that makes it quadratic fails here rather than
// as a hung tab in a gallery.
const decade = { seed: 0xdecade >>> 0, epoch: 1780000000000 };
CLOCK = decade.epoch + 10 * 365 * 86400 * 1000;
const started = process.hrtime.bigint();
const aged = passageAt(program, decade.seed, now(decade));
const agedMs = Number(process.hrtime.bigint() - started) / 1e6;

console.log(JSON.stringify({
  links, tOnlyUndoDurable, rememberedMatrixPasses, projectUndoDurable, foreignProjectDurable,
  sOnlyProjectDurable, undoModeDurable,
  staleBackingRejected, named, deterministic, byDraw: byDraw.size, byEpoch: byEpoch.size, N,
  visitors: V, distinct, disambiguated, backwards, rewound, synced, agedMs,
  agedPassage: aged.index, samples: 2001,
}));
"""

# The walk for a decade-old river was measured at ~6 ms. The budget is generous
# on purpose: it is a shape check, not a benchmark — linear passes with room to
# spare, quadratic cannot.
AGED_BUDGET_MS = 500.0


def check_arrival() -> None:
    """The visitor's river: minted on arrival, never rejoined at the source."""
    if not ARRIVAL.is_file():
        check("arrival exists", False, f"no {ARRIVAL.relative_to(ROOT)}")
        return

    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(
            ARRIVAL_PROBE
            % {
                "arrival": ARRIVAL,
                "program": ENGINE / "program.js",
                "clock": ENGINE / "clock.js",
                "programJson": PROGRAM,
            }
        )
        probe = Path(fh.name)
    try:
        out = subprocess.run([node(), probe], capture_output=True, text=True, check=False)
        if out.returncode != 0:
            check("arrival evaluates", False, out.stderr.strip().splitlines()[-1] if out.stderr else "node failed")
            return
        r = json.loads(out.stdout)
    finally:
        probe.unlink(missing_ok=True)

    check("a link resolves to the river it names", r["links"], "#s&e joins · #s&t cites · #s starts · bare mints")
    check(
        "a t-only undo restores its recalled backing river",
        r["tOnlyUndoDurable"],
        "undo/reload preserves seed, stream, and the 30-second shifted position",
    )
    check(
        "remembered-river provenance covers every fragment class",
        r["rememberedMatrixPasses"],
        "bare, s-only, t-only, cited, malformed, Project-only, foreign, and absent storage",
    )
    check(
        "Project navigation retains a t-only river's proven backing",
        r["projectUndoDurable"],
        "Project enter → New → Undo → reload returns to the pre-mint backing river",
    )
    check(
        "Project navigation retains a foreign cited river's explicit address",
        r["foreignProjectDurable"],
        "cited s+t+u survives Project enter → New → Undo → reload without local storage",
    )
    check(
        "Project navigation canonicalizes an s-only river",
        r["sOnlyProjectDurable"],
        "s-only enter → Project → New → Undo → reload preserves the synthesized epoch",
    )
    check(
        "Undo reconciles a shifted River URL with the current program mode",
        r["undoModeDurable"],
        "t-only/free → New → score-led → Undo removes stale p=free without changing t",
    )
    check(
        "a cross-tab storage change invalidates carried backing",
        r["staleBackingRejected"],
        "stale carry is rejected and an explicit synthesized href remains reloadable",
    )
    check("arrival is deterministic in its inputs", r["deterministic"], "same draw and epoch, same river")
    check(
        "a named river is yours, not the name's",
        r["named"],
        "same word one millisecond apart is a different river; same instant reproduces",
    )

    n = r["N"]
    check(
        "the draw and the epoch are both mixed into the river",
        r["byDraw"] >= n - 2 and r["byEpoch"] >= n - 2,
        f"{r['byDraw']}/{n} rivers varying only the draw, {r['byEpoch']}/{n} varying only the epoch",
    )

    # Birthday: with V visitors on a 32-bit seed the expected number of colliding
    # pairs is V²/2^33. Stating it here is the point — the bound is real and the
    # piece survives it, rather than being quietly hoped away.
    v = r["visitors"]
    expected = v * v / 2**33
    collisions = v - r["distinct"]
    check(
        "ten thousand arrivals in one millisecond are ten thousand rivers",
        collisions == 0,
        f"{r['distinct']}/{v} distinct — birthday expectation {expected:.3f} collisions",
    )
    check(
        "colliding 32-bit seeds retain distinct passage streams",
        r["disambiguated"],
        "the shared epoch derives a second passage-selection word",
    )
    NOTE.append(
        "the river seed is 32 bits, so distinct rivers cap at 4.3e9 and one collision is expected "
        "around 65,000 visitors. The shared epoch derives a second 32-bit passage-selection word, so "
        "a displayed-seed collision does not become a passage collision."
    )

    check(
        "time only moves forward",
        r["backwards"] == 0 and r["rewound"] == 0,
        f"{r['samples']} wall clocks across a year — {r['backwards']} went back, {r['rewound']} rewound a passage",
    )
    check("a shared link is the same water", r["synced"], "two clients, one frame, nothing exchanged")
    check(
        "a river a decade old still resolves",
        r["agedMs"] < AGED_BUDGET_MS,
        f"passage {r['agedPassage']:,} found in {r['agedMs']:.0f}ms (budget {AGED_BUDGET_MS:.0f}ms)",
    )


# ── 5b. the film is filable ────────────────────────────────────────────────────

REGISTER = APP / "submission" / "screendance-2027.yaml"
OPPORTUNITY_CHECKER = APP / "scripts" / "check-opportunities.py"
OPPORTUNITY_TEST = APP / "scripts" / "tests" / "opportunities.test.py"
RELEASE_CHECKER = APP / "scripts" / "release_contract.py"


def check_opportunities() -> None:
    """The release and urgent filing must share one frozen call registry."""
    try:
        spec = importlib.util.spec_from_file_location(
            "danse_opportunity_invariant", OPPORTUNITY_CHECKER
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("checker module could not be loaded")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        snapshot = checker.validate_registry()
    except ModuleNotFoundError as exc:
        detail = f"missing Python dependency {exc.name!r}; install project dependencies"
        check("every named opportunity has a source-verified disposition", False, detail)
        check("filing consumes the exact frozen opportunity digest", False, "registry unavailable")
        return
    except Exception as exc:
        check("every named opportunity has a source-verified disposition", False, str(exc))
        check("filing consumes the exact frozen opportunity digest", False, "registry invalid")
        return

    tests = subprocess.run(
        [sys.executable, str(OPPORTUNITY_TEST)],
        cwd=APP,
        capture_output=True,
        text=True,
        check=False,
    )
    if tests.returncode:
        output = (tests.stderr or tests.stdout).strip()
        detail = output.splitlines()[-1] if output else f"test process exited {tests.returncode}"
        check("every named opportunity has a source-verified disposition", False, detail)
        check("filing consumes the exact frozen opportunity digest", False, "registry regressions failed")
        return

    check(
        "every named opportunity has a source-verified disposition",
        len(snapshot["opportunities"]) == 17,
        f"{len(snapshot['opportunities'])} targets · {len(snapshot['ranked_actions'])} freeze-time actions",
    )
    try:
        receipt = checker.validate_binding(snapshot)
    except Exception as exc:
        check("filing consumes the exact frozen opportunity digest", False, str(exc))
        return
    check(
        "filing consumes the exact frozen opportunity digest",
        True,
        f"{snapshot['snapshot_id']} · {receipt['snapshot']['sha256'][:16]}…",
    )


def check_release_contract() -> None:
    """The public face has one manifest, and incomplete evidence cannot ship."""
    try:
        spec = importlib.util.spec_from_file_location("danse_release_invariant", RELEASE_CHECKER)
        if spec is None or spec.loader is None:
            raise RuntimeError("release checker module could not be loaded")
        release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release)
        manifest = release.validate_release(APP, phase="draft")
        public = release.phase_blockers(manifest, "public")
        omega = release.phase_blockers(manifest, "release")
    except ModuleNotFoundError as exc:
        detail = f"missing Python dependency {exc.name!r}; install project dependencies"
        check("one manifest owns every public and institutional artifact", False, detail)
        check("public and release phases fail closed on missing evidence", False, "release contract unavailable")
        return
    except Exception as exc:
        check("one manifest owns every public and institutional artifact", False, str(exc))
        check("public and release phases fail closed on missing evidence", False, "release contract invalid")
        return

    check(
        "one manifest owns every public and institutional artifact",
        manifest["schema"] == "danse.release.v1"
        and manifest["opportunity_snapshot"]["sha256"] == release.EXPECTED_OPPORTUNITY_SHA256,
        f"{manifest['release_id']} {manifest['version']} · {manifest['opportunity_snapshot']['sha256'][:16]}…",
    )
    check(
        "public and release phases fail closed on missing evidence",
        manifest["status"] == "draft" and bool(public) and len(omega) > len(public),
        f"{len(public)} public blocker(s), {len(omega) - len(public)} release-only blocker(s)",
    )


def check_submission(program: dict, river: dict) -> None:
    """The delivery format the program declares must be one the call accepts.

    A generative render has NO native frame rate — `f(seed, t)` samples at
    whatever rate it is asked for — so the rate is not a property of the work,
    it is a delivery decision, and the register owns it. Without this check the
    two drift silently and in the expensive direction: the master was declared
    at 60 fps against a register that allows 24 or 30, and the only thing that
    caught it was reading the register in the middle of a 35-minute render.
    """
    if not REGISTER.is_file():
        NOTE.append("no submission register — nothing holds the delivery format to a call")
        return
    try:
        import yaml
    except ImportError:
        NOTE.append("PyYAML absent — the submission register could not be read")
        return
    reg = yaml.safe_load(REGISTER.read_text()) or {}
    spec = (reg.get("package") or {}).get("master") or {}
    allowed = spec.get("fps_allowed")
    captures = {k: v for k, v in program.get("captures", {}).items() if isinstance(v, dict)}

    if allowed:
        wrong = sorted(f"{k}@{v.get('fps')}" for k, v in captures.items() if v.get("fps") not in allowed)
        check(
            "every capture records at a frame rate the call accepts",
            not wrong,
            f"{', '.join(wrong)} — allowed {allowed}" if wrong else f"{len(captures)} captures at {allowed}",
        )

    want = spec.get("aspect")
    submission = captures.get("passage") or {}
    if want and submission.get("w") and submission.get("h"):
        num, den = (float(x) for x in want.split(":"))
        ok = abs(submission["w"] / submission["h"] - num / den) < 0.01
        check(
            "the submission capture is the aspect the call expects",
            ok,
            f"{submission['w']}×{submission['h']} vs {want}",
        )

    # A passage has no fixed length, so the runtime cap applies to the LONGEST
    # one a capture could catch — the worst case, not the nominal.
    cap = next((u.get("assume_max_seconds") for u in reg.get("unstated", []) if u.get("id") == "runtime-cap"), None)
    if cap and river:
        longest = river["maxSeconds"]
        check(
            "the longest passage still fits the assumed runtime cap",
            longest <= cap,
            f"longest observed {longest:.0f}s of {cap}s",
        )


# ── 6. the sound is the same film ──────────────────────────────────────────────

SOUND = APP / "sound"

# Chosen to cross the places the two languages could disagree: zero, a short
# word list, the film's real seed, a value above 2^31 (where JavaScript's `|0`
# makes a number negative and Python's does not), and the 32-bit ceiling.
HASH_CASES = [[0], [1, 2], [20170620, 7, 401], [3735928559, 3, 1, 901], [4294967295, 1], [123, 456, 789, 101112]]

HASH_PROBE = """
import { hash } from "%(rng)s";
console.log(JSON.stringify(%(cases)s.map((c) => hash(...c))));
"""


def check_sound() -> None:
    """The sound selects from the same seed as the picture, out of that room only."""
    if not (SOUND / "score.py").is_file():
        NOTE.append("no score yet — the film is silent")
        return

    # The one that would silently desync sound from picture: the Python port of
    # engine/rng.js must agree with it exactly, or `hash(seed, cell, 401)` picks
    # a different photograph than it picks a grain and nothing lands together.
    sys.path.insert(0, str(SOUND))
    try:
        from rng import hash32
    except ImportError as exc:  # pragma: no cover - a missing file is a real failure
        check("the sound hashes like the picture", False, f"cannot import sound/rng.py — {exc}")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(HASH_PROBE % {"rng": ENGINE / "rng.js", "cases": json.dumps(HASH_CASES)})
        probe = Path(fh.name)
    try:
        out = subprocess.run([node(), probe], capture_output=True, text=True, check=False)
        js = json.loads(out.stdout) if out.returncode == 0 else None
    finally:
        probe.unlink(missing_ok=True)
    if js is None:
        check("the sound hashes like the picture", False, "the JavaScript hash would not evaluate")
    else:
        mine = [hash32(*c) for c in HASH_CASES]
        bad = [c for c, a, b in zip(HASH_CASES, js, mine) if a != b]
        check(
            "the sound hashes like the picture",
            not bad,
            f"{len(bad)} of {len(HASH_CASES)} disagree — {bad[0]}"
            if bad
            else f"{len(HASH_CASES)} cases, rng.js == rng.py",
        )

    with conditional("grain bank"):
        check_bank()


def check_bank() -> None:
    """The grain bank, when one has been cut on this machine.

    Gitignored, like the `film` tier and for the same reason — it is derived from
    2.8 GB of originals that never enter git. Absent is not a failure; wrong is.
    """
    index = SOUND / "bank" / "bank.json"
    if not index.is_file():
        NOTE.append("no grain bank on this machine — build it with sound/1_bank.py")
        return
    expected = None
    try:
        import yaml

        register = yaml.safe_load(REGISTER.read_text()) or {}
        if isinstance(register, dict):
            package = register.get("package") if isinstance(register.get("package"), dict) else {}
            audio = package.get("audio") if isinstance(package.get("audio"), dict) else {}
            recordings = audio.get("source_recordings") or []
            source_digests = audio.get("source_sha256") if isinstance(audio.get("source_sha256"), dict) else {}
            if isinstance(recordings, list) and recordings and all(isinstance(name, str) for name in recordings):
                expected = {name: source_digests.get(name, "") for name in recordings}
    except (ImportError, OSError):
        pass
    except yaml.YAMLError:
        pass
    audit = audit_bank(index, expected)

    check(
        "every grain comes from a confirmed room recording",
        not audit.provenance_errors,
        "; ".join(audit.provenance_errors)
        if audit.provenance_errors
        else f"{audit.grain_count} grains from {len(audit.sources)} recording(s)",
    )
    check(
        "the grain bank index is structurally usable",
        not audit.index_errors,
        "; ".join(audit.index_errors) if audit.index_errors else audit.summary(),
    )
    check(
        "every grain the index names exists",
        not audit.payload_errors,
        "; ".join(audit.payload_errors[:3]) if audit.payload_errors else f"{audit.grain_count} files",
    )


# The three things that would make f(seed, t) a lie. `performance.now` and
# `Date.now` make the render depend on when it ran; `requestAnimationFrame` inside
# engine/ would put a loop where a function belongs.
FORBIDDEN = (
    (re.compile(r"\brequestAnimationFrame\b"), "requestAnimationFrame"),
    (re.compile(r"\bDate\.now\b"), "Date.now"),
    (re.compile(r"\bperformance\.now\b"), "performance.now"),
    (re.compile(r"\bMath\.random\b"), "Math.random"),
)


# The same sources, minus `requestAnimationFrame` — a render loop is exactly what
# a page is supposed to have. These four are what make a result depend on WHEN it
# ran, and outside engine/ they are permitted in precisely one file.
ENTROPY = (
    (re.compile(r"\bDate\.now\b"), "Date.now"),
    (re.compile(r"\bperformance\.now\b"), "performance.now"),
    (re.compile(r"\bMath\.random\b"), "Math.random"),
    (re.compile(r"\bgetRandomValues\b"), "crypto.getRandomValues"),
)

# The one file allowed to know what time it is.
IMPURE = "arrival.js"

# The workstream launcher nests complete Git worktrees below the canonical
# checkout, and private delivery caches may also retain source-shaped files.
# Neither is part of this tree's shipped application. Scanning those ignored
# custody roots would count byte-identical copies of arrival.js as additional
# entropy owners merely because a continuation capsule exists.
NON_SOURCE_ROOTS = frozenset({".git", ".work", ".worktrees", "node_modules"})
SOURCE_SUFFIXES = frozenset({".html", ".js", ".mjs"})


def shipped_source_paths(root: Path = APP, walk=os.walk):
    """Yield entropy-bearing shipped sources without entering custody roots."""
    root = root.resolve()
    for current, directories, filenames in walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if name not in NON_SOURCE_ROOTS and not (current_path == root and name == ENGINE.name)
        )
        for name in sorted(filenames):
            path = current_path / name
            if path.suffix in SOURCE_SUFFIXES:
                yield path


def entropy_hits(root: Path = APP, walk=os.walk) -> dict[str, list[str]]:
    """Return entropy uses keyed by their stable, repository-relative path."""
    found: dict[str, list[str]] = {}
    for path in shipped_source_paths(root, walk):
        text = path.read_text(errors="ignore")
        for rx, label in ENTROPY:
            if rx.search(text):
                found.setdefault(str(path.relative_to(root)), []).append(label)
    return found


def check_entropy_walk_regression() -> None:
    """Lock pruning order and relative diagnostics with a guarded fake walk."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / "src").mkdir()
        (root / "arrival.js").write_text("Date.now();\n")
        (root / "src" / "clock.mjs").write_text("performance.now();\n")
        visited: list[str] = []

        def guarded_walk(path: Path, *, topdown: bool, followlinks: bool):
            check_root = Path(path).resolve()
            assert check_root == root
            assert topdown and not followlinks
            directories = ["src", ".worktrees", "node_modules", "engine", ".work", ".git"]
            visited.append(".")
            yield str(check_root), directories, ["arrival.js"]
            # A top-down walker observes the caller's in-place pruning before it
            # decides which children to enter. Reaching an excluded child here
            # would mean the production traversal still paid its custody cost.
            for directory in directories:
                if directory != "src":
                    raise AssertionError(f"excluded directory remained traversable: {directory}")
                visited.append(directory)
                yield str(check_root / directory), [], ["clock.mjs"]

        found = entropy_hits(root, guarded_walk)
        check(
            "entropy scan prunes custody roots before descent",
            visited == [".", "src"],
            f"visited: {', '.join(visited)}",
        )
        check(
            "entropy diagnostics retain repository-relative paths",
            set(found) == {"arrival.js", "src/clock.mjs"},
            ", ".join(sorted(found)),
        )


def check_purity() -> None:
    hits = []
    for js in sorted(ENGINE.glob("*.js")):
        text = js.read_text()
        for rx, label in FORBIDDEN:
            if rx.search(text):
                hits.append(f"{js.name}:{label}")
    check("no wall-clock or entropy inside engine/", not hits, ", ".join(hits))

    # The converse, and it is the half that makes uniqueness cheap: the engine is
    # pure BECAUSE the impurity has exactly one home. A visitor's river is a clock
    # reading and a coin toss, and if either leaks into a second file there are two
    # answers to "what time is it" and the piece can drift against itself.
    check_entropy_walk_regression()
    found = entropy_hits()
    strays = {name: labels for name, labels in found.items() if name != IMPURE}
    check(
        "entropy lives in exactly one file",
        set(found) == {IMPURE},
        ", ".join(f"{n} ({'/'.join(v)})" for n, v in strays.items())
        if strays
        else f"{IMPURE} — {'/'.join(found.get(IMPURE, []))}"
        if found
        else f"nothing reads a clock, so no visitor can be given a river ({IMPURE} missing)",
    )


# ── 3b. convergence and private custody stay fail-closed ──────────────────────

CONVERGENCE_CHECK = APP / "scripts" / "check-convergence.py"
CONVERGENCE_TEST = APP / "scripts" / "tests" / "convergence.test.py"
PRIVATE_CUSTODY_TEST = APP / "scripts" / "tests" / "private-custody.test.py"
PRIVATE_CUSTODY = APP / "docs" / "continuations" / "alpha-omega" / "private-custody-20260804.json"


def check_convergence_receipts() -> None:
    """Keep repository cleanup subordinate to durable, redacted custody proof."""
    result = subprocess.run(
        [sys.executable, str(CONVERGENCE_CHECK), "--quiet"],
        capture_output=True,
        text=True,
        check=False,
    )
    regressions = subprocess.run(
        [sys.executable, str(CONVERGENCE_TEST)],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout or regressions.stderr or regressions.stdout).strip().splitlines()
    check(
        "the convergence, archive, and session receipts validate adversarially",
        result.returncode == 0 and regressions.returncode == 0,
        detail[-1] if detail else "danse.convergence.v1 plus fail-closed regression suite",
    )

    try:
        custody = load(PRIVATE_CUSTODY)
        required = custody["policy"]["required_independent_verified_copies"]
        violations = []
        for root in custody["roots"]:
            copies = root.get("independent_verified_copies") or []
            valid = [
                copy
                for copy in copies
                if copy.get("verified") is True
                and isinstance(copy.get("medium_id"), str)
                and bool(copy["medium_id"].strip())
                and isinstance(copy.get("manifest_sha256"), str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", copy["manifest_sha256"]))
            ]
            media = {copy["medium_id"].strip() for copy in valid}
            manifests = {copy["manifest_sha256"] for copy in valid}
            restore = root.get("restore_rehearsal") or {}
            acceptance = root.get("human_acceptance") or {}
            eligible = (
                result.returncode == 0
                and len(media) >= required
                and len(manifests) == 1
                and restore.get("ok") is True
                and isinstance(restore.get("receipt"), str)
                and bool(restore["receipt"].strip())
                and acceptance.get("ok") is True
                and isinstance(acceptance.get("receipt"), str)
                and bool(acceptance["receipt"].strip())
                and root.get("tracked_tree_clean") is True
            )
            cleanup_authorized = root.get("cleanup_authorized")
            if not isinstance(cleanup_authorized, bool) or (cleanup_authorized is True and not eligible):
                violations.append(root.get("id", "unnamed"))
        check(
            "private custody cannot be reclaimed before copy, restore, and acceptance proof",
            not violations,
            ", ".join(violations) if violations else f"{len(custody['roots'])} material roots remain fail-closed",
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        check(
            "private custody cannot be reclaimed before copy, restore, and acceptance proof",
            False,
            str(exc),
        )

    snapshot_regressions = subprocess.run(
        [sys.executable, str(PRIVATE_CUSTODY_TEST)],
        capture_output=True,
        text=True,
        check=False,
    )
    snapshot_detail = (snapshot_regressions.stderr or snapshot_regressions.stdout).strip().splitlines()
    check(
        "private custody snapshots duplicate and restore exact source and material bytes",
        snapshot_regressions.returncode == 0,
        snapshot_detail[-1]
        if snapshot_detail
        else "clean tracked source, private manifest, two-copy identity, and restore",
    )


# ── 4. every frame the score names is deliverable ──────────────────────────────


def check_delivery(score: dict, manifest: dict) -> None:
    ids = {f["id"] for f in manifest["frames"]}
    named = {Path(layer["src"]).stem for tile in score["tiles"] for layer in tile["layers"]}
    check("every scored frame is in the manifest", named <= ids, f"missing {sorted(named - ids)[:4]}")

    # A `local` tier (the 3264px film plates) exists only on the machine that
    # built it and is gitignored on purpose. Requiring it here would fail every
    # checkout that has not rendered a film.
    shipped = [name for name, spec in manifest["tiers"].items() if not spec.get("local")]
    missing = []
    for tier in shipped:
        for fid in sorted(named):
            if not (CORPUS / "plates" / tier / f"{fid}.webp").is_file():
                missing.append(f"{tier}/{fid}")
    check(
        "every scored frame has a plate at every shipped tier",
        not missing,
        f"{len(missing)} missing, e.g. {missing[:3]}" if missing else f"tiers: {', '.join(shipped)}",
    )

    room = CORPUS / manifest["room"]["file"]
    check("the recovered room plate ships", room.is_file(), str(room.relative_to(ROOT)))

    # Projective texturing addresses every fragment through the 2017 camera's
    # matrix. A frame shot on anything else is registered to nothing — it may
    # appear in the solved score, because the 2017 cut genuinely used one, but a
    # generated cut reaching for it would sample a photograph of a phone screen
    # as though it were the room.
    declared = [f for f in manifest["frames"] if "registered" in f]
    total = len(manifest["frames"])
    check(
        "every frame declares whether it is registered to the 2017 camera",
        len(declared) == total,
        f"{len(declared)}/{total}" + ("" if len(declared) == total else " — rebuild with pipeline/4_corpus.py"),
    )
    strangers = [f["id"] for f in manifest["frames"] if f.get("registered") is False]
    orphans = [fid for fid in strangers if fid not in named]
    check(
        "unregistered frames are only ever there because the 2017 cut used them",
        not orphans,
        f"{', '.join(orphans)} is unregistered AND unused — drop it"
        if orphans
        else (f"{', '.join(strangers)} — in the score, withheld from generated cuts" if strangers else "none"),
    )


def main() -> int:
    if not (CORPUS / "score-2017.json").is_file():
        print("no corpus — run pipeline/4_corpus.py first", file=sys.stderr)
        return 1
    score = load(CORPUS / "score-2017.json")
    manifest = load(CORPUS / "manifest.json")

    print("danse invariants\n")
    print(" the score partitions the frame")
    check_partition(score)
    print("\n the clock is a pure f(seed, t) that returns to flat")
    probe = check_clock()
    check_purity()
    if PROGRAM.is_file():
        print("\n the program partitions time")
        check_program(load(PROGRAM), set(probe.get("cuts", [])), probe.get("river") or {})
        if probe:
            check_film(probe)
        check_submission(load(PROGRAM), probe.get("river") or {})
        print("\n every visitor gets their own river")
        check_arrival()
    else:
        NOTE.append(f"no film program at {PROGRAM.relative_to(ROOT)} — the piece runs free, nothing is cut")
    print("\n repository convergence retains private custody")
    check_convergence_receipts()
    print("\n the release follows one frozen opportunity registry")
    check_opportunities()
    print("\n the public face is phase-gated from one release manifest")
    check_release_contract()
    print("\n the progressive controls are one interface")
    check_interface_contract()
    print("\n the corpus is deliverable")
    check_delivery(score, manifest)
    print("\n the sound is the same film")
    check_sound()
    print("\n the score clock is shared by sound and image")
    check_music_contract()
    print("\n rights and attribution bind the exact work")
    check_rights_contract()
    print("\n sound and image occupy one deterministic room")
    check_room_event_contract()
    print("\n the reference room can become an evidence-bound installation")
    check_installation_contract()

    # Counted BEFORE these checks run, so the floor never counts itself and the
    # numbers below stay the number of real invariants.
    portable = sum(1 for _, group in RUN if group is None)
    ran_in = {name: sum(1 for _, g in RUN if g == name) for name in CONDITIONAL}

    print("\n the net is still the net")
    check(
        f"no portable invariant has been deleted (floor {FLOOR})",
        portable >= FLOOR,
        f"{portable} ran on this machine"
        + (
            "" if portable >= FLOOR else f" — {FLOOR - portable} missing; restore them or argue the removal in the diff"
        ),
    )
    # A conditional group is allowed to be absent — this is the machine without the
    # artifact, not a broken net. It is NOT allowed to be present and short, and it
    # is never allowed to be silent: an unstated skip is how 42 became 39 without
    # anyone noticing.
    for name, want in CONDITIONAL.items():
        got = ran_in[name]
        if got == 0:
            NOTE.append(
                f"{want} invariant(s) need the {name}, which this machine does not have — "
                f"expected on CI and on any checkout without it. The portable floor above is "
                f"unaffected; do not lower it to make the totals agree."
            )
            continue
        check(
            f"the {name} invariants are all still here (floor {want})",
            got >= want,
            f"{got} ran" + ("" if got >= want else f" — {want - got} missing"),
        )

    for n in NOTE:
        print(f"\n  note: {n}")

    print()
    if FAIL:
        print(f"danse: {len(FAIL)} invariant(s) broken — {', '.join(FAIL)}")
        return 1
    print("danse: every invariant holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
