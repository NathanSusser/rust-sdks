# Video latency & quality metrics — where every number comes from

Written so a **different WebRTC client** can reproduce these measurements and produce
numbers that mean the same thing. It documents the instrumentation, not the results.

Reference implementation: `examples/local_video/src/{publisher,subscriber}.rs`,
`examples/local_video/src/subscriber_timing.rs`, `webrtc-sys/src/packet_trailer.cpp`.

**Scope, against its siblings in this directory.** This document is for an *external*
team reproducing the measurements on a different client: what to instrument and where.
It is deliberately about method, not results, and carries no numbers except as worked
examples of a trap.

- `MEASUREMENT-DESIGN.md` — requirements traceability: which PRD clause each metric
  settles, and the metric→SDK-API mapping. Read that for *why a metric exists*.
- `RUN-DISCIPLINE.md` — the withdrawal ledger and run rules for this rig.
- `PTP-RUNBOOK-HOST-{A,B}.md` — how the clock in §1 is actually set up and verified.
- `SDK-FINDINGS.md` — defects and behaviours found in the SDK itself.

---

## 1. The problem that shapes everything: two clocks

End-to-end latency is `arrival_time − capture_time`, and those two readings are taken
on **different machines**. Any error in clock alignment lands directly in the answer.
Two things follow, and both are load-bearing:

**You need real clock sync, not NTP.** We run PTP (IEEE-1588) over a dedicated
1 GbE cable, publisher as grandmaster, subscriber as slave. Measured on our rig:
`ptp4l` rms **1.4–2.8 µs**, path delay 71–75 µs, continuously SLAVE for days. Against
transport figures in the tens of milliseconds that is four orders down and constrains
nothing. NTP over the same path would be milliseconds — the same order as the thing
being measured, which makes the measurement meaningless.

> **Record the sync state with every run.** Ours is in `clock-sync-hostb.txt`. Note
> that a grandmaster *cannot* verify sync from its own side — it has no master to
> measure against and emits no `rms` lines. Only the slave can report it.

**Never let a bulk transfer share the timing link during a run.** Copying 2.4 GB over
the PTP cable moved our rms from 1.4–4.3 µs to 3.4–10.5 µs. Harmless in a gap,
corrupting inside a cell.

**Any metric measured on one host is immune to all of this.** We therefore report
cross-host and single-host figures as *separate columns that are never summed*
(§4).

---

## 2. How capture time and frame identity cross the wire

WebRTC gives the receiver an RTP timestamp, not the sender's wall clock, and no frame
identity at all. To get per-frame end-to-end latency you must carry your own.

We append a **trailer to the encoded frame payload** (`webrtc-sys/src/packet_trailer.cpp`)
carrying two values:

| field | meaning |
|---|---|
| `user_timestamp` | capture wall-clock time, µs since Unix epoch |
| `frame_id` | monotonic counter incremented once per captured frame |

It is written by an encoded-frame transform on the send side and stripped by the
matching transform on the receive side, so it never reaches the decoder. Bytes are
obfuscated (XOR 0xFF) so a trailer cannot be mistaken for valid bitstream.

**To replicate on another client you need an equivalent.** The options, in order of
preference:

1. **An encoded-frame transform** (`RTCRtpScriptTransform` in browsers,
   `FrameTransformerInterface` in native) — same approach as ours.
2. **An RTP header extension** carrying capture time and a frame counter. Cleaner in
   principle; requires SFU cooperation to forward the extension.
3. **`abs-capture-time`** (RTP header extension, widely supported) gives you capture
   time but **no frame identity**, so you can compute latency but cannot pair frames
   between hosts or align to a reference for quality.

> **Why frame_id matters as much as the timestamp:** it is what lets you say
> *published 4324, arrived 4261* — the difference between "frames are missing" and
> "frames arrived late". Without it you can measure latency but not loss attribution.

**Trap we hit:** frames occasionally reach the CSV with `capture_timestamp_us = 0`
(17 rows in 4587). Every stage derived from that column then differs by the whole Unix
epoch. Because such rows are rare they leave the median untouched and destroy the
mean — one run reported a mean of **6,629,942,825 ms** beside a p50 of 1.1 ms. Drop
rows with a non-positive absolute timestamp at ingestion.

