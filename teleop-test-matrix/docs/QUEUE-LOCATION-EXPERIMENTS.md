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

### S2 — results (03:45:00Z, epoch 1789443900, build=d9-fast on both hosts)

Ran as registered. Pinned (`LK_PIN_BITRATE_TO_MAX=1`). Same SFU: room `RM_ve3bdMkcfck3`
from both hosts. Clock host−UTC −4.644 s (both). Upload: 12 × ~3.3 Mbps for 9.8 s then
30.9 Mbps single; 10.85 s total. Modem logs complete for the first time: Host B 6,437,498
records / 9 bad CRC; Host A 11,873,765 records / 0 resyncs.

| Series (A→10.1.20.16 unless noted) | Baseline | During upload | Rise |
|---|---|---|---|
| Host B video delay (per frame) | 36 ms | 150–248 ms, whole upload | +143 |
| UDP TTL=3 to hop 3, DSCP 0 | 21 | 106–137 | +115 |
| UDP TTL=3 to hop 3, EF (n=3–4) | 21 | 100–162 | +140 |
| TCP SYN→RST | 18 | 27–105 | +65 |
| ICMP DSCP 0 | 16 | 27–84 | +52 |
| ICMP EF | 16 | 23–89 | +52 |
| Host B → SFU ICMP | 19 | 17–29 | +10 |

- **R1 supported:** EF behaved like DSCP 0 for ICMP and UDP; no DSCP-aware classifier
  acted on our marks (marks may be bleached upstream).
- **R2, R3 not supported:** every class queued; magnitude ordered media > UDP > TCP ≈ ICMP.
  S1's "ICMP escaped" does not reproduce under a 10 s dose.
- **R4 supported:** delay held ~150–180 ms for the whole upload, 0 packets lost; the pinned
  target still dipped to 1.70 Mbps at +7…+10 s.
- **R5 supported, sustained:** Host A's fq_codel backlog 41–65 packets for the whole upload
  (baseline 0), requeue bursts at onset (748/s) and release (1,868/s); qdisc dropped 2;
  driver and QMI TX dropped 0.
- **New — part of the wait is inside Host A.** All 8,699 packetized frames order-aligned to
  8,699 distinct video RTP timestamps on Host A's wwan0 (shift 0, no negative delays).
  Packetize → last packet on the wire: baseline p50 0.5 / p95 0.9 / max 1.7 ms; during the
  upload p50 38.4 / p95 88.3 / max 127.1 ms (p50 44–47 ms at +1…+8 s); after p50 0.5 ms.
  So ~45 ms of Host B's ~150 ms is Host A's fq_codel holding media while the modem driver
  refused packets; ~100 ms is below wwan0 (modem buffer / radio uplink).
- **New — Host B's modem reacted to Host A's upload.** Same log codes, same window (DLF clock
  ±2 s), opposite or proportional moves on the two modems:

  | Code | Host A base → upload | Host B base → upload |
  |---|---|---|
  | 0xB882, 0xB8A6, 0xB958 | ~114/s → **0** | ~105/s → **~54** |
  | 0xB870/72/73/7C | 16 → 25 | 13 → 5 |
  | 0xB885 | 248 → 325 | 266 → 200 |
  | 0xB8C4, 0xB8CE, 0xB887, 0xB896 | ×4.7, ×3.1, ×2.0, ×2.5 | unchanged |

  Host B was only receiving a constant 2 Mbps. A bystander modem changing its reports exactly
  while another host uploads fits one cell scheduler redistributing resources. Codes are
  unnamed, so this is timing evidence, not decoded grants.

**Conclusion.** The spike is uplink demand above the capacity Host A is granted. The modem
flow-controls (no drops), media waits first in Host A's kernel queue and then in the UE /
radio uplink. The SFU and DSCP/protocol classification are ruled out. The coupling to Host B
points at the shared cell's scheduler (network side) rather than a modem fault.

**Checks on the coupling reading (04:05Z).**

- **Same cell: confirmed.** Host A (QMI, 03:57Z): NR-ARFCN 521310 (n41), PCI 85, cell ID
  7622566199. Host B (SCAT decode of 0xB97F in a slice of its S2 DLF): layer 0 NR-ARFCN
  521310, PCI 85, SSB 0; plus a second layer NR-ARFCN 393130, PCI 85. Host A's own DLF
  (SCAT, mid-S2 slice) shows the same two layers (521310 and 393130, PCI 85, SSB 0; QMI
  lists only n41/521310 as the active band), so there is no carrier asymmetry between the
  hosts to explain the 9 Sep 1.1 vs 16 Mbps gap.
