# Campaign log — every test run, what it measured, and what survived

Complete inventory of the 5G teleoperation video programme. Host A publishes
(NVENC, 5G uplink), Host B subscribes (5G downlink), LiveKit SFU at the T-Mobile
edge between them. PTP over a dedicated cable for cross-host timing.

Method and instrumentation: `METRICS-REPLICATION-GUIDE.md`.
Withdrawal ledger and run rules: `RUN-DISCIPLINE.md`.

---

## 1. Inventory

| bucket / tree | runs | what it was for |
|---|---|---|
| `archive/01-baseline` | 3 | first paired runs, latency decomposition |
| `archive/02-staircase-noise` | 6 | the resolution collapse (A-series) |
| `archive/03-cheap-bars` | 2 | control: trivially compressible source |
| `archive/04-codec-av1` | 2 | first AV1 arms |
| `archive/05-live-camera` | 1 | real camera source |
| `archive/06-degraded-uplink` | 3 | behaviour on a collapsed uplink |
| `archive/07-e2-throughput-sweep` | 9 | bitrate cap sweep |
| `archive/08-gcc-out-pinned` | 3 | GCC removed, bitrate pinned |
| `archive/99-void` | 4 | runs voided at the time, kept as evidence |
| `results/overnight` | **27** | the bitrate × codec ladder, 200k–8000k |
| `results/overnight-void-softwareenc` | 3 | **void** — ran on OpenH264, not NVENC |
| `results/h264-2mbps-motion` | 1 | full-rate capture for motion assessment |
| `results/vbr-compare` | 2 | CBR vs `LK_NVENC_TARGET_QUALITY=20` |
| `results/selfloop` | 1 | Host B publish+subscribe through the SFU |
| `results/live-2500`, `live-2500-r2`, `locked-10m` | 3 | paired cells during the GCC-out work |

**Data on disk:** 34 GB results, 6.2 GB joint report tree, 50 MB archive.
808 raw I420 reference frames (2.4 GB) for PSNR.

---

## 2. What the programme established

**The link is not the constraint.** Zero packet loss in all 27 ladder cells from
200 kbps to 8 Mbps. Downlink measured 199 Mbps on a bulk transfer; uplink 15–17 Mbps
on Host B. Paced probes to the SFU carried 1.96 Mbps of media-sized packets at 0.0%
loss, and burst-shaped probes at the same average rate also lost nothing.

**Host A's SIM was throttled, and it explained a night of anomalies.** Same APN
(`fast.t-mobile.com`), same tower, same operator, Host B on marginally *worse* SNR —
and 1.1 Mbps against Host B's 16. A 1.62 Mbps stream into a 1.1 Mbps pipe overshoots
47%, and that cell lost 47% of frames. The throttle later lifted to 53 Mbps on its
own, so it was transient, not a plan tier. **Every capacity conclusion recorded before
this was measured against a throttled link.**

**Repair fails when retransmissions arrive outside the reorder window.** Server logs
showed 41.6% loss on Host A's uplink with **674 NACKs and zero satisfied**, and
`received packet too old, headSN 20385, sn 20065` — repairs landing ~320 packets
behind a 300-packet, 0.86 s bucket. The SFU cannot forward frames it never assembled,
which is why 78% of missing frames left no RTP sequence gap.

**A publisher's `DeleteRoom` evicts an already-joined subscriber.** Host B joined at
03:51:07 and was active; the publisher's pre-flight deleted the room at 03:51:21 —
four seconds before the agreed epoch — then recreated it with the same name and a new
SID. Likely the real cause of several earlier zero-row runs.

**The quality ladder (27 cells, PSNR against a 46.9 dB source):**

| kbps | H.264 | AV1 |
|---|---|---|
| 200 | 38.86 | 38.09 |
| 500 | 40.38 | 39.54 |
| 1000 | 41.00 | 41.20 |
| 2000 | 41.58 | 41.98 |
| 4000 | 42.42 | 42.87 |
| 8000 | 42.96 | 44.25 |

Smooth and monotonic. **The visible knee is ~1 Mbps**; 1→8 Mbps buys ~2 dB that is
barely perceptible. H.264 beats AV1 below 1 Mbps and costs a third of the decode.
**The 1.5–4 Mbps band the operator originally asked about is entirely above the knee.**

**H.264 saturates near 6 Mbps.** Given an 8 Mbps cap it delivered 5.0 and quality went
*backwards* 0.03 dB, QP floored ~24.7. Host A's grant check settles that this is the
encoder declining offered bits, not a grant ceiling: at 6000k it was granted 5.94 of
6.00 and spent 4.43. AV1 by contrast was still climbing at 8 Mbps (+1.60 dB, 99% of
cap spent).

**AV1 holds 29 fps at 1600×1300** on real content, contradicting a registered
expectation that it would not. Decode 2.33–9.84 ms against a 33.3 ms budget — no
decode ceiling anywhere on the ladder.

