# Danse evidence and limitations ledger

[Reader routes](../../README.md#choose-your-reading-path) ·
[Machine-readable project record](../../project-record.yml) ·
[Technical edition](../audiences/technical.md) ·
[Evaluator edition](../audiences/evaluator.md)

This page is the reader-facing index to claim records. Each material claim in
`project-record.yml` resolves to an
[`assertion-evidence.v1`](https://github.com/organvm-iv-taxis/schema-definitions/blob/main/schemas/assertion-evidence.v1.schema.json)
record under [`assertions/`](assertions/). Those records bind statements to exact
repository artifacts and SHA-256 digests.

## Claim ledger

| Claim | Status | Evidence | Limitation |
|---|---|---|---|
| The corpus records 161 registered photographs from 20 June 2017 and one unregistered archival composite | Verified historical record | [`danse-corpus-session.json`](assertions/danse-corpus-session.json), `corpus/manifest.json` | This proves the tracked publishable derivative inventory, not private-original custody or every use right |
| The portable checker enforces the pure seed/time engine and isolates clock/entropy to `arrival.js` | Verified in the 2026-08-31 audited tree | [`danse-pure-engine-contract.json`](assertions/danse-pure-engine-contract.json), `scripts/check-danse.py` | A checker artifact is not the machine-bound Metal/browser result; rerun it on the exact tree |
| A Pages artwork was exercised at source commit `f19244a` | Verified deployment receipt | [`danse-public-artwork-deployment.json`](assertions/danse-public-artwork-deployment.json), replay receipt | The canonical branch has advanced; the receipt does not approve the draft project/release package |
| The broader public/release package was `draft` with named pending gates | Verified in the 2026-08-31 audited tree | [`danse-release-is-draft.json`](assertions/danse-release-is-draft.json), `release/manifest.json`, `rights/register.json` | Draft output is review material, not permission to publish, file, or promote |
| The installation was reference-only and its physical predicates were blocked | Verified in the 2026-08-31 audited tree | [`danse-installation-reference-only.json`](assertions/danse-installation-reference-only.json), `installation/gates.json` | No venue, hardware, calibration, recovery, restore, or completed installation is evidenced |
| The repository names Anthony J. Padavano as artist and records external/performer attribution boundaries | Verified repository attribution | [`danse-attribution-boundaries.json`](assertions/danse-attribution-boundaries.json), release and rights records | Public identity, performer credit/release, and several rights approvals remain pending; no granular implementation-contribution ledger exists |
| `organvm/the-thing-without-a-name` is the canonical repository migrated from `organvm/limen/apps/danse` | Verified lineage record | [`danse-canonical-lineage.json`](assertions/danse-canonical-lineage.json), `LINEAGE.json` | Personal or historical copies should not become competing factual authorities |

## Native evidence systems

Reader-mode assertions do not replace the project's more specific contracts:

- `corpus/manifest.json` owns the publishable derivative corpus inventory;
- `scripts/check-danse.py` owns the portable invariant ratchet;
- `release/manifest.json` owns public and release claims and gates;
- `rights/register.json` owns rights, credit, and human approval boundaries;
- `submission/screendance-2027.yaml` owns call dates, formats, terms, and phase acts;
- `installation/gates.json` owns physical-installation readiness;
- `LINEAGE.json` owns canonical repository and migration identity.

If a summary conflicts with one of those records, the native record wins for its
domain and the summary must be corrected.

## Known limitations

- A public browser deployment is not evidence of a public-approved project page,
  final film, submission, selection, or physical installation.
- The current canonical branch is newer than the recorded public replay source.
- Machine-bound visual verification requires macOS, Chrome, Apple Metal, and
  hydrated local material; it was not established by portable documentation work.
- Private originals, releases, signatures, contact information, recordings, and
  package receipts remain outside the public repository by design.
- The rights register remains draft. Provenance does not itself establish legal
  clearance for every public, festival, press, archive, or promotional use.
- No audience, adoption, revenue, conversion, client, or long-term venue metrics
  are represented.
- No ratified granular ledger distinguishes every human, tool-assisted,
  generated, inherited, and collaborative implementation contribution.
