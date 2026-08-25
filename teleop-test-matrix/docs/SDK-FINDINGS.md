# SDK findings

Defects found in the `livekit` Rust SDK while building the teleoperation harness, written
up in enough detail to open an upstream issue directly. These are *not* harness bugs; the
harness works around them and the workaround is named in each entry.

Checkout the findings were made against: `db3469be` (`webrtc-b9233c3-2-4-gdb3469be`).

---

## SDK-1 — `subscribe_timing_events()` silently installs nothing when the transceiver is unset

**Severity:** data-loss. Silently produces frames with no capture timestamp while the video
itself is healthy, so a consumer measuring end-to-end latency records nothing and has no
way to know why.

**Status:** open upstream, worked around in this harness.

### What happens

[`RemoteVideoTrack::subscribe_timing_events()`][sub] is the documented way to allocate the
receive-side packet trailer handler; its own doc comment instructs callers to call it before
constructing a `NativeVideoStream` "so decoder-output timing can be wired into the stream
automatically." It is also the only way, from outside the `livekit` crate, to get that
handler allocated — `packet_trailer_handler()` and `transceiver()` are both `pub(crate)`.

Internally it does three things (`livekit/src/room/track/remote_video_track.rs:152-170`):

```rust
let handler = self.ensure_subscribe_timing_handler();
if let Some(handler) = handler {
    self.apply_subscribe_timing_observer(&handler);
}

SubscribeTimingEventStream { inner: BroadcastStream::new(tx.subscribe()) }
```

And `ensure_subscribe_timing_handler` (`:182-194`) bails out early when the track has no
transceiver:

```rust
let transceiver = self.transceiver()?;   // <-- returns None, silently
```

When that `?` short-circuits, the `if let Some(handler)` is simply skipped and the function
**still returns a normal-looking `SubscribeTimingEventStream`**. There is no `Result`, no
`log::warn!`, no observable difference from the success path. Every frame subsequently
decoded on that track carries `frame_metadata.user_timestamp == None`.

### Why the transceiver can be unset at that moment

On the normal first subscribe it is set: `remote_participant.rs:164` calls
`track.set_transceiver(Some(transceiver))` before `:179` fires `TrackSubscribed` via
`remote_publication.set_track(Some(track))`.

The gap opens on a **full reconnect**. `Room::handle_restarting`
(`livekit/src/room/mod.rs:1682`) tears down every remote participant, then
`handle_restarted` (`:1705-1756`) unpublishes and republishes every local track. The
subscriber then runs `add_subscribed_media_track` again — and that constructs a **brand new
`RemoteVideoTrack`** (`remote_participant.rs:150-161`), with a fresh
`subscribe_timing_tx: Arc::new(Mutex::new(None))` and no packet trailer handler. It is
dispatched from a detached task (`room/mod.rs:1382-1384`) and opens with a
`timeout(ADD_TRACK_TIMEOUT, wait_publication)` polling loop (`:121-136`). Under the
republish storm that follows a restart, a consumer handling `TrackSubscribed` can observe
the new track before its transceiver lands.

Note that a *resume* (`handle_resuming`, `:1645`) does not do this — tracks are untouched
and no new subscription occurs. Both paths increment the same user-visible reconnect
signal, which makes this failure look intermittent and uncorrelated when it is neither.

### Observable symptom, and why it misleads

Frame IDs keep arriving while capture timestamps vanish. That looks like the two fields
travel by different mechanisms, and they do not: `libwebrtc/src/native/video_stream.rs:113-124`
resolves both from a single `lookup_frame_metadata` call on the same handler:

```rust
handler
    .and_then(|handler| handler.lookup_frame_metadata(rtp_timestamp).map(...))
    .map(|(ts, fid, user_data)| FrameMetadata {
        user_timestamp: if ts != 0 { Some(ts) } else { None },
        frame_id:       if fid != 0 { Some(fid) } else { None },
        ...
    })
```

So surviving frame IDs actually *prove* a handler is present — just not the one the caller
thought it installed. The receiver-side handler the SDK wires up on its own serves frame
IDs; the caller's `subscribe_timing_events()` call is what would have wired the observer
for timing. A consumer debugging this will reasonably but wrongly conclude the two features
are negotiated separately (they are separately negotiable at the protocol level —
`PTF_USER_TIMESTAMP` vs `PTF_FRAME_ID` — which reinforces the wrong conclusion).

