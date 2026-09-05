# Experiment plan — pinning resolution under 5G rate control

**Date:** 4 September 2026
**Supersedes:** `NEXT-EXPERIMENT.md` (its code citations were verified and are correct;
this document keeps them and corrects the experiment design around them)
**Follows:** `silent-downscale-report.html`
**Hosts:** Host A (publisher, RTX 5070, NVENC) · Host B (subscriber, i5-14400T, software H.264)

Both hosts work from this file. It is partitioned the same way `RIG-CHANGES.md` is:
`## Host A` is Host A's, `## Host B` is Host B's, `## Both hosts` changes only in a
coordinated commit. Same push protocol — commit, `git pull --ff-only`, push, pass the sha.

---

## 0. Read this first: three things must be settled before any arm runs

None of these need capture time. All three change what the arms mean. **Do not start
arm 0 until all three are closed and their answers are recorded in this file.**

### 0.1 ~~The bitrate ladder was never held constant~~ — ❌ REFUTED, gate is clear

**This section's premise was wrong and it is withdrawn. Nothing here blocks the gate.**

It assumed the A-runs inherited `max_bitrate` from `compute_appropriate_encoding`'s preset
table and derived caps of 0.80 / 1.70 / 5.00 Mbps. They did not. Every A-run passed
`--max-bitrate 10000000` explicitly. From Host A's publisher logs, read by Host B directly
rather than taken on report:

```
a1      Video encoding:  640x360  @ 30 fps, 10000000 bps
a2      Video encoding:  960x540  @ 30 fps, 10000000 bps
a2off   Video encoding:  960x540  @ 30 fps, 10000000 bps
a3      Video encoding: 1920x1080 @ 30 fps, 10000000 bps
a1r1    Video encoding:  640x360  @ 30 fps, 10000000 bps
```

Three consequences, all of which **remove** work rather than adding it:

1. **There was no cap overshoot.** A3 delivered ~9.6 Mbps against a **10 Mbps** cap — 96% of
   the ceiling, under it rather than 1.9× over. The four candidate explanations this section
   offered (RTX/FEC/padding riding on top, the cap unenforced on NVENC, the pacer bursting
   above the encoder target, a short window reading a burst as sustained) explain a
   phenomenon that did not occur.
2. **The ladder did not confound bitrate with resolution.** The requested cap was constant at
   10 Mbps across A1, A2 and A3; resolution was the only deliberate variable. What else
   varied was the *delivered* rate, and that is an outcome of the estimator — the finding
   itself, not a confound.
3. **§7 item 11 is withdrawn.** "The pipe was never the binding constraint" rested on the
   overshoot; there is no overshoot. The capacity argument stands on its original evidence:
   9.6 Mbps delivered into a 10.0 Mbps measured uplink, zero packet loss, and a collapse to
   640×360 regardless.

**This section also contradicted §0.2**, which correctly states the A-runs were direct binary
invocations and not the committed script. §0.2 is the correct one; this section then reasoned
as though the script's preset behaviour applied to runs that never touched the script.

**What survives, and it is still worth doing.** Always pass `--max-bitrate` explicitly — not
because a cap was exceeded, but because exactly one run inherited a preset and it is the one
we lean on hardest:

```
round5a (via run_publisher_test.sh)   1280x720 @ 30 fps, 3000000 bps
```

**Run B — the zero-episode control — is the only run with an inherited cap**, and it landed
at 3.0 Mbps, not the 1.7 Mbps the H720 preset row would suggest. That is direct confirmation
of the off-by-one in §9: `compute_appropriate_encoding` assigns `preset.encoding` and *then*
breaks, so 1280×720 receives H1080's 3.0 Mbps. §9's observation is upgraded from "looks
off-by-one" to measured.

That does not invalidate Run B as a control for *does load induce stalls*. It does mean
anyone comparing Run B's delivered rate against the A-runs' is comparing across a 3 Mbps
versus 10 Mbps cap difference that nobody chose, and that comparison should be avoided or
stated.

### 0.2 `run_publisher_test.sh` never ran at the resolutions in the report

The script hardcodes 1280×720. The report's runs are 640×360, 960×540, 1920×1080. So the
A-runs were **not** produced by the committed script — they were produced by direct binary
invocations with different flags, exactly as `RIG-CHANGES.md` records for the
`--low-latency` subscriber runs.

