/** Where the piece is at time t. Pure, seeded, and with no memory.
 *
 * Everything the renderer needs for a frame is `state(seed, t)`. Nothing
 * accumulates, so seeking to 4:31 costs the same as seeking to 0:01, an offline
 * renderer and a browser tab agree exactly, and two projectors driven from the
 * same seed stay in lock without talking to each other.
 *
 * The dramaturgy comes out of the probe's third result — that the flattening is
 * the CAMERA, not the arrangement. Stand where the camera stood on 20 June 2017
 * and the picture collapses back into the 2017 composite no matter how far the
 * planes have swung apart. So `divergence` is the whole reveal, and it is built
 * to RETURN to zero rather than to rise once:
 *
 *   divergence  0 ─╮      ╭──────╮      ╭─  the still is not a beginning, it is
 *                  ╰──────╯      ╰──────╯   a recurring event in the animation
 *
 * At every trough the room folds back into the photograph he made. The piece
 * keeps arriving at its own origin and leaving again.
 */

import { channel, epochAt, movementAt, movementsIn, passageAt } from "./program.js";
import { hash, range } from "./rng.js";
import { noteFieldAt, scoreAt } from "./score.js";

/** Seconds for one full departure-and-return. Long enough that the return reads
 *  as recognition rather than as a pulse. */
export const PERIOD = 74;

/** How long a plane holds one photograph before crossing to the next. Prime
 *  against PERIOD so the two cycles never phase-lock into a visible pattern. */
export const HOLD = 11;

/** Fraction of HOLD spent cross-fading. The dissolve is not a transition effect —
 *  it is the same two-layer compositing the 2017 piece already needed for its 77
 *  translucent tiles, run over time instead of over the picture plane. */
export const CROSSFADE = 0.34;

const TAU = Math.PI * 2;

/** Smooth, monotone 0→1. Used for the reveal so it eases out of and back into
 *  the flat state rather than arriving at it with a velocity. */
const smooth = (x) => x * x * (3 - 2 * x);

/** A trough-and-crest that spends real time at 0. `rest` is the fraction of the
 *  cycle held flat, which is what makes the 2017 composite legible as an image
 *  rather than as a moment passed through. */
function dwell(phase, rest = 0.16) {
  const p = phase - Math.floor(phase);
  if (p < rest / 2 || p > 1 - rest / 2) return 0;
  const t = (p - rest / 2) / (1 - rest);
  return smooth(Math.sin(t * Math.PI)); // 0 → 1 → 0
}

/** The state of the piece at (seed, t), optionally under a program.
 *
 * Without a program the piece runs free — the endless departure-and-return above,
 * which is what the live page and the gallery wall want. With one, the movements
 * declared in `render/program.json` drive every channel and the result is a film:
 * still a pure function of (seed, t), so an offline renderer can seek anywhere,
 * render segments out of order, and get bit-identical frames.
 */
export function state(seed, t, program = null, stream = 0, score = null, pose = null, timing = null) {
  const hasScore = score !== null && score !== undefined;
  const hasPose = pose !== null && pose !== undefined;
  const hasTiming = timing !== null && timing !== undefined;
  if (hasScore && hasTiming) throw new Error("score and timing-only clocks are mutually exclusive");
  if (hasTiming && hasPose) throw new Error("timing-only clocks cannot admit choreography poses");
  if (hasPose && !hasScore) throw new Error("choreography poses require a music score");
  if (hasTiming && !program) throw new Error("timing-only clocks require a bounded program");
  return program
    ? programState(seed, t, program, stream, score, pose, timing)
    : freeState(seed, t, score, pose);
}

/** Under a program, every channel is interpolated across its movement, and the
 *  MATERIAL seed changes at declared reseed points — which is how the closing
 *  movement restarts the engine with entirely different photographs while the
 *  structural moves stay the same. */