- **Host A's NR_L2_UL_BSR record rate rose +62% for exactly the upload** (16 → 26/s over
  +0…+10 s, back to 15 within 2 s): the UE reporting buffered uplink data more often.
  Fixed-cadence MAC items (0xB881, 0xB888 at 200/s; 0xB883 ~229/s) did not move, so grant
  sizes need payload decoding.
- **Traffic-volume explanation (d9): does not account for it.** Host B is the subscriber of
  Host A's stream, so its codes could fall if less media arrived. Over the upload seconds
  Host B still received 26–33 frames/s, 1.69–2.12 Mbps (WebRTC) and 2.1–2.8 Mbps on wwan0
  (baseline ~2.3–2.7), rx packets −20%, while the codes halved. S3 arm (a) removes the video
  entirely as the clean control.

- **Code names (d9, from MobileInsight `dm_collector_c/consts.h` and SCAT `diagcmd.py`):**
  0xB872 NR_L2_UL_TB, 0xB873 NR_L2_UL_BSR, 0xB881 NR_MAC_UL_TB_Stats, 0xB883
  NR_MAC_UL_Physical_Channel_Schedule_Report, 0xB888 NR_MAC_PDSCH_Stats, 0xB97F
  NR_ML1_Searcher_Measurement_Database_Update_Ext. 0xB882, 0xB8A6, 0xB8A8, 0xB958, 0xB870,
  0xB87C, 0xB983 and the opcode-158 codes are unnamed in both sources.
- **Named-code rates, baseline → upload.** UL TB and BSR records: Host A 16 → 25/s, Host B
  13 → 5/s. UL schedule report: Host A 229 → 232, Host B 195 → 175. UL TB stats and PDSCH
  stats: 200/s on both, periodic, unchanged.
- **This weakens the coupling reading for the named codes.** Host B's own uplink (mostly RTCP
  feedback) fell from ~110 to ~70 packets/s during the upload, so fewer UL TB/BSR records on
  Host B can follow from Host B sending less. Record rates track scheduling events, not
  granted bytes; fewer, larger allocations would also halve per-allocation codes. Cross-UE
  coupling is therefore **not established**. Same cell is. S3 (upload with no video) decides,
  and decoding BSR values and grant sizes (MobileInsight, being tried) would settle modem
  vs network directly.

## H3 — Host A's uplink capacity is set by a cell scheduler shared with other UEs

Other load on the same cell reduces what Host A is granted; that is the candidate mechanism
for the busy-hour < 2 Mbps periods.

### S3 — registered 04:10Z, before any arm's data

Three 120 s arms, DIAG on both hosts (build fast2, full mask), same upload traffic as S2
(12 × 4,000,000-byte uploads then one single, `--max-time 25`) at epoch+60 s. Both hosts
on NR-ARFCN 521310 / PCI 85; Host A reads its serving cell before and after each arm.

| Arm | Epoch | Load | Video |
|---|---|---|---|
| a `s3a-upload-a-novideo` | 1789445640 (04:14:00Z) | Host A uploads | none |
| b `s3b-stream-a-upload-b` | 1789446000 (04:20:00Z) | Host B uploads | Host A → Host B, pinned 2 Mbps |
| c `s3c-upload-b-novideo` | 1789446360 (04:26:00Z) | Host B uploads | none |

**Deviation, known before arm a's data:** the wrapper's no-subscriber switch was first named
`SUB`, which the wrapper already uses for the subscriber binary path, so arm a on Host B ran a
subscriber joined to an empty room (no publisher, no media; signalling and ICE keepalives
only). Fixed as `SUBSCRIBE` for arms b and c.

Predictions:

- **Q1 (arm a):** if the S2 code changes on Host B were coupling through the shared cell, Host
  B's 0xB882/0xB8A6/0xB958/0xB8A8 and 0xB872/0xB873 rates step during Host A's upload even
  with no media anywhere. If they do not step, S2's Host B changes came from Host B's own
  traffic pattern.
