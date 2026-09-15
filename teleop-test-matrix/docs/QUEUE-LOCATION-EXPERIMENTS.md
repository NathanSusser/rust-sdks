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
  epoch 1789440960 (02:56:00Z, 15 Sep; moved from 02:50 so Host A could verify
  its side live before arming). Probe at 02:58:00Z.
- **Amended before the cell (02:46Z):** hop 2 and hop 3 do not answer ICMP echo, so
  Host A measures them with a 5 Hz TTL-limited UDP probe toward the SFU media node.
  Pre-test: TTL=2 (hop 2) answered 0/15, TTL=3 (hop 3, 10.198.3.237) answered 8/15.
  **P2 therefore uses hop 3.** A queue before hop 3 covers the UE uplink buffer, the
  RAN and the first transport hops. The SFU media node is 10.1.20.16 (from A's pcap);
  10.1.20.21 is ingress/signalling only.
- At epoch+120 Host A runs the **identical** 10 Sep probe, start and end logged in ms.
- Host A: publisher stats, 5 Hz ping to hop 2 (10.169.180.252, first carrier router
  answering), hop 3 (10.198.3.237), SFU (10.1.20.21); qdisc backlog, QMI TX dropped,
  wwan0 header pcap, modem DLF.
- Host B: per-frame subscriber CSV, 5 Hz SFU ping, 1 Hz rx / UDP socket-drop counters,
  modem DLF, SFU participant list (same room SID = same SFU node).

- **Caveat on P6, known before the cell (02:49Z):** QCSuper has always discarded modem
  frames with DIAG opcode 158, which carry NR5G log headers (0xB8C0/0xB8C5/0xB8CB seen),
  at 4k–17k frames per 12 s against ~4k DLF records. No DLF so far contains them, so every
  NR5G record count, 10 Sep analyses included, is incomplete by an unknown factor. Host B
  now runs a patched qcsuper-noroot that drops them explicitly and counts them; decoding
  them is follow-up work. CRC loss swung 27–58% in back-to-back tests.

### Predictions (H1 survives only if all hold)

| # | Prediction | Falsified if |
|---|---|---|
| P1 | Host B transport delay rises ≥3× baseline within 0–6 s of probe start | no rise: dose too small for today's capacity |
| P2 | Host A → hop 3 RTT rises with it (≥ +100 ms, same onset ±1 s) | hop 3 flat while media delay rises: queue is beyond hop 3 |
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

### S1 — 2026-09-15 02:56:00Z, epoch 1789440960

Ran as registered. Unpinned (log line 1: `LK_PIN_BITRATE_TO_MAX=0`). Same SFU proven
from both hosts: RoomService at epoch+2 (B) and epoch+60 (A) both list room
`RM_VjR5FRe9TCHm` with `host-b-s1-probe-h264-2000k` and `s1-probe-h264-2000k-pub-56662`
ACTIVE. Host-minus-UTC −4.546 s (A) / −4.550 s (B) after the cell.

**Dose was small.** The probe took 4.1 s (46 Mbps parallel aggregate, 31.5 single),
against 14–20 s at ~10 Mbps on 10 Sep. t = probe start (1789441080.009).

| # | Observed | Verdict |
|---|---|---|
| P1 | B per-frame transport ~30 ms → 77 (+0.41 s) → 130 (+0.91) → 178–191 (+1.65–2.18) → 42 (+3.13, as the parallel phase ended at +3.11). Only seconds in the whole cell with p50 >2× baseline: t+120–122 | **holds** |
| P2 | A → hop 3 UDP TTL RTT 18 → 112 (+1.06) → 122 (+2.06) → 44 (+3.06) → 32 → 26 → ~20 | **holds** |
| P3 | A qdisc backlog max 2 pkts; QMI TX dropped 0. A's pcap: media left wwan0 continuously, ~1.8–2.0 Mbps at +0.5–1.25 s while B's delay was already 77–130 ms | **holds** |
| P4 | B → SFU media node ICMP 14–35 ms through the probe (cell p50 17.3) | **holds** |
| P5 | 0 packets lost, 0 retransmits, 0 NACKs; A target 2000 → 1392 (+1.0) → 975 (+2.0) kbps | **holds** |
| P6 | Host A NR MAC-range records/s rose ~30% in the first 2 s of the probe (baseline median 118 → 155, 150), Host B no clear change (107 baseline); 0xB9xx ~0 on both. Corrected 03:25Z with Host A's fixed reader (dlf-check acceptance rule): A 390,037 records / 5 resyncs, B 131,406 / 0 | **inconclusive**: the S1 reader lost ~95% of the log in the tty buffer and dropped the opcode-158 half, and a record rate is not a grant size |

**Unpredicted:** A's ICMP echo to the SFU media node did **not** queue (14–21 ms, one
49 ms sample) during the same seconds that A's UDP (media and TTL probes) queued ~100 ms.
Something before hop 3 serves ICMP ahead of UDP. Consequence: ICMP ping cannot measure
this queue, and every "the link is clean" conclusion that rested on ping needs re-reading.