---

## 3. The timestamps, and exactly where each is taken

### Publisher (`*.pub.csv`)

| column | taken at |
|---|---|
| `capture_timestamp_us` | frame leaves the capture source |
| `frame_buffer_timestamp_us` | frame enters the WebRTC video source |
| `encoder_upload_timestamp_us` | frame handed to the encoder |
| `encoder_output_timestamp_us` | encoder returns the encoded frame |
| `webrtc_packetize_timestamp_us` | frame packetised for the network |

Derived: `capture_to_buffer_ms`, `buffer_to_encoder_ms`, **`encode_ms`**,
`encoder_to_packetize_ms`, `capture_to_packetize_ms`.

### Subscriber (`subscriber.csv`)

| column | taken at |
|---|---|
| `capture_timestamp_us` | **recovered from the trailer** — the publisher's clock |
| `webrtc_receive_timestamp_us` | **first packet of the frame arriving at the NIC** |
| `decoder_upload_timestamp_us` | frame handed to the decoder |
| `decoder_output_timestamp_us` | decoder returns raw frame |
| `frame_sink_timestamp_us` | frame delivered to the app's sink |
| `frame_selected_timestamp_us` | renderer picks this frame to draw |
| `frame_prepare_timestamp_us` | texture upload begins |
| `frame_draw_encoded_timestamp_us` | draw commands encoded |
| `frame_gpu_complete_timestamp_us` | **GPU signals the frame is on screen** |

> **`webrtc_receive_timestamp_us` is wire-arrival, not jitter-buffer output.** It comes
> from `FirstPacketReceiveUnixTimeMicros(frame)` — the arrival time of the frame's first
> packet, recovered from packet metadata — even though the transform runs after
> assembly. This matters: it means `exposure_to_receive_ms` is **pure network transit**
> and excludes jitter-buffer holding time. If your client stamps arrival at
> jitter-buffer *output* instead, your transport figure includes buffering and is not
> comparable to ours.

---

## 4. The metrics that matter, and the rule about summing them

**Report these two separately. Never add them into one headline.**

| metric | span | clock |
|---|---|---|
| **`exposure_to_receive_ms`** — *transport* | capture → first packet arrives | **cross-host**, PTP-bound |
| **`receive_to_gpu_complete_ms`** — *local* | arrival → pixels on screen | single-host, PTP-independent |

`e2e_to_gpu_complete_ms` is their sum and exists in the CSV, but a single end-to-end
number attributes the receiving machine's renderer to the network. On our hardware the
renderer alone has cost **1194 ms** on one frame. A codec difference in decode cost
lands in *local*; a network difference lands in *transport*. Collapsing them hides which.

Intermediate stages, all single-host: `receive_and_assembly_ms`, **`decode_ms`**,
`render_ms`, `decode_to_sink_ms`, `sink_to_select_ms`, `select_to_prepare_ms`,
`prepare_to_draw_encoded_ms`, `draw_encoded_to_gpu_complete_ms`.

### The row-emission rule that will bite you

**Our subscriber writes one CSV row per GPU-completed frame.** A frame that arrives and
decodes but is never drawn produces **no row**. Consequences:

- Frame counts must come from **three different sources**, not one:
  - *published* — the `frame_id` span
  - *received* / *decoded* — the SDK's own counters (our `Decode health` log line)
  - *drawn* — CSV row count
- `published → received` is the **network**. `decoded → drawn` is the **renderer**.
  A single "dropped frames" figure spanning both is a lie.
- If the render surface is not composited, you get a **header-only CSV with every
  health counter normal**. See §7.

---

## 5. Delivery counters

| counter | source | notes |
|---|---|---|
| `packets_lost` | WebRTC inbound-RTP stats, cumulative | **trustworthy** |
| `frame_width` / `frame_height` | per delivered frame | catches silent downscale |
| `receive_bitrate_mbps` | WebRTC inbound stats | |
| `receive_qp` | `qp_sum` delta ÷ `frames_decoded` delta | see below |
| `freeze_count`, `total_freeze_duration_ms` | WebRTC stats | **see the warning** |

