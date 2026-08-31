# Danse installation reference contract

This directory is the machine-readable, reference-only installation layer for
issue #14. It proves that the same projector camera, score clock, room-event bus,
interaction adapter, and recovery policy can be carried into a declared room. It
does **not** prove that a room, projector, surface, speaker, host, cable, power
drop, or venue has been approved, installed, measured, or observed.

## Contract map

| File | Authority |
|---|---|
| `digital-twin.json` + schema | Deterministic reference geometry, source digests, logical outputs, speaker order, thresholds, and recovery policy. |
| `contract.py` | Strict byte, identity, geometry, calibration, venue, hardware, runtime, wall-plug, and restore validator. |
| `gates.json` | Current truth: every physical gate is blocked and issue #14 cannot close. |
| `evidence.schema.json` | Shape a venue-owned external evidence receipt must have. No completed evidence is committed here. |
| `release-manifest.schema.json` | Exact canonical-release inventory, byte counts, digests, executable modes, and installation-contract binding. |
| `runtime.py` | On-demand foreground supervisor for one exact venue-approved launcher in a canonical release. |
| `simulation.py` | Executes the real supervisor against ephemeral fixture releases and receipts bounded crash, health, and integrity scenarios without claiming physical proof. |
| `archive-disposition.json` | Claim-by-claim port/reject/defer record for the non-authoritative Limen proposal. |
| `OPERATIONS.md` | Setup, calibration, operation, recovery, strike, transport, restore, troubleshooting, and conservation procedure. |

The contract digest is computed over canonical JSON with
`identity.contract_sha256` blank. Every source path is repository-relative,
regular, non-symlinked, and bound to its raw SHA-256. A changed score, camera,
program, speaker registry, interaction adapter, or probe therefore makes the
twin stale instead of silently changing the installation.

## Reference geometry and output specification

- Coordinates use the engine’s normalized room with two metres per unit. The
  2017 picture plane is 4.0 m × 3.0 m in this **simulation**, not a venue claim.
- The projector eye is `[0, 0, 2.4]`, its vertical field of view is derived as
  `2 atan(0.75 / 2.4)`, and its aspect is exactly 4:3. These are the same values
  exported by `engine/room.js`.
- Two logical outputs receive the same complete projector view and the same
  frame ticket. Their reference surfaces sit at normalized z = ±0.5. Both
  surfaces and all physical output assignments remain venue-unassigned.
- The reference edge rule is a hard boundary with zero overlap and no blend.
  It is an artistic/reference policy inherited deliberately from the archive,
  not evidence that a lens or surface satisfies it.
- The output sync threshold is 16.667 ms at 60 fps. Hardware sync is unproven
  until it is measured at the venue.

`frame_ticket()` is pure in `(spec, seed, stream, frame)`. Every output receives
the same absolute score time `frame / fps`; seeking, restarting, or generating
tickets out of order does not introduce a second clock.

## Speaker and calibration specification

The audio field is the digest-bound `reference-quad` layout in
`sound/room-layout.json`, in channel order:

1. front-left;
2. front-right;
3. rear-left;
4. rear-right.

Those names are simulation roles. Venue evidence must bind each role to a unique
verified asset and retain the room-layout 25 ms latency budget and −1 dBFS
limiter ceiling. Diagnostic impulses are typed calibration events and are never
artwork audio.

The deterministic calibration plan orders release integrity, room safety,
surface geometry, projector registration, output synchronization, speaker
routing, audiovisual synchronization, a human-visible plane/cue test, and
runtime recovery. Admission thresholds are:

| Measurement | Maximum |
|---|---:|
| projector registration error | 2 px |
| inter-output skew | 16.667 ms |
| audiovisual skew | 25 ms |
| speaker route errors | 0 |
| limiter ceiling | −1 dBFS |

These are candidate acceptance thresholds. A venue must approve the exact twin
before its measurements can satisfy them.

`python3 scripts/check-installation.py --emit workbook` derives a clean-setup
worksheet directly from the authenticated twin. It inventories every private
receipt, hardware role, surface/output assignment, calibration stage, runtime
constraint, and completion proof without inventing a venue value. Its explicit
`worksheet-not-evidence` status prevents the worksheet itself from satisfying a
physical gate. The `evidence_contract.required_fields` list is derived directly
from the evidence schema, including per-asset identity/verification/receipt
fields and aggregate cabling, power, and ventilation receipts; schema additions
therefore cannot silently disappear from the setup checklist.

## Runtime boundary

The runtime is one foreground process supervising one exact argument vector from
an external venue receipt. It:

