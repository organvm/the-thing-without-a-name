# Frozen opportunity registry

`omega-20260901.json` is the current immutable issue #22 successor snapshot for the
Alpha → Omega release. It supersedes, but does not rewrite,
`omega-20260829.json`, `omega-20260826-2.json`, `omega-20260826.json`, or the preserved August 4
snapshot. It dispositions every target named in the tracked plan, records facts as
`verified`, `unstated`, `not-applicable`, or `conflicted`, and keeps every account
action, fee, agreement, and public send behind an explicit human gate.

The snapshot does not contact live sites during a build. The raw public responses
were hashed in `source-evidence-20260901.json`; the snapshot binds that manifest,
and `omega-20260901.receipt.json` binds the current snapshot. Response bodies are not
vendored. `submission/screendance-2027.yaml` consumes that exact SHA-256 identity
for issue #2. Issue #12's draft `release/manifest.json` cites the same identity,
while the snapshot and receipt keep that release consumer pending until the release
is completed. The submission snapshot-binding block also names the IANA shipping
timezone; the checker requires that zone to agree with the frozen offset-bearing
deadline without changing immutable snapshot bytes. Every predecessor and its
receipt remain byte-for-byte historical evidence. Run:

```bash
python3 scripts/check-opportunities.py
python3 scripts/tests/opportunities.test.py
python3 scripts/check-opportunities.py --operational-as-of now
```

The first two commands are reproducible release checks. The explicitly clocked
third command is the live-queue check: once any ranked deadline elapses it fails and
names issue #22 as the successor owner. Do not edit this frozen snapshot merely to
make a later queue current. Issue #22 owns a newly dated snapshot, new source checks,
and a new digest. A `deadline_at` on a source that publishes only a date is a
fail-closed start-of-day scheduling boundary; a human still confirms the portal
cutoff before sending.