**`packets_received` is NOT recorded** in our CSV, only `packets_lost`. Any loss
*percentage* therefore has an estimated denominator. Record both if you can. Where loss
is exactly zero this does not matter — zero needs no denominator.

**`receive_qp` is not comparable across codecs.** H.264 indices run 0–51, AV1's run
0–255. An AV1 qindex of 117 is *not* worse than an H.264 QP of 28. Put the scale in the
column header; a footnote does not survive a screenshot. QP is also not comparable
across resolutions — match resolution before comparing.

> **`freeze_count` was withdrawn as a criterion on this rig.** Across 27 cells it
> reported up to 8 freezes while `total_freeze_duration_ms` stayed **0.000** in every
> single one. A freeze of zero duration does not describe anything that happened. It
> had also been observed falling 6→2 while the latency tail worsened. Use
> `frame_id_gap` and `gpu_complete_interval_ms` for delivery smoothness instead.

---

## 6. Picture quality — how to score it without fooling yourself

Delivery metrics do not measure usability. With degradation locked, an encoder cannot
shed resolution or frame rate, so it absorbs the entire shortfall in quantiser: **a
stream can arrive perfectly intact, pass every delivery criterion, and look bad.**

We sample decoded frames to disk as raw I420 (`--sample-frames-dir`, `--sample-every`,
keyed on **frame ID** not arrival count) and compute **luma PSNR** against the source.

### Align by content, never by arithmetic

`source_index = frame_id mod clip_length` is the obvious mapping and it is **unsafe**.
The publisher counts *captures*, not clip positions; a capture loop can skip; ffmpeg's
`-r` can duplicate or drop; a loop seam is an unverified re-open. Any of these inserts
an offset, and every PSNR is then computed against the wrong picture **with no visible
symptom**.

Our aligner (`examples/local_video/scripts/tools/frame_align.py`):

1. cheap strided signature shortlists candidate source frames
2. full luma PSNR scores the shortlist
3. a **robust stride** is fitted over *every* best match (median slope + intercept)
4. the stride places all frames, overruling individual matches
5. a placement is accepted only if it **beats both neighbours**
6. a cell whose placements do not fit a stride is reported **UNALIGNED**, not scored

Steps 3–4 exist because of a measured failure: an earlier version fitted the stride only
over high-confidence matches, and under heavy degradation *nothing* clears a confidence
margin — so placement fell back to per-frame argmax and scored **6/8 while reporting
success**. The neighbour test alone only proves a local maximum, and a wrong-by-one
placement usually is one. Known-answer tested at 8/8 across light, heavy and brutal
degradation.

**Precondition to verify on your content:** all source frames must be distinct. Ours are
(808 unique luma planes out of 808). Adjacent frames sit 31–40 dB apart, so at a 42 dB
encode the correct frame beats its neighbour by only ~2.7 dB — the margin is real but
thin, which is why the global stride matters.

### Sampling rate determines what you can conclude

At 1600×1300 a raw I420 frame is 3.12 MB — **94 MB/s at 30 fps, ~14 GB per 150 s cell**.
So:

- **Sparse sampling** (we used every 90th frame ≈ one per 3.2 s) is fine for PSNR and
  **cannot show motion artefacts at all** — stutter, motion smearing, blocking that only
  appears on movement. Any "looks fine" verdict from sparse stills is about *static
  detail only*, and must be labelled as such.
- **Full-rate capture** (`--sample-every 1`) gives real playback but costs the 14 GB.
- **A bitstream recorder** — dumping received *encoded* frames — is the right answer:
  ~19 MB per cell instead of 14 GB, roughly 700× smaller, and it is exactly the bytes
  that arrived. We recommend building this rather than repeating full-rate raw capture.

---

## 7. Traps that produce confidently wrong numbers

Each of these cost us real time. They are ordered by how silently they fail.