- **Q2 (arm b), the busy-hour mechanism:** if another UE's upload on the shared cell cuts Host
  A's uplink capacity, Host A's media delay at Host B, Host A's hop-3 UDP RTT and Host A's
  qdisc backlog rise during Host B's upload although Host A offers only 2 Mbps. No rise means
  the cell had headroom for both at tonight's load, and the busy-hour periods need more
  concurrent demand than one extra UE.
- **Q3 (arm c):** mirror of arm a. Host A's codes should respond to Host B's upload the way Host
  B's responded to Host A's.
- Readout stays on per-code record rates: the v3 record layouts of 0xB872/0xB873/0xB881/0xB883
  are not supported by any public decoder we could verify (MobileInsight supports v2 only).

### S3 arm a — result (Q1)

Host A's upload: 12 × ~3.6 Mbps (43.3 Mbps aggregate) from epoch+60.02 s, single 35.4 Mbps,
end +69.9 s; serving cell PCI 85 before and after. Host B carried no media (idle subscriber in
an empty room, 5 Hz pings). Host B DLF: 3,233,148 records, 0 bad CRC.

**Q1: yes. Host B's modem changed for exactly Host A's upload, with no video anywhere.**
Host B, records/s, medians over −55…+50 s → +61…+70 s → +80…+150 s:

| Code | Before | Upload | After |
|---|---|---|---|
| 0xB872 NR_L2_UL_TB / 0xB873 NR_L2_UL_BSR | 10 | 3 | 9 |
| 0xB883 NR_MAC_UL_Physical_Channel_Schedule_Report | 148.5 | 84.5 | 132 |
| 0xB884 / 0xB885 (unnamed) | 156 / 155 | 88.5 / 71.5 | 138 / 134 |
| 0xB8D1 / 0xB8CF (unnamed) | 201.5 / 148 | 113.5 / 84.5 | 170 / 133 |
| 0xB896 / 0xB8D0 / 0xB89B / 0xB89E (unnamed) | 11 / 26 / 31 / 36 | 17 / 39 / 46 / 49.5 | 11 / 25 / 32 / 36 |

Per second the step runs +58…+67 s on the DLF clock (±2 s; the same ~2 s early offset as S2).
**Host B's own traffic did not change:** tx 16–33 packets/s, rx 15–31 packets/s, 10–55 kbps
through the whole window, no step. A smaller dip in the same codes at ~+76…+79 s has no known
load and is unexplained.

This removes the traffic-volume explanation for S2's Host B changes: Host B's modem changed its
uplink-scheduling reports because another UE on the same cell loaded the uplink. Codes are
unnamed or undecoded beyond their names, so it is timing evidence of shared-cell coupling, not
decoded grants.

Host A's path during its own upload in arm a, no video anywhere: fq_codel backlog 23–26 packets
throughout, requeue bursts 500/s at onset and 1,957/s at release, UDP TTL=3 82–148 ms, ICMP and
TCP ~26–65 ms, all back to baseline within 1 s of the end. S2's path signature therefore needs
only the upload, not the video. Host A's path showed nothing at +76…+79 s.

### S3 arm b — result (Q2)

Host B's upload: 12 × ~3.1 Mbps (37.6 Mbps aggregate) from epoch+60.01 s, single 28.0 Mbps,
end +71.56 s. Host A published pinned 2 Mbps H.264 to Host B.

**Q2: no measurable capacity effect at tonight's load.** While Host B saturated its own uplink
(Host B qdisc backlog 19–30 packets, Host B → SFU ICMP 16 → up to 91 ms), Host A's path and
Host A's video were unchanged:

| Host A / media, +61…+71 s | Baseline (+20…+55 s) | During Host B's upload |
|---|---|---|
| Host A ICMP to SFU node, p50 | 16.8 ms | 15.2–24.4 ms |
| Host A UDP TTL=3 to hop 3 | 20.7 ms | 17.5–30.0 ms |
| Host A TCP SYN→RST, p50 | 17.1 ms | 15.3–21.0 ms |
| Host A fq_codel backlog | 0 | 0 every second |
| Host A target | 2.00 Mbps | 1.99–2.00 Mbps |
| Host A video at Host B, p50 | 33.5 ms | 34.9–41.7 ms, 0 packets lost |

The shared cell couples the two modems' uplink reporting (arm a), but at ~40 Mbps uplink headroom
one other UE's 38 Mbps upload did not reduce what Host A could send. A capacity effect needs the
cell actually congested: the busy-hour periods, which the overnight loop samples.