**Deliverable:** Host A recovers and records the exact argv for A1, A2, A3, A2-off, and
Run B into §5 of this file. Shell history, tmux scrollback, whatever survives. Any run
whose argv cannot be recovered is **not** citable in the next report and must be re-run.

### 0.3 A2-off's identity is contradictory, and it is the worst run in the programme

`§02`/`§06` name it "A2-off". The `§05` caption says "jitter buffer **enabled**". The
chart's embedded data `desc` reads "960×540, jitter buffer **ON**". It holds the record
worst transport sample at 2,366 ms.

This matters directly: disabling the receiver jitter buffer was adopted as an optimization
(8 ms median / 124 ms p95, no measurable cost). If A2-off was jitter-buffer-*on*, that
2.4 s tail is evidence **for** the change. If it was off, it is evidence **against** it.
The document currently supports both readings.

**Deliverable:** Host B reports whether that run's argv carried `--low-latency`. Fix the
label everywhere it appears.

---

## 1. What the code says, verified

Every code claim below was checked against the working tree at `e9f77936`. These are
confirmed, not inferred — the citations in `NEXT-EXPERIMENT.md` were all accurate and are
retained.

### 1.1 The degradation preference is the direct cause, and it is a default

`livekit/src/room/options.rs:184` — `get_default_degradation_preference()`:

```rust
match options.source {
    TrackSource::Camera => DegradationPreference::MaintainFramerate,
    TrackSource::Screenshare => DegradationPreference::MaintainResolution,
    _ => DegradationPreference::Balanced,
}
```

`publisher.rs:1451` publishes with `source: TrackSource::Camera` and never sets
`degradation_preference` — the identifier appears nowhere else in the file. Every run in
the report was therefore configured, **by default rather than by choice**, to hold
framerate and give up resolution.

The report says libwebrtc "sheds resolution rather than compressing harder." More
precisely: we told it to. `TrackPublishOptions.degradation_preference` (options.rs:153) is
already `Option<_>`, is checked first in the function, and `DegradationPreference` is
re-exported at options.rs:21. **No SDK change is needed.**

### 1.2 Simulcast is off — and that is load-bearing evidence

`--simulcast` defaults `false` (`publisher.rs:209`) and no script sets it. There is exactly
one encoding. So the collapse is genuinely libwebrtc's quality scaler downscaling a single
stream — **not** the SFU switching simulcast layers and not dynacast pausing one.

This strengthens the report's mechanism and should be stated in it. It also means arm 2 is
a clean test with no layer-selection confound.

### 1.3 Every field needed is already exposed and unread

`libwebrtc/src/stats.rs`, on outbound-rtp:

| Field | Line | Settles |
|---|---|---|
| `quality_limitation_reason` | 435 | `Bandwidth` vs `Cpu` vs `None` — the whole mechanism claim |
| `quality_limitation_durations` | 436 | how long in each state |
| `quality_limitation_resolution_changes` | 437 | count of downscale events |
| `target_bitrate` | 423 | what the estimator told the encoder to do |
| `frame_width` / `frame_height` | 425–426 | encoder-side resolution, to pair against B's delivered |
| `qp_sum` | 432 | **did it compress harder, or shed pixels?** |
| `frames_encoded` | 430 | denominator for mean QP |
| `frames_per_second` | 427 | what `MaintainFramerate` was protecting |

and on candidate-pair: `available_outgoing_bitrate` (line 576).

`QualityLimitationReason` has `Cpu` and `Bandwidth` as distinct variants (stats.rs:51).

**`qp_sum` is an addition to the original plan and is not optional.** §03 step 4 asserts
the encoder shed resolution *rather than* compressing harder. QP is the direct measurement
of that clause; without it the claim stays inferred. Mean QP per frame is
`qp_sum / frames_encoded` deltas between ticks.

`publisher.rs:467` (`log_publisher_outbound_health`) already has `OutboundRtpStats` in hand
and prints a subset to stdout. None of it reaches `PUBLISHER_CSV_HEADER`
(`publisher.rs:605`), which is 15 timing-only columns. Host B got resolution/bitrate/codec
on 4 Sep in `24d8145b`; Host A did not.

This matters beyond convenience: Host A is NVENC, Host B is software decode. That asymmetry
is exactly the shape that produces a CPU-limited encoder masquerading as a bandwidth story.
"The cause is not bandwidth" currently rests on the absence of packet loss, which is equally
consistent with a CPU limit.

