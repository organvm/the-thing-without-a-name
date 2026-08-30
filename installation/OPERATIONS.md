# Installation operations, recovery, and conservation

This procedure starts from a canonical release package and an external venue
evidence file. It is not authorization to mount, energize, unplug, or alter
equipment. Venue staff own safety, egress, rigging, electrical, thermal, network,
insurance, and public-operation decisions.

## Before transport

1. Run `python3 scripts/check-installation.py --emit workbook` and retain the
   resulting non-evidentiary worksheet with this procedure. Use its derived role,
   geometry, calibration, runtime, and receipt inventory as the clean-room setup
   checklist; do not edit it into a passing evidence file.
2. Freeze the exact `digital-twin.json` contract digest and canonical release
   manifest. Preserve two checksum-verified release copies on independent media.
3. Inventory every required hardware role. Keep model, serial, rental, insurance,
   and private location data in the external venue receipt, never this repository.
4. Obtain written venue approval for measured room dimensions, egress, mounting,
   surface material, projector/lens/throw, power distribution, ventilation,
   interface/speaker routing, cabling, level limits, and the foreground launcher.
   Keep every launcher file argument canonical and release-relative; absolute,
   parent-traversing, home-relative, and `file:` arguments fail admission.
5. Confirm that the release contains no Git metadata or developer-only tooling,
   and that its manifest inventories every delivered file. The approved launcher
   path, bytes, digest, and executable mode must match that inventory exactly.
6. Print or retain an offline copy of this procedure, the digital twin, release
   manifest, calibration plan, and venue emergency contacts.

## Clean setup

1. Venue staff make the room safe and de-energized. Mark egress and cable routes.
2. Measure width, height, depth, mounting points, throw distances, surface
   centres/rotations/extents, viewing area, power circuits, and ventilation.
   Record—not infer—each value in the external evidence.
3. Install the two venue-approved receiving surfaces and assign each logical
   reference surface to its measured physical counterpart. If the venue cannot
   approve the hard-boundary/no-overlap policy, revise and re-digest the digital
   twin before proceeding.
4. Mount projectors with venue-approved hardware and safety bonds. Set native
   resolution/refresh, disable automatic keystone, colour, focus, and power
   changes, and record exact settings. Never compensate for geometry by silently
   changing the engine camera.
5. Place the audio interface and speakers in declared channel order. Start at a
   safe level; route diagnostic impulses only under authorized supervision.
6. Install the display host from the canonical release. Do not clone the source
   repository. Do not install a LaunchAgent or any persistent service.
7. Verify release bytes and run `scripts/check-installation.py --phase runtime`
   against the external evidence before the launcher is allowed to start.
8. Before claiming recovery or restore, run the runtime phase with
   `--emit runtime-plan`. Use its exact `configuration_sha256` in each v2
   wall-plug telemetry envelope, canonical observation receipt payload, and the
   three canonical setup/strike/restore receipt payloads. A sibling digest is not
   sufficient: retain the exact newline-terminated JSONL telemetry bytes, and the
   validator rehashes them and verifies their embedded `runtime-admitted`
   configuration before recomputing every receipt hash from its payload. The
   restore binding may be `null` only while no restore result or receipt is
   claimed.

Before transport or venue work, the portable control-plane preflight may be run:

```bash
python3 scripts/check-installation.py --emit simulation
```

This exercises logical output tickets and the real supervisor's clean-exit,
crash-budget, health-failure, and release-integrity paths. A passing portable
receipt is prerequisite engineering evidence only. It is never a calibration,
hardware-sync, power-cycle, or restore receipt.

## Calibration order

Run `python3 scripts/check-installation.py --emit calibration` and follow the
emitted sequence without reordering it.

1. **Release integrity:** verify the release manifest and installation contract.
2. **Room safety:** venue staff sign off egress, mounting, power, ventilation,
   levels, cable protection, and emergency shutdown.
3. **Surface geometry:** record each surface centre, rotation, extent, material,
   tension, and visibility from the approved viewing area.
4. **Projector registration:** show the bound projection probe on each output.
   Measure all corners and architectural lines; retain the raw measurement
   receipt and require error ≤ 2 px.
5. **Output synchronization:** show identical numbered frame tickets on both
   outputs, measure their skew with venue-approved equipment, and require
   ≤ 16.667 ms. Shared seed/time is necessary but is not a hardware-sync proof.
6. **Speaker routing:** play the separately typed diagnostic impulse to each
   channel at a safe level. Require zero route errors and record the exact asset
   heard at every channel.
7. **Audiovisual synchronization and level:** measure AV skew ≤ 25 ms and verify
   the downstream ceiling does not exceed −1 dBFS.
8. **Visible plane/cue test:** a named human observer watches the generative
   room and confirms the declared visible plane/cue relationship. Preserve a
   receipt; simulator parity is not a substitute.
