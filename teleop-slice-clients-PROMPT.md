# MASTER PROMPT — Per-Slice LiveKit Clients

Set up the worktree first, per `teleop-BRANCHING.md`:

```bash
git worktree add ../rust-sdks-slice -b teleop-slice-clients teleop-test-matrix
cd ../rust-sdks-slice && git submodule update --init --recursive
```

Open Claude Code at `../rust-sdks-slice` and paste everything below the line.

---

You are the engineering lead for a small, senior team. Add a **per-slice client** to
the existing teleoperation harness: video, audio, and telemetry published into a
single LiveKit room from three separately-addressed processes, so a Cradlepoint R980
can steer each class onto a different 5G slice by source subnet.

Read this whole document and the modules named in §3, then work. Do not ask me to
confirm the plan before starting. Ask only if two readings of a requirement would
lead to materially different work.

---

## 1. What this is, and what it is not

This is a **routing testbed built on a harness that already exists**. Its job is to
prove three traffic classes leave the host on three source subnets and land on three
PDNs, verifiably, with numbers.

It is **not** the long-term architecture. That is one client, one PeerConnection,
three DSCP marks, three QoS flows inside one slice — being built in parallel on the
`enable-dscp` branch, see §9. Build so collapsing to it is a deletion, not a rewrite.

The single largest way to get this wrong is to write new code that already exists.
§3 is the most important section here.

## 2. The constraint that produced this design

Do not re-derive this. Verified against this tree as of 2026-08-27.

**One LiveKit room connection gives you one publisher PeerConnection.**
`livekit/src/rtc_engine/rtc_session.rs:359` — `publisher_pc: PeerTransport`, with
`subscriber_pc: Option<PeerTransport>` at `:361`, and single-PC mode collapses even
that. Every published track shares it.

**It is bundled.** BUNDLE plus rtcp-mux means one ICE transport, one DTLS
association, one 5-tuple, one source address, for all tracks. The SFU requires
BUNDLE; you cannot negotiate it away.

**So one room connection occupies exactly one source subnet and one slice.** Three
classes on three slices needs three connections. Each PDU session carries its own IP
address, so splitting one 5-tuple across several would break ICE candidate
validation at the SFU. This is IP, not a limitation you can code around.

**The SDK cannot bind a PeerConnection to a chosen interface.**
`libwebrtc/src/peer_connection_factory.rs:54` exposes four fields — `ice_servers`,
`continual_gathering_policy`, `ice_transport_type`, `enable_sctp_snap`. The C++ side
confirms it: `to_native_rtc_configuration()` at
`webrtc-sys/src/peer_connection.cpp:35` sets those four on a default-constructed
`RTCConfiguration` and returns. `grep -ri network_ignore_mask` returns nothing.

libwebrtc's `BasicNetworkManager` enumerates **every** visible interface and gathers
candidates on all of them. Binding a socket does not help — ICE picks the pair.

**Isolation therefore happens below the SDK,** via Linux network namespaces: one
namespace per class containing exactly one VLAN interface, so libwebrtc can only
gather one candidate. No SDK patch required.

## 3. What already exists — reuse it

`teleop-test-matrix` is a workspace member with two bin targets and a full `lib.rs`.
Read these before writing anything:

| Module | What you get |
|---|---|
| `src/video.rs` | `SyntheticFrameSource` — deterministic moving I420 test pattern with glass-to-glass accounting. `FrameSource` enum already covers synthetic / camera / RTSP. |
| `src/audio.rs` | Audio source and publishing. |
| `src/session.rs` | `Credentials::resolve`, `mint_token`, `room_options`, `connect`, `publish_video`, `publish_audio`, `publish_control_track`, and `identity_for(room_name, suffix)` — which already takes a suffix, so per-class identity is a parameter, not new code. |
| `src/control/` | `ControlSample` with `encode`/`decode`, sequence numbers and timestamps; `ControlPublisher` driven at a configured `rate_hz`; `PublisherCounters`; and `publish_shortfall_pct(seq_published, rate_hz, duration_s)`. This *is* the telemetry generator — do not write another. |
| `src/stats.rs`, `src/counters.rs`, `src/clock.rs`, `src/probe.rs`, `src/sampler.rs` | Metrics, clock discipline, RTT probing. |

