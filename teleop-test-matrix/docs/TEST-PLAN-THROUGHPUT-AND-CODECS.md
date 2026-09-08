# Test plan — throughput, codecs, and the resolution/bandwidth/quality tradeoff

Registered before any run in it starts. Predictions in §6 are recorded so they
can be scored rather than reconstructed, which is the standing rule after a
programme with a twelve-row withdrawal ledger.

---

## 1. The tradeoff is one equation

Resolution, framerate and bitrate are not three independent knobs. They collapse
into a single currency:

```
bits per pixel  =  bitrate / (width x height x fps)
```

At production geometry — 1600x1300 at 30 fps, or 62.4 Mpx/s — a 10 Mbps budget
buys **0.160 bpp**. Everything the programme has established is a statement about
bpp:

| Content | Cost at an acceptable quantiser | Against a 0.160 budget |
|---|---|---|
| Pseudo-random noise (1080p) | ~1.4 bpp | **9x short** — hence the staircase |
| Animated bars | ~0.007 bpp | 23x spare — encoder undershoots its grant |
| **Real camera footage, crf 23** | **0.0434 bpp** | **3.7x spare** |
| Real camera footage, crf 18 (near-transparent) | 0.1389 bpp | 1.15x spare |

So the three-way tradeoff resolves like this:

- **Bandwidth sets the bpp you can afford.** It is the only term set outside the
  encoder, and on 5G it is stochastic, not a number.
- **Resolution sets how far that bpp is spread.** Halving linear resolution
  quadruples bpp at the same bitrate. Below some bitrate, spending fewer pixels
  well beats spending more pixels badly. That crossover is what §5's E3 measures.
- **The codec sets the quality you get per bpp.** It moves the whole curve; it
  does not change the currency.
- **Quality is the output**, and quantiser (QP) is the encoder's own report of how
  hard it had to squeeze to fit. QP is a *within-codec* proxy, never a
  cross-codec one — see §2.

The offline CRF anchors above turn out to predict the live path's behaviour, which
is what makes a sweep worth running rather than simulating:

| Live cap | bpp at 1600x1300@30 | Nearest offline anchor |
|---|---|---|
| 0.5 Mbps | 0.0080 | below crf 28 |
| 1.0 Mbps | 0.0160 | just under crf 28 (0.0188) |
| 1.5 Mbps | 0.0240 | between crf 28 and 23 |
| 2.5 Mbps | 0.0401 | just under crf 23 (0.0434) |
| 5.0 Mbps | 0.0801 | between crf 23 and 18 |
| 10.0 Mbps | 0.1603 | just above crf 18 (0.1389) |

The 2.5 Mbps live run measured **QP p50 24.4** on a 0-51 scale. x264 spends
crf 23 at 0.0434 bpp; NVENC was given 0.0401 bpp and chose QP ~24. Two different
encoders, offline and live, landing in the same place. **This agreement was
noticed after both measurements existed** and is not offered as a registered
prediction — but it is why P2 below is worth registering.

---

## 2. The blocking gap: we cannot currently measure quality on the live path

Every quality number the programme holds was produced **offline, by ffmpeg, with
no network in the path**. Every live run measures bitrate, resolution, QP and
latency, and *infers* quality from QP. That is adequate for one codec and
worthless across codecs:

- H.264 QP is 0-51. AV1 qindex is 0-255, and the mapping between them is
  non-linear. **"AV1 at QP 30 vs H.264 at QP 30" is not a comparison.** An encoder
  sweep scored on QP would produce a confident, meaningless answer.

So the encoder comparison in E4 **cannot run** until the live path yields PSNR or
SSIM against a known reference. E1 exists to build exactly that, and produces no
results of its own.

Two further gaps, recorded as gaps rather than discovered later:

- **No local-ethernet or local-SFU point exists.** Every run in the archive is
  A -> T-Mobile SFU -> B over 5G. Both hosts confirm this. The condition was
  specified and never run.
- **The subscriber's downlink has never been probed.** Manifests probe uplink on
  both sides, but a subscriber's uplink carries RTCP and little else; its downlink
  is the video path.

---

## 3. The fixture: a fixed clip, not the live camera

**The sweep must not use the live camera.** This is not a preference; the harness's
own `--camera-source` documentation states the reason, and it applies with full
force to a sweep:

> Every camera is an opt-in realism spot-check and never a matrix default or a
> swept axis: a lens makes bitrate depend on scene content, lighting and framing,
> which breaks the cross-host comparability every cell rests on.

A sweep compares cells. If the content differs between cells — different light,
different motion, a person walking through frame — then bitrate and quality differ
for reasons that have nothing to do with the axis being swept, and no amount of
care in analysis recovers the comparison. The live camera runs stay as realism
spot-checks; they cannot carry a sweep.

