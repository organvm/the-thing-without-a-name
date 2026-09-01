/** Arrival — the one impure module. Everything downstream of it is f(seed, t).
 *
 * The engine has no duration and no end: it traverses a phrase forever, and each
 * traversal is a passage with its own seed and its own length. That makes the
 * PIECE unrepeatable. It does not, on its own, make a VISIT unrepeatable — a page
 * that opens on a fixed seed at t=0 hands every visitor the same water.
 *
 * So a visitor's river is two numbers, both of them made by the act of showing up:
 *
 *     seed    a draw from the platform CSPRNG, mixed with the epoch
 *     epoch   the wall-clock millisecond at which the river was seeded
 *
 *     t = (now − epoch) / 1000        the river has been flowing since it began
 *
 * Everything follows from that. Time only moves one way, so a returning visitor
 * rejoins DOWNSTREAM and never at the source — close the tab, come back in an
 * hour, and your river ran for that hour without you. The pair is enough to
 * reconstruct the moment exactly, so a shared link is a shared river: two people
 * on opposite coasts, or four projectors on four walls, see the same water at the
 * same instant with nothing exchanged between them.
 *
 * There is no `t` variable anywhere above this module. EVERY navigation — hold,
 * jump to a movement, cite a moment — is an epoch shift or a freeze, which is why
 * the page below never accumulates a clock of its own and cannot drift.
 *
 * The impurity is deliberately confined here, outside engine/, and both halves of
 * that are checked: `check-danse.py` fails if entropy or a wall clock appears in
 * engine/, AND if it appears anywhere in the app other than this file.
 */

import { hash } from "./engine/rng.js";

/** Where the river is kept between visits. */
export const KEY = "danse.river";

/** The clock and the entropy, in one object so a probe can replace them.
 *
 * This seam is what makes an impure module checkable: the predicate substitutes a
 * counter for `draw` and a fixed instant for `now`, and then arrival is an
 * ordinary pure function that can be held to the same standard as the engine.
 */
export const platform = {
  now: () => Date.now(),
  draw: () => {
    const a = new Uint32Array(1);
    globalThis.crypto.getRandomValues(a);
    return a[0];
  },
};

/** localStorage, or null. Reading it THROWS outright in some privacy modes, so
 *  the guard has to wrap the access and not merely the write. */
function store() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/** The seed's grain: a name if one was given, entropy if not.
 *
 * Codepoints rather than characters, so an accent or an emoji in a name is not
 * silently collapsed into a different river than the one the visitor typed.
 */
function grainOf(word) {
  const w = (word ?? "").trim();
  if (!w) return platform.draw();
  return hash(...Array.from(w, (c) => c.codePointAt(0)));
}

/** Derive a river seed from a grain and the instant it was drawn.
 *
 * The epoch goes in as two halves because it exceeds 32 bits. Mixing it is what
 * keeps a NAMED river unique: two visitors who type the same word must not be
 * handed the same river, and without the epoch they would be.
 *
 * The result is 32 bits and stays so. `rng.js` is 32-bit by design — JavaScript
 * has no native 64-bit integer arithmetic — `sound/rng.py` is held to it value
 * for value, and `hex()` prints it as the signature. Widening the input does not
 * widen the output: distinct rivers cap at 4.3e9, and the birthday bound puts one
 * expected seed collision at roughly 65,000 visitors.
 *
 * The epoch also derives an independent stream discriminator below. The public
 * engine seed remains 32 bits, while passage selection receives the discriminator
 * so two visitors whose displayed seeds collide still see different water.
 */
export function riverOf(grain, epoch) {
  return hash(grain, epoch >>> 0, Math.floor(epoch / 4294967296));
}

/** The second half of a river identity, reproducible from a shared epoch. */
export function streamOf(epoch) {
  return hash(epoch >>> 0, Math.floor(epoch / 4294967296), 0x57ea9);
}

/** Mint a fresh river and keep it. `word` is optional and names it. */
export function mint(word = null) {
  const epoch = platform.now();
  const river = { seed: riverOf(grainOf(word), epoch), stream: streamOf(epoch), epoch };
  remember(river);
  return river;
}

/** The river kept from a previous visit, or null. */
export function recall() {
  const s = store();
  if (!s) return null;
  try {
    const raw = JSON.parse(s.getItem(KEY) ?? "null");
    if (!raw || !Number.isFinite(raw.seed) || !Number.isFinite(raw.epoch)) return null;
    const stream = Number.isFinite(raw.stream) ? raw.stream >>> 0 : streamOf(raw.epoch);
    return { seed: raw.seed >>> 0, stream, epoch: raw.epoch };
  } catch {
    return null;
  }
}