Add a **third bin target**, do not create a crate:

```toml
[[bin]]
name = "slice-client"
path = "src/bin/slice_client.rs"
```

If you find yourself writing a synthetic video generator, a telemetry publisher, a
token minter, or a stats collector, stop — it is already in `lib.rs`.

## 4. What is actually new

Everything below is the whole job.

- `--class {video|audio|telemetry}` selection, and a config type for the slice map.
- `config/slice-map.toml` — §5, the source of truth.
- Per-class LiveKit identity via the existing `identity_for`.
- `net/setup-linux.sh` and `net/teardown-linux.sh`, **generated from the slice map**.
- `net/systemd/` — one unit per class.
- `slice-verify` — the check in §8. May be a bin target or a script; it must exit
  non-zero on mismatch.
- `docs/ROUTING.md`, `docs/VERIFICATION.md`, `docs/TRADEOFFS.md`.

## 5. Traffic classes and the slice map

The contract. Put it in `config/slice-map.toml` and generate the network scripts from
it. Do not hand-copy these values anywhere.

| Class | VLAN | Subnet | Host | Router | Identity suffix | Payload |
|---|---|---|---|---|---|---|
| telemetry | 10 | 192.168.10.0/24 | .5 | .1 | `telemetry` | control data track |
| audio | 20 | 192.168.20.0/24 | .5 | .1 | `audio` | audio track |
| video | 30 | 192.168.30.0/24 | .5 | .1 | `video` | video track |

All three join the **same room**, default `teleop`.

**Only two PDNs exist on the R980 today** — a default, and one slice at SST 1 / SD
3002. Make class→PDN mapping config-driven and prove the mechanism with two classes
sharing a PDN. Do not assume three.

Telemetry runs at a known exact rate, default 200 Hz, via `ControlPublisher`. That
matters: a known bitrate turns NetCloud's byte counters into an arithmetic check
rather than a vibe. `publish_shortfall_pct` already computes the expected side.

## 6. Phases

**Phase 1 — macOS. Architecture only.**
Three processes, one room, three identities, three payload types, clean shutdown,
reconnect handling.

**macOS has no network namespaces.** Slice routing is not verifiable on a Mac and you
must not imply otherwise. Phase 1 passes when all three connect and publish, appear
as distinct participants, and a subscriber sees video, hears audio, and receives
telemetry at the configured rate with no sequence gaps. State plainly in
`docs/VERIFICATION.md` that Phase 1 proves nothing about routing.

**Phase 2 — Linux. Real isolation.** `net/setup-linux.sh`, generated from the map:

```bash
ip link add link "$UPLINK" name "$IFACE" type vlan id "$VID"
ip netns add "$NS"
ip link set "$IFACE" netns "$NS"
ip netns exec "$NS" ip addr add "$ADDR/24" dev "$IFACE"
ip netns exec "$NS" ip link set "$IFACE" up
ip netns exec "$NS" ip link set lo up
ip netns exec "$NS" ip route add default via "$GW"
```

Three gotchas, each worth an afternoon:

- **DNS.** Each namespace needs `/etc/netns/$NS/resolv.conf`. There is no inheritance
  and the LiveKit URL will not resolve without it.
- **Loopback.** `lo` starts down in a fresh namespace. Above.
- **Signalling rides the slice.** The WebSocket to the SFU and all STUN/TURN traffic
  leave through that namespace's default route. Every namespace must reach the SFU
  and TURN. If a slice has restricted egress that is a routing problem to solve, not
  a client bug.

Use systemd's native support, not `ip netns exec` wrappers:

```ini
[Service]
NetworkNamespacePath=/var/run/netns/slice-video
ExecStart=/usr/local/bin/slice-client --class video
```

**Phase 3 — Jetson.** Out of scope. Do not build for it, but keep encoder selection
behind the existing `FrameSource` / `encoder.rs` seams so NVENC is an implementation,
not a refactor.

