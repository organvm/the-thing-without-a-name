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
The completed historical HUD keyboard/touch replay is bound by an immutable public-safe
[`live-interaction-replay-20260804.json`](evidence/live-interaction-replay-20260804.json)
receipt; it closes only the legacy machine gate and does not prove the progressive
interface. `progressive-controls-replay` requires a separate exact-head browser receipt
at `release/evidence/progressive-controls-replay.json`, validated by the
[`danse.progressive-controls-replay.v2` schema](progressive-controls-replay.schema.json)
and its gate-specific identity, check-inventory, Apple-Metal, and privacy-boundary
validator. That proves only a source-bound, internally consistent record. It cannot
authenticate that an independent system-Chrome Apple-Metal run occurred, so the gate
stays pending until a trusted external producer or attestation verifier exists. Neither
receipt satisfies publication approval.

Terminal receipts use a two-stage source/evidence contract. A real ancestor commit
freezes `release/manifest.json`, validator and schema code, runtime, media, product,
rights, and package identities. Its descendants may change only the canonical gate
state plus the schema-closed evidence envelope. Every gate then binds the same source
head, source tree, raw release-manifest digest, release identity, and (when applicable)
package-manifest digest. Post-source changes to copy, schemas, validators, runtime,
media, or product files fail closed. The validator owns the exact fourteen-gate
inventory, order, issue/owner mapping, public/release phase routes, accessibility review
route, and installation evidence routes; manifest edits cannot remove or reroute
terminal predicates. Validation also rejects dirty or ignored contract bytes, shallow
history, hidden index flags, Git replacement refs, redirecting or executable
repository-local Git configuration, missing objects, wrong trees, future timestamps,
and cross-gate proof/source reuse.

`release/evidence/proof-pins.json` is a review-required digest inventory. Its tracked
hashes prevent accidental substitution, but the ledger is not a signature and cannot
turn repository-authored JSON into owner, contributor, venue, host, or tool authority.
Owner and external authority proofs therefore bind distinct immutable GitHub comment
identities and exact canonical payload digests; machine proofs bind a frozen generator
path and digest plus a typed execution receipt. Installation and presentation still
require their distinct venue/host sources. Branch protection and completed independent
review remain external governance facts that local validation cannot manufacture.

Rights receipts are claim-partitioned. The `rights-register` and
`press-stills-clearance` gates recompute separate rights-only receipts through the
frozen canonical rights checker. They bind only their registered contributor gates,
asset/use inventory, redacted receipt digests, validation date, and fixed-term expiry.
They never import final-cut, biography, link behavior, submission, festival scheduling,
archive participation, regulations, terms, upload, or no-withdrawal decisions. Those
human actions remain in their own gates and phases. Deterministic recomputation cannot
authenticate a non-owner contributor or rightsholder decision, so both rights terminal
gates stay pending until that distinct external authority has a trusted verifier. A
tool-ready rights receipt alone cannot replace the required owner/rightsholder
attestation.

The proof schemas remain useful for closed shape, source identity, deterministic
consistency, and review inventory, but self-consistent repository JSON and opaque check
digests cannot confer external authority. Validation therefore forces these operational
gates to remain pending until each named producer and current verifier exists:

| Proof kind | Required completion rail |
| --- | --- |
| `progressive-controls-replay` | Independently authenticated system-Chrome Apple-Metal producer or authority receipt over the canonical raw capture; repository-authored replay JSON remains structural evidence only. |
| `rights-validation` | Distinct authenticated contributor/rightsholder authority over the claim-partitioned deterministic rights receipt; repository ownership cannot impersonate that decision. |
| `accessibility-review` | Canonical exact-head static/browser audit receipt whose alt text, captions, transcript, reduced-motion and silent-fallback results are recomputed from the frozen release inputs; separate owner approval remains required. |
| `submission-package` | Atomic package producer receipt binding every actual delivered byte, production graph, package manifest and frozen source; the current verifier must rebuild or rehash that exact package. |
| `submission-validation` | Schema-closed portable and real macOS system-Chrome Apple-Metal raw captures, recomputed checks and exact package identity. |
| `custody-completion` | Custodian-originated redacted receipt binding independent physical copies, retention and exact material census; repository-authored hashes cannot prove custody. |
| `restore-completion` | Human-observed clean-target restore/rehearsal receipt plus recomputed portable, package, installation and Apple-Metal checks. |
| `installation-completion` | Distinct venue authority plus typed hardware, projector/speaker calibration, runtime, three wall-plug recoveries and clean setup/strike receipt records. |
| `presentation-lifecycle` | Host-authored immutable lifecycle source binding the actual program, public route, presentation time and exact package. |

Historical generator blobs are digest identities only. Receipt-selected historical
Python is never imported or executed; current reviewed source-only verifier code
evaluates the committed raw data after checkout guards run. No terminal machine-proof
path infers external authority from repository-authored JSON. No test fixture, review
pin, repository comment or self-authored digest substitutes for the human, venue, host,
physical-device or production event.

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
version, and source commit. Generated outputs are not committed from the draft phase.

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