- refuses developer checkouts, Git metadata, symlinks, special files, absolute
  executables, stale or incomplete release inventories, and launchers absent from
  the manifest, unverified hardware, failed calibration, non-loopback health
  URLs, and unapproved launchers; health probes use numeric loopback addresses
  directly and bypass ambient proxy settings;
- requires trailing file arguments to remain release-relative so they resolve
  inside the verified snapshot;
- passes the approved river seed, stream, epoch, output IDs, evidence ID, and
  contract digest through environment variables;
- opens and hashes every manifested release file through no-follow directory
  descriptors, streams the exact inventory and authenticated manifest into one
  private read/execute-only snapshot, and runs every bounded restart from that
  snapshot so release-path replacement cannot change code or dependencies;
- binds the canonical evidence, release-manifest, and launcher digests into the
  child environment, and emits append-only JSONL health/restart telemetry without
  local paths or credentials;
- admits at most three restarts in a five-minute window with fixed backoff; and
- exits when the budget is exhausted instead of looping forever.

It never installs or generates a LaunchAgent, LaunchDaemon, cron entry,
systemd user unit, plist, or other host service. A venue may approve its own
power-on/session launcher, but that external mechanism and exact command must be
captured in evidence. Do not install one on this Mac.

```bash
# Reference contracts only; this is expected to pass anywhere.
python3 scripts/check-installation.py
python3 scripts/check-installation.py --emit calibration
python3 scripts/check-installation.py --emit frame --seed 20170620 --stream 0 --frame 120
python3 scripts/check-installation.py --emit workbook
python3 scripts/check-installation.py --emit simulation

# Expected to fail until external evidence and a canonical release exist.
python3 scripts/check-installation.py --phase complete

# Venue-only admission and foreground execution after evidence exists.
python3 installation/runtime.py --check \
  --evidence /external/evidence.json --release-root /external/release
python3 installation/runtime.py --run \
  --evidence /external/evidence.json --release-root /external/release \
  --session-id venue-cycle-20260804-01 \
  --telemetry /external/recovery-session.jsonl
```

The portable simulation executes the actual foreground supervisor against a
temporary manifest-bound release. It covers clean exit, bounded crash recovery,
startup-health exhaustion, release-integrity failure, seek-stable shared frame
tickets, and the declared safety thresholds. Its receipt is deliberately marked
`passed-not-physical-evidence`; it cannot stand in for hardware sync, calibration,
wall-plug, or restore observations.

Evidence v2 replaces v1 because physical-receipt binding is not backward
inferable. Do not relabel a v1 receipt: re-collect or re-envelope the original
venue-owned telemetry and observations under v2, preserving their original
bytes and timestamps. Runtime-plan v2 is likewise required; obsolete or missing
plan fields fail with a `runtime-plan-invalid` telemetry event instead of an
uncaught field error.

Every external wall-plug telemetry envelope and observation receipt, plus each
setup/strike/restore receipt payload, must include the configuration digest
computed from the exact venue, geometry, release manifest and launcher, hardware,
calibration, runtime approval/arguments/health contract, river, and output set.
The telemetry envelope must carry the exact newline-terminated JSONL bytes, not
only their digest. The validator rehashes those bytes, requires a unique runtime
session, strict monotonic sequence/time, one leading `runtime-admitted` event,
and a final `runtime-ready` event without a terminal failure. Its configuration,
spec, evidence, release, and launcher bindings must match the admitted runtime.
All other supplied SHA-256 values are recomputed from their canonical payloads.
These hashes prove internal consistency only: because a submitter can rewrite
bytes and recompute every self-authored hash, they do not authenticate a physical
observation or its provenance.

Accordingly, `--phase complete` intentionally fails closed even for an
internally consistent v2 candidate. Completion additionally requires a reviewed
external authority verifier backed by an immutable allowlist. Each of the three
cycles must be bound by that verifier to a distinct authority receipt identity,
runtime session, telemetry digest, configuration digest, and observation time.
The repository does not yet choose or configure a signing key, immutable
owner-authored record, or other authority rail, so no portable run can certify
physical completion. Before any recovery or restore claim exists, the restore
binding may remain `null`;
`--phase runtime --emit runtime-plan` returns the exact `configuration_sha256`
used to build the three telemetry/observation envelopes and restore receipts.

## Current blockers

The tracked gate ledger intentionally records all eight physical gates as
blocked. Completion still requires venue approval; exact hardware, cabling,
power, ventilation, and safety receipts; projector/speaker/AV measurements; the
human-visible plane/cue test; launcher approval; three distinct human-observed
wall-plug recoveries; setup/strike/clean-restore evidence; and integration of the
reviewed immutable authority verifier described above. A green reference suite
cannot close issue #14.
