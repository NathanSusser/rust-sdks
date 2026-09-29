# Host B result archive

Bucketed by the question each run was asked, not by when it ran.

| Bucket | What it tested |
|---|---|
| `01-baseline` | Early calibration runs, pre-manifest, no argv record |
| `02-staircase-noise` | The resolution staircase on pseudo-random noise. `results-a3` is the original; `results-r4noise` is its replication on 2.5x the uplink |
| `03-cheap-bars` | Trivially compressible content. `results-r2paired` carries the 100.2% publisher-vs-subscriber cross-validation |
| `04-codec-av1` | AV1 arms. `results-r5av1` is superseded by `results-r6av1fix` (post QP-fix) |
| `05-live-camera` | Real IP camera at production geometry — held 1600x1300 with zero steps |
| `06-degraded-uplink` | Runs taken while the publisher uplink was collapsed; confounded by the link |
| `07-e2-throughput-sweep` | The E2 cap sweep. **Only 4 of 12 cells captured on this host** — see note below |
| `99-void` | No usable rows. `results-r2` = locked Wayland; `results-cam2500k` and `results-e1gate` = empty-room deletion; `results-e1gate2` = sampler recycled-buffer fault |

## E2 sweep, this host

Captured: 500k-r1, 1000k-r1, 1500k-r1, 1000k-r2. The other eight cells were skipped
because the subscriber overran its window and the driver refuses to start a cell
late — a cell begun after its scheduled instant is not the cell that was scheduled.

Delivered resolution ladders, from the subscriber's own `frame_width` column
(independent of the frame sampler, which was faulty):

    500k    800x648@0s -> 600x480@31s -> 400x324@61s -> 600x480@101s
    1000k   1600x1300@0s -> 1200x972@10s -> 800x648@15s -> 600x480@35s -> 800x648@85s
    1500k   1600x1300@0s -> 1200x972@52s -> 800x648@57s

These agree with the publisher-side ladders on rungs, order and direction, offset by
the ~32 s between the publisher's start and this host's first received frame.

## What these PDFs do and do not show

Subscriber-side only: per-frame receive, decode and render timing, delivered
resolution and bitrate, loss and freeze counters. They do **not** carry a quality
measure — no PSNR or SSIM — because the decoded-frame sampler was reading recycled
buffers and its output is not usable. No received pixel in this archive has ever
been compared against a sent pixel.

## E2 rerun, 2026-09-09 — one usable cell from fourteen

Attempted 12 sweep cells plus a 2-cell 8 Mbps addendum. Outcome:

| Fate | Cells | Cause |
|---|---|---|
| Usable | 1 | `e2r-500k-r1` — 3712 frames, 0.1% retransmission, both halves + report |
| Void, no publisher | 3 | Host A's runner loop was drained by `ssh` reading its stdin; it exited 0 and logged completion |
| Void, drowned | 4 | 31–38% retransmission — see below |
| Never started | 6 | Stopped once the cause was understood |

**Why the high-cap cells produced nothing.** Host A's uplink had fallen to
0.63–1.28 Mbps. The runs pinned the encoder to the cap (`LK_PIN_BITRATE_TO_MAX=1`)
so that cap would be the independent variable — which also defeats congestion
control. Pushing 5 and 10 Mbps into a 1 Mbps link drowned it: a third of packets
became retransmissions that also drowned, the SFU kept requesting keyframes
(6 → 22 → 44 as the cap rose), and this host received exactly one keyframe per
cell and then silence.

**The pin converts a measurable degradation into no measurement.** Unpinned, a
5 Mbps cell on a 1 Mbps link steps down and produces a resolution ladder, which is
data about what the link delivers. Pinned, it produces nothing. That is a cost of
the design, not a defect in it.

**A drowned cell passes the existing admission gate perfectly**, because the grant
was pinned at the cap throughout. A third test is needed: reject any cell whose
retransmission rate exceeds 5%. Tonight's data separates cleanly — 0.1% against
31–38%, no judgement call.