9. **Runtime recovery:** admit the exact venue launcher, then begin the bounded
   recovery and wall-plug protocol below.

Any miss stops the sequence. Correct the physical configuration or revise the
spec through review; never raise a threshold merely to obtain green output.

## Foreground operation

1. From the venue-approved session, start `installation/runtime.py --run` in the
   foreground with the external evidence and canonical release root.
2. Allow the supervisor to stream the complete manifested release into its
   private runtime snapshot. This admission time and temporary-storage demand
   count against the venue's 180-second wall-plug recovery budget and must be
   proven on the approved host with the final release.
3. Confirm `runtime-admitted`, `launcher-start`, and (when configured)
   `health-ready` telemetry before opening to visitors.
4. Keep the terminal/console and emergency stop accessible to venue staff but
   outside the public projection field.
5. The supervisor permits three restarts in a five-minute window. Repeated health
   failure exhausts the budget and stops; it never becomes an unbounded reboot
   loop. Investigate power, thermals, storage, release integrity, or hardware
   before restarting the supervisor manually.
6. Interaction remains optional. Permission denial, no camera, dropout, and the
   keyboard/touch accessibility path are normal supported states.

## Three wall-plug recovery proofs

Perform this only after venue electrical and equipment owners authorize it. Do
not perform it on this Mac as a substitute for venue evidence.

For each of three distinct cycles:

1. Start a new telemetry receipt and record the exact spec, release, evidence,
   hardware, calibration, river, and observer identities. Record the validator's
   exact `configuration_sha256`; it must be embedded in the telemetry envelope
   and canonical observation payload, and identical across the three proofs and
   the restore rehearsal.
2. Observe the generative display and visible plane/cue relationship before
   power removal.
3. Remove power through the venue-approved test point for at least one second.
   Do not simulate the event by killing only the child process.
4. Restore power and make no manual repair. The venue-owned launcher must return
   the foreground supervisor and exact canonical release.
5. Measure time to the returning generative display. It must be no more than 180
   seconds and must rejoin the approved river without changing seed, stream,
   epoch, spec, release, or hardware receipts.
6. A named human observer confirms the display returned and no repair occurred.
   Hash the telemetry and observation receipt before the next cycle.

The three proof IDs, observation times, and telemetry digests must be distinct.
Three software restarts, three browser refreshes, or three fixture runs do not
satisfy this predicate.

## Troubleshooting

- **Release validation fails:** stop. Restore from an independently verified
  copy; do not run from Git or patch files in place.
- **Registration drifts:** stop projection, inspect mount/surface movement and
  repeat geometry plus projector calibration. Do not change the shared camera.
- **Outputs disagree:** verify frame-ticket identity, display mode, cable path,
  and measured hardware sync. Pure engine time does not guarantee scan-out sync.
- **Wrong speaker or unsafe level:** mute immediately, correct physical routing,
  and repeat the complete speaker/AV sequence.
- **Health failures exhaust recovery:** leave the supervisor stopped and inspect
  host thermals, storage, power, network-free health endpoint, and launcher logs.
- **Camera unavailable:** use no-camera or keyboard/touch mode; never bypass
  permission or privacy gates.

## Strike and transport

1. Close the exhibition, stop the foreground supervisor, and verify the child
   process ended. Hash and copy telemetry/evidence before power-down.
2. Venue staff de-energize equipment. Allow projectors and host hardware to cool
   according to manufacturer/venue rules.
3. Photograph and record final projector, surface, speaker, cable, and mount
   state for the strike receipt. Remove equipment in the venue-approved order.
4. Inspect, label, and pack each asset against the hardware receipt. Preserve
   media and checksum copies separately from equipment.
5. Restore the room, egress, power, mounting points, and surfaces to venue
   requirements. A named observer signs the strike receipt.

## Clean restore rehearsal

1. Use a clean, non-developer host/root. Restore the canonical release from the
   custody copy, verify its manifest, and prove there is no `.git` directory.
2. Rebuild the external evidence binding for the restored root without copying
   stale absolute paths or credentials.
3. Repeat setup and every calibration stage from the tracked instructions.
4. Run the exact approved foreground launcher and human-visible plane/cue test.
5. Perform a documented setup, strike, and second restore. Preserve distinct
   canonical SHA-256 receipt payloads for all three phases and one named
   observer/timestamp. Embed the same `configuration_sha256` used by all three
   wall-plug proofs; the validator recomputes each phase receipt, so retaining a
   copied hash from another release or hardware set fails admission.

Issue #14 remains open until this restore and the three physical wall-plug cycles
validate together against one exact digital-twin digest.

## Conservation

Preserve the canonical release manifest, digital-twin JSON/schema, external
venue/hardware/calibration evidence, three wall-plug telemetry and observer
receipts, setup/strike/restore receipts, public documentation, and two independent
checksum copies. Future hardware substitution requires a new evidence ID and
calibration receipt; a camera/score/room-layout change requires a new twin digest.