function programState(seed, t, program, stream, score, pose, timing) {
  let movement, index, u, passage, music;
  if (score !== null && score !== undefined) {
    passage = passageAt(program, seed, t, stream, score);
    music = {
      ...scoreAt(score, t, { t0: passage.t0, seconds: passage.seconds }),
      note_field: noteFieldAt(score, t, { t0: passage.t0, seconds: passage.seconds }),
    };
    if (pose) {
      index = program.movements.findIndex((candidate) => candidate.id === pose.movement_id);
      movement = program.movements[index];
      if (!movement) throw new Error(`choreography movement ${pose.movement_id} does not exist in the program`);
      if (movement.cut !== pose.cut_mode) {
        throw new Error(`choreography movement ${pose.movement_id} cut ${pose.cut_mode} does not match program cut ${movement.cut}`);
      }
      u = pose.movement_u;
    } else {
      index = music.movement.index;
      movement = movementsIn(program, seed, passage.index, stream, score)[index];
      if (!movement || movement.id !== music.movement.id) {
        throw new Error(`music score movement ${music.movement.id} does not match program movement ${movement?.id}`);
      }
      u = music.movement.u;
    }
  } else {
    ({ movement, index, u, passage } = movementAt(program, seed, t, stream, null, timing));
  }
  const epoch = pose ? 0 : epochAt(movement, u);
  const arrivingMovement = pose?.transition.kind === "topology"
    ? program.movements.find((candidate) => candidate.id === pose.next_movement_id)
    : null;
  const musicalChannel = (name) => {
    let value = channel(movement, name, u);
    if (arrivingMovement && arrivingMovement !== movement) {
      const progress = pose.transition.progress;
      value += (channel(arrivingMovement, name, 0) - value) * progress;
    }
    // Score cues remain authored audio/control evidence, but photographic
    // choreography owns the camera continuously. A step-valued cue offset here
    // would reintroduce a one-frame geometry jump at its onset.
    return value + (pose ? 0 : (music?.visual.channel_offsets[name] ?? 0));
  };
  const divergence = musicalChannel("divergence");

  let turnoverRate = musicalChannel("turnover");
  let turnoverAt = null;
  let materialIndex = index;
  let materialEpoch = epoch;
  let materialRecast = music?.visual.recast;
  if (!pose && music?.visual.hold) {
    const holdCue = music.cues
      .filter((cue) => cue.visual.hold)
      .reduce((latest, cue) => (!latest || cue.second > latest.second ? cue : latest), null);
    if (!holdCue) throw new Error("music score declares a hold without an active hold cue");
    turnoverAt = passage.t0 + holdCue.second * music.scale;
    const heldMusic = scoreAt(score, turnoverAt, { t0: passage.t0, seconds: passage.seconds });
    const heldMovement = movementsIn(program, seed, passage.index, stream, score)[heldMusic.movement.index];
    if (!heldMovement || heldMovement.id !== heldMusic.movement.id) {
      throw new Error(`music score hold movement ${heldMusic.movement.id} does not match program movement ${heldMovement?.id}`);
    }
    turnoverRate = channel(heldMovement, "turnover", heldMusic.movement.u)
      + (heldMusic.visual.channel_offsets.turnover ?? 0);
    materialIndex = heldMusic.movement.index;
    materialEpoch = epochAt(heldMovement, heldMusic.movement.u);
    materialRecast = heldMusic.visual.recast;
  }

  // Every passage draws from its own seed, so the phrase recurs and the material
  // never does. The passage ORDINAL goes into the derivation too: a 32-bit seed
  // has a birthday bound around 65,000 passages — roughly nine months of
  // continuous running — and without the ordinal a gallery could, eventually,
  // show the same passage twice. Including it makes recurrence impossible rather
  // than merely unlikely, which is the whole claim the piece makes.
  const material = pose
    ? hash(passage.seed, passage.index, index)
    : music
    ? hash(passage.seed, passage.index, materialEpoch, materialIndex, materialRecast)
    : hash(passage.seed, passage.index, epoch, index);

  // Seeded drift on top of the programmed arc, so two seeds trace different paths
  // through the same dramaturgy rather than the same path twice.
  const w = movement.wander ?? 0;
  const drift = (k) => (!pose && w ? Math.sin((t / range(6.5, 13.5, seed, index, k) + range(0, 1, seed, index, k + 1)) * TAU) * w : 0);

  const result = {
    t,
    riverSeed: seed,
    riverStream: stream,
    // `reveal` is only a legibility signal — it tells the grammar the room is
    // open. Under a program the cut is declared, so nothing infers it from here.
    reveal: divergence,
    divergence,
    azimuth: musicalChannel("azimuth") + drift(111),
    elevation: musicalChannel("elevation") + drift(113),
    spread: musicalChannel("spread"),
    projK: musicalChannel("projK"),
    // Everything the grammar needs to cast this frame.
    cut: pose?.cut_mode ?? movement.cut,
    // A hold retains the exact cast at its deterministic cue onset. Setting a
    // rate to zero would instead reset turnover() to epoch zero and recast it.
    turnover: pose ? 0 : turnoverRate,
    turnoverAt,
    movement: movement.id,
    epoch,
    material,
    sceneOpacity: pose?.next_cut_mode === "black" ? 1 - pose.blend : 1,
    // Which passage of the river this is, and its name. The signature frame
    // prints both: a passage is identified by its seed AND its ordinal, so the
    // receipt is unique for as long as the piece runs.
    passage: passage.index,
    passageSeed: passage.seed,
    passageSeconds: passage.seconds,
    passageT0: passage.t0,
  };
  if (music) result.music = music;
  if (pose) result.choreography = pose;
  return result;
}

