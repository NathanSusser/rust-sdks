# Measurement design — LiveKit Rust teleoperation test matrix

Requirements traceability. All three pages fetched live from the Confluence REST API on
**2026-08-24**; `matrix.yaml` `meta` must record these verbatim.

| Page | Title | Version | Last modified | Role |
|---|---|---|---|---|
| 556974310 | Teleoperation — Product Requirements Document | **32** | 2026-08-05 | Normative. Wins all conflicts. |
| 579869319 | Teleoperation Test Matrix | **2** | 2026-08-22 | Governing test spec. |
| 574818569 | Teleoperation — Solution Architecture | **1** | 2026-08-17 | Context. |

Every symbol named in the mapping table below was read in this checkout at the cited
`file:line`. Where a citation in the master prompt no longer matches, the correction is
noted in §9.

---

## Amendment 2026-08-24 — `buffering_mode` is locked to `zero_jitter`

**This supersedes the buffering-mode parts of §3, §4 and §5 below. Those sections are
left as written, because the reasoning that led here is worth keeping; read them as
history, not as current configuration.**

`buffering_mode` is no longer an axis. It is fixed at `zero_jitter` for every cell in the
matrix. The room-level playout-delay hint modes (`playout_hint_floor`,
`playout_hint_smooth`, via `create_room_with_playout_delay`) are **retired**, and with
them the `V0_playout_units` validation-gate suite.

**Measured.** Tier 0 smoke runs against LiveKit Cloud, with
`jitter_buffer_delay_avg_ms` properly differenced from the cumulative
`jitter_buffer_delay_s` / `jitter_buffer_emitted_count` pair:

| mode | requested | `jitter_buffer_delay_avg_ms` | target/minimum delay |
|---|---|---|---|
| `playout_hint_floor` | min 0, max 10 ms | ≈ 5.9 ms | ≈ 43.2 ms |
| `playout_hint_smooth` | min 400, max 2000 ms | ≈ 6.4 ms | — |
| `zero_jitter` | true 0/0 | ≈ 2.9 ms | ≈ 25.7 ms |

The two hint modes are indistinguishable despite a 100–400x difference in requested
buffer. `zero_jitter` is lower on two independent metrics, so it is a genuinely different
and effective mechanism rather than a relabelling.

**Confirmed by the docs.**
<https://docs.livekit.io/robotics/media/performance/low-latency/playout-delay/> states
that zero jitter buffer mode *"is equivalent to setting both the minimum and maximum
playout delay to 0 ms, which isn't supported when using playout delay hints."* A true 0/0
is architecturally unreachable through the room-level hint API — §4 below reached the same
reading from the same sentence. The floor cell's ≈ 6 ms was therefore never a defect: it
is libwebrtc's natural floor under a 10 ms cap on a clean link, and there was no true zero
for it to reach.

**Directed.** The user has asked that the matrix align with LiveKit's robotics guidance
and use `zero_jitter` exclusively. This project measures teleoperation, and
<https://docs.livekit.io/robotics/media/performance/low-latency/> is explicit that
minimizing buffering matters more than playback smoothness — *"an operator needs to see
what the robot sees now."*

**Consequences.**

- §5's baseline-portability argument (a treatment cannot be its own control, so the
  portable hint mode had to be the baseline) no longer applies: with no buffering sweep
  there is no buffering delta and nothing for a control to control for. Codec portability
  is untouched — `h264` remains the reference codec for the reasons in §5 note (a).
- §4's playout-delay units question is **moot by construction, not unresolved**. The
  harness never calls `create_room_with_playout_delay`, so there is no API unit left to
  disambiguate. `V0_playout_units` and its gate are removed from `matrix.yaml`,
  `run_schema.json` and `parse_runs.py`; there is now no validation gate in the matrix.
  **Re-adding any hint mode must re-add the suite and the gate.**
- §3's "required cell" for AV1 is now keyed on codec alone. It was
  `video_codec: av1 × buffering_mode: zero_jitter`; with every cell at `zero_jitter` the
  buffering half is vacuous, and the cell it names is unchanged.
- T-3 is **unaffected**. Its `playout_window_ms` axis is the *control-path* application
  deadline that `control_late_pct` is measured against — a harness parameter with no
  relationship to the video jitter buffer or to the retired hints. The earlier revision
  merely *held* buffering at the hint floor while sweeping it; the curve is as well-defined
  under `zero_jitter` as before. Only the held value changed. The name collision between
  `playout_window_ms` and "playout delay" is the trap here, and it is a genuine trap.
- The Rust harness's `--buffering-mode` flag no longer accepts the hint modes at all
  (`PlayoutHintFloor`/`PlayoutHintSmooth` were removed from the `BufferingMode` enum,
  along with `--playout-delay-min-ms`/`--playout-delay-max-ms` and the room-level
  `create_room_with_playout_delay` call path). Only `default` and `zero_jitter` are valid
  values now. `run_schema.json` retains `playout_delay_min_ms`/`playout_delay_max_ms` as
  permanently-null, documented-retired fields — that is an analysis-side decision to
  avoid invalidating pre-retirement run records, not evidence the harness still emits
  them.

---

## Amendment 2026-08-25 — `rtt_*` is split into `network_rtt_*` and `app_rtt_*`

**This supersedes the single `rtt_p50/p95/p99_ms` metric in §1d and the `rtt_*` threshold
rows in §6.** Driven by the first real Tier 0 sweep (69 runs against LiveKit Cloud).

**Measured**, on `Q7_latency_definition__owd_ms=0,video_codec=av1__r0__1787663508`,
scored window only:

| quantity | source | p50 |
|---|---|---|
| `transport.candidate_pair_rtt_s` | ICE consent check on the selected pair | **31.0 ms** |
| four-timestamp probe | control transport, echoed by the peer | **60.6 ms** |

Roughly 2x apart, because they are not the same path. The probe traverses publisher → SFU
→ subscriber → SFU → publisher over a data channel, **and additionally includes
application-layer scheduling at both ends** — a probe rides the next control sample at the
sender, and the receiver dispatches the echo from its own task. `candidate_pair_rtt_s` is
the network round trip and carries none of that.

**Why it mattered.** The probe figure was labelled "network RTT p50" in the Q-7 report,
which made the g2g/RTT ratio read **below 1.0** — av1 0.82x, h264 0.95x on the same sweep.
That is not merely odd, it is impossible: glass-to-glass contains a network traversal and
cannot be faster than one. Against the correct denominator the same data gives av1 2.39x
and h264 1.88x, which are sensible teleoperation figures. The number was never wrong; the
name was, and the name is what made a nonsensical ratio look publishable.

**Both are kept**, under names that state what they measure. Their *difference* is the
application's own contribution to the loop, which is actionable in a way neither figure is
alone.

**The §8.1a ≤90 ms p95 bar attaches to `network_rtt_p95_ms`.** §8.1a's companion SHOULD is
"20–50 ms" — a network-path figure. An application loop containing 200 Hz control-publisher
scheduling at both ends cannot plausibly sit in 20–50 ms, so the clause is describing the
network. Scoring the probe against that bar would apply a network limit to a number that
includes harness scheduling: **the same class of error as §6 note (d2)**, where the C++
matrix scored a round-trip against a one-way clause. `app_rtt_p95_ms` is reported alongside
as OBSERVE and is never scored against §8.1a. `app_rtt_p99_ms` remains unscored, exactly as
note (d2) requires.

**Probe rate raised, and a defect fixed first.** The same run yielded only **63 probe
samples across ~105 s**, with a max of 1867 ms against a 60.6 ms median. Two separate
findings:

- The sparsity was a harness defect, not a low rate alone. `ProbeTracker` held **one**
  outstanding probe and retired it as lost whenever the next was issued. With RTT ≈ 60 ms
  against a 1 s interval, plus control-publisher queueing, good probes were routinely
  discarded — 76 sent, 64 completed, in a repeating two-polls-per-completion pattern.
  Raising the rate without fixing this would have made it *worse*: at any interval below
  the round trip, every probe is displaced before its echo lands, so the completed count
  falls as the rate rises and the loss is misattributed to the network. The tracker now
  holds a set of in-flight probes keyed by token and retires one only when it outlives
  `probe_lifetime_ms`. Probing also moved out of the sampler into its own actor, since the
  poll loop capped it at the poll rate by construction.
- The 1867 ms outlier is **a real network event, not a harness stall**. At that poll the
  sampler was healthy (interval 1000.2 ms, poll duration 3.03 ms, not overbudget, no stats
  RPC failure), but two polls earlier the control receive rate collapsed from 191 to **10
  samples/s** and `max_gap` jumped from 14 to **121 consecutive missing sequence numbers**
  — a ~600 ms control-path delivery gap. The probe in flight across it measured that gap
  correctly. No harness defect to file; the correlation check the outlier prompted is what
  distinguished the two cases, and it is the reason `sampler.overbudget` and
  `poll_duration_ms` are on every snapshot.

`probe_rate_hz` (20) and `probe_lifetime_ms` (2000) are `matrix.yaml` parameters per §12,
not hardcoded.

---

## Amendment 2026-08-25 — quality-at-bitrate, and two metrics corrected

**`qp_avg` and `quality_limitation_bandwidth_poll_pct` added (§1b, §8).** The Tier 0 sweep
showed AV1 at `quality_limitation_reason: none` for all 106 scored polls while H264 sat at
`bandwidth` for 62 of 106, at similar target bitrates. That is suggestive of AV1 efficiency
but **proves nothing on its own**, because nothing recorded quality: "efficient" and
"quietly degraded to fit" produce the same bitrate figure. Both metrics are now extracted
and both appear in the T-1 table.

`qp_avg` is `Δqp_sum / Δframes_encoded` — `qp_sum` is cumulative, so it must be differenced.
Per §8 it is **not comparable across codecs** and is reported per codec only.
`quality_limitation_bandwidth_poll_pct` is the share of scored polls whose limiter was
bandwidth; unlike QP it counts an encoder-reported condition in units no codec defines
differently, so it **is** cross-codec comparable, and it is the metric that surfaced the
difference.

First reading on that same pair of runs is worth recording, because it complicates the
efficiency story rather than confirming it: **av1 QP 40.0 at 0.31 Mbps actual against a
3.07 Mbps target; h264 QP 27.4 at 1.91 Mbps against 2.61 Mbps.** AV1 was not fitting the
same picture into fewer bits — on this run it was emitting far fewer bits at a much coarser
quantizer, under a software encoder (`libaom`). Reported per §8 as provisional for any
encoder-sensitive claim until re-run at Tier 2.