**The uploading modem's signature is the same on both hosts.** Host B's own codes while Host B
uploaded (arm b; medians −55…+50 → +61…+70) match Host A's while Host A uploaded in S2:

| Code | Host A uploading (S2) | Host B uploading (S3 arm b) |
|---|---|---|
| 0xB882, 0xB8A6, 0xB958 (unnamed) | ~114 → 0 | ~60 → 0 |
| 0xB8C4 / 0xB8CE (unnamed) | ×4.7 / ×3.1 | ×4.0 / ×3.2 |
| 0xB887 / 0xB896 (unnamed) | ×2.0 / ×2.5 | ×2.5 / ×2.6 |
| 0xB885 (unnamed) | ×1.3 | ×1.6 |
| 0xB872 NR_L2_UL_TB / 0xB873 NR_L2_UL_BSR | ×1.56 | ×2.9 |
| 0xB883 NR_MAC_UL_Physical_Channel_Schedule_Report | ×1.01 | ×1.14 |

These codes therefore mark a UE's own uplink-busy state, identically on both modems (same model
and firmware). The bystander signature is different: in arm a the idle Host B logged fewer uplink
scheduling reports (0xB872/0xB873 ×0.3, 0xB883 ×0.57) while Host A uploaded.

## Overnight loop, 2026-09-15 — anomaly log

The loop (`overnight-2026-09-15`, Host A driver) runs 600 s pinned 2 Mbps H.264 cells to
16:30Z. From ~05:09Z, DIAG runs on k%3==1 cycles until 12:00Z, none 12:00–14:30Z, and every
H.264 cycle 14:30Z–END, to concentrate modem logs in the morning busy hour. Anomalies are
recorded here as they are localized.

**#1 — cycle 1, 04:51:46Z: a Host B-only ~0.8 s radio-link hold (not the uplink question).**
Host A's video at Host B stalled to 770 ms and 27 packets were lost at 04:51:47Z, while Host A's
path to the SFU stayed 16–19 ms with no queue and Host A's modem log was flat. On Host B, pings
to both 10.1.20.16 and 10.1.20.21 got no reply for 0.81 s after 04:51:46.071, then three held
replies arrived within 20 ms (623, 425, 219 ms) and the next was 20 ms: held and released, none
lost. The subscriber's receive rate dipped to ~20 fps and caught up at ~40 fps (frame assembly
up to 335 ms). Host B's DLF shows one second with broad MAC/L1 reporting 30–55% below its
neighbours (0xB8A8 100 → 45, 0xB883 224 → 158, 0xB887 164 → 81) and no RRC messages
(0xB821 = 0), with 0xB97F measurement records steady; RSRP −89 dBm and SNR 26 dB unchanged.
No 0xB975 NR_ML1_Serving_Cell_Beam_Management records and no RACH records (0xB889 trigger,
0xB88A attempt) in the window, so neither a logged beam switch nor a random-access recovery;
fixed-cadence MAC stats (0xB881, 0xB888) and RLC DL stats (0xB84D) steady; 0xB958 94 → 40 in
the same second. Reading: a short Host B scheduling or retransmission interruption without RRC,
beam-management or RACH signalling; not a handover and not a beam-failure recovery; cause
unnamed.

**#2 — overnight render skips: ~50 ms holds inside Host B, not the network (cycle 49,
15:18Z).** Host A found frames decoded but never rendered all night (0–17 per cycle, off-peak
and busy hour, DIAG on or off), each after a frame N−1 arriving ~75–90 ms late (baseline ~27–32)
with N+1 close behind; Host A's side was clean (on-time packetize, no wwan0 egress gap > 6 ms,
qdisc 0). Host B has no packet capture (tcpdump has no capabilities, no NOPASSWD), so Host B
sampled `/sys/class/net/wwan0/statistics/rx_packets` at 5 ms on CLOCK_REALTIME
(`nic-rx-sampler.py`; period p50 5.00 / p99 5.09 ms; one 668 ms pause, overlapping no stall;
render impact within the c044–c047 spread) and ran `stall_locator.py`.