## 7. Network side

`docs/ROUTING.md` records the NetCloud config this depends on. Not yours to change,
but it must be written down. From the working E400 PoC:

- **WAN device interface profiles**, one per PDN, Connection Manager → Devices.
  Default: APN `unl.iot.t-mobile.com`, S-NSSAI SST 1, no SD. Slice: same APN, SST 1 /
  SD 3002.
- **IPv4/IPv6 on both PDNs**, XLAT enabled.
- **WAN Management → Connection State → Always On** on the second PDN. Without it the
  PDN does not return after a device reset. Discovered the hard way; keep it written
  down.
- **One Local IP Network per class**, matching §5, DHCP enabled.
- **One Traffic Steering rule per class** — Networking → Routing → Traffic Steering —
  matching source CIDR, Target tab Static, WAN Binding = *WAN Profile is \<PDN\>*,
  Direct Internet Access on, Failover **off**. Failover on will silently move a class
  to the wrong slice, defeating the entire exercise.

The PoC ran on an **E400**; the target is an **R980**. Port numbering differs — the
E400's "VLAN 3002 on Port 4 untagged" does not transfer. Confirm the R980 layout
before writing the VLAN section.

## 8. Verification

A routing claim is proven when **all three** agree. `slice-verify` runs all three,
diffs against `slice-map.toml`, exits non-zero on mismatch.

1. **Host, per namespace.** Every class leaves on its expected source address and
   nothing leaks:
   `ip netns exec slice-video tcpdump -i any -n -c 200 'udp and not port 53'`
   A packet from another subnet inside a namespace is a failure.
2. **Router, per LAN.** NetCloud per-LAN byte counters show traffic on all three,
   split as the sources dictate. Calibrate on telemetry — known Hz times known
   payload is predictable within framing overhead. More than a few percent off means
   something is not going where you think.
3. **PDN.** Per-PDN counters move in step with their bound LANs. This is the only
   place that separates "steered correctly" from "steered somewhere that happens to
   work."

Record per class, reusing `stats.rs` and `counters.rs`: join time, ICE state
transitions, **selected candidate pair local address** above all, publish success,
reconnect count, and telemetry sequence gap.

## 9. Where this is going

Do not build this. Build so it fits.

The end state is one client, one room, one PeerConnection, three DSCP marks, three
QoS flows inside one slice. libwebrtc already implements RFC 8837 — per-packet DSCP
driven by `RtpEncodingParameters.priority`, exposed at
`libwebrtc/src/rtp_parameters.rs:136` and already round-tripping to `network_priority`
at `native/rtp_parameters.rs:97` and `:246`. It is gated on
`media_config.enable_dscp`, which defaults false. That patch is being built in
parallel on the `enable-dscp` branch and will merge in cleanly — the branches are
file-disjoint.

3GPP puts media classes on QoS flows within a slice, not on separate slices. Slices
are for tenancy and SLA isolation — teleop versus corporate. This testbed exists
because QoS flow provisioning is not in place yet, not because three slices is right.

What survives the collapse: the class abstraction, the slice map, the metrics, the
verification harness. What gets deleted: the multi-process launcher and the namespace
scripts. Note that `session::room_options()` is where `rtc_config` will eventually
carry `enable_dscp` — keep that function the single place room configuration is
built.

## 10. Working agreement

- **File discipline.** This branch touches only `teleop-test-matrix/`, `config/`,
  `net/`, `docs/`. It must not touch `webrtc-sys/` or `libwebrtc/` — those belong to
  `enable-dscp`, and disjointness is what keeps the two branches mergeable. If you
  need a change outside your set, put it on `teleop-test-matrix` and say so.
- Small commits, each leaving the tree runnable.
- `docs/VERIFICATION.md` is written before Phase 2 code, not after.
- When a value appears twice, generate one from the other. The slice map is the
  source of truth; the network scripts are downstream of it.
- Report what you could not verify. "Phase 1 proves nothing about routing" is a
  better status than a green check that means nothing.