/** The free-running piece: no program, no end. Unchanged behaviour — the flat
 *  state at t=0 and at every PERIOD is what `check-danse.py` asserts. */
function freeState(seed, t, score = null, pose = null) {
  const phase = t / PERIOD;
  const reveal = dwell(phase);

  // Azimuth and elevation drift on their own slow, seed-dependent periods, so
  // successive departures leave in different directions and no two returns are
  // approached from the same side.
  const aPeriod = range(0.31, 0.53, seed, 101);
  const ePeriod = range(0.17, 0.29, seed, 102);
  const aPhase = range(0, 1, seed, 103);
  const ePhase = range(0, 1, seed, 104);

  const result = {
    t,
    riverSeed: seed,
    reveal,
    // The camera leaves the projector's eye. This alone un-flattens the picture.
    divergence: reveal * range(0.55, 0.95, seed, 105),
    azimuth: Math.sin((phase * aPeriod + aPhase) * TAU) * 0.85,
    elevation: Math.sin((phase * ePeriod + ePhase) * TAU) * 0.34,
    // Geometry departs slightly AHEAD of the camera, so the arrangement is
    // already built by the time the move begins to disclose it — invisible while
    // on-axis, which is exactly what the probe proved is possible.
    spread: dwell(phase + 0.045),
    // 0 = every plane is a window onto the room. Held there: `projK = 1` makes
    // each plane carry its own crop, which duplicates the poster row. That is a
    // real state of the piece, but it is a departure from the room, not the room.
    projK: 0,
    // The same fields a programmed state carries, so no consumer has to ask which
    // kind of clock it is holding. Free-running, the cut is inferred from `reveal`
    // and the material never reseeds.
    cut: pose?.cut_mode ?? null,
    turnover: 1,
    movement: pose?.movement_id ?? null,
    epoch: 0,
    material: seed,
    // Free-running there are no passages — the piece departs and returns on one
    // continuous breath rather than in phrases. Declared anyway, so a consumer
    // never has to ask which kind of clock it is holding.
    passage: 0,
    passageSeed: seed,
    passageSeconds: PERIOD,
    passageT0: Math.floor(t / PERIOD) * PERIOD,
  };
  if (score !== null && score !== undefined) {
    result.music = {
      ...scoreAt(score, t),
      note_field: noteFieldAt(score, t),
    };
  }
  if (pose) result.choreography = pose;
  return result;
}

/** Which photograph a cell is showing at time t, and what it is crossing to.
 *
 * Each cell gets its own phase offset from its id, so the corpus turns over
 * continuously across the picture rather than all at once — the room is always
 * partly changing and never entirely.
 */
export function turnover(id, seed, t, rate = 1) {
  // rate 0 freezes the corpus — movement ONE holds a single photograph for forty
  // seconds, and a cell that crossfades under it would break that claim.
  if (rate <= 0) return { epoch: 0, next: 0, mix: 0 };
  const offset = range(0, 1, seed, id, 201);
  const p = (t * rate) / HOLD + offset;
  const epoch = Math.floor(p);
  const frac = p - epoch;

  if (frac < 1 - CROSSFADE) return { epoch, next: epoch + 1, mix: 0 };
  return { epoch, next: epoch + 1, mix: smooth((frac - (1 - CROSSFADE)) / CROSSFADE) };
}
