# Black-screen metrics — subscriber-side instrumentation spec

Metrics for answering one question: **why did the subscriber's picture go black, and where was it
born?** Scope is `examples/local_video/src/subscriber.rs` (Host B, the render host) plus the
matching statistic at the publisher.

Status: **specification.** Tiers 0 and 1 do not exist. Tiers 2-4 exist in parts, at the wrong
cadence or without differencing. Nothing here is built.

---

## 0. Why the existing log cannot answer it

`SUBSCRIBER_CSV_HEADER` (`subscriber_timing.rs:426`) is keyed on **frames that were drawn**. A row
exists because a frame reached `record_frame_draw_encoded`. During a blackout no frame is drawn, so
the file emits **no rows at all**.

A gap in that file is indistinguishable between: no frame arrived, the frame arrived and was not
painted, the render thread stalled, the logger died, the process exited, the run ended. The
instrument goes silent exactly when the event happens.

> **The blackout must produce rows, not a gap.** Every design rule below follows from this.

### Design rules

| # | Rule | Consequence |
|---|---|---|
| 1 | Fixed-cadence sampler on the render thread | Ticks whether or not a frame exists. Frame-keyed logging stays, as a second stream. |
| 2 | Every metric must move the posterior on ≥1 hypothesis | A number that reads the same under all causes is decoration. Cut it. |
| 3 | Interval deltas, never cumulative | A cumulative counter cannot localise a 650 ms event inside a 900 s cell. The codebase already does this for QP (`subscriber.rs:595-602`) — follow that precedent. |
| 4 | One clock | PTP servo locked + measured offset (§6 of the runbook). Rows carry the same `capture_timestamp_us` lineage as the existing CSV. |

Cadence: **20 Hz** (50 ms). The repaint tick is already 100 ms
(`DIAGNOSTICS_REPAINT_INTERVAL`, `subscriber.rs:1933`) and the stats poll is 1 Hz
(`subscriber.rs:1590`) — both too coarse for a 650 ms stall.

---

## 1. Tier 0 — what was actually on screen

**Does not exist. Highest value tier.** One row per paint attempt.

| Field | Type | Definition | Discriminates |
|---|---|---|---|
| `draw_state` | enum | `DREW` \| `SKIP_NO_VIDEO_SIZE` \| `SKIP_DIMS_ZERO` \| `NO_PAINT` | Names which early return fired: `subscriber.rs:2463` (`video_size` unset) or `subscriber.rs:2892` (`dims == (0,0)`). A blackout becomes a labelled run of rows. |
| `paint_interval_ms` | f64 | Wall gap since previous paint callback | Separates "render thread stalled" from "render thread ran and chose not to draw". Two different bugs, one symptom. |
| `frame_age_at_draw_ms` | f64 | `now` − sink timestamp of the texture content being sampled | Redrawing stale textures **is** a freeze. A freeze is invisible in a frame-keyed log because no new rows appear. |
| `texture_generation` | u64 | Increments on texture recreate (`subscriber.rs:2778`) | A draw at a generation with no upload behind it is the green-frame case (§2). |
| `dims_at_draw` | (u32,u32) | Dimensions in the params uniform at draw | Catches a resolution switch mid-blackout. |

> Why `NO_PAINT` is a distinct state: egui may not call the paint callback at all. "The callback ran
> and returned early" and "the callback never ran" are different failures and must not collapse into
> one absent row.

---

## 2. Tier 1 — pixel content

**Does not exist.** Subsample the CPU-side I420 in the prepare path, before upload — a 32x32 grid is
~1k reads per frame, no GPU readback.

`luma_mean`, `luma_p99`, `chroma_mean_u`, `chroma_mean_v`

| Signature | Verdict |
|---|---|
| Y≈16, U≈V≈128 | Genuinely black **video**. The decoder handed you black. Go upstream. |
| Y=0, U=V=0 | Uninitialised buffer. Renders **green**, not black — see below. |
| Normal Y spread | Pixels are fine. The screen is black for a non-pixel reason: Tier 0 or Tier 2. |

> **Zeroed GPU memory is green, not black.** `yuv_shader.wgsl:37-45` is limited-range BT.601. At
> Y=0,U=0,V=0: `c=-0.063, d=e=-0.5` → r and b clamp to 0, g≈0.53. If you ever see a green flash,
> that is a texture being sampled that was never written — which is a *different* bug from a black
> screen, and the only thing that tells them apart is this tier.

Emit the same statistic at the publisher on capture. One comparison then settles whether the black
was already black at the sensor, with no reference frames and no pixel scoring.

---

## 3. Tier 2 — signal lifecycle

**Partially exists, wrong form.** Room events are `debug!`-logged only (`subscriber.rs:2242`). They
need to be timestamped rows on the shared timeline.

Events: `TrackSubscribed`, `TrackUnsubscribed`, `TrackUnpublished`, `TrackMuted`, `Reconnecting`,
`Reconnected`, `Disconnected`. Fields: `room_event_seq`, `event`, `active_sid`,
`video_size_set` (bool).

Derived: **`recovery_ms`** — unsubscribe → resubscribe → first `draw_state=DREW`.

