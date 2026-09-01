# THE THING WITHOUT A NAME

> **Danse** is a seeded, browser-based photographic artwork that turns one 2017
> dancer session and a hand-cut composite into an unbounded, reproducible room
> of moving image and sound.

[Enter the public artwork](https://danse.pages.dev/)
· [Start without technical background](docs/audiences/general.md)
· [Inspect the production system](docs/audiences/technical.md)
· [Read the humanities interpretation](docs/audiences/humanities.md)
· [Review evidence and limits](docs/evidence/README.md)

## What am I looking at?

This is the canonical source repository and project record for Danse. A
repository is an organized collection of the artwork's code, publishable image
corpus, production contracts, tests, documentation, and revision history. It is
both a place from which the browser work can run and a record against which its
technical and publication claims can be checked.

The public Pages artwork is evidenced at source commit `f19244a`. The broader
public project package remains **draft**: film approval, rights, installation,
accessibility, custody, and publication gates are still open. A working browser
artwork is not evidence of a finished festival package or a tested physical
installation.

## Choose your reading path

| I am reading as... | Begin with... |
|---|---|
| A general reader or first-time GitHub visitor | [What Danse is and what happens when you enter it](docs/audiences/general.md) |
| An engineer or media technologist | [Architecture, execution, tests, privacy, and failure boundaries](docs/audiences/technical.md) |
| A humanities scholar, artist, critic, or educator | [Time, photographic history, simultaneity, and computational form](docs/audiences/humanities.md) |
| A curator, presenter, or production partner | [Presentation forms, requirements, readiness, and unresolved gates](docs/audiences/business.md) |
| An evaluator, collaborator, or hiring reader | [Contribution boundaries and inspection paths](docs/audiences/evaluator.md) |

## Project at a glance

| | |
|---|---|
| **What it is** | A deterministic generative image engine, a living browser artwork, and production tooling for finite captures and a proposed room installation. |
| **Source material** | 161 registered photographs from one 20 June 2017 session plus one unregistered archival composite. |
| **What a visitor receives** | A locally stored river identity and an evolving view whose exact moments can be cited by seed and time. |
| **Implementation state** | Active source and test system; public Pages artwork evidenced at an earlier source commit. |
| **Release state** | Draft. The public project package, festival package, and physical installation remain gated. |
| **Tracked project identity** | Anthony J. Padavano is named as the artist. Performer credit and several authorship/rights approvals remain pending. |
| **Evidence** | [Claim ledger](docs/evidence/README.md), source, tests, manifests, release gates, rights register, and lineage record. |
| **Machine-readable facts** | [`project-record.yml`](project-record.yml) |

## Canonical long-form project record

The remainder of this README preserves the project's existing full explanation,
technical argument, and production instructions.

A room that never repeats, built from one afternoon.

On **20 June 2017**, 161 photographs were made during one dancer session — an
apartment room with a row of framed classic-horror posters standing against the wall,
carpet, a guitar. The camera barely moved. On **25 July 2017**, material from that
session was cut apart by hand and recomposed into a tiled composite: fragments of her,
at different scales and opacities, over one continuous room.

Then it sat for nine years.

This is the machine that does it now — and doesn't stop.

## What it is

A seeded generative engine. Photographs hang as translucent planes at different depths
and angles in a 3D room; the engine selects fragments — anatomy, not rectangles — from
different frames of that afternoon and composes them. It never repeats, and every state
it can reach has a number.

Five faces, one engine:

| Face | What it is |
|---|---|
| **The river** | The work itself: the engine running, unbounded, never the same water. |
| **The passage** | One traversal of the declared phrase, with its own seed and its own length. |
| **The capture** | A recording of the river. Named by the passage it caught, never mistaken for the piece. |
| **The visitor** | Their own river, minted on arrival, kept, and shareable. |
| **The room** | The same engine driving real projectors onto real hanging scrim. |

### Final Evolution

1. **User Interaction**: An opt-in, local pose adapter now turns presence, position,
   openness, reach, dwell, and a small crowd into bounded room modulation. Keyboard and
   touch controls exercise the same contract without a camera.
2. **Spatial Sound Triggering**: Sound derived from the room/space that each generation of panel/slice triggers between the background's XY axes as material assembles and moves.

## Arriving is the seed

The piece has no duration and no end. It traverses a declared **phrase** forever, and each
traversal is a **passage** with its own seed, its own material and its own length — so a
passage that has gone by does not come back.

That makes the *piece* unrepeatable. What makes a *visit* unrepeatable is `arrival.js`: a
visitor's river is two numbers made by the act of showing up.

```
seed    a draw from the platform CSPRNG, mixed with the epoch
epoch   the wall-clock millisecond it was drawn

t = (now − epoch) / 1000      the river has been flowing since it began
```

Time only moves one way, so a returning visitor rejoins **downstream** and never at the
source: close the tab, come back in an hour, and your river ran for that hour without you.
The river is kept in `localStorage` under `danse.river`, so it is *yours* across visits —
what does not repeat is the water, not the riverbed.

Two links, and they are different objects:

| Link | What it hands over |
|---|---|
| `#s=<seed>&e=<epoch>` | **Your river**, live and still flowing. The recipient lands in the same water at the same instant, having exchanged nothing with you but those two numbers. |
| `#s=<seed>&t=<seconds>` | **One moment**, cited. The frame that reproduces exactly what you saw, wherever it is opened. |
| `#s=<seed>` | A river named by seed alone — no birthday, so it starts at its source. `#s=20170620` is the archival one. |
| `#p=free` | The older free-running dwell cycle, which `verify.html` pins the 2017 reproduction to. |

The address bar is written once a second and deliberately **never** carries `t`: persisting
it would make a reload resume where you left, which is a loop wearing a river's clothes.

`arrival.js` is the only file in the app permitted to read a clock or draw entropy, and both
halves of that are checked — `check-danse.py` fails if either appears inside `engine/`, and
also if either appears anywhere else in the app. The engine stays a pure `f(seed, t)`;
uniqueness costs it nothing.

## Local embodied interaction

Open **Controls** and choose either **Use camera locally** or **Use keyboard / touch**.
The river remains complete with interaction off. Camera permission is requested only by
that button; denial, missing hardware, no person, device loss, reconnect, and stop are
distinct visible states, and the fallback remains available throughout.
That native range-control fallback is also the low-power and pose-accessibility path.

Pose inference uses a vendored Apache-2.0 MediaPipe runtime and model. It makes no CDN
request. Video frames stay in the hidden local capture element, raw landmarks are reduced
immediately to anonymous controls, and neither is retained or transmitted. A visitor may
explicitly download a bounded ten-minute JSON receipt containing only those controls, then
replay it against the same river by absolute river time. Receipts from another river fail
closed.

The adapter wraps the renderer; it does not enter `engine/`. With no visitor—or while a
reduced-motion frame is held—the renderer receives the engine's original state and draw
objects unchanged. A live visit and a receipt replay therefore query the same pure
`f(seed, t)` frame and apply the same deterministic modulation outside it.

| Derived input | Bounded room response |
|---|---|
| horizontal / vertical position | camera azimuth / elevation |
| openness and crowd | divergence / plane spread |
| reach and dwell | carried-picture and figure-matte emphasis |
| no person or dropout | short deterministic fade to the untouched river |

## The three decisions

**Projective texturing, not per-plane UVs.** Every photograph is registered to one room
frame, and every fragment samples through a shared room-projector matrix. Two planes at
different depths and angles therefore place the floor line and the poster line on the
*same screen-space lines* — the continuity is a property of how pixels are fetched, not
a rule the generator has to remember.

**Two independent axes, not one.** The still opens into a room along *geometry* — planes
leaving the picture plane for angles and depths — and along `projK`, one uniform mixing
plane-local UVs against projector UVs. `projK = 0` makes a plane a **window**: it shows
whatever the room casts onto wherever it now is, so its content changes as it moves.
`projK = 1` makes it a **carried picture**: it holds its assigned crop and takes it
along. At the home position the two are *numerically identical*, which is why the 2017
composite is ambiguous between collage and room — and why the flattening is really the
**camera**, not `projK`. Stand where the camera stood and the composite returns no matter
what the planes are doing.

**The engine is a pure `f(seed, t)`.** No accumulated state, no `requestAnimationFrame`
inside `engine/`. That single property buys deterministic film renders, O(1) seek,
shareable permalinks, and multi-projector sync for free.

## What the corpus turned out to be

Measured, not assumed — and it changed the design:

- The tracked corpus contains **162 records: 161 registered raw photographs and one
  unregistered archival composite**. Of the raw photographs, 160 carry a person matte
  and `IMG_1570` is dancer-free; the archival composite also carries a person matte.
- **Body-pose detection finds joints in only 65**, and never reaches 8 confident ones.
  The histogram says why: knees 40%, ankles 37%, hips 35% — then shoulders 3%, faces 2%.
  **The shoot frames legs.** There is no upper body for a whole-person model to anchor on.
- So the **matte is the primary instrument** and pose is an optional refinement. Gating
  on pose would have thrown away 60% of a corpus in which the subject is unmistakably
  present.
- **The camera is locked off.** The poster row sits at identical pixel coordinates across
  frames, which makes registration nearly free and is exactly why the 2017 hand-cuts
  aligned so cleanly.
- **Although the archival composite is not a registered raw camera frame, its
  architecture aligns to the room to within 0.4% of frame height.** Its horizontal
  seams (0.4622, 0.4857) land on the poster-rail transition measured independently from
  the dancer-free `IMG_1570` (0.4661, 0.4886). The artist's own rule was *cut on the
  architecture* — so the engine derives its bands from the room rather than inventing
  a grid.

## The 2017 piece, solved

Before evolving it, recreate it. Stage 3 does not approximate the composite — it **solves
it back into a score** against the 162-record corpus: which source record each region
was drawn from, and what treatment was applied. The model per rectangle is

```
C  =  gain · S  +  lift          (per colour channel, least squares)
```

which is not merely noise-tolerant. Normal-blending a photograph over a light ground at
opacity `a` is exactly `gain = a, lift = (1-a)·ground`, and desaturating is exactly a
per-channel spread in gain. Several tiles come back at `gain ≈ 0.64, lift ≈ 0.36` — pairs
summing to 1.0. The solver was never told about opacity; it fitted a line, and the line
came back as the 2017 hand-treatment in the two numbers a shader takes.

**The result** — [`corpus/score-2017.json`](corpus/score-2017.json), 256 rectangles,
**32.3 dB PSNR**, mean absolute error 0.015:

| rectangles | 32 | 64 | 128 | 256 | 384 | 512 |
|---|---|---|---|---|---|---|
| **PSNR (dB)** | 25.98 | 29.29 | 31.18 | 32.27 | 33.11 | 33.59 |
| **sources used** | 21 | 34 | 48 | 77 | 90 | 110 |

Reading the curve: the piece is *about a hundred rectangles* — past that, fidelity is
bought a fifth of a dB at a time. Which is a statement about how much grammar the engine
actually needs.

![reconstruction](reference/reconstruction-comparison.png)

*Left, the 2017 composite as it was cut by hand. Centre, the same picture re-derived from
the 162-record corpus by the solver at 256 rectangles — 32.3 dB. Right, where each region
came from. The middle panel is not a filter applied to the left one: every output region
is resolved through declared source layers and placed by a number.*

What the solve found:

- **77 of 256 rectangles need two source layers**, at a 15% error-reduction threshold.
  The composite is not a mosaic of opaque tiles; roughly a third of its area is two
  photographs superimposed, and a one-source model produces *diagonal* residual ridges
  where a translucent limb crosses the frame beneath it.
- **77 distinct corpus sources are in play: 76 registered raw photographs and the
  unregistered archival composite.** The distribution is steep: `IMG_1611` alone
  accounts for 17.5% of the picture and `IMG_1615` another 14.3%.
- **The major horizontal band edges land at 0.500 and 0.799** of frame height. Stage 2's
  independent seam measurement of the same composite said 0.486 and 0.802, and the room's
  own poster rail — measured on the one dancer-free frame — sits at 0.489. Three
  measurements, three methods, one architecture.

![provenance](reference/score-2017-provenance.png)

*The provenance map: one hue per source record. The current solve draws on 76 registered
raw photographs and the archival composite. It is also the fastest correctness check
available: a real solve reads as flat contiguous plates, a failed one reads as noise.*

## The projection holds — go/no-go

One claim carries the design: that photographs hung on planes at unrelated angles and
depths still read as **one room**, because every fragment fetches pixels through a single
shared projector matrix instead of through its own surface. If it were false, the piece
would be a pile of floating cutouts and the architecture would have to change before
anything else was built — so [`probe.html`](probe.html) tests it first, against the real
256-rectangle score rather than a toy.

The projector stands where the camera stood on 20 June 2017, and casts a stand-in plate
carrying the room's measured horizontals (0.489 and 0.802). Those two lines *are* the
experiment: if they stay straight across 256 tumbling rectangles, the claim holds.

![projection probe](reference/projection-probe.png)

Three results:

- **The self-test is exact.** A tile's rect is precisely its share of the projector
  frustum, so at the home position the window path and the carried-picture path must
  resolve to the same texel. Sweeping `projK` across all 256 tiles changes **max Δ 0/255**
  — bit-identical, not merely within tolerance. The home state is the 2017 composite by
  construction, not by tuning.
- **Continuity survives arbitrary geometry.** At `spread = 0.85` the planes tumble through
  depth and rotation, and the poster rail is still one straight line crossing every one of
  them, verticals still plumb, posters still at true scale and registration.
- **The flattening is the camera.** With the arrangement fully exploded, standing at the
  projector still returns the flat composite — projective texturing looks painted-on from
  the projector's own viewpoint. So the reveal needs no geometry animation at all: *walking
  away from where the photograph was taken is what un-flattens it.*

That third result is the piece. It also fixes the film's dramaturgy: the arrangement can be
built up invisibly while the camera is on-axis, and revealed by a move rather than a cut.

## Three grammars, one operation

The transmutation practice this engine generalises is older and wider than the ballet
piece. Analytic cubism's actual move is not angular shapes, it is **simultaneity**:
several viewpoints of one subject coexisting in one picture plane. The 2017 works are
three cut-geometries over that identical operation —

| Work | Corpus | Cut |
|---|---|---|
| **danse** | 161 registered raw photographs + 1 archival composite | rectangular grid, aligned to the room's architecture |
| **noonlight** | 21 frames, one face turning | polygonal shards with white kerf, over sky |
| **b/w remix** | supplied frames, one face | staggered bands keyed to anatomy — eyes, lips, hair, arm |

Different scissors, same cut. So `engine/grammar.js` carries a **cut vocabulary** rather
than a hard-coded grid, and the seed chooses among the geometries.

And this is why the room is not decoration. Picasso flattened his viewpoints into the
picture plane because a canvas has no depth to hang them in. Screens at different angles,
depths and transparencies put them back. `projK = 1` is literally that flattening;
animating it toward `0` is literally its undoing.

## Layout

```

  index.html   the living page          film.html    capture harness (no UI, no rAF)
  arrival.js   the ONE impure module — a visitor's river, and the only clock
  interaction/ bounded local pose, fallback, receipt replay · vendored runtime/model
  probe.html   projection go/no-go       interaction-test.html   browser contract
  engine/      gl · mat4 · rng · room · room-events · grammar · renderer · corpus · clock · program
  music/       layered repertoire provenance · MIDI compiler · fixture and production score contracts
  sound/       shared room-event bus · stereo/WebAudio/offline/multichannel plans
  installation/ reference twin · calibration/evidence contracts · foreground recovery runtime
  corpus/      score-2017.json · manifest.json · plates/ · masks/
  pipeline/    corpus preparation (local only, never deployed)
  render/      deterministic offline renderer (local only, never deployed)
  release/     one phase-gated manifest for project, pitch, access, press, and media
```

## Pipeline

Runs on this machine, against Photos.app. Originals never enter git — `.work/` is
ignored, and only the code that regenerates everything is versioned.

```bash
cd pipeline
./0_export.sh                      # Photos ▸ etcetera ▸ ballerina danse ▸ danse → .work/raw
./1_vision/build.sh                # dependency-free Swift + Vision.framework
./1_vision/danse-vision .work/raw .work/vision
python3 2_measure_transmutation.py ../reference/T-2017-full.png \
        --room-frame .work/raw/IMG_1570.JPG -o .work/reference/transmutations.json
python3 3_reconstruct.py --target ../reference/T-2017-full.png \
        --frames .work/raw --depth 2 --leaves 256 -o .work/reference/score-danse.json
python3 3_reconstruct.py ... --sweep 32,64,128,256,384,512   # rate/distortion curve
python3 4_corpus.py --limit 8       # isolated smoke corpus under .work/corpus-smoke-8
```

## Delivery

The submission register owns formats and phase-specific human acts. Generated media,
package attestations, and private sources remain ignored; the tracked prose in
`submission/text/` is the package source.

```bash
# After export + Vision hydration, build the ignored full-camera tier.
python3 pipeline/4_corpus.py --tiers film --skip-room

# Read-only: validates the exact dependency plan and source denominator.
python3 render/deliver.py --preflight \
  --out <scratch-render-root> --package <package-root>

# Build one default passage (seed 20170620, absolute start 0).
python3 render/deliver.py \
  --out <scratch-render-root> --package <package-root>

# Text and origin builds do not invoke the renderer or score.
python3 render/deliver.py --only text --only origin \
  --out <scratch-render-root> --package <package-root>

# Cumulative validation; later phases add only the acts they own.
done.sh --package <package-root> --phase package
done.sh --package <package-root> --phase uploaded
done.sh --package <package-root> --phase submitted
```

Run media commands in a Python environment providing NumPy, SciPy, Pillow, and
Playwright. `deliver.py --preflight` reports missing modules, the Metal Chrome
surface, the film tier, declared audio dependencies, and the registered origin
photograph without creating output directories.

Package publication additionally requires a kernel atomic no-replace rename. The
supported delivery hosts are Linux kernel 3.15+ with glibc 2.28+ exporting
`renameat2(RENAME_NOREPLACE)`, or macOS 10.12.3+ exporting
`renameatx_np(RENAME_EXCL)`. `deliver.py --preflight` checks the live kernel and
the filesystem that will contain the package with independent 128-bit,
`O_EXCL` probe names. Each inode is captured while its creator descriptor is
still open, matched to the named edge after every rename, and removed only on
an exact identity match. An unowned winner is preserved and makes the gate fail;
an uncontended probe retains no output. A non-preflight build repeats the same
gate before creating render or package output. This protects accidental and
uncoordinated concurrent publishers. Linux and macOS expose no portable
inode-conditional unlink, so actively hostile same-UID code with write access to
the package directory is outside this delivery rail's threat boundary.

The ScreenDance capture is being hardened around two selected Delibes movements at
native tempo: *Valse lente* from *Sylvia* and *Valse* from *Coppélia*, followed by the
four-second black signature. Selection is not final-cut approval: the exact score,
choreography, MIDI, rendered audio, toolchain, and artist decision must all agree before
the package can pass. Music credit: “Music by Léo Delibes. Source arrangements by Paul
De Bra, adapted and re-orchestrated for Danse under CC BY 4.0. Changes include
instrumentation, sequencing, cue markers, and mix.” See
[`music/README.md`](music/README.md) for the score and provenance contracts.
The deterministic spatial bus and its reference-only speaker maps are documented
in [`sound/ROOM_EVENTS.md`](sound/ROOM_EVENTS.md); they do not claim cleared audio
bytes, venue approval, or a completed physical room test.
The installation reference twin, projector/speaker calibration contract, bounded
foreground recovery runtime, and setup/strike/restore protocol are documented in
[`installation/README.md`](installation/README.md). Its tracked gate ledger keeps
venue, hardware, measurement, three wall-plug, and restore predicates explicitly
blocked; a green simulator does not claim a physical installation.

## Public and institutional artifacts

The live artwork remains at `/`. The reserved `/project/` page, deterministic pitch
PDF, accessibility and caption/transcript materials, press/credits kit, posting plan,
and release-media inventory all build from `release/manifest.json`. The manifest
consumes the frozen Omega opportunity digest and represents incomplete human/external
evidence as named gates instead of placeholders that can accidentally ship.

```bash
python3 scripts/check-release.py --phase draft --list-gates
python3 scripts/tests/release-manifest.test.py
```

Draft output is visibly marked and `noindex`. Public and release phases fail closed
until the exact cut, room, rights, accessibility, identity, custody, restore, and real
presentation receipts exist. Source `release/` and `project/` paths never enter the
Pages allowlist. After the public predicates pass, the deployment workflow builds and
verifies a separate public release artifact, then copies only its receipt-bound
`/project/`, generated public documents, and cleared external media into the staged
Pages artifact. With the current pending gates, that workflow stops before upload or
deployment. Building review files locally never deploys or sends anything. See
[`release/README.md`](release/README.md) for the phase and evidence contract.

## Provenance

Nothing is synthesised. Every pixel is a photograph taken on 20 June 2017. The pose
model is a measuring instrument — it locates a knee; it does not draw one. There is no
diffusion, no training on anyone else's work, and no synthetic frame anywhere in this
project.

## Run

Pure static — no build step or network dependency.

```bash
python3 -m http.server 8080
```

The deterministic adapter test requires Node; the browser predicate also proves the local
model instantiates without any external request:

```bash
node scripts/tests/interaction.test.mjs
python3 scripts/check-installation.py
python3 render/browser.py --interaction
```

Plan: [`docs/plans/2026-07-30-danse-generative-engine.md`](docs/plans/2026-07-30-danse-generative-engine.md)

Repository lineage and custody are recorded in [`LINEAGE.json`](LINEAGE.json).
