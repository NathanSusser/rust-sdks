# 08 — GCC out, bitrate pinned, degradation locked

Runs after the operator's ruling of 8 Sep 2026 that Google congestion control is
out permanently. Publisher configuration for every run in this bucket:

    LK_PIN_BITRATE_TO_MAX=1        the estimator cannot lower the allocation
    LK_MAX_START_BITRATE_KBPS=N    defeats the SDK's hardcoded 1 Mbps start ceiling
    --degradation locked           MaintainFramerateAndResolution

**Read the receive side before believing a send-side "it holds".** The publisher
reported 147.2 MB at 7-9 Mbps with no step. Over the same path minutes later the
subscriber measured the stream decaying 9.37 -> 0.71 Mbps in 88 s with 17,014
packets lost, while resolution held at 1600x1300 because it was pinned. Both
readings are correct about their own end. Only one of them is what an operator
would see.

**Mechanism: a keyframe recovery spiral.** 17 keyframes in 88 s on an
infinite-GOP encoder, so all 17 were receiver-requested loss recovery. Loss forces
a PLI, the PLI forces a 1600x1300 keyframe into an already-dropping path, which
causes more loss.

**WHERE THE FRAMES WENT — measured, not inferred.**

    published (frame-id span)   2561    88.4 s at 29.0 fps, matching the publisher's 29 fps
    arrived at the receiver      575    LOST IN NETWORK 1986  (77.5%)
    decoded                      575    lost in decoder     0
    reached the screen           373    not drawn         202  (35.1% of arrivals)

An earlier version of this file blamed the renderer for the 202. **That attribution
was wrong.** Render p50 is 3.5 ms and exactly 1 render in 373 exceeded the 33.3 ms
frame budget — 0.3%. A renderer that idle does not lose frames by being slow; the
202 are most likely stale frames superseded when a backlog arrives in a burst, which
makes them a consequence of the transport problem rather than a second, separate one.
The counters prove the renderer had capacity; they do not prove why it skipped, so
treat the burst explanation as inference.

**Transport is 95.8% of end-to-end latency.**

    transport  capture -> receive   p50 472.7 ms   p95 1401.2   max 3002.9
    local      receive -> screen    p50   9.9 ms   p95   28.4   max 1194.2

89% of arriving frames are over 100 ms late, 46% over 500 ms, 8% over a second, and
transport p50 climbs 394 -> 557 ms across the run with a 2058 ms spike. `webrtc_receive`
is stamped from `FirstPacketReceiveUnixTimeMicros`, i.e. the first packet's arrival, so
this figure is wire time and does not include the jitter buffer.

**Receive-side buffering is one buffer, and the kernel is not queueing.** The WebRTC
jitter buffer ran delay_avg 38 -> 118 ms, settling ~97 ms, with its target growing
48 -> 84 ms as network jitter grew. Every other receive stage is sub-millisecond at p50
(assembly 0.14, decode->sink 0.16, select->prepare 0.03). The kernel shows **zero** UDP
receive-buffer errors and zero drops, and every qdisc backlog is 0 packets. Note one
unresolved discrepancy: the SDK reports ~97 ms of jitter-buffer delay while measured
`receive_and_assembly_ms` is 0.14 ms at p50, which `--low-latency` may explain by
playing frames out immediately. Do not add the 97 ms to the transport figure until
that is settled.