> This is the tier that catches the most likely mechanism for an *instant* blackout that takes the
> HUD with it: `clear_hud_and_simulcast` (`subscriber.rs:1669-1694`) zeroes the HUD fields and calls
> `video_size.clear()`, reached from `TrackUnsubscribed` and `TrackUnpublished`
> (`subscriber.rs:2262-2284`). The paint callback then returns at `2463` and the central panel —
> `Frame::NONE` (`subscriber.rs:2073`) — shows egui's dark background. No pixel in the video path is
> involved. An SFU-side unsubscribe under load looks exactly like a decoder failure on screen.

---

## 4. Tier 3 — the three yields

**Inputs exist, differencing does not.** This is the spine: chained, the three ratios localise the
break to one link.

```
decode_yield = Δframes_decoded  / Δframes_received     arriving but not decoding
render_yield = Δframes_rendered / Δframes_decoded      decoding but not rendering
draw_yield   = Δdraws (Tier 0)  / Δframes_rendered     rendered by WebRTC, not painted by us
```

`keyframe_wait_ms` — time since `key_frames_decoded` last advanced while `pli_count` climbs. The
classic black-after-loss signature.

Per-interval deltas from `InboundRtpStreamStats` (`libwebrtc/src/stats.rs:347-401`):
`frames_received`, `frames_decoded`, `key_frames_decoded`, `frames_rendered`, `frames_dropped`,
`packets_lost`, `packets_discarded`, `nack_count`, `pli_count`, `fir_count`, `freeze_count`,
`total_freeze_duration`, `pause_count`, `total_pause_duration`, `jitter_buffer_delay`,
`jitter_buffer_emitted_count`, `total_inter_frame_delay`, `total_decode_time`, `qp_sum`,
`bytes_received`, `frame_width`, `frame_height`.

Already present and worth keeping: the degenerate-case warning at `subscriber.rs:1120`
(`frames_received > 0 && frames_decoded == 0`) is `decode_yield = 0` stated as a log line. Promote it
to a metric so it can be counted rather than grepped.

---

## 5. Tier 4 — confounders as columns

`diag_on`, `bitrate_pinned`, `codec`, `cell_label`, plus the PSI `io_full` / `cpu_some` per-interval
deltas from `sysrec.sh` (§3 of the runbook).

> **`diag_on` is not optional.** Per §8, a cell with DIAG running cannot be used to characterise
> render timing — on `cell15m-a` the single 650 ms render stall sat in the one second of 900 where
> the disk blocked hardest, which was the DIAG capture itself. If `diag_on` is not a column you can
> filter on, the render numbers mislead. A `DIAG=0` cell is still the outstanding test.

---

## 6. Decision table

| Signature | Verdict |
|---|---|
| `draw_state=SKIP_NO_VIDEO_SIZE` + `TrackUnsubscribed` within ~100 ms | SFU dropped the subscription. Not a pixel problem. |
| `draw_state=DREW` + `luma_mean≈16`, `chroma≈128` | Source or decoder produced black. Compare publisher `luma_mean`. |
| `draw_state=DREW` + `frame_age_at_draw_ms` climbing | Freeze, not black. Last good frame still on screen. |
| `draw_state=DREW` + `luma_mean=0`, `chroma=0` (green on screen) | Texture sampled before upload. Check `texture_generation` against dims changes. |
| `decode_yield → 0` + `keyframe_wait_ms` climbing + `pli_count` climbing | Keyframe starvation after loss. |
| `render_yield < 1` sustained | Decoded frames never reach the sink. Render path, not network. |
| `draw_yield < 1` sustained | WebRTC rendered, we did not paint. Our GPU path. |
| `paint_interval_ms` spike + `io_full` spike + `diag_on=1` | Host stall, confounded by DIAG. Not a stream fault — re-run with `DIAG=0`. |
| `NO_PAINT` run with everything else healthy | Window/compositor/surface. Outside the video path entirely. |

---

## 7. Cost

~20 Hz x ~40 columns x 900 s ≈ 18k rows, ~2 MB per cell. Against ~9 GB per host per 15-minute cell
at full DIAG mask (§5), free.

---

## 8. Honest gaps

- **Tiers 0 and 1 are not built.** They are the two that settle black vs green vs freeze, and
  neither can be recovered from existing artefacts — the data was never recorded.
- **No blackout has yet been observed with `diag_on=0`.** Every render-timing number to date carries
  the §8 confound.
- **The publisher-side `luma_mean` is half of a two-host comparison.** It is worth nothing until both
  ends emit it in the same run under a locked PTP servo.
- **Cadence is asserted, not measured.** 20 Hz is chosen to resolve a 650 ms stall with ~13 samples;
  it has not been checked against the cost of the sampler itself on the render thread.

---

*Written 2026-09-22 against the code as it stands, read out of the files rather than recalled.*

> **Trap, paid for once.** `subscriber.rs` grew 66 lines mid-session while this spec was being
> written — the checkout at `~/code/rust-sdks` is shared by every Claude session on Host A, and a
> peer's pull moves files under you with a clean `git status`. Every anchor above was re-derived by
> grepping for the code, not by trusting an earlier read. Do the same: **grep the anchor text, never
> trust the line number.**

*Code references are `path:line` at commit `700dd72e` and will drift.*
