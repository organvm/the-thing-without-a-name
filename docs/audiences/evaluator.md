# Danse for evaluators and collaborators

[General edition](general.md) · [Technical edition](technical.md) ·
[Humanities edition](humanities.md) · [Evidence and limits](../evidence/README.md) ·
[Full project record](../../README.md)

## Initial condition

The project began with a locked-camera dancer session from 20 June 2017 and a
hand-cut composite made on 25 July. The tracked corpus now contains 161
registered photographs from that session and the unregistered archival
composite. The canonical repository was later migrated from `organvm/limen`;
`LINEAGE.json` preserves the source path, base/head commits, and imported commit
list.

## What the repository attributes

`release/manifest.json` names **Anthony J. Padavano** as the artist. The tracked
bio and rights declaration further describe a photographer/programmer practice
and visual-source authorship, but the rights register explicitly keeps those
texts behind human approval gates. They are evidence of the repository's
attribution, not a substitute for a finalized contribution or rights record.

Within that boundary, the repository presents the following work for
inspection:

- the artistic system connecting the 2017 photographic record to an unbounded
  seeded work;
- the deterministic engine, browser renderer, projective-texture model, local
  interaction adapter, and offline rendering path;
- the reconstructed 2017 score and its provenance visualizations;
- production contracts for music, sound, a finite screendance package, Pages,
  a reference installation, rights, release, and private custody;
- executable invariant, regression, package, and evidence checks;
- repository migration and revision history.

## Contribution boundary

The repository does not yet contain a ratified, line-by-line contribution ledger
that separates handwritten, tool-assisted, generated, inherited, and
collaborative implementation. It would therefore be unsupported to claim that
every tracked line or production decision was solely authored by one person.

The following boundaries are explicit:

| Area | What can be inspected | Boundary |
|---|---|---|
| Project identity | Anthony is named as artist in the release manifest | Biography and public identity copy are pending human approval |
| Visual sources | Corpus manifest, rights register, and tracked declaration identify the 2017 session and composite | Performer release/credit and pictured-object review remain pending |
| Software | Canonical Git history, source modules, tests, and migration lineage | No granular human/tool/generated authorship ledger exists |
| Music | Delibes compositions, Paul De Bra arrangements, project adaptation records, and license files are separately named | Final music clearance and exact package binding remain pending |
| Pose runtime | Vendored MediaPipe assets and Apache-2.0 attribution records | Third-party runtime/model are not project-original code |
| Installation | Reference twin, simulator, contracts, and operations plan | No completed physical installation is evidenced |
| Public presentation | Replay receipt for a Pages deployment at `f19244a` | No audience, adoption, or outcome metrics are recorded |

## Evidence inspection path

1. Read [`project-record.yml`](../../project-record.yml) for invariant identity,
   status, routes, and claim references.
2. Open the [claim ledger](../evidence/README.md) to see which statements are
   verified, current, limited, or pending.
3. Inspect `LINEAGE.json` and Git history for repository custody and change
   provenance.
4. Inspect `corpus/manifest.json`, `release/manifest.json`,
   `rights/register.json`, and `installation/gates.json` for project-native
   records rather than prose summaries.
5. Run the portable batch in the [technical edition](technical.md). Treat the
   Metal/browser batch as unverified on any environment where it was not run.

## What changed because of this work?

The repository evidences a transformation from a finite 2017 photographic
composite into a system that can compute citable states, render a living browser
work, derive finite captures, and express proposed installation behavior from a
shared engine. It also turns publication readiness into explicit machine and
human gates.

It does not evidence a festival decision, client outcome, commercial metric,
completed venue installation, or finalized rights/publication package. Those
claims remain outside the present evaluation record.