**Motion, measured once at full frame rate** (2 Mbps H.264, 2483 frames, 0 dropped):
no blocking even in dark regions, no content stutter, **smearing is the only real
artefact** — flat texture like carpet is smoothed away, whiteboard writing softens.
12.7% of frames were rendered >50 ms apart, but source capture was regular and
transport lost nothing: **that judder is Host B's render loop, not the link.**

---

## 3. Withdrawn — do not resurrect without new evidence

Sixteen. The withdrawal rate is the honest measure of how much to trust the survivors.

| # | claim | killed by |
|---|---|---|
| 1–9 | nine Phase-A hypotheses (BWE decay, SFU frame dropping, keyframe latency tail, uplink-grant loss, standing 2× gap, estimator settles at 1.2 Mbps, estimator is wrong, scaler never ratchets up, episode count non-monotonic) | see `RUN-DISCIPLINE.md` |
| 10 | "the quality scaler sheds the pixels" | `scaling_settings = kOff` on every hardware encoder |
| 11 | "CBR fills its target" | 0.44 Mbps sent against a 6.09 Mbps target |
| 12 | the A-series source was animated bars | it was noise; the withdrawal itself was wrong and cost 1.5 days |
| 13 | volume-to-collapse ~20–25 MB | 147.2 MB sent with no step |
| 14 | the cap sweep as a rate-response curve | five caps ascending once each on a link that moved 7.3× |
| 15 | "the SFU is withholding frames" | server logs: it never assembled them |
| 16 | "the subscriber never opened a socket" | it connected; a `DeleteRoom` evicted it |

**Withdrawn as criteria, not as claims:** `freeze_count` (reported up to 8 while
`total_freeze_duration_ms` stayed 0.000 in all 27 cells — it was the *only* clause
that ever fired, and produced scatter); absolute "≥29 fps" (specified against an
assumed 30 fps source when the publisher delivers 29.04).

**Under test, expected to become #17:** that `NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY`
with preset P4 is what sets the QP floor. `LK_NVENC_TARGET_QUALITY=20` produced a
*higher* QP (30.07 vs 28.21) and a *lower* bitrate (1.13 vs 1.72 Mbps) — the reverse
of the prediction. Pending confirmation from the publisher log that VBR engaged at all;
if it did, the hypothesis is refuted.

---

## 4. Runs voided, and why

Voiding is recorded rather than deleted, because a voided run is evidence about the rig.

| run | cause |
|---|---|
| `overnight-void-softwareenc` (3 cells) | server capability difference silently downgraded NVENC → OpenH264. Nothing in the data revealed it |
| overnight cells 1–3, first attempt | uncomposited Wayland surface: decoded perfectly, header-only CSV, all counters normal |
| overnight cell 1 (second attempt) | same, before the Xwayland fix landed |
| `live-2500` | Host A's RTSP source threw 535 local decode errors before any network |
| 4 runs in `99-void` | subscriber joined empty rooms the SFU then deleted |
| `h264-2mbps-motion`, first attempt | publisher died 4 ms after its epoch: `LIVEKIT_API_KEY` unset |

---

## 5. Coordination protocol, arrived at the hard way

1. Agree a wall-clock **epoch and duration as explicit numbers**, ≥30 s out. A change
   to either after arming is an abort, not an adjustment. *(We armed 1800 s against
   300 s once because duration travelled in prose while the epoch travelled as a
   number.)*
2. Start a runbook script at **T minus its pre-flight**, not at T.
3. **Join rooms by name from the agreed schedule.** Discovery is worse: `ListRooms`
   returned null publisher counts on one deployment, `int(None)` crashed the helper,
   and the caller logged "no live room found" while 21.5 MB was flowing into it.
4. **A launch is not confirmed until the process has produced output.** `nohup … &`
   always succeeds. Report `LIVE`/`DEAD`, never a pid.
5. **Never rsync over the PTP cable during a cell** — it costs ~2.4× on `ptp4l` rms.
6. **A standing instruction does not survive proof that its premise is false** — an
   instruction not to restart presumes a run is collecting data.
7. **Both hosts capture; neither host's numbers alone describe the path.** Send-side
   health is not delivery: a publisher reported 147 MB with no collapse while the
   receiver got 22% of frames.

---

## 6. Open

- **Bitstream recorder.** ~19 MB/cell of received encoded frames instead of 14 GB of
  raw I420, and the only practical way to assess motion routinely.
- **`LK_NVENC_TARGET_QUALITY` with a cap well above what the target costs** — the
  2 Mbps cap may be why it undershot.
- **AV1 at 2 Mbps in motion.** The H.264 recommendation rests partly on decode
  latency; motion is where AV1's temporal tools could earn it back. Untested.
- **Whether the DSCP→5QI rules are provisioned on the carrier slice.** Until answered,
  a QoS test returns a clean null that cannot be distinguished from success.
- **Host B's render loop** — 12.7% of frames >50 ms apart is local, and unexplained.
- **Host A's capture loop** delivers 29.04 fps not 30: relative pacing accumulating
  ~1.1 ms of sleep overshoot per frame. Diagnosed, not fixed (fixing mid-campaign
  would break comparability).