**`available_outgoing_bitrate_bps` renamed to `subscriber_available_outgoing_bitrate_bps`.**
It read exactly 300 000.0 on every scored poll of both runs, never varying, while H264 was
concurrently reporting bandwidth limitation — so the real constraint was moving and this
field was not. The cause is neither a stale field nor a deserialization fault
(`libwebrtc/src/stats.rs:576` is a plain `f64` with `#[serde(default)]`): the whole
`TransportSample` is read from the **subscriber's** peer connection. On that run it shows
`packets_received` 46 190 against `packets_sent` 22 743, while the publisher pushed 4.5 MB
of video that never crossed it. A peer connection that sends only RTCP never ramps its
bandwidth estimator off libwebrtc's default start bitrate — `kDefaultStartBitrateBps =
300000` in `api/transport/bitrate_settings.h`. The field is correct for what it measures;
the name invited it to be read as the publisher's uplink estimate. For publisher-side
bandwidth pressure use `quality_limitation_bandwidth_poll_pct` and `target_bitrate_bps`.

**`audio_level` is now the maximum across scored polls, not the median (§1g rule (ii)).**
The rule has always read "`audio_level == 0` for the **whole run**", which is a maximum
test. As a median it produced a false `silent_audio_source` on
`T1_video_floor__video_profile=minimum,video_codec=av1,uplink_mbps=10__r2`: peak level
0.5026 with 42 of 78 scored polls at 0.0, so the median read 0.0 while the synthetic tone
generator was working correctly. The zeros were a reconnect storm — that run took **5
reconnects**, and a freshly re-subscribed audio track reads zero until samples flow. The
same storm accounts for its `control_publish_shortfall_pct` of 64.7 and its 2 stats-RPC
failures, so the three symptoms are one fault and **not** process starvation: the sampler
never went overbudget once in that run. `audio_level_median` is retained as a separate
observation, since a partially-audible run is worth seeing; it just does not decide
validity. The generator needs no change.

---

## Amendment 2026-08-25 — a real camera source, opt-in and never pooled

**Adds `--camera-source` to the harness. Changes no axis, no cell and no default.**

The synthetic pattern remains the source for every matrix cell. The matrix's cross-host
and cross-run comparability rests on every host presenting the encoder with an *identical*
problem; a camera makes bitrate depend on scene content, lighting and framing, so a camera
cell would not be comparable to the same cell run anywhere else. `--camera-source` exists
for realism spot-checks and for the eventual Tier 2 rig, and is opt-in only.

`--camera-source` takes `test_pattern` (the default) or a capture device, addressed by
enumeration index or by a case-insensitive substring of its name. The name is the portable
identifier: an index names a different lens on a different host.

**One code path.** Camera frames land in the same `NativeVideoSource`, carry the same
in-band `user_timestamp`/`frame_id`, and are encoded, published and sampled by the same
code as the pattern. Only the pixels differ. A camera run that took a different publish
path would not be comparable to a pattern run even as a spot-check, which is the only
reason the option exists.

**Two properties are enforced rather than documented.**

- **No fallback.** A camera that cannot be enumerated, matched or opened fails the run.
  A run recorded as `camera` that actually carried the pattern would be pooled with pattern
  runs and nothing in the record could detect it afterwards — the same mislabeling class as
  the H.265→H.264 publish fallback that keeps H.265 out of the matrix entirely (§3).
- **`camera_source` joins `never_pool_across`.** Camera and pattern runs are different
  experiments, and so are two different cameras. `parse_runs.py`'s `pool_key` picks the key
  up from `matrix.yaml` generically, so this is a one-line matrix change plus a test.

**The record carries what ran, not what was asked for.** `RunMetadata.camera_source` is the
*resolved* source and `parse_runs.py` overwrites `environment.camera_source` with it, so the
value that keys the non-poolable group is the one that produced the pixels. Alongside it,
`camera_device` records the device name, index, backend description, and the geometry, frame
rate and pixel format the device **negotiated**. That last part is load-bearing: devices
downgrade requests silently — a request for 3840x2160@60 against the MacBook's built-in
camera negotiates 1920x1080@30 — and a downgraded capture presents the encoder with a
different problem that the requested values alone cannot reveal.

Capture reuses `examples/local_video/src/publisher.rs`'s machinery: `nokhwa` at the same
pinned revision and feature set, YUYV preferred over MJPEG because it needs no decode step,
and libyuv for conversion with the `image` crate as the fallback for JPEG variants libyuv
declines. The device's own capture timestamp is deliberately **not** used — backends report
it in inconsistent epochs and a wrong epoch makes G2G read negative — so the frame is
stamped from the run clock at the same point in the loop as a synthetic frame, keeping the
two sources' G2G figures measurements of the same interval.

---

## Amendment 2026-08-26 — RTSP / IP camera as a third source

**Extends `--camera-source` to accept an `rtsp://` or `rtsps://` URL. Changes no axis, no
cell and no default.** Everything the amendment above establishes applies unchanged: opt-in
only, one publish path, no fallback, and `camera_source` still keys `never_pool_across`.

**Why a third source at all.** The Tier 2 rig's camera is a Pegatron "Muscat" IP camera
reachable only over Ethernet — `rtsp://192.168.100.123/full1080p` (H.264 1920x1080, ~10 fps,
plus an AAC track) and `rtsp://192.168.100.123/4k`. `nokhwa`, which the local-device path
uses, enumerates USB/AVFoundation/V4L2/MSMF devices and cannot open a network stream at all,
so the existing path reports the URL as a missing device. This is a hard blocker for Tier 2,
not a convenience.

**Decoding is an `ffmpeg` subprocess**, not an in-process RTSP or H.264 crate:

```
ffmpeg -nostdin -loglevel error -rtsp_transport tcp -i <url> \
       -an -f rawvideo -pix_fmt yuv420p -s WxH -r FPS pipe:1
```

Frames are read off stdout as fixed `w * h * 3 / 2` byte I420 blocks. Real IP cameras are
full of quirks ffmpeg has already absorbed, and this keeps a decoder out of a build graph
every measuring host has to compile. Audio is discarded with `-an`: the harness publishes
its own synthetic tone, and the camera's AAC track would only add a stream to demux.

**TCP transport by default** (`--rtsp-transport`, `tcp` or `udp`). UDP RTSP degrades by
silently dropping media on a filtered or congested path, which reaches the record as a
camera producing missing frames — indistinguishable from a genuinely bad camera. TCP turns
the same condition into a connection error that names itself. This is the same principle as
"no fallback": a failure mode that cannot be told apart from a result is not acceptable.

**A stall is a distinct, bounded failure.** A wedged RTSP session leaves ffmpeg alive
holding its pipe open with no bytes flowing, which is byte-for-byte indistinguishable from a
merely slow stream. Every frame read is therefore bounded by
`meta.parameters.rtsp_stall_timeout_s` (15 s, `--rtsp-stall-timeout-s`) and a stall is its
own error variant. Without the bound the capture loop blocks for the run's full duration and
the failure appears nowhere. Likewise a short read on a frame boundary ("the stream ended")
and one part-way through a frame ("the harness holds half a frame") are separate errors, and
a partial frame is discarded rather than published — its lower rows are uninitialised memory
that the encoder would happily encode as content. ffmpeg's stderr is drained on a background
thread and replayed into every one of these errors, because it is the only place an auth
failure, an unreachable host or a wrong stream path is ever explained.

**RTSP runs are realism spot-checks, not matrix cells.** The Muscat runs ~10 fps at 1080p
while every matrix cell targets 30 fps. ffmpeg duplicates frames to reach the requested rate,
so `camera_device.negotiated_fps` records the rate the *encoder was fed*, not the rate the
sensor ran at; the two differing is expected rather than a fault. A 10 fps sensor cannot
support a `video_fps_p50` claim against the 27 fps bar, and an RTSP run must never be read as
evidence for or against it.

**Credentials never reach the record.** RTSP URLs routinely embed `user:pass@`, and run
records are committed and shared. The harness redacts the authority's userinfo to `***`
everywhere it logs or records the source, and `run_matrix.py` does the same to the requested
value and to its `--dry-run` output. The two implementations are checked against each other
in `test_parse_runs.py`.

**The record self-identifies the source kind.** `camera_device.kind` is `local_device` or
`rtsp`, and `camera_source` is `rtsp:<redacted url>` for an RTSP run, so all three sources
are distinguishable from the record alone — which is exactly what `never_pool_across`
requires. For RTSP, `device_index` carries the media transport, since that is the host-local
detail that changes what the same URL delivers.

---

## Amendment 2026-08-25 — the AV1 efficiency claim is retracted, and T-1 needs redesign

**Retracted.** The Tier 0 observation that AV1 ran `quality_limitation_reason: none` for
all 106 polls while H264 sat at `bandwidth` for 62 of 106, at similar target bitrates, was
read as evidence of AV1 efficiency. **It is not, and no efficiency claim may be drawn from
that sweep.** The quality metrics added the same day (see the previous amendment) show why:

| codec | `qp_avg` | actual bitrate | target bitrate | encoder |
|---|---|---|---|---|
| av1 | **40.0** | 0.31 Mbps | 3.07 Mbps | `libaom` (software) |
| h264 | **27.4** | 1.91 Mbps | 2.61 Mbps | hardware |

Independently reproduced at av1 QP 40.0 / 3.9 MB against h264 QP 25.3 / 23.0 MB over
comparable frame counts. AV1 was not encoding the same picture into fewer bits; it was
encoding a **visibly coarser picture**. QP 40 and QP 25 are two different pictures, and
the one with fewer bits is not thereby the more efficient codec.

### The structural problem, which is not specific to that sweep

**T-1 encodes to a bitrate target, so quality is an output of the experiment rather than a
control.** Each codec's rate control is handed a bitrate and picks whatever quantizer
reaches it. Comparing the resulting bitrates across codecs compares two encoders that were
solving different problems and settled at different operating points — the comparison has
no fixed point.

Within a single codec the comparison remains sound: every rung faced the same rate control
with the same objective, so a bitrate difference across rungs is a real difference. §8's
existing rule that `qp_sum`-derived quality is never pooled across codecs already implied
this; what was missing is that **the bitrate column inherits the same restriction**, which
was not stated and is now.

Two consequences are recorded rather than left implicit:

- `qp_avg` sits **immediately adjacent to bitrate** in the T-1 table, not in a separate
  quality group. A bitrate figure must never be legible without its quantizer.
- T-1's stated question is amended in `matrix.yaml` and §9 from "which combinations fit
  under the 5 Mbps ceiling" to "what bitrate each combination emitted, at whatever quality
  its rate control chose". The original phrasing claimed more than the suite delivers.

### Recommendation for Tier 1/2 — fixed-quality encoding. Not implemented.

To answer the codec question the experiment must be inverted: **pin the quality target
(QP or CQ) across codecs and measure the resulting bitrate**, rather than pinning bitrate
and letting quality float. Then "AV1 needs N% fewer bits for the same picture" becomes a
measurable claim instead of an inference. The fps and resolution ladder is unaffected.

**This is a suite redesign, deliberately not built here.** Whoever runs Tier 1/2 must treat
T-1 as needing that redesign before it can answer the Figure question.

**Blocked on an SDK gap — see SDK-FINDINGS SDK-2.** This checkout exposes no quality target
at any layer: `VideoEncoding` (`livekit/src/room/options.rs:56-59`) carries only
`max_bitrate` and `max_framerate`; `RtpEncodingParameters`
(`libwebrtc/src/rtp_parameters.rs:132-145`) adds only `scale_resolution_down_by` and
`scalability_mode`; `TrackPublishOptions` adds `degradation_preference`, which chooses what
to sacrifice under pressure but sets no quality floor. So the redesign cannot be built
against this SDK as it stands — it needs an upstream change, a per-codec field trial, or
an out-of-band encoder configuration path.

---

## 1. Metric → API mapping

Units follow the WebRTC stats spec as deserialized in `libwebrtc/src/stats.rs`: all
`total_*_time`, `*_delay`, and `jitter` fields are **seconds** (f64); the harness converts
to ms at extraction and every JSON field name carries its unit suffix.

**Counter discipline.** `libwebrtc/src/stats.rs` exposes raw cumulative counters only —
there is no rate or percentile anywhere in `RtcStats`. The doc's own warning
(docs.livekit.io/robotics/media/performance/stats/) applies to every counter: cumulative
since subscription start. The `kind` column below is therefore load-bearing:

- **raw** — read the field as-is at end of run (a level, not a rate).
- **Δ** — difference two consecutive polls; the value is per-interval.
- **Δratio** — difference *both* numerator and denominator between the same two polls,
  then divide. Never divide two lifetime cumulatives to get an interval average; that
  yields a session average that hides exactly the transient the suite is looking for.
- **app** — computed application-side by the harness; not from `RtcStats`.

Sampling cadence is 1 Hz for all `RtcStats` polls (`room.get_stats()` /
`track.get_stats()`), matching the C++ harness's poll loop. Per-frame and per-sample
metrics are recorded at their native rate in the harness and summarized at poll time.

### 1a. Video — receive side

Source: `RtcStats::InboundRtp(InboundRtpStats)`, fields via
`inbound.inbound: dictionaries::InboundRtpStreamStats` (`libwebrtc/src/stats.rs:347-401`),
`inbound.received: ReceivedRtpStreamStats` (`:338-342`), `inbound.stream: RtpStreamStats`
(`:328-333`).