**The fixture** is `results/08-offline-content-cost/ref.mp4` — 901 frames, 30 s,
1600x1300 at 30 fps, real camera content, already the reference for every offline
quality number we hold. Served in a loop over RTSP:

```
ffmpeg -re -stream_loop -1 -i ref.mp4 -c copy -f rtsp rtsp://127.0.0.1:8554/fixture
```

This needs **no harness change** — `--camera-source` already accepts an `rtsp://`
URL. It also makes the reference free: both hosts hold the same file, so a decoded
frame can be compared against its source by frame ID without shipping reference
frames anywhere.

**Determinism must be verified, not assumed** (E1). `-c copy` re-sends the same
encoded frames each loop, so the *source* is bit-identical across cells; what needs
checking is that frame IDs align to source frames stably across a loop boundary.

---

## 4. Metrics

Four groups. The fourth is an admission gate, not a result: a run that fails it is
not a data point, and every gate in it exists because a specific past run died of
its absence.

### 4.1 What we spent

| Metric | Source | Why |
|---|---|---|
| `bitrate_delivered_mbps` | publisher outbound | what was actually sent, not the cap |
| `bpp` | derived | the currency; makes cells comparable across geometry |
| `media_bytes / transport_bytes` | publisher | padding and header overhead; measured 1.039 with no padding |
| `grant_vs_cap` | BWE target vs `--max-bitrate` | distinguishes "encoder chose less" from "estimator offered less" |

### 4.2 What we got

| Metric | Source | Why |
|---|---|---|
| **`psnr_y_db`, `ssim_y`** | offline, decoded vs fixture by frame ID | the only cross-codec quality measure. **New in E1** |
| `encoded_resolution`, `resolution_steps` | publisher | the staircase, counted |
| `fps_delivered` | subscriber | AV1 managed 10 fps on noise at 1080p; throughput is a real constraint |
| `qp_p50`, `qp_p95` | publisher | within-codec squeeze; **never compared across codecs** |
| `quality_limitation_reason` | publisher | separates bandwidth from CPU from none |
| `frames_lost`, `frames_dropped`, `freeze_count` | subscriber | quality has a failure mode that PSNR averages away |

### 4.3 What it cost in time

`encode_ms`, `transport_ms`, `decode_ms`, `recv_to_render_ms`, and end-to-end
glass-to-glass at p50/p95/p99. p99 is the one the deliverable is scored against;
a p50 that looks fine with a p99 that does not is a failed operating point.

The latency budget itself is **a parameter this plan does not set** — it comes from
the teleoperation literature, not from us. The plan's structure does not depend on
its value: every cell reports the full latency decomposition, so any budget can be
applied afterwards. What we can state is the measured cost of the pieces: decode
p50 2.65 ms and transport p50 34.4 ms on real content at 2.5 Mbps, cross-host.

### 4.4 Admission gates

| Gate | Threshold | The run that taught us |
|---|---|---|
| Uplink probed at start **and** end | spread <= 1.5x | arm1, r1 — link collapsed mid-run, unrecoverable |
| PTP `rms` recorded from the slave | recorded, not assumed | a grandmaster emits no `rms` and cannot verify itself |
| `phc2sys` restart count, start and end | recorded | 97 restarts in one day; no run has overlapped one, checked by hand |
| Negotiated codec == requested codec | exact | a silent VP9 fallback would pass every other check |
| Source actually used == source intended | exact, from the log's own line | the a-series was analysed as bars for a day and a half; it was noise |
| `exit_reason` derived from a real cause | never a literal | arm2b was killed by hand and its manifest said `completed` |
| Both halves present and non-empty | pub and sub rows > 0 | r2, livecam-2500k — publisher fine, subscriber zero rows |
| Process list clean before start | verified, no `pkill -f` | `pkill -f` matched its own shell four times across both hosts |
| Logging window closes on `>=`, not `==` | code-level, fixed | r1 overran by 19 minutes on a 1-in-26 chance |

---

## 5. Experiments

Fixed for every cell unless it is the swept axis: fixture over RTSP, 1600x1300,
30 fps, `--publish-only` on Host A, Host B as the sole subscriber,
`--buffering-mode zero_jitter`, `--attach-timestamp --attach-frame-id`, 120 s with
10 s warmup. Every invocation goes through `--validate-args` before the sweep
starts, so a CLI drift fails in a second rather than after the first cell.

### E1 — Reference plumbing (prerequisite, produces no results)

1. Serve `ref.mp4` in a loop over RTSP; confirm the publisher's encoded output is
   stable across two loop passes.