### 1.4 The stats tick is 1 Hz. The event under study takes 0.5–1.0 s.

`publisher.rs:500` and `publisher.rs:526` both use `Duration::from_secs(1)`.

The collapse completes in 0.48–1.05 s. **At 1 Hz you get one sample inside that window,
possibly zero.** `quality_limitation_reason` could plausibly read `None` at t=0 and
`Bandwidth` at t=1 with the entire transition unobserved. Without a faster tick, arm 1
answers nothing and the whole programme stalls on an unreadable result.

### 1.5 Two independent stats loops are racing

`update_publisher_video_stats` (line 497) and the encoder-implementation poller (line 526)
each run their own 1 s timer and each call `track.get_stats()`. Two snapshots, two
timestamps, one CSV row. Merge them before adding columns or the new fields can disagree
with each other within a row.

### 1.6 The startup exclusion is one constant in two files

`run_publisher_test.sh:21` and `run_subscriber_test.sh:21` both set `START_FRAME=60`. The
report's own §03 callout — "the measurement ate its own evidence" — is this line. The
excluded window is now the interval under study.

### 1.7 Delivered resolution is ground truth

`subscriber.rs:1261` sets `s.width`/`s.height` from the decoded frame buffer in the video
sink, not from a stats field. Bitrate and codec come from `inbound-rtp` on the subscriber's
own peer connection, which is the correct PC for those. Nothing in the resolution finding
depends on a stats estimate, and the known sampler-reads-the-wrong-PC trap does not apply.

---

## 2. The arms

Baseline for all arms: **1920×1080, 30 fps, 120 s, 10 Mbps uplink.** That is the arm that
collapsed to 640×360 and the pixel-equivalent of the 1600×1300 production feed
(2.07 vs 2.08 MP).

**`--max-bitrate` is set explicitly and held constant across every arm.** This is the
correction to the previous plan. Pick one value — 5 Mbps, matching what A3 implicitly got —
and pass it on every run so resolution is the only thing that varies.

| Arm | Change | Question it answers |
|---|---|---|
| **0. Replicate** | rerun A3 config, explicit `--max-bitrate` | Does the collapse reproduce? Within-condition variance at the arm that matters. |
| **1. Instrument** | publisher logs new columns at 100 ms | Is the limitation reason `Bandwidth` or `Cpu`? Does QP rise before resolution falls? **Gate — see below.** |
| **2. Maintain resolution** | `degradation_preference = MaintainResolution` | Does resolution hold, and what does it cost — framerate, or blocking within the frame? |
| **3. Bitrate floor** | `--min-bitrate` at 4 Mbps | Does flooring the target stop the downscale on its own? |
| **4. Pin resolution** | `scale_resolution_down_by = 1.0`, single encoding | Fallback if arm 2 fails. Removes the scaler's freedom rather than asking it politely. |
| **5. Both** | arms 2 + 3 | Candidate production config. |

**Arm 1 is a gate, not an arm.** Instrument, run **one** 120 s replicate, read
`quality_limitation_reason`. Do not spend 30 minutes of capture before reading one column.

- Returns `Bandwidth` → mechanism confirmed end to end; arms 2–5 are the fix.
- Returns `Cpu` → the bandwidth story is wrong, the NVENC/software-decode asymmetry is the
  lead, arms 3 and 5 become uninformative, and the next step is encoder-side profiling.
- Returns `None` throughout → the tick is still too coarse, or the limitation is upstream
  of the encoder. Do not proceed to arms until this reads something.

**Arm 3 is predicted to do nothing.** A minimum bitrate floors what the encoder is *told to
target*; libwebrtc generally does not let it override a lower BWE estimate, so it can still
downscale. Run it — the prediction is worth testing — but arm 2 and arm 4 are the real
levers. Arm 4 is new and exists precisely because arm 3 is likely inert.

### Replication and source material

Three runs per arm, not one. The report's §08 records a repeat producing 17 stall episodes
against 11 — a ~55% swing. Resolution collapse is a far larger effect and will likely
survive, but **no number in the next report should be quoted to two significant figures off
n=1.** 15+ runs × 120 s ≈ 30–40 min of capture. This is cheap.

Run arms 0–2 with **both** the noise source and real camera footage. §08 flags synthetic
incompressible noise as a caveat four sections after the production claim it qualifies.
Running both closes it rather than restating it. If camera footage does not collapse, that
is a finding that bounds production impact, not a disappointment.