### Evidence

From a 24-run Tier 0 sweep against LiveKit Cloud, 2026-08-25. Poll index of the publisher's
`frames_encoded` counter resetting (i.e. the republish) versus the first poll carrying G2G
data, and the count of frames arriving with no timestamp:

```
BAD  h264 r1   reset@22  first_g2g@22  no_timestamp=23
BAD  h264 r2   reset@24  first_g2g@49  no_timestamp=22
BAD  vp9  r0   reset@21  first_g2g@22  no_timestamp=14
BAD  vp9  r2   reset@22  first_g2g@29  no_timestamp=16
OK   h264 r0   reset@19  first_g2g@43  no_timestamp=0
OK   vp8  r1   no reset  first_g2g@21  no_timestamp=0
```

Two readings matter. In every failing run the timestamp count is zero **from the first G2G
poll onward** — there is no healthy prefix that later degrades, which rules out a mid-run
race between two concurrent streams and points squarely at subscription-time wiring. And
`h264 r0` restarted too yet stayed clean, because its restart completed well before video
flowed, leaving the transceiver in place by the time the surviving subscription was wired.

### Suggested upstream fix

Any of these would close it; the first is cheapest and non-breaking:

1. Log at `warn` when `ensure_subscribe_timing_handler` returns `None`, naming the track
   sid and the missing transceiver. Non-breaking, and turns a silent data-loss bug into a
   greppable line.
2. Return `Result<SubscribeTimingEventStream, _>` (or make the stream carry an
   `installed: bool`) so the caller can retry. Breaking, but honest.
3. Have the SDK install the handler itself when the transceiver is set — i.e. move the
   `ensure_subscribe_timing_handler` call to `set_transceiver`, so it cannot be missed by
   timing. This removes the ordering hazard entirely rather than reporting it, and would
   also let the "call this before `NativeVideoStream::new`" instruction be dropped.

A companion documentation fix is worthwhile regardless: the current doc comment describes
the ordering requirement against `NativeVideoStream` but says nothing about the transceiver
precondition, so a caller following it exactly can still get nothing.

### Harness workaround

`teleop-test-matrix/src/run.rs`, `install_timing_handler`. Because
`RtcVideoTrack::packet_trailer_handler()` *is* public in `libwebrtc`
(`libwebrtc/src/video_track.rs:45`) even though the `livekit`-level accessor is not, the
harness can check directly whether installation took, and retry until it does:

```rust
drop(track.subscribe_timing_events());
if track.rtc_track().packet_trailer_handler().is_some() { /* installed */ }
```

Retrying is safe because `ensure_subscribe_timing_handler` is idempotent — it early-returns
any existing handler — and a late success fixes every subsequent frame. The harness retries
for one second at 20 ms intervals, logs at `warn` when an attempt after the first succeeds,
logs at `error` if the deadline passes, and records the outcome in the run metadata as
`g2g_timing_handler_installed` so a run that lost the race is self-identifying rather than
merely showing low coverage.

### Blast radius — the reconnect storm damages four independent measurements, not one

Added 2026-08-25. The silent timestamp loss above is the *most dangerous* symptom of the
full-reconnect path, because it passes every validity gate. It is not the only one. A
single Tier 0 run,
`T1_video_floor__video_profile=minimum,video_codec=av1,uplink_mbps=10__r2__1787663508`,
took **5 reconnects** and produced four apparently unrelated defects that all trace to this
one SDK behavior:

| symptom | what it looked like in isolation | what it actually was |
|---|---|---|
| `g2g: None` with healthy video | the `subscribe_timing_events` ordering fault | this finding, on the re-subscribed track |
| `audio_level == 0` | a broken synthetic tone generator | a re-subscribed audio track reads zero until samples flow; the source peaked at **0.5026** |
| `control_publish_shortfall_pct = 64.7` | publisher failing to hit 200 Hz | control delivered **zero** samples for most polls across the re-subscription |
| 2 stats-RPC failures | a flaky stats channel | `get_stats` against a peer connection being torn down and rebuilt |