2. Sample decoded frames on the subscriber, keyed by in-band frame ID. Full I420
   at this geometry is 94 MB/s; **sample every 30th frame** (3.1 MB/s, ~380 MB per
   120 s run) rather than all of them.
3. Compute PSNR-Y and SSIM-Y offline, decoded sample vs the fixture frame of the
   same ID.
4. **Validate the pipeline against a known answer**: run one cell at 10 Mbps and
   check its PSNR lands near the offline sweep's 45.56 dB. If the plumbing is
   wrong, this is where it shows, not three experiments later.

Gate: E4 does not start until E1 step 4 passes.

### E2 — Throughput sweep (H.264)

Six caps, two runs each: **0.5, 1.0, 1.5, 2.5, 5.0, 10.0 Mbps**.

The interesting region is *below* 2.5 Mbps, not above it. 2.5 Mbps has already
been run twice on real content and held 1600x1300 with zero steps and QP p50 24.4
— no distress at all. A sweep upward from there would confirm what is already
known; the knee is downward.

### E3 — Resolution ladder at fixed bitrate

Fixed 2.5 Mbps, four geometries at constant aspect ratio:

| Geometry | Mpx | bpp at 2.5 Mbps |
|---|---|---|
| 1600x1300 | 2.080 | 0.0401 |
| 1280x1040 | 1.331 | 0.0626 |
| 960x780 | 0.749 | 0.1113 |
| 640x520 | 0.333 | 0.2504 |

This is the experiment that answers "resolution vs bandwidth" directly, and it has
a methodological choice that must be stated rather than buried: **every rung is
scored by PSNR against the full-resolution fixture**, so a downscaled encode is
penalised for the blur its upscale introduces. That is the correct comparison for
an operator looking at a fixed-size display — which is the deliverable — but it is
not the only defensible one, and a rung that wins here would also win less
decisively when scored at its native resolution.

Repeat at 1.0 Mbps if E2 shows distress there, since the crossover is expected in
that region.

### E4 — Codec comparison

H.264 vs AV1 at matched caps: **1.0, 2.5, 5.0, 10.0 Mbps**, two runs each.
VP9 as a third arm only if AV1 fails to hold 30 fps, since it would then be the
better modern-codec candidate on this hardware.

H.265 is **not** available — the harness offers h264, vp8, vp9, av1 only, despite
the SFU's hostname containing `h265`.

Two things must be true for an AV1 number to mean anything, and both have failed
before: the negotiated codec must actually be `av1`, and the encoder must hold
30 fps at 2.08 Mpx. On 1080p noise it managed 10 fps.

### E5 — Capacity characterisation (background, continuous)

The 5G uplink has been measured at 0.03, 0.15, 1.9, 12, 24, 28, 29.7 and 33.5 Mbps
across this programme. A spot probe is not a capacity. Probe every 5 minutes for
24 hours and report the distribution.

**This changes what the sweep is for.** An operating point chosen against the mean
capacity fails half the time. The point worth shipping is the one that degrades
acceptably at the 5th percentile, and that percentile is currently unknown.

---

## 6. Registered predictions

Stated to be scored. A prediction whose falsifying condition never arises is
**untested**, not confirmed.

**P1 — the knee is below 2.5 Mbps.** No resolution steps at 2.5 Mbps or above.
First steps at or below 1.5 Mbps.
*Falsified by* any step at >= 2.5 Mbps, or by no step even at 0.5 Mbps (0.0080 bpp,
below crf 28) — which would mean the scaler is not engaging on this content at all
and the mechanism differs from the noise case.

**P2 — offline predicts live within 1.5 dB.** Live PSNR-Y at each cap lands within
1.5 dB of the offline sweep at the same cap (42.50 / 44.22 / 45.02 / 45.56 dB at
2.5 / 5 / 7.5 / 10 Mbps).
*Falsified by* a gap above 1.5 dB, which means either NVENC is materially worse
than x264 at equal bitrate, or the E1 reference alignment is broken. Both are worth
knowing and the second is more likely.

**P3 — full resolution wins at 2.5 Mbps.** 1600x1300 beats every downscaled rung on
PSNR at 2.5 Mbps, because 0.0401 bpp is already near crf 23 on this content. The
crossover where downscaling wins is below 1.0 Mbps.
*Falsified by* any rung beating full resolution at 2.5 Mbps.

**P4 — AV1 buys 25-40% bitrate, if it can keep up.** AV1 reaches H.264's PSNR at
60-75% of the bitrate on this content.
*Falsified by* AV1 needing >= 90% of H.264's bitrate, **or** by AV1 failing to hold
30 fps at 2.08 Mpx — in which case P4 is not tested, it is moot, and the finding is
a throughput ceiling rather than a compression comparison.

