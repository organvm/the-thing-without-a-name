/** The river as declared data — read, validated, and sampled at any t, forever.
 *
 * This is NOT a film. A film has a duration and an end, and rendering one locks
 * the piece into the single passage that happened to be recorded. The work is
 * the engine running: the same shape, never the same water.
 *
 * So time here is unbounded, and what the program declares is a PHRASE — an
 * order of movements and how the camera behaves through them. The phrase is
 * traversed over and over, and each traversal is a PASSAGE with its own seed and
 * its own durations. Same argument, different photographs, different pacing, and
 * a passage that has gone by does not come back.
 *
 *     passage 0    ONE 34s · ASSEMBLY 27s · DIVISION 51s · …   seed 0x8F21C4
 *     passage 1    ONE 51s · ASSEMBLY 22s · DIVISION 64s · …   seed 0x2D90AB
 *     passage 2    ONE 39s · ASSEMBLY 31s · DIVISION 44s · …   seed 0xC1078E
 *
 * Movements declare a `share` of the passage rather than a start and an end, and
 * the shares are NORMALISED to the passage length. That is what keeps the old
 * partition invariant true: the 2017 score tiles the frame with no gaps and no
 * overlaps, a passage tiles its own time the same way, and now it does so by
 * construction rather than by arithmetic that a human kept in agreement by hand.
 *
 * Durations vary per passage for one specific reason: a fixed phrase length is a
 * clock. A viewer who stays twenty minutes would learn that the black frame
 * arrives every six and a half minutes, and from then on would be watching a
 * loop with the fill changed. Varying the durations removes the thing there is
 * to anchor to.
 *
 * Nothing here holds state that affects a result. A program is a value; every
 * function is a pure function of (program, seed, t).
 */

import { hash, range } from "./rng.js";

/** Channels a movement interpolates. Every one is `[from, to]`; a movement that
 *  omits one holds it at zero, which is the flat, on-axis, untreated state. */
export const CHANNELS = ["divergence", "spread", "azimuth", "elevation", "projK", "turnover"];

/** Cuts a movement may name. `black` renders nothing — it exists so the passage's
 *  closing signature is part of the declared partition rather than a special case
 *  bolted onto the end of the renderer. */
export const CUTS = ["solo", "score", "grid", "bands", "figure", "black"];

const PASSAGE_TIMING_SCHEMA = "danse.program.passage-timing.v1";

const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);

/** A minimal clock contract for a score-free, fixed-length passage.
 *
 * This value deliberately carries no score cues, notes, choreography, or other
 * visual inputs.  It can only replace the generated passage length so an A/B
 * control shares the selected film's clock without sharing its authored motion.
 */
export function fixedPassageTiming(seconds) {
  if (!Number.isFinite(seconds) || !(seconds > 0)) {
    throw new TypeError("fixed passage timing must be finite and positive");
  }
  return Object.freeze({
    schema: PASSAGE_TIMING_SCHEMA,
    mode: "fixed-passage",
    seconds,
  });
}

function fixedTimingSeconds(timing) {
  if (timing === null || timing === undefined) return null;
  if (!timing || typeof timing !== "object" || Array.isArray(timing)
      || Object.keys(timing).sort().join(",") !== "mode,schema,seconds"
      || timing.schema !== PASSAGE_TIMING_SCHEMA
      || timing.mode !== "fixed-passage"
      || !Number.isFinite(timing.seconds)
      || !(timing.seconds > 0)) {
    throw new TypeError("invalid fixed passage timing contract");
  }
  return timing.seconds;
}

export const EASE = {
  linear: (x) => x,
  smooth: (x) => x * x * (3 - 2 * x),
  in: (x) => x * x,
  out: (x) => 1 - (1 - x) * (1 - x),
};

export async function load(url = "render/program.json") {
  const program = await fetch(url).then((r) => {
    if (!r.ok) throw new Error(`program ${r.status} at ${url}`);
    return r.json();
  });
  validate(program);
  return program;
}