Stalled frames (e2r ≥ 2.5× the 31.6 ms baseline, next frame ≤ 25 ms later): 64 of 17,388.
- Whole-frame assembly 0.23 ms p50 (max 0.39), so not a late last packet.
- At each stalled frame's expected on-time arrival (capture + 31.6 ms, window −10…+15 ms) the
  interface counted p50 23 packets (p10 17; 0% zero), against p50 12 (p10 8; 0.1% zero) for
  ordinary frames. At the actual WebRTC receive it counted ~11 (one frame, N).
- 59 of 64 had their packets at the interface on time; 3 (frames 4458, 6768, 17228) had ≤ 2
  and look upstream.
- Stall counts do not follow DIAG: c044 (DIAG off) 138; c046, c047, c049 (DIAG on) 7, 71, 64.

Reading: the packets reached Host B's kernel on time and WebRTC's receive timestamp came ~50 ms
later, released together with the next frame: a hold between kernel receive and the WebRTC
receive-thread read on Host B (thread scheduling or a blocked socket read), the same family as
Host B's known render judder. Caveat: the counter marks NAPI handoff, so modem/USB batching would
count as upstream. Being repeated on cycles 50, 52 and 53.

Host A's independent classifier (different window: recv−(e2r−base) … recv−3 ms, longest quiet
stretch against a 1,549-frame control, synthetic-tested 7/7) agrees: of 63 stalls, 55 Host B,
3 upstream, 5 unclear. Upstream agreed on 4458 and 6768 (Host A adds 7041; calls 17228 Host B).

Properties of the hold, from both hosts:
- **Near-constant length.** c049: e2r − baseline p10 47.6, p50 50.4, p90 55.8 ms (2 ms bins
  46:10, 48:20, 50:15, 52:11). All night (Host A): 1,385 of 1,605 in 40–54 ms, independent of busy
  hour or DIAG. A network queue would give variable durations.
- **Aperiodic onsets.** No phase-locking at 1 s (Rayleigh R = 0.02, p ≈ 0.97), 2 s, 5 s, 10 s,
  0.1 s or 0.05 s; 0.5 s R = 0.25 (p ≈ 0.02, not significant across 7 periods). Inter-stall gaps
  p10 0.83 s, p50 4.7 s, p90 18.6 s. The subscriber's 1 Hz `get_stats` poll is therefore not the
  trigger.
- **Not CPU power state or tick.** Host B: i5-14400T, 16 threads; intel_pstate, governor
  powersave, EPP balance_performance; C1/C2/C3 enabled (C3 exit 1,048 µs); CONFIG_HZ=1000,
  NO_HZ_FULL; no CPU isolation or thread affinity. None produces a fixed ~50 ms hold. At one
  snapshot network_thread ran on CPU 4 and the decoder on CPU 13.
- Every stall coincides with a render gap > 50 ms (vs 45% by chance within ±150 ms), but that is
  mechanical (a late frame plus a burst forces a render gap); Host B had 1,059 render gaps vs 64
  stalls, so render judder is broader.

Reading: packets wait in Host B's kernel for a fixed ~50 ms at irregular moments, which fits a
missed wakeup in the receive path falling back to a fixed wait or timer. Code and system changes
are left to the operator.

### S3 arm c — result (Q3)

**Q3, modem side: yes, and the coupling is symmetric.** Host A's DLF (Host A's parser with the
dlf-check acceptance rule, 0 resyncs; host = modem − 4.719 s) during Host B's upload, with no
media on Host A and Host A only recording; medians +5…+55 → +61…+70 → +80…+150 s, verified on
Host B from Host A's `dlf-rates-epoch.csv`:

| Code | Before | Upload | After | Ratio |
|---|---|---|---|---|
| 0xB872 NR_L2_UL_TB / 0xB873 NR_L2_UL_BSR | 9 | 2 | 10 | ×0.22 |
| 0xB883 NR_MAC_UL_Physical_Channel_Schedule_Report | 156 | 106.5 | 187 | ×0.68 |
| 0xB884 / 0xB885 (unnamed) | 167 / 148 | 102.5 / 57 | 169 / 153 | ×0.61 / ×0.39 |
| 0xB8D1 / 0xB8CF (unnamed) | 194 / 191 | 110.5 / 116 | 211 / 197 | ×0.57 / ×0.61 |
| 0xB882 / 0xB8C4 (uploader markers) | 30 / 15 | 24 / 12.5 | 26 / 11 | ×0.80 / ×0.83 |