**An uncomposited render surface voids the run.** A native Wayland surface that is not
visible receives no frame callbacks, so the render loop never runs: the client decodes
**perfectly** — 1684 received, 1684 decoded, 0 dropped — and writes a header-only CSV
with every health counter normal. Force Xwayland (`unset WAYLAND_DISPLAY`) for
unattended runs, and carry `DISPLAY`, `XDG_RUNTIME_DIR` and `XAUTHORITY` into any
detached process. *(Three hypotheses were offered for this symptom before the right one;
what settled it was a cell that carried the proposed fix and failed anyway.)*

**An unset environment variable produces a capability that is silently absent.** Three
instances on this project: `CUDA_HOME` unset compiled NVENC out (runs used the software
encoder and looked healthy); `SSL_CERT_FILE` unset killed the SFU connection in one
second with no log; `LIVEKIT_API_KEY` unset killed a publisher 4 ms after its epoch.
**Read the capability from the run's own stats, not from the build.** We now assert
`encoder_implementation` reads `NVIDIA ...` on the first cell of every campaign — a
server capability difference silently downgraded us to OpenH264 once, and nothing in the
data revealed it.

**A statistic computed over the wrong population.** Five instances, three of which would
have been reported as findings about the network or the codec:

| quoted | wrong population | correct |
|---|---|---|
| PSNR 42.44 dB | sampled frames | 43.70 full-clip |
| 70% delivered | windowed p50 | 94% full-run mean |
| QP +3.4 | per-frame | +1.6 per-interval |
| QP +3.5 | pooled | +0.2 resolution-matched |
| 29 fps | instantaneous sample | 29.04 windowed |

**Rule: name the population before quoting the statistic, and name it in the table.**

**A criterion specified against an assumed value.** We registered "≥29 fps" assuming a
30 fps source; the publisher's capture loop actually delivers **29.04** (relative pacing
accumulating ~1.1 ms of sleep overshoot per frame). The bar sat 0.04 fps above what
could be produced, so cells passed or failed on measurement slop. Judge frame rate as
**delivered ÷ published ≥ 0.98** — it measures the path rather than the publisher.

> **If you change a criterion after seeing data, prove it flips no verdict you already
> hold.** We adopted the ratio only after checking it against four cells (all 0.999,
> no change). That check is what separates a specification fix from a moved goalpost —
> and note it is *unavailable* for a criterion you invent late, which is why we
> deliberately never converted PSNR into a pass threshold.

**A ladder whose bottom rung passes.** Indistinguishable in the data from a ladder that
found its floor — every counter is healthy either way. When a range arrives inside the
question rather than from a measurement, **look at the bottom rung's output before
trusting the range**, and extend downward until something fails.

**A "most interesting segment" heuristic finds your fixture's seams.** Selecting a clip
by maximum inter-frame motion selected the loop seam of the test content — by
construction the largest inter-frame delta in the capture (11.2× the median against a
next-largest of 3.6×). Exclude seams explicitly when the source loops.

**`pkill -f <pattern>` kills the shell that ran it** when the pattern appears in its own
command line. The bracket trick (`grep '[p]attern'`) does **not** save you — it stops
grep matching its own process, not the parent shell. Record the PID at launch and kill
that.

---

## 8. Minimum viable replication

To produce comparable numbers on another WebRTC client:

1. **Clock**: PTP between hosts, µs-level, recorded per run. Report the slave's rms.
2. **Carry capture time + frame id per frame** — encoded-frame transform preferred.
3. **Stamp arrival at first-packet wire time**, not jitter-buffer output.
4. **Emit transport and local latency as separate columns.** Never sum them.
5. **Count frames from three sources** (published / received+decoded / drawn) and
   attribute network loss and render skip separately.
6. **Record `packets_lost` and `packets_received`**, resolution per frame, and QP with
   its codec scale in the column name.
7. **Sample decoded frames** keyed on frame ID; align to the source **by content**;
   report an unalignable cell as unaligned rather than scoring it.
8. **State the population of every statistic** in the table it appears in.

A run that reports latency without saying which clock, which span, and over what window
is not comparable to anything — including itself on another day.