/** Throws on anything that would render silently wrong. */
export function validate(program) {
  const bad = (msg) => {
    throw new Error(`program: ${msg}`);
  };
  if (program.schema !== "danse.program.v2") bad(`unknown schema ${program.schema}`);
  const moves = program.movements;
  if (!Array.isArray(moves) || !moves.length) bad("no movements");

  const p = program.passage;
  if (!p || !(p.seconds > 0)) bad("passage.seconds must be positive");
  if (!(p.vary >= 0 && p.vary < 1)) bad("passage.vary must be in [0, 1)");

  for (const m of moves) {
    if (!(m.share > 0)) bad(`${m.id} has non-positive share`);
    if (!(m.vary >= 0 && m.vary < 1)) bad(`${m.id}.vary must be in [0, 1)`);
    if (!CUTS.includes(m.cut)) bad(`${m.id} names unknown cut ${m.cut}`);
    if (!(m.ease in EASE)) bad(`${m.id} names unknown ease ${m.ease}`);
    for (const c of CHANNELS) {
      if (m[c] === undefined) continue;
      if (!Array.isArray(m[c]) || m[c].length !== 2) bad(`${m.id}.${c} must be [from, to]`);
    }
    for (const r of m.reseeds ?? []) {
      if (r < 0 || r >= 1) bad(`${m.id} reseed ${r} outside [0, 1)`);
    }
  }

  // The partition, asserted rather than assumed. Normalisation should make this
  // exact for every passage; if a change to the weighting ever breaks it, it
  // breaks here rather than as a black frame in the middle of a gallery.
  for (let n = 0; n < 8; n++) {
    const laid = movementsIn(program, program.seed ?? 0, n);
    const seconds = passageSeconds(program, program.seed ?? 0, n);
    let cursor = 0;
    for (const m of laid) {
      if (Math.abs(m.t0 - cursor) > 1e-6) bad(`passage ${n}: ${m.id} starts at ${m.t0}, expected ${cursor}`);
      if (!(m.t1 > m.t0)) bad(`passage ${n}: ${m.id} has non-positive duration`);
      cursor = m.t1;
    }
    if (Math.abs(cursor - seconds) > 1e-6) bad(`passage ${n} ends at ${cursor}, its length is ${seconds}`);
  }

  for (const [name, c] of Object.entries(program.captures ?? {})) {
    if (name === "_doc") continue;
    // A capture is either a fixed number of seconds (Times Square wants exactly
    // 170) or a whole number of passages — and a passage has no fixed length, so
    // those are genuinely different requests, not two spellings of one.
    if (!(c.seconds > 0) && !(c.passages > 0)) bad(`capture ${name} declares neither seconds nor passages`);
    if (c.seconds > 0 && c.passages > 0) bad(`capture ${name} declares both seconds and passages`);
  }
  return program;
}

// ── passages ───────────────────────────────────────────────────────────────────

/** The seed of passage n. Every passage is its own film. */
export function passageSeed(seed, n, stream = 0) {
  return stream ? hash(seed, 0x1a1, stream, n) : hash(seed, 0x1a1, n);
}

function nativeSeconds(score, timing = null) {
  const hasScore = score !== null && score !== undefined;
  const hasTiming = timing !== null && timing !== undefined;
  if (hasScore && hasTiming) {
    throw new TypeError("a passage cannot use score and timing-only clocks together");
  }
  if (hasScore && (!score || typeof score !== "object" || Array.isArray(score))) {
    throw new TypeError("invalid music score contract");
  }
  const fixed = fixedTimingSeconds(timing);
  if (fixed !== null) return fixed;
  return score?.time?.passage_mapping === "native-tempo" ? score.time.duration_seconds : null;
}

/** How long passage n runs. Drawn from the passage's own seed, never fixed. */
export function passageSeconds(program, seed, n, stream = 0, score = null, timing = null) {
  const native = nativeSeconds(score, timing);
  if (native !== null) return native;
  const { seconds, vary } = program.passage;
  return seconds * range(1 - vary, 1 + vary, passageSeed(seed, n, stream), 0x2b2);
}

/** Cumulative passage edges, extended on demand.
 *
 * Passage lengths vary, so the start of passage n is a sum over every passage
 * before it and cannot be divided out in closed form. This walks, and keeps what
 * it walked. It is memoisation, not state: the edges are a pure function of
 * (program, seed) and re-deriving them always gives the same numbers — which is
 * what `check-danse.py` verifies by evaluating the clock out of order.
 */
let edgeCache = { program: null, seed: null, stream: null, score: null, timing: null, edges: [0] };

function edgesTo(program, seed, stream, score, timing, t) {
  if (edgeCache.program !== program || edgeCache.seed !== seed || edgeCache.stream !== stream
      || edgeCache.score !== score || edgeCache.timing !== timing) {
    edgeCache = { program, seed, stream, score, timing, edges: [0] };
  }
  const { edges } = edgeCache;
  while (edges[edges.length - 1] <= t) {
    const n = edges.length - 1;
    edges.push(edges[n] + passageSeconds(program, seed, n, stream, score, timing));
  }
  return edges;
}