| metric | PRD clause | LiveKit Rust API | field / derivation | kind | cadence | validity precondition | risk |
|---|---|---|---|---|---|---|---|
| `video_fps_p50` | §7.1a | `RemoteVideoTrack::get_stats` → `InboundRtp` (`livekit/src/room/track/remote_video_track.rs:206`) | `inbound.frames_per_second` — libwebrtc-computed instantaneous gauge; p50 over the run's poll samples | raw (gauge) | 1 Hz | ≥30 post-warmup samples | Gauge is already smoothed by libwebrtc; a freeze shorter than its window is invisible. Pair with `video_frame_interval_p99_ms`. |
| `video_fps_delta` (cross-check) | §7.1a | same | `Δframes_decoded / Δt` | Δ | 1 Hz | monotonic counter, no reset | Disagreement with `frames_per_second` >10% ⇒ INVALID (poll stall). |
| `video_frame_interval_p99_ms` | §7.1a, §8.1b | harness `on_frame` callback on the `RemoteVideoTrack` stream | app: histogram of wall-clock spacing between successive decoded frames | app | per frame | video stream active | Measures *delivery to app*, not to glass. Under `zero_jitter` this is the true pacing metric — it is where AV1 frame-assembly sensitivity appears. |
| `video_freeze_count` / `video_freeze_duration_ms` | §7.1c/d | `InboundRtp` | `Δfreeze_count`, `Δtotal_freeze_duration × 1000` | Δ | 1 Hz | — | libwebrtc's freeze definition is fixed; comparable across codecs, so this is the loss-recovery metric of record. |
| `video_pause_count` | §7.1d | `InboundRtp` | `Δpause_count`, `Δtotal_pause_duration` | Δ | 1 Hz | — | Distinguishes §7.1d intentional pause from a freeze. |
| `jitter_buffer_delay_avg_ms` | §8.1b, §8.2 | `InboundRtp` | `(Δjitter_buffer_delay / Δjitter_buffer_emitted_count) × 1000` | Δratio | 1 Hz | `Δjitter_buffer_emitted_count > 0` | **This is the buffering-mode ground truth** (§4). Dividing lifetime cumulatives instead of deltas is the single most likely defect in this harness. |
| `jitter_buffer_target_delay_ms` | §8.2 | `InboundRtp` | `(Δjitter_buffer_target_delay / Δjitter_buffer_emitted_count) × 1000` | Δratio | 1 Hz | same | Target vs actual: the *applied* playout-delay hint. See §4 on requested-vs-actual. |
| `video_packets_lost_pct` | §8.3b | `InboundRtp` | `Δreceived.packets_lost / (Δreceived.packets_lost + Δreceived.packets_received) × 100` | Δratio | 1 Hz | denominator > 0 | `packets_lost` is `i64` and **may go negative** on duplicates. Clamp at 0 and log; a negative delta is a reorder artifact, not a gain. |
| `video_rtx_pct` | §5 hypothesis | `InboundRtp` | `Δretransmitted_packets_received / Δreceived.packets_received × 100` | Δratio | 1 Hz | denominator > 0 | **The RLC-AM/UM discriminator.** Under `zero_jitter` a retransmit that arrives after deadline is pure waste; this quantifies it. |
| `video_nack_rate_per_min` | §8.3b | `InboundRtp` | `Δnack_count / Δt × 60` | Δ | 1 Hz | — | Codec-independent enough to pool; PLI is not. |
| `pli_rate_per_min` | §8.3b | `InboundRtp` (recv) and `OutboundRtp` (send) | `Δpli_count / Δt × 60` | Δ | 1 Hz | — | **Codec-sensitive** (§7). Report per codec. |
| `key_frames_decoded_rate` | §8.3b | `InboundRtp` | `Δkey_frames_decoded / Δt × 60` | Δ | 1 Hz | — | Codec-sensitive. AV1 keyframe cadence differs from H264 at equal config. |
| `decode_time_avg_ms` | Q-7 | `InboundRtp` | `(Δtotal_decode_time / Δframes_decoded) × 1000` | Δratio | 1 Hz | `Δframes_decoded > 0` | **Codec-sensitive; the decode share of G2G.** This is what makes Q-7's three-column table codec-dependent. |
| `assembly_time_avg_ms` | Q-7, §5 | `InboundRtp` | `(Δtotal_assembly_time / Δframes_assembled_from_multiple_packets) × 1000` | Δratio | 1 Hz | denominator > 0 | **The AV1 × zero_jitter cell's primary metric.** Larger, more variable AV1 frames span more packets; with no jitter buffer, assembly is where the cost lands. |
| `processing_delay_avg_ms` | Q-7 | `InboundRtp` | `(Δtotal_processing_delay / Δjitter_buffer_emitted_count) × 1000` | Δratio | 1 Hz | denominator > 0 | Packet-arrival→decoder-output. Complements decode time. |
| `decoder_implementation` | §7 | `InboundRtp` | `inbound.decoder_implementation` (String) | raw | 1 Hz | non-empty | Recorded, not thresholded. Empty until first frame decodes. |
| `frame_width` / `frame_height` | §7.1a | `InboundRtp` | `inbound.frame_width` / `frame_height` | raw | 1 Hz | — | **Actual, not requested** — the SDK may downscale under `quality_limitation_reason`. T-1 must score on this. |

### 1b. Video — send side

Source: `RtcStats::OutboundRtp(OutboundRtpStats)`, fields via
`outbound.outbound: OutboundRtpStreamStats` (`libwebrtc/src/stats.rs:414-445`),
`outbound.sent: SentRtpStreamStats` (`:406-409`).

| metric | PRD clause | LiveKit Rust API | field / derivation | kind | cadence | validity precondition | risk |
|---|---|---|---|---|---|---|---|
| `video_bitrate_bps` | **§8.0b** | `LocalVideoTrack::get_stats` → `OutboundRtp` (`livekit/src/room/track/local_video_track.rs:309`) | `(Δ(sent.bytes_sent + outbound.header_bytes_sent) × 8) / Δt` | Δ | 1 Hz | `Δt > 0`, post-warmup | **Must include `header_bytes_sent`.** Payload-only bytes understate the wire rate the §8.0b 5 Mbps ceiling is about. Codec-sensitive by definition. |
| `video_target_bitrate_bps` | §8.0b | `OutboundRtp` | `outbound.target_bitrate` (f64, bps — gauge) | raw | 1 Hz | — | The encoder's *target*. Divergence from actual ⇒ encoder cannot hit target ⇒ check `quality_limitation_reason`. |
| `encode_time_avg_ms` | Q-7, §7 | `OutboundRtp` | `(Δtotal_encode_time / Δframes_encoded) × 1000` | Δratio | 1 Hz | `Δframes_encoded > 0` | **Encoder-tier-sensitive.** Software AV1 on Apple Silicon says nothing about NVENC AV1. Never pool. |
| `quality_limitation_reason` | §7, validity | `OutboundRtp` | `outbound.quality_limitation_reason` → `QualityLimitationReason` enum (`stats.rs:51-57`) | raw | 1 Hz | — | `Cpu` for >10% of post-warmup polls ⇒ **INVALID** (§6). A bitrate produced by a starved encoder is not a measurement of the network. |
| `quality_limitation_cpu_pct` | §7, validity | `OutboundRtp` | `Δquality_limitation_durations["cpu"] / Δt × 100` | Δratio | 1 Hz | key present | `quality_limitation_durations` is `HashMap<String, f64>` (`stats.rs:436`); keys are `"none"/"cpu"/"bandwidth"/"other"`. Missing key ⇒ treat as 0, do not error. |
| `quality_limitation_bandwidth_pct` | §8.0b | `OutboundRtp` | `Δquality_limitation_durations["bandwidth"] / Δt × 100` | Δratio | 1 Hz | key present | This is the *expected* limiter under `uplink_mbps` sweep — not an INVALID condition. |
| `quality_limitation_resolution_changes` | §7.1c | `OutboundRtp` | `Δquality_limitation_resolution_changes` | Δ | 1 Hz | — | Evidence for §7.1c adaptation before drop. |
| `fps_ceiling` | §7.1a | `OutboundRtp` | `outbound.frames_per_second` | raw | 1 Hz | — | **Encoder-tier-sensitive.** |
| `encoder_implementation` | §7 | `OutboundRtp` | `outbound.encoder_implementation` (String) | raw | 1 Hz | non-empty | Reference impl: `find_video_outbound_encoder` at `examples/local_video/src/publisher.rs:406-424` — prefers the `active` layer, falls back to first non-empty. **Reuse this selection rule**; picking an arbitrary `OutboundRtp` gives the wrong layer. |
| `power_efficient_encoder` | §7 | `OutboundRtp` | `outbound.power_efficient_encoder` (bool) | raw | 1 Hz | — | Corroborates `encoder_tier`; libwebrtc's own hw/sw signal. |
| `keyframe_service_polls` (distribution) | §8.3b | harness, from `OutboundRtp` polls at 10 Hz during T-2 | app: on a poll where `Δpli_count > 0`, start a timer; stop on the next poll where `Δkey_frames_encoded > 0`; record the **poll count**, report the full distribution plus `max` | app (Δ-triggered) | **10 Hz** in T-2; 1 Hz elsewhere | ≥20 PLI events for a distribution; else report raw values | Reported in **poll intervals, not milliseconds**, and never as a percentile — see below. Codec-sensitive. |
| `malformed_bitstream` | validity | `OutboundRtp` | `outbound.frames_encoded > 0 && sent.packets_sent == 0` | raw | 1 Hz | — | The condition at `publisher.rs:462-466`. **INVALID reason, not a zero-bitrate FAIL.** |
| `actual_codec` | §6c | `RtcStats::Codec(CodecStats)` → `codec.mime_type` (`stats.rs:316-323`), joined via `stream.codec_id` | parse `"video/AV1"` → `av1` | raw | 1 Hz | codec stat present | **Requested ≠ actual.** See §4. |

**Keyframe service time is a poll-count distribution, not a millisecond percentile.** The
Rust SDK exposes no PLI callback, so this can only be measured by differencing `pli_count`
and `key_frames_encoded` between polls. That makes the measurement resolution equal to the
poll period, and it produces a quantized value — the underlying quantity is "how many polls
elapsed," which at 1 Hz makes every realizable value a multiple of 1000 ms. Calling the
result a "p95 in milliseconds" implies a precision the method cannot deliver: with a handful
of PLI events, a p95 over ~{0, 1000, 2000} ms is just the maximum wearing a percentile's
name.

Two decisions follow, both committed here rather than left conditional:

- **T-2 polls the video track at 10 Hz**, not 1 Hz. T-2 is the suite where keyframe recovery
  after a loss burst is a primary metric, and 100 ms resolution is the minimum that makes
  recovery timing meaningful. Every other suite stays at 1 Hz. The raised cadence is
  recorded per-run as `video_poll_hz` so a reader never has to infer the resolution, and it
  increases `poll_overbudget_pct` risk — which the existing INVALID gate catches rather than
  silently absorbing.
- **Report the distribution in poll intervals plus the observed maximum**, never a
  percentile in milliseconds. Convert to ms only when quoting, and quote as a bound
  ("recovery within 2 polls ≈ ≤200 ms at 10 Hz"). Codec comparison is valid because all
  codecs are measured at the same cadence.

This supersedes gap 6's earlier conditional phrasing; there is no remaining "if it proves
necessary."

### 1c. Control path (body-state, 200 Hz)

The Rust SDK exposes **no per-frame receive statistic** for data tracks —
`livekit-datatrack` has no stats surface at all, and `RtcStats::DataChannel`
(`stats.rs:526-535`) carries only `messages_sent/received` and `bytes_sent/received` and
covers the legacy channel path only. Every control metric is therefore **app-derived from
a harness-owned sequence number and timestamp in the payload**.

Control payload (harness-defined, 32 bytes fixed): `seq: u64`, `t_send_unix_us: u64`,
`probe_echo_token: u64`, `pad: u64`. Fixed size so payload length never confounds loss.

**On `DataTrackFrame::with_user_timestamp`.** The setter
(`livekit-datatrack/src/frame.rs:74-77`) is **unit-agnostic** — it stores a bare `u64` and
imposes no interpretation. Microseconds would pass through it intact. The millisecond
assumption lives only in the two convenience helpers: `with_user_timestamp_now` (`:80-88`,
`d.as_millis()`) and `duration_since_timestamp` (`:57-64`, `Duration::from_millis`).

The harness nonetheless carries its own microsecond stamp **in the payload** and does not
use this field, for two reasons that are not about units. First, the field is
`Option<u64>` and carries one value; the control path needs a sequence number *and* a
send timestamp *and* a probe echo token travelling together, so a payload struct is
required regardless. Second, using the field with microsecond values would leave
`duration_since_timestamp` silently wrong for anyone who later calls it on these frames —
a trap for the next reader. Keeping the field unset makes the payload the single source of
timing truth. Phase 3 should not read this as "the SDK cannot carry microseconds"; it can.