**Conclusion.** The latency spike is a lossless ~100–190 ms queue located after Host A's
network interface and before hop 3 (10.198.3.237): Host A's modem uplink data path, the
radio uplink, or the first transport hop. Not Host A's host, not the SFU, not Host B.

## H2 — the queue is where uplink data waits for grants, and it is class-aware

Offered uplink load above granted capacity for a few seconds fills the UE uplink data
path; ICMP is prioritised over UDP there (H2), or in a class-based queue in the RAN /
first transport hop (H2′). Distinguishing them is the modem-vs-network question.

### S2 — agreed 03:20Z: epoch 1789443900 (03:45:00Z), room `s2-class-h264-2000k`

Host A's probes, all 5 Hz to the SFU media node 10.1.20.16 from epoch−60 to +340:
ICMP DSCP 0, ICMP DSCP EF (`-Q 0xb8`), UDP TTL=3 DSCP 0, UDP TTL=3 EF, and TCP SYN to
closed port 3478 (RST round trip; opens no connection). Kernel queue at 10 Hz. Publisher
pinned. Upload probe N=12 at 03:47:00Z. Both hosts run d9's lower-CPU modem-log build
with the opcode-158 half written into the DLF, if it verifies in time.

**Modem-log caveat found before S2 (03:30Z, d9, Host B, 60 s idle).** The reader used
for every DLF so far (10 Sep, 15 Sep, S1) was pegged at 99.7% CPU and silently lost
~95–97% of the modem's log stream in the tty buffer: 346 records/s against 14,100/s from a
faster reader with 0 CRC failures. Per-code undersampling is ~11–28× and **not uniform**
(0xB8C8 42 vs 942/s; 0xB885 5 vs 138/s), and the opcode-158 half was absent entirely.
All earlier DLF rates are unusable for comparing log types, not just lower bounds. S1's P6
is void, not merely inconclusive. The fast build writes ~8.2 MB/s (full mask).

Registered readout:

- **R1** EF-marked ICMP and UDP queue exactly like DSCP 0 → no DSCP-aware classifier
  acted on our mark. (Marks may be bleached before a classifier, so this is not proof
  that none exists.)
- **R2** TCP RST round trip queues like UDP → the classifier separates ICMP from
  everything else.
- **R3** TCP stays flat like ICMP → it separates UDP specifically.
- **R4** Pinned: Host B's delay grows through the whole ~10 s probe with no target cut;
  loss appears only if a buffer overflows.
- **R5** 10 Hz requeues burst during the probe, as in S1.

Original proposal:

- 300 s, H.264 2 Mbps **pinned**; probe N=12 at t+120 for a ~10 s episode.
- Paired 5 Hz, 84-byte probes from A to the SFU media node for the whole cell: ICMP DSCP 0,
  ICMP DSCP EF, UDP TTL=3 DSCP 0, UDP TTL=3 DSCP EF, TCP SYN if a port answers.
- Reduced modem log mask (NR MAC UL: BSR, grants, PHR, UL TB) on both hosts, so the reader
  keeps up; decoded if possible.
- Readout: EF escapes the queue → DSCP-aware classifier; only ICMP escapes regardless of
  DSCP → protocol classifier (typical of UE-side uplink prioritisation); BSR high with small
  grants → network not granting; grants large but data waiting → modem.