/** Which passage covers t, when it began, how long it runs, and its seed. */
export function passageAt(program, seed, t, stream = 0, score = null, timing = null) {
  const at = Math.max(0, t);
  if (!Number.isFinite(at) || at > 100 * 365 * 86400) throw new RangeError(`passage time is outside the 100-year bound: ${t}`);
  const native = nativeSeconds(score, timing);
  if (native !== null) {
    const index = Math.floor(at / native);
    return { index, t0: index * native, seconds: native, seed: passageSeed(seed, index, stream) };
  }
  const edges = edgesTo(program, seed, stream, score, timing, at);
  let lo = 0;
  let hi = edges.length - 1;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (edges[mid] <= at) lo = mid;
    else hi = mid;
  }
  return {
    index: lo,
    t0: edges[lo],
    seconds: edges[lo + 1] - edges[lo],
    seed: passageSeed(seed, lo, stream),
  };
}

/** The movements of passage n, laid out over its own length.
 *
 * Each share is jittered by the passage's seed and then the whole set is scaled
 * to fit. Scaling is what makes the tiling exact: the shares do not have to sum
 * to anything in particular, so no edit to this file can leave a hole in time.
 */
export function movementsIn(program, seed, n, stream = 0, score = null, timing = null) {
  const pseed = passageSeed(seed, n, stream);
  const moves = program.movements;
  const weights = moves.map((m, i) => m.share * range(1 - m.vary, 1 + m.vary, pseed, i, 0x3c3));
  const total = weights.reduce((s, w) => s + w, 0);
  const seconds = passageSeconds(program, seed, n, stream, score, timing);

  const out = [];
  let cursor = 0;
  for (let i = 0; i < moves.length; i++) {
    // The last movement takes exactly the remainder, so accumulated float error
    // lands nowhere rather than as a sliver of dead air before the passage ends.
    const span = i === moves.length - 1 ? seconds - cursor : (weights[i] / total) * seconds;
    out.push({ ...moves[i], t0: cursor, t1: cursor + span });
    cursor += span;
  }
  return out;
}

/** The movement covering t anywhere in the river, and how far into it we are. */
export function movementAt(program, seed, t, stream = 0, score = null, timing = null) {
  const passage = passageAt(program, seed, t, stream, score, timing);
  const laid = movementsIn(program, seed, passage.index, stream, score, timing);
  const local = Math.max(0, t) - passage.t0;
  for (let i = 0; i < laid.length; i++) {
    const m = laid[i];
    if (local < m.t1 || i === laid.length - 1) {
      return { movement: m, index: i, u: clamp01((local - m.t0) / (m.t1 - m.t0)), passage };
    }
  }
  return { movement: laid[0], index: 0, u: 0, passage };
}

/** One channel of a movement at local progress u, already eased. */
export function channel(movement, name, u) {
  const span = movement[name];
  if (!span) return 0;
  const e = EASE[movement.ease](u);
  return span[0] + (span[1] - span[0]) * e;
}

/** Which reseed epoch we are in. 0 until the first declared reseed passes, so a
 *  movement with no reseeds always reports 0 and leaves the material alone. */
export function epochAt(movement, u) {
  let epoch = 0;
  for (const r of movement.reseeds ?? []) {
    if (u >= r) epoch++;
  }
  return epoch;
}

/** A named capture preset — how long to record and in what format.
 *
 * A capture is a RECORDING of the river, not the work. It has a length and a
 * format and a place it starts, and it is named by the passage it caught.
 */
export function captureOf(program, name = "passage") {
  const c = program.captures?.[name];
  if (!c) throw new Error(`program: no capture "${name}"`);
  return { name, ...c };
}

/** Select a capture by passage ordinals, never by stepping over an edge epsilon. */
export function captureSpan(program, seed, from, capture, stream = 0, score = null, timing = null) {
  if (capture.seconds > 0) return { t0: from, t1: from + capture.seconds };
  const opening = passageAt(program, seed, from, stream, score, timing);
  let t1 = opening.t0;
  for (let offset = 0; offset < capture.passages; offset++) {
    t1 += passageSeconds(program, seed, opening.index + offset, stream, score, timing);
  }
  return { t0: opening.t0, t1 };
}
