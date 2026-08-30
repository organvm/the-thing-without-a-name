/** One step of the piece. The live page and the film both come through here.
 *
 * This module exists so there is exactly one answer to "what is on screen at
 * (seed, t)". A browser tab at 60fps, an offline renderer walking 23,400 frames
 * out of order across two processes, and a still exported for Instagram all call
 * `step` and all get the same frame — because `step` is a pure function and the
 * clock underneath it has no memory.
 *
 * The only thing the live page does differently is `quantise`, and that is a
 * parameter rather than a fork: casting cells walks 162 index entries per cell,
 * which at 60fps is work nobody can see. Geometry and camera still move every
 * frame; only the CHOICE is held. The film passes 0 and pays for exactness.
 */

import { state } from "./clock.js";
import { poseAt } from "./choreography.js";
import { cells } from "./grammar.js";
import { passageAt } from "./program.js";

/** What is on screen at (seed, t). `program` null runs the piece free. */
export function step(
  corpus,
  seed,
  t,
  program = null,
  {
    quantise = 0,
    stream = 0,
    score = null,
    choreography = null,
    conductor = null,
    timing = null,
  } = {},
) {
  const hasScore = score !== null && score !== undefined;
  const hasChoreography = choreography !== null && choreography !== undefined;
  const hasTiming = timing !== null && timing !== undefined;
  if (hasChoreography && hasTiming) throw new Error("timing-only clocks cannot admit choreography");
  if (hasChoreography && !hasScore) throw new Error("score-led choreography requires a music score");
  if (hasScore && hasTiming) throw new Error("score and timing-only clocks are mutually exclusive");
  if (hasTiming && !program) throw new Error("timing-only clocks require a bounded program");
  // A bounded film supplies a passage window; the free river deliberately does
  // not.  Both still query the same score/choreography contract, so changing the
  // visitor mode cannot silently remove the conducted panel score.
  const posePassage = hasChoreography && program ? passageAt(program, seed, t, stream, score) : null;
  const pose = hasChoreography
    ? poseAt(score, choreography, seed, t, posePassage ? { t0: posePassage.t0, seconds: posePassage.seconds } : null, conductor)
    : null;
  const s = state(seed, t, program, stream, score, pose, timing);
  // The state is always sampled at the exact t; only the cast may be held.
  const ct = quantise > 0 ? Math.floor(t / quantise) * quantise : t;
  const castAt = s.turnoverAt ?? ct;
  const cast = cells(corpus, s.material, castAt, {
    reveal: s.reveal,
    cut: s.cut,
    rate: s.turnover,
    pose,
  });
  return { state: s, cast, pose };
}

/** Step and draw. Returns the renderer's stats plus the state that produced them. */
export function frameAt(renderer, corpus, seed, t, program = null, opts = {}) {
  const {
    quantise = 0,
    stream = 0,
    score = null,
    choreography = null,
    conductor = null,
    timing = null,
    ...draw
  } = opts;
  const { state: s, cast } = step(corpus, seed, t, program, {
    quantise,
    stream,
    score,
    choreography,
    conductor,
    timing,
  });
  // The signature is not an overlay added afterwards — it is the last movement,
  // and it comes through the same canvas as every frame before it.
  const closing =
    s.cut === "black" && program?.signature
      ? { signature: signature(program, s), signatureStyle: program.signature }
      : {};
  return { ...renderer.draw(cast, s, { seed, ...closing, ...draw }), state: s, cells: cast.length };
}

/** The seed as it appears in the film's last frame, in a post caption, and in a
 *  still's filename. One spelling everywhere, so the number a viewer reads off
 *  the screen is the number that reproduces what they saw. */
export function hex(seed) {
  return `0x${(seed >>> 0).toString(16).toUpperCase().padStart(6, "0")}`;
}

/** The closing line of a passage: what it was, and which one it was.
 *
 * A passage is named by its seed AND its ordinal. The seed alone is 32 bits and
 * would eventually repeat — around 65,000 passages, which a gallery reaches in
 * about nine months — and a receipt that can be issued twice for two different
 * things is not a receipt. The ordinal also says something the seed cannot: that
 * this is the 1,552nd time the phrase has been through, and it will not be that
 * one again.
 */
export function signature(program, state) {
  const sig = program?.signature;
  const seed = state?.passageSeed ?? 0;
  if (!sig) return hex(seed);
  return sig.format
    .replace("%SEED%", hex(seed).replace(/^0x/, ""))
    .replace("%RIVER_SEED%", hex(state?.riverSeed ?? 0).replace(/^0x/, ""))
    .replace("%RIVER_STREAM%", hex(state?.riverStream ?? 0).replace(/^0x/, ""))
    .replace("%PASSAGE_T0%", Number(state?.passageT0 ?? 0).toFixed(3))
    .replace("%PASSAGE%", String(state?.passage ?? 0));
}