---

## 3. Host A (publisher) — tasks

### A0. Fix the TLS root before anything else

Per `RIG-CHANGES.md`, Host A's CA bundle lives in a session-temporary directory and is
deleted when that session ends, after which Host A cannot reach the SFU and the error reads
as a network fault. This is the most urgent item on either machine and it will silently
abort a run mid-programme.

Install the root properly (`/usr/local/share/ca-certificates/` + `update-ca-certificates`).
**Confirm the fingerprint against a value published by T-Mobile IT first** — the one on
record was taken from the chain the server itself presented, which proves the bundle matches
what we connected to, not that what we connected to is genuine.

### A1. Recover the argv for every run in the report

See §0.2. Any run whose exact invocation cannot be recovered is not citable and must be
re-run. Record what you find in §5 below.

### A2. Merge the two stats loops and add `--stats-interval-ms`

Merge `update_publisher_video_stats` (line 497) with the encoder poller (line 526) into one
loop, one `get_stats()` call, one snapshot per tick. Then add `--stats-interval-ms`,
**default 100** for the experiment arms.

Do this *before* adding columns — otherwise the new fields can carry two different snapshots
in one row.

### A3. Add outbound stats to the publisher CSV

Extend `PUBLISHER_CSV_HEADER` (line 605) following the pattern Host B used in `24d8145b`
(`InboundQualitySnapshot` → snapshot struct → copied into rows at the stats tick).

```
target_bitrate_mbps, available_outgoing_bitrate_mbps,
quality_limitation_reason, quality_limitation_resolution_changes,
encoded_frame_width, encoded_frame_height, frames_per_second,
qp_sum, frames_encoded, encoder_implementation
```

`available_outgoing_bitrate` comes off the **candidate-pair** stat, not outbound-rtp.

Sanitise `quality_limitation_reason` and `encoder_implementation` through `csv_text()`
(`frame_log.rs:109`) — a comma in a stats string shifts every later column. Note `csv_text`
is currently **dead code in the publisher binary** (compiler warns), because no publisher
column is a string yet. These are the first.

### A4. Add the CLI flags

- `--min-bitrate <bps>` — only `--max-bitrate` exists today (line 206)
- `--degradation-preference <maintain-framerate|maintain-resolution|balanced>`, defaulting
  to today's behaviour so arm 0 is unchanged
- `--scale-resolution-down-by <f64>` for arm 4

`DegradationPreference` is re-exported at options.rs:21; set it on `TrackPublishOptions`
next to `source: TrackSource::Camera` (publisher.rs:1451).

### A5. Set the resolution and bitrate from flags, not from the preset table

`run_publisher_test.sh:73` hardcodes 1280×720 and omits `--max-bitrate`. Make width, height
and max-bitrate script variables, and **always pass `--max-bitrate` explicitly** so no run
silently inherits a preset (§0.1).

### A6. `START_FRAME=0`

`run_publisher_test.sh:21`, or an env knob defaulting to 0. Must match Host B exactly. The
first second is the interval under study now.

### A7. Record provenance into run metadata

Governor state, EPP state, encoder backend, `CUDA_HOME`, and the full argv — so a run
carries its own provenance rather than relying on the operator having read a warning.

Keep the governor guard. It is doing real work: an 85.6× inflation in `capture_to_buffer`
with no symptom other than the number itself is exactly the class of thing that invalidates
a dataset silently.

**Confirm after every build** that the log reports `encoder=NVIDIA H264 Encoder`. A missing
`CUDA_HOME` compiles NVENC out with only a `cargo:warning` and the binary encodes in
software — this has already produced one wrong result. `webrtc-sys/build.rs` reads
`CUDA_HOME` at line 277 but declares `rerun-if-env-changed` only for `LK_DEBUG_WEBRTC` and
`LK_CUSTOM_WEBRTC` (lines 28–29), so cargo replays a cached build-script result and the
variable appears to do nothing. `touch webrtc-sys/build.rs` when changing it.

---

## 4. Host B (subscriber) — tasks

### B1. Settle the A2-off label — ✅ ANSWERED

**A2-off did NOT carry `--low-latency`. The jitter buffer was ENABLED.**

Evidence, in descending order of strength:

