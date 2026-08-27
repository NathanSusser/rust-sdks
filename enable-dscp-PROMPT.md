# MASTER PROMPT — DSCP Marking for LiveKit Rust

Set up the worktree first, per `teleop-BRANCHING.md`:

```bash
git worktree add ../rust-sdks-dscp -b enable-dscp teleop-test-matrix
cd ../rust-sdks-dscp && git submodule update --init --recursive
```

Open Claude Code at `../rust-sdks-dscp` and paste everything below the line.

---

Wire per-track DSCP marking through the LiveKit Rust SDK, so that audio, video, and
data published on a single bundled PeerConnection leave the host with three distinct
DSCP code points. Then prove it on the wire.

Read this whole document, then work. Do not ask me to confirm the plan before
starting. Ask only if two readings would lead to materially different work.

---

## 1. Why this is small

libwebrtc already implements RFC 8837, *DSCP Packet Markings for WebRTC QoS*. The
whole chain exists in this tree and runs end to end, then gets dropped at the last
step:

1. `RtpEncodingParameters.priority` — `libwebrtc/src/rtp_parameters.rs:136`
2. maps to `network_priority` — `libwebrtc/src/native/rtp_parameters.rs:97` and `:246`,
   already round-tripping both directions through `set_parameters`/`get_parameters`
3. libwebrtc maps **(media type, priority) → DSCP**
4. `MediaChannel` writes it into `rtc::PacketOptions::dscp` per packet
5. `AsyncUDPSocket::SendTo` calls `setsockopt` before each `sendto`

Step 3 is why this solves the problem: the mapping is media-type dependent, so audio,
video, and data at the *same* priority get *different* code points. One
PeerConnection, one 5-tuple, three marks.

It is gated on `RTCConfiguration.media_config.enable_dscp`, which defaults to false
and is never set. `grep -ri dscp` across the workspace returns nothing but a
reference document.

## 2. The patch sites

Four, verified as of 2026-08-27. All four are the exact footprint of the existing
`enable_sctp_snap` field.

| File | What |
|---|---|
| `webrtc-sys/src/peer_connection.rs:91` | cxx bridge shared struct `RtcConfiguration` |
| `webrtc-sys/src/peer_connection.cpp:35` | `to_native_rtc_configuration()` — sets four fields on a default-constructed `RTCConfiguration` and returns |
| `libwebrtc/src/peer_connection_factory.rs:54` | public `RtcConfiguration`, with `Default` at `:64` |
| `libwebrtc/src/native/peer_connection.rs:163` | `impl From<RtcConfiguration> for sys_pc::ffi::RtcConfiguration` |

**Follow the precedent rather than inventing one.** The identical change has already
been made in this repo once:

```bash
git log -S "enable_sctp_snap" -p
```

That commit touches exactly these four files. Mirror it.

The C++ line is one:

```cpp
rtc_config.media_config.enable_dscp = config.enable_dscp;
```

Everything downstream already threads `rtc_config` from `RoomOptions`:
`livekit/src/room/mod.rs:446` → `livekit/src/rtc_engine/mod.rs:105` →
`livekit/src/rtc_engine/peer_transport.rs:589`. No changes needed there.

## 3. The gotcha to check first

Read the comment at `webrtc-sys/src/peer_connection.rs:94` and the one at
`webrtc-sys/src/peer_connection_factory.cpp:70`. This codebase has already been
bitten by immutable `RTCConfiguration` fields: `enable_sctp_snap` had to be carried
on the configuration rather than set via a field trial, because setting it
inconsistently made `set_configuration` fail with *"Modifying the configuration in an
unsupported way."*

`media_config` is consumed at channel creation. **Determine before you build whether
it can change via `SetConfiguration` at all.** If it cannot, `enable_dscp` must be
fixed at PeerConnection creation and carried identically on every subsequent
`set_configuration` call — the same treatment `enable_sctp_snap` gets. Note that
`rtc_session.rs:2553` rebuilds an `RtcConfiguration` from proto on server-driven
reconfiguration; if that path drops the flag, DSCP will silently stop after the first
reconfigure.

Finding this before writing the patch is worth more than the patch.

## 4. Expected mapping

RFC 8837 Table 1. Treat every value as `[VERIFY]` — confirm on the wire in §6 before
anyone builds routing rules on them.

