# Danse for curators, presenters, and production partners

[General edition](general.md) · [Technical edition](technical.md) ·
[Humanities edition](humanities.md) · [Evidence and limits](../evidence/README.md) ·
[Full project record](../../README.md)

This page is a production-readiness view, not a claim of a commission, client
deployment, festival selection, or commercial outcome.

## The presentation problem

Danse has one computational source but several materially different public
forms: an unbounded browser artwork, a finite screendance capture, and a
site-specific projected room. A presenter needs to know which form is under
discussion, what it consumes and produces, and which evidence must exist before
that form can be announced or opened.

## Available and proposed forms

| Form | Audience receives | Current status |
|---|---|---|
| Browser artwork | A seeded, ongoing photographic river with optional local interaction | Public deployment evidenced at source commit `f19244a`; canonical source has advanced since that receipt |
| Screendance capture | One finite passage with fixed media, credits, accessibility material, and package receipts | Draft; final-cut, music, rights, upload, and submission acts remain gated |
| Physical installation | Projected planes/scrim, spatial sound, local interaction, and recovery procedures | Reference design and simulator only; physical predicates are blocked |
| Project/press package | Project page, pitch, captions/transcript, credits, stills, and press material | Draft and `noindex`; public build fails closed until approvals and media clearances exist |

## Inputs and outputs

### Browser presentation

Inputs are static project bytes and a compatible browser/WebGL surface. Optional
camera interaction uses a vendored local model; keyboard and touch are the
fallback. The output is a visitor-specific river and shareable seed/time links.

### Finite capture

Inputs include the hydrated film corpus, approved score/choreography, rendered
audio, final artistic decision, exact package specification, and required rights
and submission attestations. Outputs include the master, screener, stills,
origin image, text, accessibility material, and a receipt-bound package.

### Physical room

Inputs must be established for a real venue: room geometry, circulation and
accessible route, blackout, throw distance, surfaces, projectors, signal path,
speakers, operating limits, calibration, safety, setup/strike, recovery, and
restore evidence. The tracked digital twin is a planning reference, not a venue
measurement.

## Integration requirements

- Confirm the presentation form before discussing dates, media, or promotion.
- Bind any finite media to exact source and package receipts.
- Keep private raw media, releases, signatures, contacts, and credentials in
  their controlled custody rather than in the public repository.
- Resolve performer, pictured-object, music, recording, press-still, archive,
  and promotional scopes for the actual intended use.
- For an installation, produce venue-specific calibration and three
  wall-plug-recovery receipts; do not substitute simulator output.
- Review reduced-motion, silent operation, captions/transcript applicability,
  alt text, controls, and accessible circulation against the final form.

## Risks and constraints

| Risk | Existing control | Remaining boundary |
|---|---|---|
| A draft artifact is mistaken for a public release | Phase-gated builder and `noindex` draft output | Human publication approval and cleared media are still required |
| A simulator is mistaken for a tested room | Separate reference twin and blocked gate ledger | Venue, hardware, calibration, recovery, and restore evidence do not yet exist |
| Technical provenance is mistaken for legal clearance | Rights register separates source identity from permissions | Several human and rights gates remain pending |
| A finite capture is mistaken for the unbounded work | Passage/capture vocabulary is explicit | Program and communications must preserve the distinction |
| Camera interaction creates privacy exposure | Opt-in local processing; no declared raw-frame retention/transmission | Host/browser policy and final accessibility review still matter |

## Evidence versus projected value

The repository supports claims about a functioning deterministic system,
declared provenance, a deployed browser artifact at one recorded commit, and a
substantial production-control architecture. It does **not** contain evidence
of attendance, conversion, revenue, long-term venue operation, client adoption,
festival acceptance, or a completed physical installation.

Any proposal should therefore describe artistic and operational possibilities
as proposed until the corresponding receipt appears in the
[evidence ledger](../evidence/README.md) or the project's native release,
rights, submission, and installation records.

## Technical appendix

The [technical edition](technical.md) maps the system boundaries and portable
verification commands. `installation/README.md`, `release/README.md`,
`rights/README.md`, and `submission/screendance-2027.yaml` remain the canonical
operational records for their respective phases.