1. **The data.** `receive_and_assembly_ms` p95 is **0.27 ms** for A2 and **131.88 ms** for
   A2-off. A jitter buffer holding frames for playout is the only thing in the pipeline that
   produces that gap; with the buffer disabled, assembly is first-to-last packet spread on
   1.8-packet frames and cannot reach 131 ms.
2. **The launcher.** The run log records `pre-flight (a2off, low-latency=off)` and
   `lowlatency=off`, and the helper only appends `--low-latency` when that argument is `on`.
3. **Live argv.** Verified by `ps` before GO was sent: `--low-latency` absent from A2-off,
   present on A1/A2/A3/Run B.

**§0.3 is resolved, but its premise was wrong.** The report's captions — "jitter buffer
enabled", "jitter buffer ON" — are **correct and consistent with each other**. What is
ambiguous is the run *name*: "A2-off" means the `--low-latency` **flag** is off, which means
the **buffer is on**. The reviewer read "A2-off" as "buffer off". Nothing in the report
contradicts itself; the name invites the misreading and should be changed.

**Rename to `A2-buffer-on` everywhere**, in this file, the report and the results directory.

**The finding is unchanged and the conclusion strengthens.** The 2,366 ms worst transport
sample occurred with the buffer **enabled**, so it is evidence **for** disabling it, which is
the direction already reported. The isolation pair stands: on `receive_to_gpu_complete`,
which excludes transport entirely, the buffer cost 8 ms at p50 and 124 ms at p95 while
leaving episode count unchanged at 9 either way.

*Caveat on one piece of evidence I checked and discarded:* the startup line
`Low-latency mode enabled: WebRTC-ForcePlayoutDelay/min_ms:0,max_ms:0/` appears in neither
`a2.log` nor `a2off.log`, so its absence proves nothing here. A2 was run with
`RUST_LOG=local_video=debug`, a wrong target that matched nothing and suppressed all output
including INFO. Do not use that line as evidence for runs before A2-off.

### B2. `START_FRAME=0`

`run_subscriber_test.sh:21`. Must match Host A — the two scripts pair frames by ID, and
mismatched windows are exactly how the "two metrics disagree" confusion in §07 happened.

### B3. Teach `generate_frame_report.py` about resolution

It currently has no reference to frame dimensions; it predates the column that caught this.
Per run it should emit: modal delivered resolution, frame count at modal, time to first
resolution change, and the full sequence of distinct resolutions with frame spans.

**A run whose delivered resolution changes must be flagged in the report header**, not left
for a reader to notice. The failure mode being fixed here is precisely that this was
invisible.

### B4. Pair encoder-side against decoder-side resolution

Once Host A emits `encoded_frame_width/height`, the join on frame ID gives sent-vs-delivered
directly. That separates "the encoder shed it" from "it was lost in transit" without
inference.

### B5. Keep `episodes.py` thresholds fixed

Results stay comparable only while thresholds do not move. If one must change, reprocess
every prior run rather than comparing across the change.

### B6. Persist the governor

Host B has neither persistence nor a run-time guard — the only combination on either machine
that is both silent and unprotected. Install `cpufrequtils` with
`GOVERNOR="performance"`, and port Host A's guard into `run_subscriber_test.sh`. Note
`cpufrequtils` restores the governor but **not** EPP, which is an `intel_pstate` knob outside
its scope; EPP still needs checking after a reboot.

---

## 5. Run provenance

*To be filled in by Host A per §0.2 and Host B per §0.3. A run not recorded here is not
citable in the next report.*

All Host B invocations share this argv, which is `run_subscriber_test.sh`'s own argument
list plus the flags noted — the script was deliberately never modified:

```
target/release/subscriber --url "$LIVEKIT_URL" --room-name round5-mso \
  --identity viewer-1 --participant cam-1 --display-timestamp [--low-latency] \
  --log-csv results-<run>/subscriber.csv --log-start-frame-id 60 --log-end-frame-id 3660
```

| Run | Host A argv | Host B `--low-latency` | Host B recovered? | Source |
|---|---|---|---|---|
| Run B | *Host A to fill* | **yes** | ✅ | live `ps` before GO |
| A1 | *Host A to fill* | **yes** | ✅ | run log `lowlatency=on` |
| A2 | *Host A to fill* | **yes** | ✅ | run log `lowlatency=on` |
| A3 | *Host A to fill* | **yes** | ✅ | run log `lowlatency=on` |
| A2-off → **A2-buffer-on** | *Host A to fill* | **no** | ✅ | run log + live `ps` + assembly p95 |
| A1r1 (repeat) | *Host A to fill* | **yes** | ✅ | run log `lowlatency=on` |

