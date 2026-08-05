# Score → motion A/B capture evidence (2026-08-05)

For one fixed (seed, stream, passage) the same absolute time is sampled with and
without the score clock. `score_delta` is exactly the choreography the score
contributes; the image alone (`without_score`) is the control. `audio.notes` is
what `planWebAudio` schedules in the same 250 ms window.

- score contract: `0979888d8b4aac050a8cabc6c9c5b358209694e2be2555d041887ccb5c78fe7d`
- seed: `0x12345678`, stream: `7`
- passage: `0` (t0=0s, 362.367024s) over 390 source seconds

**220 boundaries sampled** (25 structural, 22 where the score moves the image across the boundary, 31 with an audible note in-window). The full machine receipt is `score-to-motion-ab.json`; downbeats are in the JSON.

## Declared structural boundaries that move the image

`score_delta` = visual state WITH the score minus WITHOUT it at the same time (the choreography the score contributes). `score_transition` = the image motion the boundary itself causes under the score (just-after minus just-before, ±10 ms).

| t (s) | boundary | movement | beat | dynamic | score_transition (div, azi, ele, spread, projK, turn) | recast | hold | audio notes |
|---|---|---|---|---|---|---|---|---|
| 37.166 | movement ASSEMBLY | ASSEMBLY | 80 ↓ | 56 | d-0.050 | y |  | — |
| 37.166 | phrase assembly | ASSEMBLY | 80 ↓ | 56 | d-0.050 | y |  | — |
| 60.395 | movement DIVISION | DIVISION | 130 | 76 | d+0.031, a+0.037, e-0.036, s+0.031 | y |  | fixture-piano-ch1-p0 61@60.394504 |
| 60.395 | phrase division | DIVISION | 130 | 76 | d+0.031, a+0.037, e-0.036, s+0.031 | y |  | fixture-piano-ch1-p0 61@60.394504 |
| 60.395 | cue division-entry | DIVISION | 130 | 76 | d+0.031, a+0.037, e-0.036, s+0.031 | y |  | fixture-piano-ch1-p0 61@60.394504 |
| 111.498 | movement PHRASE | PHRASE | 240 ↓ | 104 | a+0.055, e+0.030, t+0.142 | y |  | fixture-piano-ch1-p0 62@111.497546 |
| 111.498 | phrase countable | PHRASE | 240 ↓ | 104 | a+0.055, e+0.030, t+0.142 | y |  | fixture-piano-ch1-p0 62@111.497546 |
| 111.498 | cue phrase-entry | PHRASE | 240 ↓ | 104 | a+0.055, e+0.030, t+0.142 | y |  | fixture-piano-ch1-p0 62@111.497546 |
| 118.931 | cue phrase-accent-a | PHRASE | 256 ↓ | 104 | a+0.001, e+0.001, s+0.038, t+0.166 | y |  | fixture-piano-ch1-p0 63@118.930716 |
| 122.647 | cue phrase-accent-b | PHRASE | 264 ↓ | 104 | a-0.045, t+0.229 | y |  | fixture-piano-ch1-p0 64@122.6473 |
| 126.364 | cue phrase-accent-c | PHRASE | 272 ↓ | 104 | e+0.049, t+0.300 | y |  | fixture-piano-ch1-p0 65@126.363885 |
| 209.058 | movement STILLNESS | STILLNESS | 450 | 36 | a-0.022, e+0.080 | y | y | fixture-piano-ch1-p0 66@209.057898 |
| 209.058 | phrase stillness | STILLNESS | 450 | 36 | a-0.022, e+0.080 | y | y | fixture-piano-ch1-p0 66@209.057898 |
| 209.058 | cue stillness-entry | STILLNESS | 450 | 36 | a-0.022, e+0.080 | y | y | fixture-piano-ch1-p0 66@209.057898 |
| 264.807 | movement RESEED | RESEED | 570 | 92 | a+0.124, e+0.121, p+0.052 | y |  | fixture-piano-ch1-p0 67@264.806671 |
| 264.807 | phrase reseed | RESEED | 570 | 92 | a+0.124, e+0.121, p+0.052 | y |  | fixture-piano-ch1-p0 67@264.806671 |
| 264.807 | cue reseed-entry | RESEED | 570 | 92 | a+0.124, e+0.121, p+0.052 | y |  | fixture-piano-ch1-p0 67@264.806671 |
| 286.177 | cue reseed-accent-a | RESEED | 616 ↓ | 92 | a-0.001, e+0.001, t+0.292 | y |  | fixture-piano-ch1-p0 60@286.177034 |
| 315.910 | cue reseed-accent-b | RESEED | 680 ↓ | 92 | a+0.001, e-0.001, t+0.391 | y |  | fixture-piano-ch1-p0 61@315.909713 |
| 358.650 | movement SIGNATURE | RESEED | 771 | 92 | d-0.920, a-0.787, e-0.336, s-1.000, p-0.550, t-1.600 | y |  | fixture-piano-ch1-p0 62@358.650439 |
| 358.650 | phrase signature | RESEED | 771 | 92 | d-0.920, a-0.787, e-0.336, s-1.000, p-0.550, t-1.600 | y |  | fixture-piano-ch1-p0 62@358.650439 |
| 358.650 | cue signature-entry | RESEED | 771 | 92 | d-0.920, a-0.787, e-0.336, s-1.000, p-0.550, t-1.600 | y |  | fixture-piano-ch1-p0 62@358.650439 |

## Declared structural boundaries that do not perturb the image

These land exactly on their declared time without a measurable image delta —
the choreography only moves what each movement declares.

| t (s) | boundary | movement | beat | dynamic | audio notes |
|---|---|---|---|---|---|
| 0.000 | movement ONE | ONE | 0 ↓ | 48 | fixture-piano-ch1-p0 60@0 |
| 0.000 | phrase origin | ONE | 0 ↓ | 48 | fixture-piano-ch1-p0 60@0 |
| 0.000 | cue origin-entry | ONE | 0 ↓ | 48 | fixture-piano-ch1-p0 60@0 |