| metric | PRD clause | LiveKit Rust API | field / derivation | kind | cadence | validity precondition | risk |
|---|---|---|---|---|---|---|---|
| `control_delivered_pct` | **§8.3a** | `RemoteDataTrack::subscribe_with_options` → `DataTrackStream` (`livekit-datatrack/src/remote/mod.rs:92-113`); legacy: `RoomEvent::DataReceived` | app: `distinct_seq_received / expected_seq_count × 100`, where `expected_seq_count` is the **publisher-side** seq range intersected with the scored window — see below | app | per frame | ≥10 000 samples (50 s at 200 Hz); publisher seq log present | Denominator must come from the publisher, not from received sequence numbers. See the bias note below. |
| `control_publish_shortfall_pct` | validity | harness publisher | app: `1 − seq_published / (200 × duration_s)` | app | per run | — | **Harness-health metric.** >2% ⇒ INVALID: the publisher, not the network, set the rate. |
| `control_late_pct` | **§8.2b** | harness | app: share of received samples with `owd_corrected_us > playout_window_ms × 1000` | app | per frame | `clock_sync_confidence ≥ probe` | §8.2b: a late sample *is* loss. This metric is the whole of T-3. |
| `control_owd_p50_ms` / `p99_ms` | §8.1a, Q-7 | harness | app: `owd_corrected = (t_recv_unix_us − t_send_unix_us) − θ`; θ from the probe (below) | app | per frame | θ valid | Requires clock-offset correction; raw OWD is meaningless across hosts. |
| `control_jitter_ms` | §8.2 | harness | app: RFC 3550 interarrival jitter over receive wall-clock spacing, `J += (|D| − J)/16` | app | per frame | — | Single-clock, so skew-immune. Direct reimplementation of `stats.h:294-310`. |
| `control_effective_rate_hz` | §8.3, T-2 | harness | app: `distinct_seq_received / Δt` per poll interval | Δ | 1 Hz | — | The T-2 collapse signature is a **rate** collapse (200→40 Hz), which delivered-% alone hides. Governing page §T-2. |
| `control_gap_p99` | §7.2a | harness | app: p99 of consecutive-seq gap length | app | per frame | — | §7.2a/§9.3b: the robot must not act on stale input. Gap length bounds how long a watchdog must tolerate. |
| `control_gap_p99_ms` / `control_max_gap_ms` | §7.2a, §8.1a | harness | app: `gap / control_rate_hz × 1000` | app | per frame | publisher rate known | **OBSERVE.** The same gaps in the unit the latency budget is written in — a gap at a fixed publisher rate *is* a duration. `max` is a single worst-case event, not a distribution, and is never scored. See the amendment below. |
| `dc_messages_received` (legacy only) | §8.3a | `RtcStats::DataChannel` | `Δdc.messages_received` | Δ | 1 Hz | `control_transport ∈ {dc_reliable, dc_lossy}` | Cross-check on the app counter for the legacy path only. **Absent for `data_track_buf1`** — data tracks do not surface here. |

**The `control_delivered_pct` denominator, and why the obvious one is wrong.** Deriving the
expected count from *received* sequence numbers — `max_seq − min_seq + 1` — is
self-referential and **biased toward passing**, against a 99.9% blocking bar where the whole
question is whether 0.1% of samples went missing.

The failure is at the window edges. If the last 40 samples of the scored window are lost,
`max_seq` is simply 40 lower, the denominator shrinks by exactly the number lost, and the
loss is invisible. The same applies at the head via `min_seq`. A burst straddling either
boundary — precisely the T-2 and T-5 signature — is silently discounted, and the metric
reports 100% delivered on a run that dropped samples.

**Correct denominator:** the publisher logs every `seq` it emits with its send timestamp.
`expected_seq_count` is the count of publisher-emitted seqs whose send time falls inside the
scored window (post-warmup). Loss at either edge then shows up as a shortfall in the
numerator while the denominator stays fixed.

This requires the publisher-side seq log to reach the analysis, so it is a run-record
artifact, not just a runtime counter. `control_publish_shortfall_pct` remains a separate
metric measuring a different failure — the publisher not achieving 200 Hz at all — and is
computed against wall-clock, not against received data.

### 1d. Latency probes

| metric | PRD clause | LiveKit Rust API | field / derivation | kind | cadence | validity precondition | risk |
|---|---|---|---|---|---|---|---|
| `app_rtt_p50/p95/p99_ms` | §8.1a (observe) | harness probe over the control transport, echoed by the peer | app: four-timestamp `rtt = (t3−t0) − (t2−t1)` | app | probe rate | ≥30 completed probes | **Application loop, not the network round trip** — see the amendment below. Clock-skew immune by construction. Reimplements `stats.h:446-498`. |
| `network_rtt_p50/p95_ms` | **§8.1a** | `RtcStats::CandidatePair` → `candidate_pair.current_round_trip_time` (`stats.rs:575`) | `× 1000`, percentiled over scored polls | raw | 1 Hz | selected pair nominated | **This is the metric §8.1a's 90 ms ceiling is scored against**, and Q-7's ratio denominator. |
| `probe_loss_pct` | §8.3 | harness | app: `probes_lost / probes_sent × 100` | app | probe rate | `probes_sent > 0` | `probes_lost` is the harness's explicit aged-out count. **Not `sent − completed`**: several probes are legitimately in flight at once. |
| `clock_offset_ms` / `theta_ms` | Q-7 | harness | app: on each probe completion, `θ = min(owd_window) − rtt_this_probe / 2` — **paired**, see below | app | 1 Hz (on probe completion) | ≥8 OWD samples | Residual error `(d_in − d_out)/2` on asymmetric links — which cellular is. Bound it in the report; do not present corrected OWD as exact. |
| `clock_sync_confidence` | validity | harness | app: enum `none` / `probe` / `external` | raw | per run | — | Gates whether any one-way figure may be published. `none` ⇒ OWD **and G2G** columns suppressed; run stays valid for RTT. Whether suppression invalidates the run depends on the suite — see the scoping rule below. |
| `ice_rtt_ms` (legacy alias) | §8.1a | `RtcStats::CandidatePair` → `candidate_pair.current_round_trip_time` (`stats.rs:575`) | median of `× 1000` | raw | 1 Hz | selected pair nominated | **Superseded by `network_rtt_p50_ms`**, which is the same series. Retained under its original name so pre-rename run records stay readable; prefer the new name in analysis. It was described here as "not the media-path RTT and not scored against §8.1a" — the 2026-08-25 amendment reverses that: it *is* what the bar is scored against. |
| `rtcp_rtt_ms` (corroborating) | §8.1a | `RtcStats::RemoteInboundRtp` → `remote_inbound.round_trip_time` (`stats.rs:452`) | `× 1000` | raw | 1 Hz | `round_trip_time_measurements > 0` | RTCP-derived, ~1 s cadence, video path only. Second corroborator. |

**The θ estimator must pair its two minima, not take them from independent windows.** The
estimator is `θ ≈ min(owd) − min(rtt)/2`, and the error is in *which* minima. Taking
`min(rtt)` over its own rolling window and `min(owd)` over a separate one mixes measurements
from different times — and because OWD samples arrive at 200 Hz while probes complete at
1 Hz, the two windows span very different intervals. The OWD minimum is then drawn from a
few seconds of traffic and the RTT minimum from a minute or more, so the difference between
them absorbs path-condition drift as if it were clock offset.

The correct form, which the C++ implementation uses (`stats.cpp:272-291`, and the comment at
`stats.h:516-520` naming this exact hazard): **when a probe completes, scan the current OWD
ring for its minimum and pair it with the RTT of that same probe exchange**, then store
`θ = min_owd_at_probe − rtt_this_probe / 2`. Both terms are contemporaneous by construction,
so queueing that inflates one inflates the other and largely cancels.

`θ` is recomputed on every probe completion and the OWD ring is 64 samples. Requires ≥8 OWD
samples before `theta_valid` is set (`stats.h:524`); until then `clock_sync_confidence` is
`none` and no one-way figure is published.

### 1e. Glass-to-glass