| Flow | very-low | low | medium | high |
|---|---|---|---|---|
| Audio | CS1 (8) | DF (0) | EF (46) | EF (46) |
| Interactive video | CS1 (8) | DF (0) | AF42 (36) | AF41 (34) |
| Data | CS1 (8) | DF (0) | AF11 (10) | AF21 (18) |

`Priority::Low` is the current default — `libwebrtc/src/rtp_parameters.rs:173` — which
maps to DF. So today every track is marked identically as best-effort, and nothing in
`livekit/src` sets priority at all. Confirm that: `grep -rn "priority" livekit/src`
returns nothing.

## 5. The knob

Two things need exposing, and they are separate decisions.

**Enabling DSCP at all** belongs on `RtcConfiguration`, reachable from
`RoomOptions.rtc_config`. Default it **false** — this changes packets on the wire and
must be opt-in.

**Per-track priority** already works through the existing API:

```rust
let mut params = sender.parameters();
params.encodings[0].priority = Priority::High;
sender.set_parameters(params)?;
```

Verify that round-trip actually survives — `get_parameters` then `set_parameters`
then `get_parameters` should be stable. `rtp_parameters.rs:61` carries a comment
about preserving fields across round-trips, which suggests this has been fragile.

Add a way to exercise both from `teleop-harness`, since that is the only client in
this tree. **Keep that change minimal and confined** — see §7. A CLI flag and a call
site is enough; do not build a configuration system for it.

## 6. Verification

The patch is not done until packets on the wire carry three different marks.

```bash
# EF (46) — expect audio.  0xB8 = 46 << 2
tcpdump -i any -n -c 50 'ip[1] & 0xfc == 0xb8'

# AF41 (34) — expect video. 0x88 = 34 << 2
tcpdump -i any -n -c 50 'ip[1] & 0xfc == 0x88'

# CS1 (8) — expect data/telemetry. 0x20 = 8 << 2
tcpdump -i any -n -c 50 'ip[1] & 0xfc == 0x20'
```

The `0xfc` mask ignores the low two ECN bits, which vary with congestion. Matching
without it produces filters that work in the lab and fail under load.

**Check IPv6 separately.** This is the most common way DSCP work fails and it fails
silently — an IPv6 flow going out unmarked looks identical to a working IPv4 capture
if you only ever filter on `ip[1]`:

```bash
tcpdump -i any -n -c 50 'ip6[0:2] & 0x0fc0 == 0x0b80'   # EF over IPv6
```

Confirm libwebrtc's socket layer sets `IPV6_TCLASS` and not only `IP_TOS`. If it does
not, say so plainly — that is a finding, not a failure.

Then confirm the three marks appear **simultaneously on one 5-tuple**. That is the
whole point: same source address, same source port, three code points. A capture
showing three marks across three connections proves nothing.

## 7. Keeping this upstreamable

This is a legitimate contribution to `livekit/rust-sdks` — the RFC 8837 plumbing
underneath already works and only the configuration bridge is missing. Protect that.

- **Touch only `webrtc-sys/` and `libwebrtc/`.** Those are upstream files. The
  `teleop-slice-clients` branch owns everything else, and file-disjointness is what
  keeps the two branches mergeable without conflict.
- The one exception is the §5 harness knob. Put it in its **own commit**, last, so it
  can be dropped when extracting the PR.
- Single-purpose commits. No "and also fixed a typo." Extraction should be
  `git cherry-pick <sha>..<sha>` onto `main`.
- No T-Mobile-specific naming, comments, or defaults anywhere in the first commits.

## 8. What this is for

Context, not scope. T-Mobile is provisioning 5G QoS rules that map DSCP to 5QI within
a single network slice, so a teleoperation client can put audio, video, and telemetry
on three QoS flows over one PDU session — one IP address, one ICE candidate pair, one
PeerConnection. That is the 3GPP-native design: slices carry tenancy and SLA
isolation, QoS flows carry media classes.

A parallel branch, `teleop-slice-clients`, is building a three-process workaround
using VLANs and network namespaces because the QoS provisioning is not in place yet.
This patch is what retires it.

You do not need any of that to do this work. Do not build toward it.

## 9. Working agreement

- Report the §3 finding before writing the patch.
- Small commits, each leaving the tree buildable.
- If the mapping in §4 turns out wrong on the wire, the capture wins. Correct the
  table and say what you observed.
- Report what you could not verify.