Host B's side of §0.2 is closed: every run's invocation is recovered and every one is
citable. Note `--display-timestamp` was on for all of them, so `SHOW_TIMING` is constant
across the comparison as §6 requires.

**One Host B run is NOT citable and is not listed:** `a3r1`, discarded because Host A's
previous publisher was still running when its subscriber started. Its CSV spans two
publishers and it was moved aside rather than deleted.

---

## 6. Both hosts

### Run manifests — a run must describe itself

Five confusions in this programme came from reconstructing a run's meaning afterwards, from
shell history, scrollback and memory: an argv nobody recorded (§0.2), a bitrate cap nobody
recorded (§0.1 — which was then reasoned about wrongly and blocked the gate), a control that
silently inherited a different cap, a run that overlapped the previous publisher, and a label
("A2-off") carrying meaning it could not hold (§0.3). None of those artifacts were ever meant
to be evidence.

Each host writes a manifest beside its CSV — `publisher.manifest.json`,
`subscriber.manifest.json` — at run start, closed with the outcome at run end. **A run whose
manifest is absent or incomplete is not citable.** That makes §0.2's rule enforceable when
the run is written rather than discoverable months later.

Nesting and shared field names are identical across hosts:

```
role  started_utc
invocation   { argv (array), cwd }
environment  { hostname, kernel, cpu_governor, cpu_epp, git_sha, git_dirty, ... }
media        { ... }
window       { log_start_frame_id, log_end_frame_id }
outcome      { rows_written, first_frame_id, last_frame_id, exit_reason, ended_utc }
```

Three design decisions, each aimed at a failure we actually hit:

- **Written before the first frame**, rewritten at the end. A run killed mid-flight is
  precisely the one whose configuration gets disputed later — `a3r1` is that case. Waiting
  until exit would leave it with no provenance at all.
- **`requested_max_bitrate_bps` is `null` when absent, never `0`.** That distinction is what
  let §0.1 be written against a guessed cap: "no cap passed, so a preset applied" and "a cap
  of zero" are different facts, and a reader cannot separate them if both render as `0`.
- **Implementation fields come from the stats tick, not the CLI flag.** The flag says what
  was asked for; NVENC silently compiling out is exactly where those disagree.

#### The asymmetry is the point, not an omission

Host A carries `media.requested_width/_height/_fps/_codec/_max_bitrate`. Host B **cannot** —
the subscriber requests nothing, it receives whatever arrives. Conversely Host B carries
`outcome.delivered_resolutions` and Host A cannot, because the publisher never sees what was
delivered.

**That pairing is the join: what was asked for against what arrived.** It is the exact
comparison that would have caught the downscale on the day rather than three rounds later. A
missing `requested_width` on the subscriber side is the design, not a gap.

Host B adds `flags.low_latency` — which retires the A2-off naming problem, since the flag
becomes authoritative and the run name decorative — plus `flags.display_timestamp`,
`media.decoder_implementation`, `outcome.delivered_resolutions`,
`outcome.resolution_changed`, and a `sync` block: `ptp_port_state`, `ptp_grandmaster`,
`ptp_rms_ns_start/_end`, `phc2sys_servo_start/_end`, and `method`, which reads `"journal"`
because `pmc` needs root and that path has no TTY for a password.

Host A's emitter is `src/run_manifest.rs`; Host B's is shell in `run_subscriber_test.sh`,
deliberately — it already knows the argv and needs no rebuild to change.

- Note the `START_FRAME` change in run metadata. Runs before and after are **not** directly
  comparable on any startup-window metric.
- Keep PTP running and keep the clock-check gate in `run_report.sh`. The negative-sample and
  floor checks in §06 are what make cross-machine transport defensible; do not drop them for
  a "quick" arm.
- ~~Host A's `ptp4l` is a hand-started foreground process with no systemd unit~~ —
  **resolved.** `ptp4l-A.service` is installed and enabled, so the grandmaster survives a
  terminal close and a reboot. The hazard it named was real and was observed: when that
  service was restarted, Host B promoted itself to grandmaster for 5 seconds and `phc2sys`
  reported `s2` throughout. Only the port-state transition or `grandmasterIdentity` reveals
  it — neither `offsetFromMaster` nor `s2` will. The manifest's `sync` block records both,
  which is why it is there.
