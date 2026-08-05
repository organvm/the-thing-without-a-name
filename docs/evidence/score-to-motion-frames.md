# Score → motion A/B — observable frames (2026-08-05)

The numeric A/B receipt proves the score moves the image in state
arithmetic. This renders the actual frame at each declared boundary,
WITH the score and WITHOUT it, at the same absolute time, and measures
the pixel difference. Every number here is a picture first: the contact
sheet shows the pair, and the PSNR is the number under it.

- score contract: `0979888d8b4aac050a8cabc6c9c5b358209694e2be2555d041887ccb5c78fe7d`
- seed: `0x12345678`, stream: `7`, passage: 0 (t0=0s)
- tier `screen` at 1024×768 on ANGLE (Apple, ANGLE Metal Renderer: Apple M5, Unspecified Version)
- contact sheet: `score-to-motion-frames.png` (sha256 `41eaaefa904b496e`)
- determinism: the WITH frame at t=0.0s rendered in a fresh process is **byte-identical** (`21afe5e36bf9` vs `21afe5e36bf9`). The instrument reports real differences or nothing; a rerun of the same input proves it.

## Why there is no identical control

The A/B numeric receipt samples the six camera channels and records `score_delta` as their difference. It never samples `material`. The score changes `material` (the plates drawn) even where all six camera channels are flat — at t=0 the pair legitimately differs by ~14.6 dB. So WITH vs WITHOUT is never the identity check; the determinism re-render is. The material coupling is itself part of what the score contributes, and it is visible in the sheet at every row.

## Boundary pairs

`Δmax` is the largest of the six camera `score_delta`s at that boundary;
PSNR is measured on the actual WITH vs WITHOUT pixels.

| t (s) | boundary | movement | Δmax | PSNR (with vs without) |
|---|---|---|---|---|
| 0.000 | origin ONE | ONE | 0.000 | 14.6 dB |
| 37.166 | movement ASSEMBLY | ASSEMBLY | 0.050 | 11.3 dB |
| 60.395 | movement DIVISION | DIVISION | 0.031 | 25.3 dB |
| 111.498 | movement PHRASE | PHRASE | 0.153 | 11.7 dB |
| 118.931 | cue phrase-accent-a | PHRASE | 0.226 | 11.6 dB |
| 122.647 | cue phrase-accent-b | PHRASE | 0.293 | 13.5 dB |
| 126.364 | cue phrase-accent-c | PHRASE | 0.359 | 14.1 dB |
| 209.058 | movement STILLNESS | STILLNESS | 0.161 | 13.0 dB |
| 264.807 | movement RESEED | RESEED | 0.127 | 15.4 dB |
| 286.177 | cue reseed-accent-a | RESEED | 0.314 | 14.8 dB |
| 315.910 | cue reseed-accent-b | RESEED | 0.424 | 14.6 dB |
| 358.650 | movement SIGNATURE | RESEED | 0.004 | 14.4 dB |
