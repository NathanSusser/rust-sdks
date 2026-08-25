---
---

<!--
No crates need bumping. `teleop-test-matrix` is `publish = false` and is not a
knope-tracked package (like the `examples/*` members). Nothing outside that crate is
touched: no public API, dependency, or behavior of any published crate changes.
-->

# Split network from application RTT, and fix probe sparsity, in `teleop-test-matrix`

Six corrections to the measurement harness and its analysis layer, all driven by the first
real Tier 0 sweep (69 runs against LiveKit Cloud, 2026-08-25).

**`rtt_*` is split into `network_rtt_*` and `app_rtt_*`.** They differ by roughly 2x —
measured 31.0 ms against 60.6 ms on the same run — because the four-timestamp probe
traverses publisher → SFU → subscriber → SFU → publisher *plus* application scheduling at
both ends, while `candidate_pair_rtt_s` is the ICE network round trip. Reporting the probe
figure as "network RTT" made Q-7's glass-to-glass/RTT ratios read below 1.0, which is
impossible: G2G contains a network traversal and cannot be faster than one. Against the
correct denominator the same data gives 1.9–2.4x. PRD §8.1a's ≤90 ms p95 bar now scores
`network_rtt_p95_ms`; the application loop is reported alongside as OBSERVE.

**The probe tracker no longer discards probes in flight.** It held a single outstanding
probe and retired it as lost whenever the next was issued, so at any probe interval below
the round trip a good probe was discarded before its echo landed — 76 sent against 64
completed on the measured run, and raising the rate would have made the completed count
*fall* while misattributing the loss to the network. It now tracks a set keyed by token and
retires a probe only when it outlives `probe_lifetime_ms`. Probing moved out of the stats
sampler into its own actor, since the poll loop capped the rate by construction, and the
rate rose from 1 Hz to 20 Hz — 63 usable samples across a 105 s window was too few for the
percentiles the latency bar is scored against.

**Quality-at-bitrate metrics added.** `qp_avg` (per codec only — QP scales are not
comparable across codecs) and `quality_limitation_bandwidth_poll_pct` (which is
cross-codec comparable), both in the T-1 table. Without them "this codec is more efficient"
and "this codec quietly degraded quality to fit" are indistinguishable from a bitrate
figure alone.

**`available_outgoing_bitrate_bps` renamed to `subscriber_available_outgoing_bitrate_bps`.**
It was frozen at exactly 300000.0 on every poll because the transport sample is read from
the subscriber peer connection, which sends only RTCP and so never ramps off libwebrtc's
`kDefaultStartBitrateBps`. The value was correct for what it measured; the name invited it
to be read as the publisher's uplink estimate.

**`audio_level` is now the maximum across scored polls, not the median.** The validity rule
is "zero for the whole run", which is a maximum test; as a median it reported a working
tone generator as a silent source on a run that peaked at 0.5026 but sat at zero through a
reconnect storm.

**The AV1 efficiency reading is retracted, and T-1's stated question is narrowed.** The new
quality metrics show AV1 ran at QP 40.0 against H264's QP 27.4 on the sweep that prompted
the claim — a coarser picture, not fewer bits for the same one. T-1 encodes to a *bitrate
target*, so quality is an output rather than a control, and cross-codec bitrate comparison
has no fixed point. `qp_avg` now renders immediately beside the bitrate it qualifies, the
T-1 caveat rules the comparison out explicitly, and the suite's question is amended to
"what bitrate each combination emitted, at whatever quality its rate control chose".
Answering the codec question needs fixed-quality encoding, which this SDK cannot express —
recorded as SDK-2 in `docs/SDK-FINDINGS.md` and as a redesign recommendation, not built.

**Control-path stalls are reported in milliseconds.** `control_max_gap_ms` and
`control_gap_p99_ms` convert the existing gap counts at the known publisher rate. Added
after a Tier 0 run showed a 121-sample gap — 605 ms at 200 Hz — on LiveKit Cloud with no
induced loss and a healthy sampler. Nothing else surfaced it: 121 of ~21 000 samples is
0.58%, which `control_delivered_pct` absorbs without a trace. OBSERVE, never scored.

**Connection failures are retryable and fully logged.** The harness exits 75 when the
session never established, distinctly from other failures, so `run_matrix.py` can retry
without pattern-matching stderr — a single transient event voided 15 of 69 runs in the
sweep. Retry is scoped to connect failures only: a lost session or a failed AV1 publish may
be exactly what a suite is measuring. Full harness stderr now goes to a per-run log file
rather than being truncated to 300 characters in the run record, and every attempt is
recorded so a retried run is never presented as a clean first-try run.