The C++ harness could not measure this at all (governing page: "the video receive path
carries no frame identity"). The Rust SDK closes the gap: `FrameMetadataFeatures`
(`livekit/src/room/options.rs:80-82`) carries `user_timestamp: bool` and `frame_id: bool`
in-band with the encoded frame, set on `TrackPublishOptions.frame_metadata_features`
(`examples/local_video/src/publisher.rs:1189-1199`) and read back on the subscriber at
`metadata.user_timestamp` / `metadata.frame_id`
(`examples/local_video/src/subscriber.rs:1124-1127`, `2185-2188`).

| metric | PRD clause | LiveKit Rust API | field / derivation | kind | cadence | validity precondition | risk |
|---|---|---|---|---|---|---|---|
| `g2g_p50_ms` / `g2g_p99_ms` | **§8.1b** | `FrameMetadataFeatures { user_timestamp: true, frame_id: true }`; recv via frame metadata | app: `t_recv_unix_us − metadata.user_timestamp − θ`, histogrammed | app | per frame | `clock_sync_confidence ≥ probe`; `attach_timestamp` on | **Capture→app-delivery, not capture→photons.** It excludes display/compositor latency. Name that boundary in the report. |
| `g2g_pixel_p50_ms` (Tier 2) | §8.1b | `--burn-timestamp` (`publisher.rs:248`) + `TimestampOverlay` (`src/timestamp_burn.rs`) + `src/clock.rs` | manual: camera pointed at display, read burned timestamp vs on-screen clock | app | manual | physical rig | The only true camera→photons measure. Tier 2 only; used to calibrate the offset between it and `g2g_p50_ms` **once per codec/encoder tier**, not per run. |
| `g2g_frame_loss_pct` | §8.1b | frame metadata | app: `1 − distinct frame_id received / frame_id span` | app | per frame | `attach_frame_id` on | Distinguishes "G2G is fine but half the frames vanished" from a genuine latency result. |

**Decomposition for Q-7.** `g2g ≈ encode_time_avg + one-way transport + assembly + jitter_buffer_delay + decode_time`. Reporting the decomposition — not just the total — is what makes Q-7's answer actionable, and every term except transport is either codec- or encoder-tier-sensitive. Hence Q-7 is run per codec.

**Ordering constraint: `subscribe_timing_events()` must be called before constructing
`NativeVideoStream`.** This is load-bearing and its failure mode is silent.
`RemoteVideoTrack::subscribe_timing_events()`
(`livekit/src/room/track/remote_video_track.rs:152`) allocates the underlying transformer
**lazily on first call**, and the SDK's own doc comment (`:148-151`) states it must be
called "before constructing a `NativeVideoStream` so decoder-output timing can be wired
into the stream automatically." If the stream is constructed first, the transformer does
not exist when the stream is wired up and `frame_metadata` arrives **empty for every
frame**.

Nothing else degrades. Frames are received, decoded, and rendered normally; `InboundRtp`
counters, fps, and freeze metrics all look healthy. The only symptom is that
`metadata.user_timestamp` is `None` on every frame, so every G2G sample is dropped and
`g2g_p50_ms` reports `None`. Phase 3 hit exactly this: 702 frames received, 0 with
metadata, `g2g: None`, everything else green.

`examples/local_video/src/subscriber.rs` gets the ordering right —
`subscribe_timing_events()` at **1073**, `NativeVideoStream::new` at **1103** — but nothing
in the SDK enforces it and no compile-time error catches it.

**This is the single most dangerous failure mode in the harness**, because it produces a
run that passes every validity gate in §1g while silently omitting the one column Q-7
exists to produce. It is therefore a harness-health condition, not merely an
implementation note:

- The harness asserts the ordering at construction and fails loudly if violated.
- `g2g_metadata_coverage_pct` = `frames_with_metadata / frames_received × 100` is recorded
  per run. **< 95% ⇒ INVALID** with reason `g2g_metadata_missing`. A run cannot report
  `g2g: None` and still be scored — that is the confident-wrong-answer path.

Added to the `invalid_reason` vocabulary in §11.

### 1f. Session health

| metric | PRD clause | LiveKit Rust API | field / derivation | kind | cadence | validity precondition | risk |
|---|---|---|---|---|---|---|---|
| `session_drops` | **§8.6a** | `RoomEvent::Disconnected { reason }` (`livekit/src/room/mod.rs:256`) | app: count of terminal disconnects not initiated by the harness | app | event | — | Must exclude harness-initiated close. `reason` is recorded verbatim. |
| `reconnect_count` | §8.6b | `RoomEvent::Reconnecting` / `Reconnected` (`livekit/src/room/mod.rs:259-260`) | app: count of `Reconnecting` events | app | event | — | A survived reconnect is **not** a drop — §8.6b explicitly permits it. Scoring these as `session_drops` would fail runs the PRD passes. |
| `recovery_p95_ms` | §8.6b | same pair | app: p95 of `t(Reconnected) − t(Reconnecting)` | app | event | ≥5 events | The T-5 distribution. |
| `connection_state_changes` | §8.6 | `RoomEvent::ConnectionStateChanged(ConnectionState)` (`:249`) | app: transition log with timestamps | app | event | — | Timeline context for T-5. |
| `connection_quality` | observe | `RoomEvent::ConnectionQualityChanged { participant, quality }` (`:196`) | raw enum, per participant | raw | event | — | SFU's own opinion. Recorded, never thresholded — it is a smoothed heuristic, not a measurement. |
| `ice_selected_pair_changes` | §8.6b | `RtcStats::Transport` → `transport.selected_candidate_pair_changes` (`stats.rs:556`) | `Δ` | Δ | 1 Hz | — | Path migration during handover-sim. |
| `dtls_state` / `ice_state` | §8.6 | `TransportStats` (`stats.rs:547-548`) | `Option<DtlsTransportState>` / `Option<IceTransportState>` | raw | 1 Hz | — | `IceTransportState::Failed` is the ICE-failure signal; the C++ `ice_failures` counter has no direct equivalent (§2). |
| `join_to_first_video_ms` | T-6 context | harness | app: `t(first frame on RemoteVideoTrack) − t(connect() called)` | app | once | — | Not a PRD clause; gates operator-takeover work. |
| `join_to_connected_ms` | T-6 context | harness | app: `t(ConnectionState::Connected) − t(connect() called)` | app | once | — | Coarser than the C++ 13-milestone breakdown (§2). |

### 1g. Harness health — the INVALID axis

Per §12 of the master prompt and the governing page's four-verdict model, these **never
FAIL a run**; a breach marks it INVALID and excludes it from every breakpoint. A run where
the client stalled measured the client, not the network.

| metric | source | derivation | threshold | effect |
|---|---|---|---|---|
| `poll_overbudget_pct` | harness sampler | `polls where (actual_interval > 1.5 × nominal) / polls_total × 100` | ≤ 5% | INVALID |
| `poll_interval_p99_ms` | harness sampler | p99 of actual poll spacing | observe | context for the above |
| `control_publish_shortfall_pct` | harness publisher | §1c | ≤ 2% | INVALID |
| `quality_limitation_cpu_pct` | `OutboundRtp` | §1b | ≤ 10% | INVALID (`cpu_limited_encoder`) |
| `malformed_bitstream` | `OutboundRtp` | §1b | must be false | INVALID (`malformed_av1_bitstream`) |
| `codec_mismatch` | `CodecStats.mime_type` vs requested | §4 | must match | INVALID (`codec_fallback`) |
| `g2g_metadata_coverage_pct` | frame metadata | `frames_with_metadata / frames_received × 100` (§1e) | ≥ 95% | INVALID (`g2g_metadata_missing`) — catches the `subscribe_timing_events` ordering fault, which is otherwise silent |
| `clock_sync_confidence` | probe | §1d | `≥ probe` for OWD/G2G metrics | **column-scoped, conditionally run-scoped** — see rule (i) |
| `audio_level` | audio `InboundRtp` / `MediaSource` | §7 | > 0 for some part of the run | **column-scoped only** — see rule (ii) |
| `warmup_excluded_s` | harness | first 15 s of every run discarded before scoring | — | recorded, not scored |

### Column-scoped vs run-scoped invalidity

Not every validity breach invalidates a whole run. Two of the rows above suppress a *subset
of columns*, and treating them as run-level INVALID would discard runs that measured their
suite's actual question perfectly well. The distinction was underspecified in the first
version of this document; both rules below are now normative.

**Rule (i) — `clock_sync_confidence: none` invalidates the run if and only if the suite's
`primary` metric set contains a one-way or G2G metric.**

`none` means θ is unavailable, so every metric deriving from `owd_corrected` or
`t_recv − metadata.user_timestamp − θ` is unpublishable: `control_late_pct`,
`control_owd_p50_ms`, `control_owd_p99_ms`, `g2g_p50_ms`, `g2g_p99_ms`. RTT is unaffected —
the four-timestamp probe is skew-immune by construction (§1d) — as are all counter-derived
video, control-delivery, and session metrics.

Implementation is mechanical, not a per-suite lookup table: intersect the suite's `primary`
list with the set of metrics whose `requires` clause in `matrix.yaml` mentions
`clock_sync_confidence` or `theta`. Non-empty intersection ⇒ INVALID. This stays correct
automatically if a suite's `primary` set changes later, which a hardcoded suite list would
not.

Evaluating that rule against `matrix.yaml` as it currently stands:

| suite | θ-gated metrics in `primary` | verdict on `none` |
|---|---|---|
| T-3 jitter tolerance | `control_late_pct`, `control_owd_p50_ms`, `control_owd_p99_ms` | **INVALID** |
| Q-7 latency definition | `control_owd_p50_ms`, `control_owd_p99_ms`, `g2g_p50_ms`, `g2g_p99_ms` | **INVALID** |
| T-1 video floor | `g2g_p50_ms` | **INVALID** |
| T-2 loss collapse | `control_late_pct` | **INVALID** |
| T-4 capacity | `control_late_pct` | **INVALID** |
| T-5 availability | none | valid, columns suppressed |
| V-0 playout units | none | valid, columns suppressed |

**Only T-5 and V-0 survive**, and that is the correct answer even though it is broader than
it first looks. The reason is `control_late_pct`: it is a **blocking** threshold under §8.2b
(≤ 0.1%), and a run cannot be scored PASS against a blocking threshold whose value is null.
T-2 and T-4 both carry it in `primary`, so an unsynchronized run genuinely cannot produce
their verdict — T-2's answer is the collapse point *including* late-arrival loss, which is
precisely what §8.2b defines as loss. T-1 carries `g2g_p50_ms`, also blocking under §8.1b.

I had previously written that T-1, T-2 and T-4 stay valid. That was wrong, and the
mechanical rule caught it — it is exactly the class of error a hardcoded suite list would
have preserved. The rule is authoritative; this table is its current output, not an
independent specification, and it must be recomputed rather than trusted if `primary` sets
change.

**One `matrix.yaml` gap this surfaced:** `g2g_p99_ms` has no `requires` clause, though it
derives from θ identically to `g2g_p50_ms`. It should carry
`requires: "clock_sync_confidence >= probe; frame_metadata_features.user_timestamp"`. No
current verdict changes, since every suite with `g2g_p99_ms` also has `g2g_p50_ms`, but the
rule would miss it in a suite that took only p99.

**Rule (ii) — `audio_level == 0` is column-scoped only and never sets a run-level
`invalid_reason`.**

A silent source makes the concealment and accel/decel metrics meaningless, because they
measure damage to a signal that was not there. It says nothing about video, control, or
session health. Since audio is scored OBSERVE and never blocking (§7), a silent-audio run
still answers its suite's question in full.

Therefore: suppress the audio rows, record `silent_audio_source` in `invalid_detail`, and
leave run-level `invalid_reasons` empty. The analyst's Phase 4 handling is correct and is
now the specified behavior.

`silent_audio_source` nonetheless remains in the `invalid_reasons` vocabulary (§11) so the
value is legal wherever detail strings are validated against it — but it is **never emitted
at run level**. This asymmetry is deliberate; see the note in §11.

---

## 2. Gap list — C++ metrics with no Rust-SDK equivalent

Every gap has a resolution. None are left open.

| # | C++ metric (`stats.h`) | Why it does not map | Resolution |
|---|---|---|---|
| 1 | `ProbeStats` four-timestamp RTT (`:446-498`) | `RtcStats` has no application-level probe. `CandidatePair.current_round_trip_time` is STUN-consent RTT; `RemoteInboundRtp.round_trip_time` is RTCP at ~1 Hz on the video path. Neither is the control-path RTT §8.1a is about. | **Derive app-side.** Reimplement the four-timestamp probe over the control transport. Both SDK RTTs are recorded alongside as corroborators (§1d) so an implementation error in the probe is visible rather than silent. |
| 2 | `OwdCalibration` θ estimation (`:522-560`) | No SDK support; requires paired min-OWD/min-RTT windows. | **Derive app-side.** Direct reimplementation. Publish `clock_sync_confidence` with every one-way figure and state the `(d_in−d_out)/2` residual bound in the report. |
| 3 | `VideoReceiveStats::frame_interval_hist` (`:651`) | `InboundRtp` gives `total_inter_frame_delay` and `total_squared_inter_frame_delay` — sufficient for mean and variance, **not** for p99. A freeze is a tail event. | **Derive app-side** from the frame-receive callback. Keep `Δtotal_inter_frame_delay / Δframes_decoded` as a mean cross-check. |
| 4 | `PollStats` overbudget (`:771-803`) | The C++ client had an explicit `poll()` the harness owned. There is no equivalent SDK concept. | **Derive app-side.** The harness owns its sampler loop and accounts for its own cadence. This is the primary INVALID gate (§1g). |
| 5 | `VideoSendStats::loop_latency` G2G (`:598-601`) | The C++ scheme was a send-stamped ID echoed over the reliable channel. | **Substitute, and it is an upgrade.** `FrameMetadataFeatures` carries the timestamp and frame id **in-band with the encoded frame** (§1e), removing the echo-path latency the C++ scheme included in its own measurement. This closes a gap the governing page listed as blocking one column of Q-7. |
| 6 | `VideoSendStats::kf_service_hist` (`:579`) | No PLI callback in the Rust SDK; `pli_count` is a cumulative counter sampled only at poll cadence. | **Substitute at reduced resolution, with the cadence fixed rather than conditional.** Δ-triggered timing at **10 Hz for T-2**, 1 Hz elsewhere; reported as a poll-interval distribution plus maximum, never as a millisecond percentile. `video_poll_hz` is recorded per run. Full reasoning and the reporting rule are in §1b. |
| 7 | `JoinMilestones` 13-point breakdown (`:391-432`) | The Rust SDK surfaces `RoomEvent`s (`Reconnecting`, `Reconnected`, `ConnectionStateChanged`), not per-SDP-step callbacks. Publisher/subscriber offer/answer timings are internal to `rtc_engine`. | **Drop the fine breakdown; keep the two endpoints.** `join_to_connected_ms` and `join_to_first_video_ms` (§1f) answer the operator-takeover question the milestones existed for. The intermediate steps were diagnostic for a bespoke client and have no PRD clause. |
| 8 | `IceStats::ice_failures` (`:727`) | No ICE-failure event; libwebrtc's failure surfaces as state. | **Substitute.** Count transitions into `IceTransportState::Failed` from the 1 Hz `TransportStats.ice_state` poll, plus `selected_candidate_pair_changes`. A failure shorter than the poll period is missed — state that limit in T-5. |
| 9 | `IceStats::publisher_sctp_srtt_ms` (`:743`) | usrsctp `SCTP_STATUS` is not exposed by libwebrtc's stats or by this SDK. | **Drop.** It diagnosed the C++ SCTP stack. The head-of-line-blocking finding it supported is reproduced directly by `control_late_pct` on `dc_reliable` (§5, T-2), which is the observable that actually matters. |
| 10 | `SignalingRttStats` (`:337-373`) | No signaling ping/pong RTT exposed. | **Drop.** Signaling RTT is not on the media path and no PRD clause depends on it. |
| 11 | `SessionStats` sig_* message counters (`:839-848`) | Not exposed. | **Drop.** Protocol-debug instrumentation, no clause. |
| 12 | `DataChannelStats` for the data-track path (`:256-330`) | `RtcStats::DataChannel` covers SCTP channels only; `livekit-datatrack` has no stats surface. | **Derive app-side** (§1c). This is why the control payload carries its own sequence number — it is the only way to measure the data-track path at all. |
| 13 | `RoomStats::diff()` (`:936`) | No SDK equivalent; `RtcStats` is raw. | **Derive app-side.** The harness writes one JSON snapshot per poll and `parse_runs.py` differences them. Making differencing an analysis-side operation, not a client-side one, means a bug in it is fixable without re-running the matrix. |
| 14 | **Audio (all)** | The C++ harness measured none. | **See §7 — specified, not deferred.** |

---

## 3. Axes

### `video_codec: [av1, vp9, vp8, h264]`

`PublisherCodec` (`examples/local_video/src/publisher.rs:46-52`) → `livekit::options::VideoCodec` (`:54-64`). H265 exists in the enum but is **excluded from the matrix**: it is the one codec with an automatic fallback path (`publisher.rs:1210-1224`, H265→H264 on publish failure), so an H265 cell silently becomes an H264 cell. Including it would inject exactly the requested-vs-actual confusion §4 exists to prevent, for a codec no PRD clause needs.

Crossed with `video_profile` for T-1; swept for Q-7 and T-2.

### `encoder_tier: [sw, videotoolbox, vaapi, nvenc, jetson]`

Recorded, never requested. Selection is automatic per the encoder docs; the harness reads
back `encoder_implementation` and `power_efficient_encoder` and classifies. The
`--encoder` flag (`PublisherEncoder`, `publisher.rs:106-128`, mapping to
`VideoEncoderBackend` at `:130-140`) is set to `auto` for the matrix and its value recorded
— forcing a backend would measure a configuration no robot runs.

Per the support matrix, **Apple Silicon has no AV1 hardware encoder**. On a MacBook,
`--codec av1` is `encoder_tier: sw`. Analysis never pools across tiers.

### `buffering_mode: [default, playout_hint_floor, playout_hint_smooth, zero_jitter]`

| value | mechanism | applied value |
|---|---|---|
| `default` | no configuration | SDK default jitter buffer |
| `playout_hint_floor` | `create_room_with_playout_delay(name, opts, 0, 10)` (`livekit-api/src/services/room.rs:129-145`) | **no minimum set, maximum 10 ms** — the docs' "low latency" preset verbatim. See note below. |
| `playout_hint_smooth` | `create_room_with_playout_delay(name, opts, 400, 2000)` | minimum 400 ms, maximum 2000 ms — the docs' "smooth playback" preset; the deliberate contrast case |
| `zero_jitter` | `livekit::webrtc::enable_zero_playout_delay()` (`livekit/src/lib.rs:34`) | true 0/0, subscriber-side, video tracks only |

**`min=0` means "not set", not "pinned to zero".** The docs state *"A value of 0 means 'not
set.'"* So `playout_hint_floor` does not request a zero-millisecond buffer — it requests a
**10 ms ceiling with no floor**, leaving the receiver free to choose anything from 0 up to
10 ms based on observed network conditions. That is a materially weaker request than
`zero_jitter`, which forces 0/0 unconditionally. The two are not a "floor and a slightly
lower floor"; they are a *bounded adaptive buffer* and *no buffer at all*, and the axis
exists to measure the difference between them.

**Process-global constraint.** `enable_zero_playout_delay` mutates `LK_RUNTIME` state and
errors with `WebRtcRuntimeInitializedError` if the runtime is already up without it
(`livekit/src/rtc_engine/lk_runtime.rs:40-49`, `:72-77`). It cannot be toggled per room or
per subscriber within one process. **Consequence for the runner:** `buffering_mode` must be
a per-process axis — each run is a fresh subscriber process, and `zero_jitter` runs may not
share a process with any other mode. `run_matrix.py` must group runs by `buffering_mode`
and never reuse a process across the boundary. Getting this wrong produces a run labelled
`zero_jitter` that silently ran with the default buffer.

### `control_transport: [data_track_buf1, dc_reliable, dc_lossy]`

- `data_track_buf1` — `DataTrackSubscribeOptions::new().with_buffer_size(1)`
  (`livekit-datatrack/src/remote/mod.rs:227-234`). Note the **default is 16 frames**
  (`:212`), so `buffer_size(1)` is an explicit choice, not the default; at 200 Hz, 16
  frames is up to 80 ms of queued staleness — precisely what §8.2b forbids. `buffer_size`
  is a **frame count, not a duration** (`:215-221`); the doc comment is unambiguous. Also
  set `RemoteDataTrackPipelineOptions::with_max_partial_frames(1)` (`:288-295`, already the
  default at `:271`) and record it — a 32-byte control payload never spans packets, so
  higher values buy nothing and add reassembly state.
- `dc_reliable` / `dc_lossy` — `LocalParticipant::publish_data(DataPacket { reliable, .. })`
  (`livekit/src/room/participant/local_participant.rs:703-704`), received via
  `RoomEvent::DataReceived`. The legacy path, kept to reproduce the head-of-line-blocking
  finding that is the evidence *for* the data-track design.

### `ran_profile`

Recorded, never set by the harness. Five fields owned by the network team, defaulting to
`unknown`: `rlc_mode` (AM/UM), `aqm_mode` (OFF/Non-GBR/GBR), `pdcp_discard_timer_ms`,
`pdcp_reordering_timer_ms`, `rlc_reassembly_timer_ms`. On `path: loopback` or `lan`,
`ran_profile: n/a` and the report must state those results do not transfer to cellular.

### Netem axes (Tier 1 only)

`loss_pct [0, 0.5, 1, 2, 5, 10, 15, 20, 30, 50]`, `owd_ms [0, 10, 25, 45, 70, 100]`,
`jitter_ms [0, 2, 5, 10, 20, 40]`, `uplink_mbps [10, 7, 5, 3, 2, 1, 0.5]` — values from the
governing page §2, unchanged. `concurrency [1, 10, 25, 50, 70]`. Every one is excluded from
the Tier 0 shaping-free subset.

`video_profile`, all at 30 fps:

| name | resolution | source |
|---|---|---|
| `target` | 1600×1300 | PRD §7.1a and governing page, literal |
| `tolerable` | 1920×1080 | governing page "1080p", literal in PRD §7.1a |
| `minimum` | 1280×720 | governing page "720p", literal in PRD §7.1a |
| `sub_min` | 854×480 | **derived** — see below |

`sub_min` sits below the §7.1a minimum deliberately, to verify §7.1d pause behavior
triggers. The governing page specifies only **"480p"** with no width, and the PRD does not
mention it at all. 854×480 is this document's derivation: 480p at the 16:9 aspect ratio the
other two named-by-shorthand rungs (1080p, 720p) both use, rounded to an even width. The
alternative reading, 640×480 at 4:3, would change the pixel count by 25% and would be the
only non-16:9 rung in the ladder. Recorded as derived so that if Figure intended 4:3, one
`matrix.yaml` value changes rather than the result being silently misattributed to the page.

### Required cell

**`video_codec: av1` × `buffering_mode: zero_jitter` is mandatory, not optional.** Zero
jitter buffer plus larger, more variable frames is where frame-assembly sensitivity shows
up; `assembly_time_avg_ms` and `video_frame_interval_p99_ms` (§1a) are its primary metrics.
The cell must appear in Q-7 and T-2 and must not be excluded by any Tier-0 filter — it
needs no netem.

---

## 4. Requested vs actual

Three places where a requested value can differ from the applied one. In each case the
analysis uses the **actual**; the requested is recorded for the diff.

| quantity | requested field | actual field | analysis uses | on mismatch |
|---|---|---|---|---|
| video codec | `TrackPublishOptions.video_codec` (`publisher.rs:1197`) | `CodecStats.mime_type` (`stats.rs:319`) via `stream.codec_id` | actual | **INVALID** (`codec_fallback`). H265 is excluded from the matrix precisely because it has a built-in fallback (`publisher.rs:1210-1224`). |
| encoder | `--encoder auto` | `encoder_implementation` + `power_efficient_encoder` (`stats.rs:441-442`) | actual, classified into `encoder_tier` | record; not a failure — automatic selection is the documented behavior |
| resolution | `--width/--height` | `OutboundRtp.frame_width/frame_height` (`stats.rs:425-426`) | actual | record; downscale under `quality_limitation_reason: Bandwidth` is the §7.1c behavior T-1 is measuring |
| playout delay | `create_room_with_playout_delay(min, max)` | `jitter_buffer_target_delay / jitter_buffer_emitted_count` | actual | record both; see below |
| netem | requested `tc` parameters | verbatim `tc qdisc show` output captured post-apply | actual | record both |

### The playout-delay units question — resolved

The apparent conflict is not a conflict. Both accounts describe the same behavior at
different layers:

1. **The Rust API takes milliseconds.** `create_room_with_playout_delay`
   (`livekit-api/src/services/room.rs:129-145`) forwards raw `u32` min/max into
   `proto::CreateRoomRequest.min_playout_delay` / `max_playout_delay`
   (`livekit-protocol/protocol/protobufs/livekit_room.proto:104-105`). Nothing in this
   checkout scales, reinterprets, or validates the values.
2. **The docs confirm milliseconds and name the granularity.** The playout-delay page gives
   `--min-playout-delay 0 --max-playout-delay 10` and
   `create_room_with_playout_delay("my-robot", CreateRoomOptions::default(), 0, 10)` as the
   "low latency" preset, and states: *"The values apply in steps of 10 ms, rounded down: a
   value of 15 ms becomes 10 ms."*
3. **That 10 ms granularity is the wire format.** The SFU consumes the proto field and sets
   the standard WebRTC `PlayoutDelay` RTP header extension, whose wire encoding is defined
   in 10 ms units. This is libwebrtc/RTP-spec behavior, not something this checkout
   implements.

**Resolution: the API unit is milliseconds, quantized to 10 ms steps rounded down.**
David Chen's "units of 10 ms" describes the wire granularity correctly; the docs' apparent
milliseconds reading is also correct at the API. The floor is not `min=0, max=1` — the docs
state *"A value of 0 means 'not set.' When both values are 0, playout delay is off"*, and
separately that true 0/0 *"isn't supported when using playout delay hints"* and that zero
jitter buffer mode is the supported route to it. So the lowest request expressible through
hints is `min=0, max=10` (ms), which is **a 10 ms ceiling with no floor, not a 0 ms
buffer** (§3), and `zero_jitter` is the only true 0/0. **`matrix.yaml` states values in
milliseconds** and the two mechanisms remain distinct axis values, never conflated.

**Tier-0/Tier-1 empirical confirmation (required, not optional).** Before any suite is
scored, run one cell at `playout_hint_smooth` (400/2000 ms) and one at
`playout_hint_floor` (0/10 ms) on an otherwise identical configuration, and compute
`jitter_buffer_delay_avg_ms` as the **Δratio** in §1a.

The test rests on the **smooth cell**, because it is the only one of the two whose
*minimum* is actually set. `min=400` pins the buffer at no less than 400 ms if the value is
milliseconds, or no less than 4000 ms if it is 10 ms units. The two predictions differ by
10×, well outside any measurement noise:

- `jitter_buffer_delay_avg_ms` ≈ 400–2000 ⇒ **milliseconds confirmed**, this resolution
  stands.
- `jitter_buffer_delay_avg_ms` ≈ 4000–20000 ⇒ values are 10 ms units at the API. Halt and
  correct `matrix.yaml`.

The floor cell is **not** part of the discriminator and no threshold is asserted on it. With
`min=0` meaning "not set" (§3), the receiver may legitimately choose anything from 0 to
10 ms, so any observed value in that range is consistent with both hypotheses and falsifies
neither. It is run only to record what the unconstrained receiver actually chooses — which
is itself the `playout_hint_floor` vs `zero_jitter` delta the buffering axis is measuring.

Record the outcome in the run record as `playout_units_confirmed: true|false`. I have not
run this live; it is a gate, not an assumption.

---

## 5. Reference configuration

The baseline cell every suite runs and every result is a delta from. Derived from the docs,
with citations — not invented.

**Selection rule, applied consistently below.** The baseline must be *portable across every
execution tier*, because a delta is only meaningful if the thing it is measured against is
the same experiment on a MacBook, a Linux host, and a Jetson. Where the docs'
lowest-latency recommendation is not portable, the baseline takes the most aggressive
*portable* setting and the non-portable one becomes a swept treatment. This rule decides
both note (a) and note (c); it is the reason the baseline is not simply "every latency
knob at its most aggressive."

```yaml
reference_config:
  # Video track — docs.livekit.io/robotics/media/video/video-tracks/
  video:
    track_layout: one_named_track_per_camera   # NativeVideoSource per camera
    source: TrackSource::Camera                # TrackPublishOptions{source, ..Default}
    codec: h264                                # see note (a)
    encoder: auto                              # encoders/: "Encoder selection is
                                               # automatic ... No configuration is required"
    profile: tolerable                          # 1920x1080 @ 30 fps   (PRD §7.1a)
    max_bitrate: 5_000_000                      # PRD §8.0b uplink ceiling
    max_framerate: 30
    simulcast: false                            # see note (b)
    dynacast: false
    frame_metadata_features:                    # livekit/src/room/options.rs:80-82
      user_timestamp: true                      # G2G send stamp
      frame_id: true                            # G2G frame-loss accounting

  # Buffering — docs.livekit.io/robotics/media/performance/low-latency/playout-delay/
  buffering_mode: playout_hint_floor            # see note (c)
  video_playout_delay:                          # create_room_with_playout_delay(name,
    min_ms: 0                                   #   opts, 0, 10)
    max_ms: 10                                  # the docs' "low latency" preset.
                                                # min=0 means NOT SET, not pinned to
                                                # zero — a 10 ms ceiling, no floor (§3).
  audio_buffering: sdk_default                  # see note (d) — NOT affected by
                                                # zero_jitter, and not settable
                                                # independently through any Rust API.

  # Control — docs.livekit.io/robotics/teleop/robot/
  control:
    transport: data_track                       # lossy delivery; "drops older frames rather
                                                # than delaying newer ones"
    buffer_size: 1                              # verbatim from the robot teleop page:
                                                # DataTrackSubscribeOptions::new()
                                                #   .with_buffer_size(1)
                                                # "stale commands are usually worse than
                                                # dropped commands"
                                                # (SDK default is 16 — remote/mod.rs:212)
    max_partial_frames: 1                       # remote/mod.rs:271 (default)
    rate_hz: 200                                # PRD §7.2 mocap sample rate
    apply_policy: latest_valid_command          # robot teleop page

  # Audio — PRD §8.0, §7.4
  audio:
    enabled: true                               # see §7
    direction: bidirectional
    target_bitrate: 250_000

  # Environment
  environment:
    path: cloud
    encoder_tier: <recorded>
    ran_profile: n/a                            # unless on a lab RAN
```

**(a) Why H264 is the reference codec, not AV1.** By the selection rule: AV1 has no
hardware encoder on Apple Silicon, so an AV1 baseline would make the Tier 0 baseline and
the Tier 2 baseline different experiments, and no delta computed against them would be
comparable. H264 has hardware support on every platform in the encoder matrix. AV1 remains
the most important *swept* value — it is what T-1 and Q-7 exist to evaluate — but it is a
treatment, not the control. Trade-off: the baseline is not the lowest-bitrate configuration
available; it is the most portable one.

**(b) Simulcast off.** `--simulcast` / `--dynacast` exist (`publisher.rs:1166`, `:940`) and
change bitrate behavior substantially. Fixed off for the core matrix, recorded in the
environment block, treated as a follow-on question.

**(c) Why `playout_hint_floor` is the baseline, not `zero_jitter`.** The zero-jitter page
does recommend zero jitter buffer mode for teleoperation, and on a pure latency argument it
would be the baseline. The selection rule overrides that, for the same reason it rejected
AV1 — and `zero_jitter` is in fact *less* portable than AV1:

- It is **Rust-SDK only**. A robot or operator app on any other LiveKit SDK cannot
  reproduce the baseline at all.
- It is **subscriber-side and process-global** (`lk_runtime.rs:40-49`), so it is a property
  of how the measuring client was launched rather than of the session under test.
- It applies to **video tracks only** — see note (d).

`playout_hint_floor` is room-level, set through the server API, and therefore applies
identically to every subscriber on every SDK and every tier. It is the most aggressive
*portable* buffering setting available, which is exactly what the selection rule asks for.

This matters beyond consistency: `zero_jitter` is the single most interesting treatment in
the matrix — it is half of the mandatory AV1 × zero_jitter cell and the precondition for the
§5 RAN hypothesis, under which a retransmitted late packet is strictly worse than a dropped
one. A treatment cannot be its own control. Making it the baseline would mean the headline
`zero_jitter` result is a delta against itself, i.e. zero, and the buffering axis would
answer nothing. With `playout_hint_floor` as baseline, "what does forcing a zero jitter
buffer actually buy" becomes a measured number.

Trade-off, stated plainly: the baseline is **not** the lowest-latency configuration LiveKit
recommends. Every reported delta is therefore measured from a slightly more conservative
starting point than a production teleop deployment would use, and the report must say so
whenever it quotes a baseline latency figure.

**(d) Audio buffering is unconfigured, and that is recorded rather than assumed.** Audio and
video have independent jitter buffers. `enable_zero_playout_delay` sets the
`WebRTC-ForcePlayoutDelay` field trial, which the docs scope to video tracks, and the
room-level playout-delay hints set an RTP header extension on video. **No Rust API in this
checkout configures the audio jitter buffer**, so audio runs at the SDK default under every
value of `buffering_mode`, including `zero_jitter`.

`audio_buffering: sdk_default` is therefore a *recorded fact*, not a setting the harness
applies. It exists in the run record so that a reader of a `zero_jitter` run cannot infer
that audio was also unbuffered. `audio_jitter_buffer_delay_ms` (§7) measures what the audio
buffer actually did; if it fails to move across `buffering_mode` values, that confirms the
independence rather than indicating a harness fault, and the analysis must not treat it as
one.

---

## 6. Thresholds

Traced to PRD version 32 and Test Matrix v2, both fetched 2026-08-24. The `source` column
states where each row's *normative force* comes from, because it is not uniform: most
blocking rows are PRD MUSTs, but two are PRD SHOULDs promoted to blocking by the governing
Test Matrix page, and one threshold value appears in no PRD clause at all. Do not assume
`blocking == PRD MUST`.

| metric | op | value | clause | source | verbatim basis | blocking |
|---|---|---|---|---|---|---|
| `network_rtt_p95_ms` | ≤ | 90 | §8.1a | PRD MUST | "MUST NOT exceed ~90 ms" | **yes** — see note (a1) |
| `network_rtt_p50_ms` | ≤ | 50 | §8.1a | PRD SHOULD | "SHOULD stay within 20–50 ms" | no (advisory) |
| `app_rtt_p95_ms` | — | — | §8.1a | — | reported, never scored | no — **OBSERVE**, note (a1) |
| `g2g_p50_ms` | ≤ | 100 | §8.1b | **PRD SHOULD, promoted by governing page** | "E2E latency, camera to headset, SHOULD stay under 100 ms" | **yes** — see note (c1) |
| `control_owd_p99_ms` | ≤ | 100 | §8.1c | PRD SHOULD | "The p99 of the robot → TC SHOULD stay under 100 ms" | no (advisory) — see note (d2) |
| `control_delivered_pct` | ≥ | 99.9 | §8.3a | PRD MUST; **value from governing page** | "control and force paths MUST run at near-zero loss" | **yes** |
| `control_late_pct` | ≤ | 0.1 | §8.2b | PRD MUST; **value from governing page** | "A control sample that misses its playout deadline MUST be dropped" | **yes** |
| `video_bitrate_bps` | ≤ | 5 000 000 | §8.0b | PRD (stated target) | "the sustained uplink target is ≤ 5 Mbps" | **yes** |
| `video_fps_p50` | ≥ | 27 | §7.1a | **governing page only** | see note (e) | **yes** |
| `session_drops` | == | 0 | §8.6a | PRD MUST | "a session MUST NOT drop from network causes" | **yes** |
| `poll_overbudget_pct` | ≤ | 5 | — | governing page §3 | harness validity | invalidates |
| `poll_overbudget_multiplier` | — | 1.5 | — | **harness-derived** | defines "overbudget" as `interval > 1.5 × nominal`; see note (f) | parameter |
| `control_publish_shortfall_pct` | ≤ | 2 | — | **harness-derived** | this design §1c | invalidates |
| `quality_limitation_cpu_pct` | ≤ | 10 | — | **harness-derived** | master prompt §6c | invalidates |
| `malformed_bitstream` | == | false | — | harness validity | `publisher.rs:462-466` | invalidates |
| `codec_mismatch` | == | false | — | harness validity | this design §4 | invalidates |

Rows marked **harness-derived** have no external authority behind their numeric value. They
are engineering judgments made in this document and are open to revision on evidence; they
must not be presented in the report as requirements.

**Precedence.** The masthead rule is that the PRD wins conflicts, and it holds for every
*factual* conflict — stream rates, latency figures, what a clause actually says. Note (c1)
is the one row where the governing page's **stricter** treatment is adopted over the PRD's
modal verb. That is not a conflict in fact: the PRD says 100 ms is the bar and the governing
page agrees, differing only on how hard to enforce a breach. Adopting the stricter of two
agreeing sources is not overriding the PRD, and it is applied in exactly one direction —
the governing page may make a bar harder, never looser, and may never change a number. If
the two sources ever disagree on a *value*, the PRD wins without exception.

**Corrections against the C++ matrix.**

- **(a1) The §8.1a bar attaches to the network round trip, not the probe.** Added
  2026-08-25; see the amendment above for the measurements. §8.1a's companion SHOULD is
  "20–50 ms", a network-path figure that an application loop containing 200 Hz
  control-publisher scheduling at both ends cannot plausibly meet, so the clause is
  describing the network. Scoring `app_rtt_p95_ms` against it would apply a network bar to
  a number that includes harness scheduling — structurally the same mistake as note (d2)
  below. The probe figure is reported alongside as OBSERVE because the gap between the two
  is the application's own contribution.

- **(c1) §8.1b is a SHOULD, promoted to blocking.** The PRD says "SHOULD stay under 100 ms";
  the governing Test Matrix page (v2) marks it Blocking, as does the C++ matrix. **Kept
  blocking** — G2G is the number Q-7 exists to settle and the suite is worthless if a breach
  is only advisory. The report must state the bar is a PRD SHOULD promoted by the test spec,
  not a PRD MUST.
- **(d2) The C++ implementation's `rtt_p99_ms ≤ 100 / §8.1c` row is a bug, not stale
  requirements.** §8.1c says *"The p99 of the robot → TC SHOULD stay under 100 ms"* — a
  **one-way** figure, not a round-trip. Scoring RTT p99 against it applies a one-way bar to
  a round-trip number, roughly 2× too strict. Corrected here to `control_owd_p99_ms`,
  advisory.

  The root cause is worth naming precisely, because it changes what Phase 2 may trust.
  **Test Matrix v2 already scores this correctly** — its threshold table reads "Robot → TC
  p99 ≤ 100 ms, §8.1c, Advisory." So the C++ `matrix.yaml` diverges from its own governing
  spec; the spec was never wrong and the PRD never moved. **Consequence: the governing
  page's threshold table is a trustworthy transcription source for `matrix.yaml`, and the
  C++ `matrix.yaml` is not.** Where the two differ, Phase 2 takes the governing page and
  re-derives from the PRD rather than porting the C++ row.
- **§8.1a's lower bound is not a floor to enforce.** "20–50 ms" is a SHOULD *range*;
  measuring below 20 ms on loopback is not a violation. Only the upper bounds are scored.
- **(e) `video_fps_p50 ≥ 27` has no PRD basis and is not a PRD MUST.** PRD §7.1a gives the
  resolution/fps ladder ("Target: 1600 × 1300 at 30 fps ... Minimum: 720p at 30 fps") but
  states **no frame-rate threshold and no tolerance figure**. The 27 fps bar and the 10%
  tolerance behind it come from the governing page's threshold table ("Video frame rate
  ≥ 27 fps"), which cites §7.1a for the 30 fps ladder rather than for the tolerance. Kept
  blocking, following the governing page, but labelled governing-page-derived. The report
  must not cite "PRD §7.1a" as the authority for the number 27.
- **(f) The 1.5× overbudget multiplier is harness-invented.** "Overbudget" is defined here
  as a poll interval exceeding 1.5 × nominal. No source specifies it. Per master prompt §12,
  thresholds and rates live only in `matrix.yaml` — **Phase 2 must put this multiplier
  there as a named parameter**, not leave it hardcoded in the sampler or in
  `parse_runs.py`. Flagged so it does not become a magic number in Python.

**Audio thresholds** are in §7. Note the PRD's audio row reads "50 ms" flat, not the
"0–50 ms" range the C++ `matrix.yaml` meta block encodes — a one-sided budget.

---

## 7. The audio decision — specified, not deferred

The PRD names audio as one of four concurrent streams (§8.0), gives it a ≤50 ms latency
budget and ~0.25 Mbps each way, and §7.4 makes it a MUST: *"Audio MUST run both ways,
operator ↔ robot, within the §8 latency."* The C++ harness measured none. **Audio is
specified here.** Deferring it a second time would leave a PRD MUST with no evidence in a
report that reads as complete.

Scope is deliberately bounded: audio is exercised as a **concurrent load and a
latency-budget check**, not as a quality study. No MOS, no POLQA.

### Audio metrics

`RtcStats::MediaPlayout(MediaPlayoutStats)` → `audio_playout: AudioPlayoutStats`
(`libwebrtc/src/stats.rs:506-513`); audio `InboundRtp` reuses `InboundRtpStreamStats`,
whose audio fields are at `stats.rs:382-390`;
`RtcStats::MediaSource` → `audio: AudioSourceStats` (`stats.rs:481-491`).

| metric | PRD clause | LiveKit Rust API | field / derivation | kind | cadence | risk |
|---|---|---|---|---|---|---|
| `audio_playout_delay_avg_ms` | **§8.0 (50 ms)** | `MediaPlayout` | `(Δtotal_playout_delay / Δtotal_samples_count) × 1000` | Δratio | 1 Hz | The nearest available proxy for the 50 ms budget. It is **playout-side delay only**, not end-to-end mouth-to-ear — name that boundary. |
| `audio_jitter_buffer_delay_ms` | §8.0 | audio `InboundRtp` | `(Δjitter_buffer_delay / Δjitter_buffer_emitted_count) × 1000` | Δratio | 1 Hz | Audio and video have independent jitter buffers; `zero_jitter` is a **video** field trial and must not be assumed to affect this. Measure, do not infer. |
| `audio_concealment_pct` | §8.3b | audio `InboundRtp` | `Δconcealed_samples / Δtotal_samples_received × 100` | Δratio | 1 Hz | The audio analogue of packet loss. Includes `silent_concealed_samples`; report both so silence is not counted as damage. |
| `audio_concealment_events` | §8.3b | audio `InboundRtp` | `Δconcealment_events` | Δ | 1 Hz | Event count separates one long gap from many short ones. |
| `audio_accel_decel_pct` | §8.2 | audio `InboundRtp` | `Δ(inserted_samples_for_deceleration + removed_samples_for_acceleration) / Δtotal_samples_received × 100` | Δratio | 1 Hz | NetEQ time-stretching — the audio jitter-adaptation signal, and the earliest indicator that jitter is biting. |
| `audio_bitrate_bps` | §8.0 | audio `OutboundRtp` | `(Δ(bytes_sent + header_bytes_sent) × 8) / Δt` | Δ | 1 Hz | Confirms the ~0.25 Mbps budget claim. |
| `audio_synthesized_pct` | §8.3b | `MediaPlayout` | `Δsynthesized_samples_duration / Δtotal_samples_duration × 100` | Δratio | 1 Hz | Playout-side gap fill; corroborates concealment. |
| `audio_packets_lost_pct` | §8.3b | audio `InboundRtp` | as video, on the audio stream | Δratio | 1 Hz | Same negative-delta clamp as video. |
| `audio_level` | validity | audio `InboundRtp` / `MediaSource` | `inbound.audio_level`, `source.audio_level` | raw | 1 Hz | **Harness-health, column-scoped.** A silent source makes every concealment metric meaningless. `audio_level == 0` for the whole run ⇒ audio columns suppressed, `silent_audio_source` recorded in `invalid_detail`, **run-level `invalid_reasons` left empty** — the run stays valid for video, control, and session. See §1g rule (ii). |

### Threshold

| metric | op | value | clause | source | blocking |
|---|---|---|---|---|---|
| `audio_playout_delay_avg_ms` | ≤ | 50 | §8.0 (audio row), §7.4 | PRD, literal value | no — **OBSERVE** |
| `audio_concealment_pct` | ≤ | 5 | §8.3b | **harness-derived** — see below | no — OBSERVE |

Audio is scored OBSERVE, not blocking, on purpose: the PRD's 50 ms is a mouth-to-ear
budget and `total_playout_delay` measures only the playout-side share. Making a blocking
verdict turn on a proxy that measures less than the clause describes would produce
confident wrong answers — the exact failure the four-verdict model exists to prevent. The
report states the measured share and names what it excludes.

**The 5% concealment figure has no source.** PRD §8.3b says only that "video and audio
tolerate loss and adapt" — it gives no audio loss or concealment number, and neither does
the governing page. 5% is an engineering judgment in this document, chosen as a visible
starting point rather than a requirement. It must be reported as an observation against a
harness-chosen reference, never as a PRD threshold, and should be revised once real
distributions exist. This is the weakest-sourced number in the design and is labelled as
such deliberately.

### Where audio is exercised

- **`reference_config`** — audio on, bidirectional. Every baseline cell carries it, so the
  four-stream concurrency of §8.0 is real in every suite rather than a video-only
  approximation.
- **T-4 concurrency** — audio metrics are primary. This is where a fourth stream competing
  for the same uplink actually shows.
- **T-2 loss collapse** — audio concealment and accel/decel reported alongside control, so
  §8.3b's "video and audio tolerate loss" gets evidence rather than assertion.
- **T-1, T-3, T-5, Q-7** — audio published as load, metrics recorded, not primary.

Source: `NativeAudioSource` (`livekit/src/room/participant/local_participant.rs:295-299`)
→ `LocalAudioTrack::create_audio_track` (`livekit/src/room/track/local_audio_track.rs:53`).
The harness publishes a deterministic synthetic tone, not a microphone — a microphone would
make `audio_level` environment-dependent and the runs non-reproducible.

---

## 8. Sensitivity — what may never be pooled

Pooling across either dimension produces an average describing no real configuration.

### Codec-sensitive — report per codec

| metric | why |
|---|---|
| `video_bitrate_bps` | The point of AV1. Bitrate-at-quality is the entire T-1 question. |
| `keyframe_service_polls` | Keyframe size and cadence differ by codec; drives T-2 recovery. Comparable across codecs only because all are polled at the same `video_poll_hz`. |
| `pli_rate_per_min`, `key_frames_decoded_rate` | Loss-recovery behavior is codec-specific. |
| `decode_time_avg_ms` | **The decode share of G2G.** Makes Q-7's three-column ratio codec-dependent. |
| `assembly_time_avg_ms` | Frame-to-packet mapping differs by codec; the AV1 × zero_jitter metric. |
| `video_frame_interval_p99_ms` | Larger variable frames change pacing under zero jitter. |
| `qp_sum`-derived quality | Not comparable across codecs at all — QP scales differ. Recorded per codec, never compared across. |

### Encoder-tier-sensitive — report per tier

| metric | why |
|---|---|
| `encode_time_avg_ms` | **The encode share of G2G.** Software AV1 on Apple Silicon says nothing about NVENC AV1. |
| `fps_ceiling` | A software encoder's fps ceiling is a CPU fact, not a codec fact. |
| `quality_limitation_cpu_pct` | Entirely a property of the encoder tier. |
| `power_efficient_encoder` | Definitionally tier-specific. |

**Largely portable across tiers** (report pooled, with the tier recorded):
`video_bitrate_bps` at a fixed profile — bitrate efficiency at quality is a codec property
more than an encoder-implementation one. This is the one claim from Tier 0 that transfers,
and it is exactly the claim T-1 needs. Every Tier 0 or Tier 1 result at `encoder_tier: sw`
or `videotoolbox` is **provisional** for any encoder-sensitive metric until re-run at
Tier 2, and must be labelled as such wherever it appears.

Also never pooled, per §12: across `buffering_mode`, `control_transport`, `ran_profile`, or
`path`.

---

## 9. Suite boundaries — what each answers, and what it provably cannot

| suite | answers | provably cannot answer |
|---|---|---|
| **T-1 video floor** | What bitrate each profile × codec × encoder emitted under the §8.0b 5 Mbps ceiling while holding the governing page's ≥27 fps bar (§6 note (e) — a test-spec figure, not a PRD one), **at whatever quality that cell's rate control chose**, and the resulting profile→codec→encoder→bitrate→QP→fps table. Within a single codec the bitrate comparison across rungs is sound. | Whether any rung supports fine manipulation — a human judgment (Figure — Arun); the harness supplies the table and footage and must never imply a floor. **And, as built, which codec is more efficient**: see the amendment below. Cross-codec bitrate comparison requires matched quality, which this suite does not hold. |
| **T-2 loss collapse** | The loss at which control breaks, **per transport**, reported as effective control rate (Hz) as well as delivered %. Settles the §16 ~50% vs ~10% conflict by showing the two transports have opposite failure signatures. | Whether the PRD's original figures came from either configuration measured here. It resolves what the number *is*, not what the historical number *was*. |
| **T-3 jitter tolerance** | Max tolerable jitter as a curve over playout window (5/10/20/40 ms). A curve, not a number. | The operating point. Figure configures the window (§8.2a); the harness delivers the curve. |
| **T-4 concurrency** | The SFU-side session limit on one instance. | **Cell uplink capacity, and therefore per-site capacity.** §8.7a requires the tighter of two limits and this harness measures one. A site number needs real RAN measurement (Numan Suri / Scott Jacka). Load generators must run on separate hosts or `poll_overbudget_pct` rises and client saturation is misattributed to the SFU. |
| **T-5 availability** | Fraction of each fault class survived by resume vs ending the session, and the outage-duration distribution. | Real radio events. Netem fault injection approximates a fade; it is not a fade. ICE failures shorter than the 1 Hz poll are invisible (§2 gap 8). |
| **Q-7 latency definition** | The three-column table — network RTT, one-way, glass-to-glass — measured simultaneously under the same injected delay, **per codec**, with the G2G decomposition. The ratio between columns is the answer. | Which measure the PRD authors intended. It supplies the evidence; Sid/Michael make the call. And `g2g_p50_ms` is capture→app-delivery, not camera→photons — the pixel measurement (§1e) is Tier 2 only. |
| **RAN hypothesis (§5)** | On a lab RAN where `ran_profile` is variable: whether RLC-AM with long discard timers shows higher G2G and no freeze benefit versus RLC-UM under `zero_jitter`. `video_rtx_pct` (§1a) is the discriminator. | Anything, on a network where `ran_profile: unknown`. Until a lab RAN exists this is a recorded field, not an axis, and the report may not compare across profiles without saying so. |

---

## 10. Corrections to the master prompt's citations

Verified against this checkout on 2026-08-24. Everything not listed matched.

| master prompt claim | actual |
|---|---|
| `enable_zero_playout_delay` wired at `subscriber.rs:1678` | **1679** (`args.low_latency` test is at 1678). |
| `PublisherCodec` at `publisher.rs:46–61` | Enum body is **46–52**; the `From<PublisherCodec> for VideoCodec` impl runs 54–64. Both correct in substance. |
| codec fallback at `publisher.rs:1131–1211` | **1210–1224**. And it is **H265→H264 only** — there is no AV1 fallback path in this example. An AV1 publish failure returns the error (`:1219`). The `codec_fallback` INVALID reason therefore fires on negotiated-codec mismatch, not on this code path. |
| `qualityLimitationReason` handling at `publisher.rs:521` | 521 is `shared.codec_implementation` assignment inside `update_publisher_encoder_overlay`. The example **does not read `quality_limitation_reason` at all** — the field exists in `stats.rs:435` but no example consumes it. New code. |
| malformed-AV1 check at `publisher.rs:464` | **462–466** (the `warn!` string is on 464). Confirmed. |
| `DataTrackSubscribeOptions::with_buffer_size` default is 16 | Confirmed, `remote/mod.rs:212`. Also confirmed **frame count, not time** (`:215-221`), and zero is clamped to one (`:227-231`). |
| `--simulcast` at `publisher.rs:1165`, `1939–1947` | 1165 is `compute_simulcast_presets_30fps` call; the arg is at **200**, `dynacast` at **244**/**940**, the preset fn at **1939-1953**. Substance correct. |
| C++ matrix threshold `rtt_p99_ms ≤ 100 / §8.1c` | **A C++ implementation bug, not PRD staleness.** §8.1c is a one-way robot→TC p99, not RTT. Test Matrix v2 already scores it correctly as one-way/advisory, so the C++ `matrix.yaml` diverges from its own governing spec. Corrected in §6 note (d2), which also states the consequence for Phase 2. |
| Playout delay: David's account contradicts the docs | **Not a contradiction.** Both are right at different layers — ms at the API, 10 ms wire granularity. See §4. The floor via hints is `0/10 ms`, not `min=0,max=1`; true 0/0 is `zero_jitter` only. |

---

## 11. Run-record requirements

Conditions and metrics travel together in one record — a metrics blob without its
conditions is unanalyzable.

Every run record carries: `suite`, `cell_id`, `repeat_index`, all axis values as
**requested and actual** (§4), `environment { path, encoder_tier, ran_profile{5 fields},
camera_source, host_id, sfu_version, sdk_git_sha }`, `verbatim_tc_command`,
`warmup_excluded_s: 15`, `playout_units_confirmed`, per-poll snapshots, summarized metrics,
and `verdict ∈ {PASS, FAIL, OBSERVE, INVALID}` with `invalid_reason` when applicable.

Also required, each because an analysis-side derivation depends on it and would be silently
wrong without it:

| field | why it must be in the record |
|---|---|
| `video_poll_hz` | Keyframe service time is quantized to this. A reader must never infer the resolution (§1b). |
| `audio_buffering` | Distinguishes "audio buffer unconfigured" from "audio buffer at zero" under `zero_jitter` (§5 note (d)). |
| `publisher_seq_log` | The `control_delivered_pct` denominator (§1c). Without it the metric silently biases toward passing. |
| `buffering_mode` + subscriber `process_id` | Proves the process-per-mode grouping held; a `zero_jitter` run sharing a process with another mode is not a `zero_jitter` run (§3). |
| `poll_overbudget_multiplier` | Sourced from `matrix.yaml`, not hardcoded (§6 note (f)). Recorded so a threshold change is traceable. |
| `g2g_metadata_coverage_pct` | Distinguishes "G2G genuinely unmeasurable on this path" from "the `subscribe_timing_events` ordering was wrong" (§1e). Both present as `g2g: None`; only one is a valid result. |
| `clock_sync_confidence` + `theta_ms` | Gates whether the OWD and G2G columns may be published at all (§1d). |

`invalid_reason` vocabulary: `codec_fallback`, `cpu_limited_encoder`,
`malformed_av1_bitstream`, `poll_overbudget`, `control_publish_shortfall`,
`clock_unsynchronized`, `silent_audio_source`, `session_lost_mid_run`,
`g2g_metadata_missing`.

**Two of these are not unconditionally run-level, and the vocabulary alone does not say so.**
The list defines which strings are *legal*, not where each may appear:

- `silent_audio_source` — **never** emitted at run level. It appears only in
  `invalid_detail`; run-level `invalid_reasons` stays empty (§1g rule (ii)).
- `clock_unsynchronized` — emitted at run level **only** when the suite's `primary` set
  contains a one-way or G2G metric (§1g rule (i)). Otherwise it appears in `invalid_detail`
  alongside the suppressed columns.

Both remain in the vocabulary because both are legal `invalid_detail` values and detail
strings are validated against this same list. Keeping one vocabulary and scoping its use
here is simpler than maintaining two overlapping lists; the cost is that this note is
load-bearing, so `matrix.yaml` should carry the same scoping as a comment next to the
`invalid_reasons` block rather than restating the list.

≥3 repeats per cell. A breakpoint whose repeats disagree is reported as a range, not a
point.
