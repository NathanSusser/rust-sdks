---
---

<!--
No crates need bumping. `teleop-test-matrix` is `publish = false` and is not a
knope-tracked package (like the `examples/*` members), and this change is confined to
that crate: no other crate's public API, dependencies, or behavior are modified. The
`livekit` defect this works around is documented, not patched.
-->

# Recover glass-to-glass timestamps lost after a full reconnect

A full reconnect re-subscribes the remote video track, and the SDK hands out a brand new
`RemoteVideoTrack` with no packet trailer handler. `subscribe_timing_events()` needs the
track's transceiver to allocate that handler and silently installs nothing when it is not
set yet, returning an ordinary-looking stream either way. Every frame on that subscription
then arrives with no capture timestamp while frame IDs keep flowing, so the run yields no
glass-to-glass latency at all and nothing in the harness could tell.

`video_receive_loop` now verifies the handler actually installed — via
`RtcVideoTrack::packet_trailer_handler()`, which is public even though the `livekit`-level
accessor is not — and retries for one second at 20 ms intervals. Installation is idempotent,
so a late success wires up every subsequent frame. A retry that succeeds after the first
attempt logs at `warn`; exhausting the deadline logs at `error`.

`TrackUnsubscribed` is now handled, and a superseding subscription retires the previous
receive loop. Two loops feeding one `G2gTracker` across a reconnect would have
double-counted every frame.

`RunMetadata` gains two additive fields, `g2g_timing_handler_installed` and
`video_subscription_count`, mirrored in `run_schema.json`. The first makes a lost
subscription self-identifying instead of merely showing low coverage; the second separates
a full restart, which re-subscribes, from a transport resume, which does not — a distinction
the reconnect count alone does not carry.

The underlying SDK defect is written up in `teleop-test-matrix/docs/SDK-FINDINGS.md` with
enough detail to open an upstream issue. No `livekit` code is changed here.
