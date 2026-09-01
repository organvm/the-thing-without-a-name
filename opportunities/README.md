# Frozen opportunity registry

`omega-20260829.json` remains the immutable Alpha → Omega release freeze consumed
by issue #2 and reserved by issue #12. `omega-20260901.json` is the current issue
#22 maintenance successor. It updates the operating queue after the ScreenDance
hard wall without rebinding or rewriting the frozen release, its submission
register, or any predecessor. Both snapshots disposition every tracked target,
record facts as `verified`, `unstated`, `not-applicable`, or `conflicted`, and keep
every account action, fee, agreement, and public send behind an explicit human gate.

The maintenance snapshot does not contact live sites during a build. The raw public
responses were hashed in `source-evidence-20260901.json`, and
`omega-20260901.receipt.json` binds the resulting immutable snapshot to issue #22
only. Response bodies are not vendored. `submission/screendance-2027.yaml` and
`release/manifest.json` deliberately retain `omega-20260829`; a maintenance update
cannot rewrite a filing or release identity after the fact. Every predecessor and
its receipt remain byte-for-byte historical evidence. Run:

```bash
python3 scripts/check-opportunities.py
python3 scripts/check-opportunities.py --maintenance-current
python3 scripts/tests/opportunities.test.py
python3 scripts/check-opportunities.py --maintenance-current --operational-as-of now
```

The first command validates the frozen release and its exact consumers. The second
validates the current maintenance snapshot and proves that it remains unbound from
those consumers. The explicitly clocked fourth command is the live-queue check:
once any ranked deadline elapses it fails and names issue #22 as the successor
owner. Do not edit either immutable snapshot merely to make a later queue current;
issue #22 publishes another dated snapshot, source check, and digest. A
`deadline_at` on a source that publishes only a date is a fail-closed start-of-day
scheduling boundary; a human still confirms the portal cutoff before sending.
