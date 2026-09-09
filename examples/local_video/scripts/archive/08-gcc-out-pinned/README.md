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

**Do not quote frame rate or e2e latency from this bucket's subscriber CSV.**
Host B renders on Intel UHD 730 via the CPU I420 upload path and dropped ~35% of
decoded frames locally (373 CSV rows against 575 decoded, 0 dropped by the
decoder). The renderer, not the link. `render_ms` reached 1187 ms and contaminates
every e2e figure. Trustworthy here: `receive_to_gpu_complete` (p50 10.0 ms),
`decode_ms` (p50 5.3 ms), `receive_qp`, `packets_lost`, `receive_bitrate_mbps`,
resolution, and the SDK's own receive-fps log lines.
