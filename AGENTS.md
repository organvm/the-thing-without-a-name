# THE THING WITHOUT A NAME — agent contract

This repository is the canonical home of Danse. Read this file before changing the
engine, corpus, renderer, sound system, delivery trunk, or submission package.

## Verification

Run the portable batch once per exact tree:

```bash
python3 scripts/check-danse.py
python3 scripts/tests/danse-delivery.test.py
python3 -m py_compile render/*.py pipeline/*.py sound/*.py submission/*.py scripts/*.py
node --check sound/control.mjs
bash -n done.sh
```

The first command enforces the executable portable-invariant ratchet declared as
`FLOOR` in `scripts/check-danse.py`; do not duplicate that changing number here. A
hydrated local grain bank adds three explicitly conditional checks. Never lower a
floor or a quality threshold to make a change pass.

The machine-bound visual batch is:

```bash
python3 render/browser.py --check --verify --arrival --probe
```

It requires macOS, Chrome, and Apple Metal. It proves the 31.60 dB reproduction,
visitor arrival, and projection continuity. The terminal package predicate is:

```bash
./done.sh --package <package-root> --phase package
```

## Invariants that define the work

- The engine is a pure `f(seed, t)`: no clock, entropy, accumulated state, or
  `requestAnimationFrame` inside `engine/`.
- Entropy has exactly one home, `arrival.js`.
- The flat state is the 25 July 2017 composite. Never weaken its measured threshold.
- A capture is a recording of one passage, never the unbounded work itself.
- The engine seed remains 32-bit; the sound and visual RNGs stay value-identical.
- Every delivered pixel and sample retains exact photographic or recording provenance.

## Custody and publication

- Never commit `.work/`, raw photographs, private recordings, `sound/sources.json`,
  generated packages, credentials, or personal paths.
- The tracked corpus is the publishable derivative corpus. Raw/private inputs hydrate
  only ignored roots from the exact private lock in `assets/` and remain in external
  archival custody; remote availability never changes their publication status.
- Use topic branches and pull requests for future changes. Do not rewrite a reviewed
  exact head merely because the base moved.
- Current source lineage is machine-readable in `LINEAGE.json`. Future work belongs in
  this repository's issues, with an executable predicate and durable receipt target.

The piece, architecture, and run instructions are in [`README.md`](README.md). Dates,
formats, and phase ownership are canonical only in
[`submission/screendance-2027.yaml`](submission/screendance-2027.yaml).