**Process starvation is ruled out**, which is the obvious wrong explanation for three of
these at once. The sampler never went overbudget in that run — not one poll — and poll
durations stayed in the low single-digit milliseconds throughout. The client had ample CPU;
what it did not have was a stable subscription.

Two consequences worth carrying upstream. First, this is a stronger case for fix (3) in the
list above — installing the handler in `set_transceiver` — because the ordering hazard is
only one facet of a republish storm that disturbs every receive-side measurement at once.
Second, a consumer diagnosing any single symptom here will reasonably conclude they have
four separate bugs, and will fix the wrong things: the audio symptom in particular looks
exactly like a broken source, and was initially filed as one.

**Harness handling.** None of these is worked around, because none should be — they are
real conditions the run record must show. `reconnect_count` is recorded per run, and
`audio_level` is now the **maximum** across scored polls rather than the median, so an
intermittently-audible source is no longer misreported as a silent one. See
MEASUREMENT-DESIGN §1g rule (ii).

[sub]: ../../livekit/src/room/track/remote_video_track.rs

---

## SDK-2 — no encoder quality target is exposed, so bitrate-at-quality cannot be measured

**Severity:** capability gap. Blocks a codec-efficiency comparison entirely; there is no
workaround at the SDK boundary.

**Status:** open upstream, **not** worked around — the affected test suite is documented as
needing redesign instead.

### What is missing

Publishing exposes a **bitrate target** and nothing else. To compare codec efficiency
honestly you must hold quality fixed and measure the resulting bitrate; every available
knob does the opposite, holding bitrate and letting quality float:

| type | file | quality-relevant fields |
|---|---|---|
| `VideoEncoding` | `livekit/src/room/options.rs:56-59` | `max_bitrate`, `max_framerate` — no quality target |
| `RtpEncodingParameters` | `libwebrtc/src/rtp_parameters.rs:132-145` | adds `scale_resolution_down_by`, `scalability_mode` — resolution and SVC, not quality |
| `TrackPublishOptions` | `livekit/src/room/options.rs:153` | `degradation_preference` — chooses *what to sacrifice* under pressure, sets no floor |

`degradation_preference` is the closest thing and is not close: `MaintainFramerate` and
`MaintainResolution` select which axis absorbs a shortfall, but neither pins a quantizer,
so quality still floats to whatever the bitrate target implies.

### Why it matters

Quality is observable but not controllable. `qp_sum` comes back on `OutboundRtp`, so the
harness can *see* the quantizer each codec chose — and it diverges sharply. Measured on
LiveKit Cloud, 2026-08-25, at comparable target bitrates:

```
av1   qp_avg 40.0   0.31 Mbps actual   3.07 Mbps target   libaom (software)
h264  qp_avg 27.4   1.91 Mbps actual   2.61 Mbps target   hardware
```

A consumer comparing only the bitrate column concludes AV1 is ~6x more efficient. The
quantizer column shows the two encoders produced materially different pictures, so the
bitrates are not comparable at all. **Without a quality target there is no configuration in
which they would be**, which is what makes this a capability gap rather than a
configuration mistake.

### Suggested upstream fix

1. Add an optional quality target to `VideoEncoding` — a QP ceiling, or a CQ level, mapped
   per codec (AV1/VP9 `cq-level`, H264 QP). Optional and defaulting to today's behavior, so
   it is non-breaking.
2. Failing that, expose the underlying encoder configuration so a caller can set it
   out of band, and document the per-codec mapping — quality scales are not comparable
   across codecs and a naive shared integer would silently mean different things.

Either would also let libwebrtc's own quality-vs-bitrate tradeoff be exercised deliberately
rather than inferred after the fact.

### Harness handling

**Not worked around; scope reduced instead.** T-1's stated question was amended from "which
profile × codec × encoder combinations fit under the 5 Mbps ceiling" to "what bitrate each
emitted, at whatever quality its rate control chose", and cross-codec bitrate comparison is
explicitly ruled out in the T-1 report caveat. `qp_avg` and
`quality_limitation_bandwidth_poll_pct` are recorded and `qp_avg` renders immediately beside
the bitrate it qualifies, so the divergence is visible rather than inferred. The
fixed-quality redesign is written up as a recommendation in MEASUREMENT-DESIGN and is
blocked on this finding.
