# Release manifest and public-project framework

`manifest.json` is the single source for the project page, installation pitch,
accessibility materials, captions/transcript, press copy, credits, posting plan,
and release-media inventory. It consumes frozen opportunity snapshot
`omega-20260829` at SHA-256
`c9941a027bd91236f6e48157f332d6ca11f08d9946af2bfc7f029e44bbc67294`,
frozen at `2026-08-29T22:12:19Z`, and binds its source-evidence manifest at
`7e9ba1c74f8ac78df116ada8c94d8af4e7d04813f2a3c026693258cd6c974bc8`.
The snapshot also binds the complete ScreenDance YAML consumer contract; a release
build never fetches changing call terms. Its August 26 predecessors,
`omega-20260826` and `omega-20260826-2`, remain byte-for-byte historical evidence.

The installation section consumes the exact reference digital twin and its
eight-gate ledger by path, byte count, raw SHA-256, and embedded installation
contract digest. The installation checker revalidates that binding during every
release build. The generated project page and pitch identify the twin as a
reference simulation: it is not venue, hardware, calibration, recovery, restore,
or public-approval evidence.

The verified custody claim binds the tracked, non-destructive snapshot/restore
contract. It verifies what the workflow requires; it does not claim that the
private material has been copied or restored. The `release-custody` gate remains
pending until an external redacted receipt and explicit human acceptance exist.

The manifest has three cumulative phases:

- `draft` validates all existing bytes and emits visibly marked, `noindex` local
  review artifacts while named human and external gates remain open;
- `public` additionally requires public approval, verified claims and technical
  requirements, cleared credits and media, an approved contact route, and final
  accessibility material; and
- `release` adds independent custody, restore rehearsal, and actual presentation
  evidence.

Current tracked state is intentionally `draft`. `public` and `release` fail before
creating an output directory. A passing draft is not permission to publish.
The completed HUD keyboard/touch replay is bound by a public-safe
[`live-interaction-replay-20260804.json`](evidence/live-interaction-replay-20260804.json)
receipt; it closes only that machine gate and does not satisfy publication approval.

## Validate and build

```bash
python3 scripts/check-release.py --phase draft --list-gates
python3 scripts/tests/release-manifest.test.py

release_output="$(mktemp -d)/danse-release"
python3 scripts/build-release.py \
  --output "$release_output" \
  --phase draft \
  --source-commit "$(git rev-parse HEAD)"
python3 scripts/build-release.py \
  --verify "$release_output" \
  --phase draft \
  --source-commit "$(git rev-parse HEAD)"
```

The output contains:

- `project/index.html`
- `pitch/danse-installation-pitch.pdf`
- accessibility summary, WebVTT captions, and transcript
- press kit, public credits, and a non-sending posting calendar
- release-media inventory and only media whose source digest and clearance both pass
- `release-build.json`, which digests every delivered byte and binds the exact source
  commit, release manifest, installation reference and gate ledger, opportunity
  snapshot and receipt, source-evidence manifest, phase, and version

The builder accepts only an absent or empty output outside the repository, rejects
symlinks and path traversal, and checks that CLI builds start and finish on the clean
Git worktree named by the exact source commit. It sets deterministic file timestamps
and reproduces the same PDF and other bytes for the same manifest, phase, dependency
version, and source commit. The generated project page is passive by contract: an exact
fail-closed Content Security Policy blocks scripts, connections, frames, objects, forms,
workers, and remote media, while a no-referrer policy prevents outbound link
navigation from disclosing the project path. Artifact verification rejects a weakened
policy, active elements, redirect metadata, duplicate attributes, or inline event
handlers even if a modified artifact is self-rehashed. Generated outputs are not
committed from the draft phase. The Pages verifier reapplies the same policy after
composing the artwork and public release, so rehashing the outer Pages manifest cannot
admit weakened project markup.

## Closing a gate

Do not change a status alone. A completed claim, credit, medium, or gate names a
tracked public-safe evidence file and its SHA-256. Ready media also names an exact
source path, SHA-256, byte count, public destination under `media/assets/`, and
accessible description. Its ID and phase scope must match `rights/register.json`,
and its clearance must satisfy the typed rights/production receipt contract.
Generated project, pitch, accessibility, caption, transcript, press, and credit
products are a separate manifest inventory: they never name prebuilt source copies.
Their exact output bytes are derived from the manifest and recorded in the release
artifact receipt. The generated project page links directly to each public access and
presentation product, and artifact verification rejects broken or escaping local links.
Private releases, signatures, contacts, raw media, package roots, and credentials stay
in their owning custody; only their redacted receipt can be referenced here.

Before changing `status` to `public-approved` or `released`:

1. Replace draft/pending language with approved factual copy.
2. Bind the exact #10 cut, #14 room evidence, and #16 redacted rights register.
3. Resolve caption/transcript applicability against the final media.
4. Run `check-release.py` at the intended phase and build twice byte-identically.
5. Render and visually inspect every pitch PDF page.
6. Record human publication approval separately; the builder never deploys, posts,
   sends outreach, creates accounts, pays fees, or accepts terms.

## Publication boundary

The root GitHub Pages artifact remains the immersive artwork. `scripts/build-pages.py`
never copies source `release/` or `project/`. After public rights and release phases
pass, it can accept one separately built and verified public release artifact and copy
only the receipt-bound products and cleared public media declared by that artifact.
Draft, missing, wrong-commit, tampered, symlinked, or overfull release artifacts fail
before Pages bytes are staged. Current pending gates stop the workflow before upload.
