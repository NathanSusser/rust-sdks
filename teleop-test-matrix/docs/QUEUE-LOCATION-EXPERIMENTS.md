# Where does the latency queue sit? — experiment log

Operator's question (2026-09-15): what caused the latency spikes in most runs of the
10 Sep overnight ladder — modem, network, or SFU — with data a partner can check.

Method: state a hypothesis with predictions that could fail, run one test, compare,
update. Each entry is written **before** its data exists; results are appended, never
edited into the prediction.

Clock: both hosts PTP-locked (Host A grandmaster, software timestamping; Host B slave).
Both run ~4.5 s behind UTC; host-to-host alignment is unaffected.

---

## Starting evidence (10 Sep, no modem logs that night)

- 89% of 5,191 transport-spike frames (of 108,925) fall inside Host A's uplink speed
  test (`uplink-monitor.sh`: 4 parallel + 1 single 4 MB upload to Cloudflare, ~190 s).
- Every spike episode: Host B's transport delay rises first, Host A's delay-based
  estimator cuts ~1 s later, zero packets lost.
- Probes at 54–68 Mbps uplink caused nothing; probes after Host A's uplink fell to
  10–25 Mbps (08:03–08:06Z, RSRP/SNR flat) caused 200–500 ms spikes.
- The probe's traffic goes to Cloudflare and never reaches the SFU.

## H1 — the queue is in Host A's uplink, before the first carrier router

The spike is packets waiting in Host A's modem uplink buffer / radio uplink for grants,
when total offered uplink load exceeds uplink capacity. The SFU is not congested.

### S1 — reproduce one spike with every hop instrumented

- 300 s, H.264 2 Mbps, **unpinned** (as on 10 Sep), room `s1-probe-h264-2000k`,
  epoch 1789440600 (02:50:00Z, 15 Sep).
- At epoch+120 Host A runs the **identical** 10 Sep probe, start and end logged in ms.
- Host A: publisher stats, 5 Hz ping to hop 2 (10.169.180.252, first carrier router
  answering), hop 3 (10.198.3.237), SFU (10.1.20.21); qdisc backlog, QMI TX dropped,
  wwan0 header pcap, modem DLF.
- Host B: per-frame subscriber CSV, 5 Hz SFU ping, 1 Hz rx / UDP socket-drop counters,
  modem DLF, SFU participant list (same room SID = same SFU node).

### Predictions (H1 survives only if all hold)

| # | Prediction | Falsified if |
|---|---|---|
| P1 | Host B transport delay rises ≥3× baseline within 0–6 s of probe start | no rise: dose too small for today's capacity |
| P2 | Host A → hop 2 RTT rises with it (≥ +100 ms, same onset ±1 s) | hop 2 flat while media delay rises: queue is beyond the RAN |
| P3 | Host A qdisc backlog ≈ 0, QMI TX dropped unchanged | backlog grows: host-side queue |
| P4 | Host B → SFU RTT flat (≤ +20 ms) | rises with A's probe: SFU node / shared core congested |
| P5 | 0 packets lost at B; A's target cut within 3 s | loss: a drop, not only a queue |
| P6 | Host A DLF uplink-family record rate (0xB8C8/0xB8C9) rises during the probe | — (supporting only; contents need QCAT) |

### Next test by outcome

- **All hold** → H1 supported. S2: repeat with the publisher **pinned** (no backoff, the
  queue must grow or drop), then the busy-hour loop to ask modem vs network with both
  hosts' DLF (grants vs buffer status).
- **P1 fails** (no spike) → capacity too high tonight. S1b: raise dose (N=16 parallel)
  and gate on a gap probe measuring ≤ 25 Mbps.
- **P2 fails** → H2: queue beyond the first carrier router (core or SFU). S1c: hop 3 and
  a Host B-side probe to separate core from SFU; request SFU metrics.
- **P4 fails** → H3: shared SFU/core congestion. S1d: probe from Host B while Host A
  streams, and a probe from Host A with no video.

---

## Results
