# Results archive — 5G teleoperation video latency rig

Every result either host holds, in buckets named for **the question the run was
asked**. The old names (`a1`, `a2off`, `arm2b`, `r2p`, `r5av1`, `round5b`) encode
nothing about source, codec, geometry, cap or link, which made the archive
unreadable without reading each log. `MANIFEST.csv` records those five facts plus
a validity verdict for all 27 runs.

Raw data is gitignored. `MANIFEST.csv`, this file, and the plan under
`teleop-test-matrix/docs/` are the committed record.

## Buckets

| Bucket | Question | Runs |
|---|---|---|
| `01-baseline-and-calibration` | Does the rig work, and what is the uplink? | round4, round5a, round5b, probe, uplink-calibration |
| `02-encoder-probes` | Which codec/encoder combinations come up at all? | av1, h264nvenc, noprev |
| `03-resolution-staircase-noise` | Why does the picture shrink? | a1, a1r1, a2, a2off, a3, r4 |
| `04-cheap-content-control-bars` | Same geometry, cheap content — does it still shrink? | r2, r2p |
| `05-codec-av1` | Is AV1 usable on this path? | r5av1, r6, r7 |
| `06-live-camera` | What does real content cost? | smoke, live-2500k, live-2500k-b |
| `07-degraded-uplink` | Runs whose link or window broke under them | arm1, arm2b, r1 |
| `08-offline-content-cost` | Content cost with no network in the path | sweep.csv |
| `99-void` | Runs that measure nothing | a3r1, optimized |

## The four results that matter

Everything else is supporting detail or a failed run.

1. **`03/results-a3` and `03/results-r4`** — the staircase, and its replication on
   2.5× the uplink. Same rungs, same order, same terminal resolution (640×360),
   9.59 vs 10.02 Mbps delivered, 1.39 vs 1.45 bpp, decode p50 3.65 vs 3.63 ms.
   The mechanism is content cost against grant: 1080p noise wants ~1.4 bpp and a
   10 Mbps grant at 1080p30 offers 0.160. QP pins at the H.264 ceiling of 51
   before every down-step and relaxes to 38 on reaching an affordable rung.
   **Bandwidth is excluded by measurement, not by argument** — 15 Mbps of uplink
   was never asked for and the collapse happened anyway.

2. **`04/results-r2p`** — the same geometry and cap on cheap content, with **zero**
   steps and 0.52 Mbps delivered against a 10 Mbps grant. This is what makes (1)
   a statement about the source rather than about the pipeline. It also
   cross-validates the two measurement legs at 100.2%: publisher outbound
   0.521 Mbps against subscriber inbound 0.521 Mbps, opposite ends of a two-hop
   path, different codebases.

3. **`06/live-2500k-b`** — real camera content at production geometry (1600×1300),
   2.508 Mbps against a 2.5 Mbps cap, **zero resolution changes across 2464
   frames**, zero loss, decode p50 2.65 ms, cross-host and PTP-synced. Both halves
   exist. This retires the staircase as a property of the synthetic source.

4. **`08/sweep.csv`** — the same camera content re-encoded offline at production
   geometry, measured from the files rather than transcribed:

   | Setting | Delivered | bpp | Fraction of a 10 Mbps budget | PSNR-Y | SSIM-Y |
   |---|---|---|---|---|---|
   | 2500k target | 2.502 Mbps | 0.0401 | 0.25× | 42.50 dB | 0.9830 |
   | 5000k target | 5.005 Mbps | 0.0802 | 0.50× | 44.22 dB | 0.9870 |
   | 7500k target | 7.527 Mbps | 0.1206 | 0.75× | 45.02 dB | 0.9884 |
   | 10000k target | 10.051 Mbps | 0.1611 | 1.01× | 45.56 dB | 0.9893 |
   | crf 18 | 8.666 Mbps | 0.1389 | 0.87× | — | — |
   | crf 23 | 2.708 Mbps | 0.0434 | 0.27× | — | — |
   | crf 28 | 1.173 Mbps | 0.0188 | 0.12× | — | — |

   Real content at crf 23 costs **0.0434 bpp against a 0.160 budget — 3.7×
   headroom**. Even crf 18, visually near-transparent, fits at 0.87×.

   The `.mp4` intermediates (200 MB) are **not** preserved; `sweep.sh` regenerates
   them. Earlier reports quoted SSIM values of 0.9812/0.9857/0.9874/0.9885 — those
   were SSIM-**All**, not SSIM-Y; both columns are in `sweep.csv`.

## What the archive does not contain

- **No local-ethernet or local-SFU point.** Every run in this tree is
  A → T-Mobile SFU → B over 5G. Both hosts confirm this; the "test over local
  ethernet" condition was specified but never run.
- **No throughput sweep.** Only 2.5 Mbps has been run on real content, twice, and
  only in H.264. 5, 7.5 and 10 Mbps exist offline only (bucket 08).
- **No AV1 point on real content.** Every AV1 number comes from noise or bars.
- **No quality metric from any live run.** PSNR and SSIM exist only offline; the
  live runs measure bitrate, resolution, QP and latency, and infer quality from QP.

## Caveats attached to specific runs

- **The a-series source was misidentified for a day and a half.** `a1`–`a3` were
  analysed as animated bars; they are pseudo-random noise, which the run logs said
  on line one. Any conclusion drawn from those runs before 2026-09-06 should be
  re-derived, not re-read.
- **`arm2b`'s manifest claims `exit_reason: completed`.** It was killed by hand.
  The string was hardcoded; it is now derived from an actual shutdown cause.
- **`r1` overran its logging window by ~19 minutes** — 15405 rows against a
  3600-frame bound — because the window's end test used `==` and the exact frame
  ID had roughly a 1-in-26 chance of surviving the pipeline. Fixed to `>=`.
- **`results-optimized` is unidentifiable.** No manifest, no log, no argv record.
  It is in `99-void` because a reading that cannot be attributed to a
  configuration is not a measurement.
