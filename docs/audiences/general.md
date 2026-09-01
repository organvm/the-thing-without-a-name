# Danse, in ordinary language

[Artwork](https://danse.pages.dev/) ·
[Technical edition](technical.md) · [Humanities edition](humanities.md) ·
[Evidence and limits](../evidence/README.md) · [Full project record](../../README.md)

## What is this?

Danse is a photographic artwork that changes over time in a web browser. Its
source is deliberately finite: 161 photographs made during one dancer session
on 20 June 2017, together with a composite assembled from that material a month
later. The software rearranges registered fragments from those records inside a
three-dimensional model of the photographed room.

The work is not a single video on a loop. Its engine can calculate the image for
any declared seed and moment. A visit therefore enters an ongoing "river"; a
finite film is only one capture from that larger work.

## What problem led to it?

The 2017 composite held several moments of one body in one picture, but it was
still a fixed image. Danse asks whether that picture can open into time and depth
without losing the room that makes its fragments cohere or the provenance that
ties each fragment to a photograph.

That creates two linked problems:

- an artistic problem: how can one afternoon remain specific while becoming an
  unbounded work?;
- a documentary problem: how can every output remain accountable to its source
  while the composition keeps changing?

## What happens when someone enters?

The browser creates two numbers: a seed and the moment at which that visitor's
river began. Those numbers identify the visit. The image engine combines the
seed with elapsed time, selects photographic material, and positions translucent
planes in a shared model of the room.

Returning later rejoins that river farther downstream. A link can share the
living river or cite one exact moment. Camera-based interaction is optional and
local; keyboard and touch controls use the same bounded control system.

## A concrete example

The archival seed is `20170620`. A link containing that seed names the same
deterministic river. Adding a time value cites one exact state, so another person
or an offline renderer can ask the engine for the same image without replaying
everything that came before it.

This reproducibility does not make the work a loop. It makes moments citable.

## Why it matters

Danse treats a photograph as both an image and a record of where a camera stood.
The room's lines are shared across fragments, so a body can separate across
depths and moments while the architecture remains continuous. The code is not a
delivery wrapper around a finished picture; its rules determine what the work
can remember, repeat, and reveal.

## What currently exists?

- A public browser artwork is documented by a replay receipt tied to source
  commit `f19244a`.
- The canonical repository contains the image engine, publishable derivative
  corpus, local interaction adapter, deterministic rendering tools, tests,
  provenance records, and production contracts.
- A finite ScreenDance capture and public project package remain in draft.
- The installation is a reference design and simulator. Its venue, hardware,
  calibration, recovery, and restore gates are blocked.
- Madison Garber is credited as performer and primary choreographer; Anthony J.
  Padavano is credited for concept, direction, additional choreography,
  photography, editing, sound, software, archive, and production. Performer
  release, pictured-object review, music clearance, selected press stills, and
  several publication approvals remain governed by the draft rights and release
  records.

These are not small-print caveats. They distinguish an implemented artwork from
uses that require other people, a venue, a final cut, or explicit permission.

## Where next?

- To understand the code and tests, read the [technical edition](technical.md).
- To study the work's argument about time, history, and simultaneity, read the
  [humanities edition](humanities.md).
- To evaluate a presentation, read the [presenter edition](business.md).
- To inspect what is verified, pending, or out of scope, use the
  [evidence ledger](../evidence/README.md).