- Do not change `SHOW_PREVIEW` / `SHOW_TIMING` between arms. The preview costs 0.24 ms at the
  median and more in the tail; it must be constant across a comparison.
- Full unfiltered suite before every push:
  `CC=clang-21 CXX=clang++-21 cargo test --release -p local_video --features desktop`.
  The two binaries share `frame_log.rs` and `subscriber_timing.rs`.

---

## 7. Corrections owed to `silent-downscale-report.html`

Found by cross-checking the prose against the report's own tables and embedded chart data.
Fix before anything is republished — several are cases where **the report understates its
own finding**.

1. **§04 "quarter-resolution" is wrong, and wrong in our favour.** 640×360 into 1600×1300 is
   230,400 / 2,080,000 = **11%**, about one-ninth. The §02 table already says 12%. The
   headline consequence understates the loss by ~2.3×. Correct it upward.
2. **The dek's "seven eighths … in every loaded run" is false for A1**, which kept 25%. The
   stat tile scopes 1/8 correctly to the highest load point; the dek over-generalises it.
3. **A3's "pixels kept" is 11.1%, printed as 12%** — while A2 and A2-off, the same exact 1/9
   ratio, are printed as 11%. One ratio, two renderings, one table.
4. **Three different values for the same transport statistic.** §06 table, §05 captions, and
   the chart's embedded data disagree (A3 p50: 50.46 / "50 ms" / 52.4). Two computations
   shipped in one document. Pick one and regenerate.
5. **§03 first-drop times do not match the plotted data.** §03 says 0.71 / 0.55 / 1.05 s;
   the chart series shows 0.62 / 0.48 / 0.84 s. The "0.55–1.05 s" stat tile does not hold
   against the report's own chart.
6. **The chart clamps at 20 ms** (`Math.max(20, …)`) while §06's minimum is 18.95 ms, so
   sub-20 ms samples are silently clipped in the plot.
7. **§01 "A1 and A2 were the same experiment" overstates.** Both mode at 320×180, but A1 sat
   there for 95.6% of frames and A2 for 80.3%. A2 held higher resolutions ~4× as often.
8. **"PTP synced, −246 ns" appears once and is never substantiated.** §06 validates the clock
   by a different argument entirely (zero negative samples, 19–22 ms floor). PTP quality is
   the central defensibility claim of the whole rig — it needs its own evidence, with the
   measurement method stated.
9. **State that simulcast was off** (§1.2 above). It rules out layer-switching and dynacast
   as explanations and makes the single-stream scaler claim airtight.
10. **Add the `MaintainFramerate` default** (§1.1). "We told it to shed resolution" is a
    stronger and more actionable finding than "libwebrtc chose to."
11. ~~**Withdraw or qualify "the pipe was never the binding constraint"**~~ — **withdrawn,
    see §0.1.** There was no overshoot: A3's cap was 10 Mbps, not 5, so 9.6 Mbps delivered is
    under the ceiling. The claim stands on its original evidence and needs no correction.

---

## 8. On the AV1 decision

The report concludes the codec plan was aimed at the wrong constraint. That is right about
*resolution* and worth stating plainly. It does not follow that AV1 has no value: a codec
delivering equal perceptual quality at lower bitrate gives the estimator more headroom before
it sheds anything. The honest framing is **resequencing, not cancellation** — fix rate control
first, then re-evaluate AV1 against a pipeline that is no longer collapsing. Smaller claim,
much harder to argue with.

## 9. Open items this plan does not address

- **Sender pacer queue depth is unsampled**, so pacer / radio scheduler / SFU remain
  indistinguishable (report §08). Needs sampling well above even the 100 ms tick.
- **Production is 1600×1300; every arm here is 1920×1080.** Once an arm holds, run the actual
  production geometry. Pixel-count equivalence is a reasonable proxy, not a substitute —
  1600×1300 is 4:3-ish and selects a different preset row than 16:9.
- **The preset selection loop looks off-by-one.** `compute_appropriate_encoding`
  (options.rs:287–294) assigns `encoding = preset.encoding` and *then* breaks, so 1280×720
  receives H1080's 3.0 Mbps rather than H720's 1.7. Upstream behaviour, not ours, but it means
  the requested bitrate is not what you would read off the table by eye. Another reason to
  always pass `--max-bitrate` explicitly.