**P5 — the decode model holds out of sample.** Host B's fit
`decode_ms = 0.2267 x kB + 1.73` was built entirely on pseudo-random noise and
colour bars. Its intercept is a per-pixel floor measured at 1080p (2.07 Mpx), and
1600x1300 is 2.08 Mpx — a 0.3% difference, so the floor transfers unscaled rather
than needing rescaling. At 2.5 Mbps and 30 fps, 10.4 kB/frame predicts **4.11 ms**.
*Falsified by* >30% error. This is a genuine out-of-sample test: a model fitted on
synthetic content predicting real footage at a real budget.

---

## 7. What each outcome licenses

| Outcome | What it settles |
|---|---|
| P1 holds, knee below 1.5 Mbps | Production geometry is safe at any plausible operating point, and the staircase is a property of synthetic content. **Decisive for the deliverable** |
| P1 fails, steps at 2.5 Mbps+ | Real content also triggers the scaler; the mechanism is broader than content cost and the whole content-cost account needs revisiting |
| P2 holds | Offline transcoding is a valid proxy for the live path, and future sweeps can be run in minutes instead of hours |
| P2 fails | Live and offline are not interchangeable; every offline number in the archive becomes suggestive rather than predictive |
| P3 holds | Never downscale at these bitrates. Simplifies the shipping config to one geometry |
| P3 fails | There is a resolution ladder worth implementing, and its rungs are now measured |
| P4 holds and AV1 keeps up | AV1 is worth its decode cost when the link binds. Decision becomes budget-dependent, and E5 supplies the budget |
| P4 moot (throughput ceiling) | AV1 is out on this hardware regardless of its compression, and that is a hardware finding, not a codec one |

---

## 8. Where the reports come from

Decided up front rather than when the reports are due, because `--publish-only`
changed the answer and made it non-obvious.

**Host A writes no `.sub.csv` any more.** With `--publish-only` there is no local
receiver, so A produces `<prefix>.pub.csv` plus the JSON-lines snapshots and
nothing else. `generate_frame_report.py` pairs a publisher and a subscriber CSV by
frame ID, so **every PDF in this plan depends on Host B's half arriving.**

Three consequences:

1. **There is no publisher-only arm.** Every cell in E2, E3 and E4 requires B
   logging. An arm where only one host logs is an arm with no report, and the
   "both halves present and non-empty" admission gate already voids it — so a
   missing subscriber half means the cell is re-run, not analysed.
2. **Both hosts hold both CSVs and the report for every cell.** B ships its
   `.sub.csv` to A after each cell and A ships the `.pub.csv` back, per the
   standing rule that both sides hold both halves. A cell whose halves live on
   separate machines is one machine's crash away from being unpairable.
3. **Thirty per-cell PDFs is the wrong deliverable.** Nobody reads thirty frame
   reports. Instead:
   - **Per cell**: both CSVs, the snapshots, and the manifest, archived. No PDF.
   - **Per experiment**: one sweep report — E2, E3, E4 — plotting each metric
     against the swept axis, which is the artefact that actually answers the
     question the experiment was asked.
   - **On demand**: a per-cell frame report for any cell that looks anomalous in
     its sweep report. That is what the per-frame decomposition is *for* — it
     answers where a given frame's latency went, which is a drill-down question,
     not a summary one.

The sweep reports are the deliverable. The per-cell frame reports are the
instrument.

## 9. Cost and division of labour

About 30 runs at ~3 minutes each including setup: **~90 minutes of link time**,
plus E1's plumbing work and E5 running in the background for 24 hours
independently of everything else.

Proposed split, per Host B's own proposal:

- **Host A** owns this plan, the results manifest, the fixture and the publisher
  side, and pushes those.
- **Host B** owns the subscriber side — including the decoded-frame sampling that
  E1 requires and the inactivity-timeout fix — and pushes those.
- Neither host edits the other's files without saying so. `--ff-only` with an
  explicit handoff, as `RIG-CHANGES.md` already specifies.

One rule adopted from B's timeout post-mortem, because it generalises well past
timeouts:

> **A timeout verified against a simulated failure has been verified against your
> model of the failure, not the failure.** Any test where you construct the fault
> is testing your imagination of it.

B's inactivity timeout was verified with SIGKILL, which stops frames without
unpublishing. A clean unpublish — the common case — breaks the stats loop on its
track-sid check before the inactivity branch is ever evaluated, so the timeout
never fires. It cost 7:51 of a subscriber sitting on an empty room with 2,489 rows
and no frames.
