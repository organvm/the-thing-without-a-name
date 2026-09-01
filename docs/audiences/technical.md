# Danse for engineers and media technologists

[General edition](general.md) · [Humanities edition](humanities.md) ·
[Presenter edition](business.md) · [Evidence and limits](../evidence/README.md) ·
[Full project record](../../README.md)

## System boundary

Danse is a static browser application with an offline media-production and
release toolchain. Its central invariant is a pure visual engine:

```text
frame = f(seed, absolute_time)
```

Clock reads and entropy are isolated to `arrival.js`. The engine carries no
accumulated frame state and schedules no animation internally. Browser, capture,
and installation surfaces ask for a state; they do not advance one by mutation.

## Component map

| Boundary | Responsibility | Primary paths |
|---|---|---|
| Arrival | Create or recover a visitor river; own wall-clock and entropy access | `arrival.js` |
| Pure engine | Seeded RNG, phrase clock, grammar, room state, and draw objects | `engine/` |
| Browser renderer | WebGL presentation and projective texturing | `index.html`, `engine/renderer.js` |
| Interaction adapter | Optional local pose input, keyboard/touch fallback, bounded modulation, receipt replay | `interaction/` |
| Corpus | Publishable derivative imagery, masks, plates, manifest, and 2017 score | `corpus/` |
| Media production | Deterministic offline rendering, score compilation, delivery, and package checks | `render/`, `music/`, `sound/`, `submission/` |
| Installation | Reference twin, calibration/evidence contracts, runtime, and simulator | `installation/` |
| Release control | Phase-gated public artifacts, rights register, and allowlisted Pages build | `release/`, `rights/`, `scripts/build-pages.py` |

## Data flow

1. `arrival.js` provides a 32-bit seed and epoch or parses an explicit seed/time
   citation.
2. The clock resolves absolute river time and the engine computes the requested
   state directly.
3. The grammar selects registered photographic regions; the renderer resolves
   them through a shared room projector.
4. Optional interaction is reduced to bounded controls outside the pure engine.
5. Browser, offline film, and reference installation surfaces consume the same
   deterministic state under different delivery contracts.

## Corpus and provenance

`corpus/manifest.json` records 162 items: 161 registered photographs from the
20 June 2017 session and one unregistered archival composite. Raw/private inputs
do not enter Git. The checked-in corpus is the publishable derivative surface;
private originals hydrate ignored local roots under the custody contract.

The reconstructed 2017 composite is represented by
`corpus/score-2017.json`. The long-form [README](../../README.md) documents the
rate/distortion sweep and projection probe; those numerical claims should be
read with their exact source files and machine requirements.

## Privacy and security boundaries

- Camera use is opt-in. The browser requests permission only after the visitor
  selects the local camera path.
- Raw frames and landmarks are not retained or transmitted by the declared
  interaction architecture.
- Keyboard and touch remain available without a camera.
- Interaction receipts contain bounded controls, are explicitly downloaded by
  the visitor, and fail closed when replayed against another river.
- The MediaPipe runtime/model is vendored and attributed; no CDN request is
  required for inference.
- Release builders use allowlists and phase gates so draft source and uncleared
  media do not become public artifacts merely because a build ran.

## Run locally

The living browser work is static:

```bash
python3 -m http.server 8080
```

Then open `http://localhost:8080/`.

The repository's portable verification contract is:

```bash
python3 scripts/check-danse.py
python3 scripts/tests/danse-delivery.test.py
python3 -m py_compile render/*.py pipeline/*.py sound/*.py submission/*.py scripts/*.py
node --check sound/control.mjs
bash -n done.sh
```

Do not copy a changing test-count floor into documentation. The checker owns
that ratchet. Additional CI exercises Pages, release, rights, interaction, and
score-motion contracts.

## Machine-bound verification

The visual batch below requires macOS, Chrome, Apple Metal, and hydrated local
material:

```bash
python3 render/browser.py --check --verify --arrival --probe
```

A portable Linux pass does not establish those machine-bound results. Likewise,
a green installation simulator does not establish venue calibration or a
physical room test.

## Failure modes and fail-closed behavior

| Failure | Boundary |
|---|---|
| Clock or entropy appears outside `arrival.js` | Portable checker fails |
| Engine accumulates state or schedules animation | Portable invariant fails |
| Interaction receipt targets another river | Replay rejects it |
| Camera is denied or lost | Visible state and keyboard/touch fallback remain |
| Release evidence is missing or mismatched | Public/release build stops before output or deployment |
| Venue, hardware, calibration, or recovery receipt is absent | Installation remains reference-only |
| Private/raw material appears in tracked delivery paths | Custody and allowlist checks reject it |

## Current technical status

The source system is active. A public Pages artifact is evidenced at commit
`f19244a`; the canonical branch has advanced since that receipt. The public
project package remains `draft`, and the current physical-installation ledger is
blocked. See the [claim ledger](../evidence/README.md) before translating an
implemented component into a deployment, clearance, or outcome claim.