/** Keep a river. Only `mint` calls this — a river arrived at by a URL belongs to
 *  whoever sent it, and a river shifted for debugging is not a new river. */
export function remember(river) {
  const s = store();
  if (!s) return;
  try {
    s.setItem(KEY, JSON.stringify({ seed: river.seed, stream: river.stream, epoch: river.epoch }));
  } catch {
    /* quota, private mode, disabled — the river still runs, it just isn't kept */
  }
}

/** Identify the durable local-storage river that Undo must restore.
 *
 * A `#t=`-only arrival shifts the recalled river's epoch, so object equality is
 * intentionally insufficient: its seed and stream still identify the backing
 * river that the fragment will shift again after reload. A cited `#s&t` river
 * belongs to its sender and must never overwrite the visitor's stored river.
 */
export function rememberedRiverForUndo(river, fragment, remembered) {
  if (!remembered || river.seed !== remembered.seed || river.stream !== remembered.stream) {
    return null;
  }
  if (!river.shifted) return river.epoch === remembered.epoch ? remembered : null;
  const query = new URLSearchParams(String(fragment ?? "").replace(/^#/, ""));
  const rawSeed = query.get("s");
  const hasCitedSeed = rawSeed !== null && Number.isFinite(Number(rawSeed));
  return query.has("t") && !hasCitedSeed ? remembered : null;
}

/** How far into a river we are, right now. */
export function now(river) {
  return (platform.now() - river.epoch) / 1000;
}

/** A river whose `now` is exactly `at`. Every navigation in the piece is this:
 *  there is no clock to set, only a birthday to move. */
export function shiftTo(river, at) {
  return { seed: river.seed, stream: river.stream ?? 0, epoch: platform.now() - at * 1000 };
}

/** Resolve an arrival into the river it should show.
 *
 * Precedence, highest first:
 *
 *   #s & #e    someone handed you their river — you join it where they are
 *   #s & #t    a cited moment: the same seed, wound to that offset and running
 *   #s         a river named by seed alone. No birthday, so it starts at its
 *              source — which is what `#s=20170620` means and why a citation of
 *              the archival river reproduces from the top
 *   stored     yours, still flowing, from whenever you first arrived
 *   —          first arrival: mint one
 *
 * `#t` on its own winds YOUR river without renaming it. Anything reached by `#t`
 * is marked `shifted`, and the page stops writing the address bar while it is —
 * a debugging position must not be able to overwrite the river you keep.
 */
export function arrive(fragment = globalThis.location?.hash ?? "") {
  const q = new URLSearchParams(fragment.replace(/^#/, ""));
  const num = (k) => {
    const raw = q.get(k);
    if (raw === null) return null;
    const v = Number(raw);
    return Number.isFinite(v) ? v : null;
  };

  const s = num("s");
  const e = num("e");
  const tRaw = q.get("t");
  const t = num("t");
  if (tRaw !== null && (t === null || t < 0)) throw new Error(`invalid cited time: ${tRaw}`);
  const u = num("u");

  if (s !== null && e !== null) {
    return { seed: s >>> 0, stream: u === null ? streamOf(e) : u >>> 0, epoch: e, minted: false, shifted: false };
  }
  if (s !== null && t !== null) {
    return { ...shiftTo({ seed: s >>> 0, stream: u === null ? 0 : u >>> 0, epoch: 0 }, t), minted: false, shifted: true };
  }
  if (s !== null) return { seed: s >>> 0, stream: 0, epoch: platform.now(), minted: false, shifted: false };

  const kept = recall();
  const named = q.get("name");
  const river = kept && !named ? kept : mint(named);
  if (t !== null) return { ...shiftTo(river, t), minted: !kept, shifted: true };
  return { ...river, minted: !kept || Boolean(named), shifted: false };
}

/** A link to a river, or to one moment of it. They are different objects.
 *
 *   href(river)              your river, live and still flowing — the recipient
 *                            lands in the same water you are in, in sync
 *   href(river, { at })      a moment, cited — the frame that reproduces exactly
 *                            what you saw, wherever they open it
 */
export function href(river, { at = null, mode = null } = {}) {
  const base = (globalThis.location?.href ?? "").split("#")[0];
  const link = at === null
    ? `${base}#s=${river.seed}&e=${river.epoch}&u=${river.stream ?? 0}`
    : `${base}#s=${river.seed}&t=${String(at)}&u=${river.stream ?? 0}`;
  return mode === "free" ? `${link}&p=free` : link;
}

export function modeOf(fragment = globalThis.location?.hash ?? "") {
  return new URLSearchParams(fragment.replace(/^#/, "")).get("p") === "free" ? "free" : "program";
}
