# ScreenDance opening revision — phrase-native cut

**Decision recorded:** 1 September 2026  
**Scope:** the first minute of *The Thing Without a Name*  
**Status:** edit logic implemented and portable tests passing; a revised full screener has not yet been rendered or uploaded from the submitted Vimeo master in this environment.

## Governing direction

The first thirty seconds are approved and remain protected. The problem is the following interval: the deadline revision converted the hand-edited 2017 composite into a nearly static plate from `00:30` to `00:58`, then used a two-second dissolve at `00:58–01:00`. Those round-number blocks were rough editorial descriptions, not authored musical boundaries.

The replacement therefore keeps `00:00–00:30` intact and rebuilds the remainder of the first minute around the actual Delibes phrase turns already encoded in `music/score.json` and `render/choreography.json`.

## Final opening map

| Delivery interval | Authored function | Picture treatment |
|---|---|---|
| `00:00.000–00:30.000` | Protected opening | Keep the accepted 161-image forward/reverse scan unchanged. |
| `00:30.000–00:34.533` | Complete `sylvia-03` / ASSEMBLY | Continue the photographic scan while it resolves into the moving hand-edited 2017 composite. |
| `00:34.533–00:45.967` | Complete `sylvia-04` / ASSEMBLY | Let the hand-edited composite move as an image-object rather than sit as a static plate. |
| `00:45.967–00:57.400` | Complete `sylvia-05` / DIVISION | Transition from the moving composite into the matching-time canonical moving film. |
| `00:57.400–01:00.000` | Beginning of `sylvia-06` | Canonical moving film only; the splice at `01:00` is no longer an editorial event. |
| `01:00.000 onward` | Existing film | Preserve the previously rendered tail frame-for-frame. |

The source-score boundaries are `00:34.527651875`, `00:45.956211875`, and `00:57.384771875`. They are snapped once to the 30 fps delivery grid as frames `1036`, `1379`, and `1722`, yielding the displayed times above.

## Why this solves the opening

The second section no longer asks one image to survive for twenty-eight seconds through several musical changes. It now has three distinct jobs: the photo scan resolves; the 2017 composite becomes active; the canonical film takes over. Each change completes on a whole score phrase rather than on an arbitrary round timestamp.

The method introduces no synthetic dancer, motion interpolation, or generated replacement imagery. It uses only the registered photographs, the exact hand-edited composite, and the source-time-aligned canonical film. Motion in the composite section is a bounded camera path over the original image, followed by source-native crossfades.

## Reproducible render path

`submission/revise_screendance_opening.py` produces an exact 1,800-frame, 60-second H.264 replacement and a digest-bound `opening-revision-receipt.json`. The canonical macOS exporter then concatenates that replacement with the original film beginning at frame `1800` and preserves the audio master.

```bash
submission/prepare-screendance-macos.sh /absolute/new-output-directory
```

To preserve an already-exported approved first thirty seconds as the literal source asset, pass it as the optional second argument:

```bash
submission/prepare-screendance-macos.sh \
  /absolute/new-output-directory \
  /absolute/approved-first-30s.mp4
```

The optional asset must be a 1280×720 H.264 file at 30 fps with exactly 900 decoded frames. Without it, the script deterministically reconstructs the same approved 161-image score scan from the registered corpus.

## Approval boundary

The edit implementation and timing contract are complete. Final approval still requires watching the newly rendered full film, with special attention to the three phrase handoffs and the revised closing credit card. The existing Vimeo file remains the earlier deadline cut until a new full export is rendered and deliberately replaces it.