The step starts ~2–3 s early on the DLF clock (as on Host B) and recovers as Host B's parallel
uploads end. **Whichever UE on PCI 85 uploads, the other UE's uplink BSR/TB reporting drops to
about a quarter and its uplink schedule reports to about two-thirds, for the upload window only,
independent of that UE's own traffic.** Two code sets are now empirical: a *UE-own-uplink-busy*
set (0xB882/0xB8A6/0xB958 stop; 0xB8C4/0xB8CE/0xB887/0xB896 rise) and a *bystander* set
(0xB872/0xB873/0xB883/0xB884/0xB885/0xB8D1/0xB8CF fall). Both unnamed beyond the MobileInsight
names; QCAT would name them. Observations only: Host A's 0xB883/0xB884/0xB885/0xB8D1 sit
10–25% above baseline for +72…+150 s, and a single second at +76 s shows 2–3× spikes in
0xB882/0xB8C4/0xB8CE, near the unexplained +76…+79 s dip on Host B in arm a.

**Q3, path side:**

Host B's upload: 12 × ~3.24 Mbps (38.9 Mbps aggregate) from epoch+60.01 s, single 29.9 Mbps,
end +71.38 s. No video on either host; Host B ran with `SUBSCRIBE=0` (no subscriber, no room
join). Host B's wwan0 sent 3.1–4.4 k packets/s with a 17–22 packet qdisc backlog for +61…+69 s.
Host A's serving cell PCI 85 before and after (RSRP −92 / −90 dBm).

**Host B's uploader signature without video** (medians −55…+50 → +61…+70): 0xB882/0xB8A6/
0xB958 28 → 0; 0xB8C4 ×18.5, 0xB8CE ×12.8, 0xB887 ×8.4, 0xB896 ×7.2 (lower no-video baselines,
so larger ratios than arm b); 0xB872/0xB873 ×2.55; 0xB885 ×2.05; 0xB884 ×1.68; 0xB883 ×1.33.

**Host A's path while Host B uploaded, no video anywhere:**

| Host A series | Baseline +10…+55 s (p50 / p90) | +61…+70 s |
|---|---|---|
| ICMP to SFU node | 19.2 / 26.2 ms | p50 21.5, max 28.1 |
| UDP TTL=3 to hop 3 | 23.0 / 29.4 ms | p50 24.8, max 32.5 (32 ms in 4 of 10 s) |
| TCP SYN→RST | 14.8 / 24.3 ms | p50 23.2, max 37.8 (level already ~23 from +56 s, before the upload) |
| fq_codel backlog / requeues | 0 / 0 | 0 / 0 every second |

At most a ~5–10 ms rise in Host A's round trips, partly preceding the upload, and no queue: the
same answer as Q2. At tonight's ~40 Mbps headroom another UE's upload on PCI 85 does not
meaningfully reduce what Host A can send.

**Separate observation (not Q2): a 1.41 s render stall on Host B.** At +72.9…+74.8 s, just after
Host B's upload ended, the per-frame CSV has no rows and frame_id jumps 2096 → 2152. This is not
loss: Host B's wwan0 received a normal ~390–415 packets/s at 2.4–2.6 Mbps (0 rx_dropped, 0 UDP
socket-buffer errors), 0 packets were lost, and the subscriber's decode-health counters rose
~29 frames/s straight through (received and decoded 2093 → 2123 → 2152 → 2181, 0 dropped). The
CSV logs a row only on GPU render completion, so the missing IDs were received and decoded but
not drawn: one gpu_complete_interval of 1,411 ms. That is Host B's display/render loop (the known
render judder), not the network or the decoder. Logged for the loop; it does not affect Q2.

Original proposal:

- 300 s, H.264 2 Mbps **pinned**; probe N=12 at t+120 for a ~10 s episode.
- Paired 5 Hz, 84-byte probes from A to the SFU media node for the whole cell: ICMP DSCP 0,
  ICMP DSCP EF, UDP TTL=3 DSCP 0, UDP TTL=3 DSCP EF, TCP SYN if a port answers.
- Reduced modem log mask (NR MAC UL: BSR, grants, PHR, UL TB) on both hosts, so the reader
  keeps up; decoded if possible.
- Readout: EF escapes the queue → DSCP-aware classifier; only ICMP escapes regardless of
  DSCP → protocol classifier (typical of UE-side uplink prioritisation); BSR high with small
  grants → network not granting; grants large but data waiting → modem.
