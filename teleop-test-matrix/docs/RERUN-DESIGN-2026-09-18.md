# Re-run design: attributing latency to network, modem or host

Written 2026-09-17 after the codec×bitrate matrix produced one usable cell in six.
The operator's question, verbatim: *"confirm whether network is dropping packets or is
taking too long etc or if its on the modem or host side."*

This document exists because the previous campaigns could not answer that question, and
the reason was a single missing instrument rather than a flaw in the analysis.

## Why the last campaigns could not attribute anything

Host A captures packets on `wwan0`. **Host B does not** — `tcpdump` carries no
capabilities, `AF_PACKET` raw sockets are denied, and there is no passwordless sudo. So
every statement about where a frame was delayed has been *inferred* from 5 ms interface
byte counters rather than observed from the packets themselves.

That inference is what two hosts spent an afternoon failing to agree on. The stall
classifier went through ten clauses and four convention changes — window bounds, edge
clipping, a 5 ms sampling-interval subtraction, tie-break order, an inverted sign, a
catch-all bucket that asserted findings from negative results, and the control-set
selection — and still ended with *four of five cells unpublishable* because the control
distribution overlapped the stall distribution. Not one of those defects was visible from
inside a single implementation.

With packets observed at both ends, that entire class of argument disappears.

## The one prerequisite

```
sudo setcap cap_net_raw,cap_net_admin=eip /usr/bin/tcpdump
```

One command, once, on Host B. Without it this design degrades to what we already have.

**Done, 2026-09-17.** `getcap /usr/bin/tcpdump` reports `cap_net_admin,cap_net_raw=eip`, and
`~/diag-capture/pcap.sh SECONDS [label] [iface]` is written to match `capture.sh`'s conventions:
headers only (`-s 96`), UDP, `-U` so a killed capture keeps everything up to the kill, a 2×512 MB
ceiling, a per-interface lock, a disk guard, and a capability check that refuses to start rather
than producing an empty file. Re-check `getcap` before each campaign — a package upgrade drops it.

## What each observation then attributes, decisively

RTP sequence numbers are matched between Host A's capture and Host B's capture, and
against Host B's per-frame render log.

| Observation | Verdict |
|---|---|
| Sent by A, never seen at B | **network loss** |
| Seen at B, arrived late | **network or modem delay** |
| Seen at B on time, rendered late | **host side (Host B)** |
| Sent late by A | **host side (Host A)** |

Each row is a table lookup, not a threshold. The modem logs then say *which* of network
or modem for row 2 — and that is the only row where the modem logs are load-bearing.

## Forcing the bitrate: the operator's decision, and what it actually sets

**Decision, 2026-09-17: the re-run forces the bitrate.** Recorded here with the mechanism spelled
out, because "force the bitrate" can mean either of two knobs that behave very differently.

- **`--max-bitrate` is the ceiling** (harness default 5,000,000; `cli.rs:375`). Both the 10 Sep
  ladder and the 17 Sep matrix passed it, so the cell's bitrate was always being *set*.
- **`LK_PIN_BITRATE_TO_MAX=1` raises the floor to meet it**, munging `x-google-min-bitrate` up to
  the configured maximum (`peer_transport.rs:382`, `munge_min_bitrate_to_max`). This is what
  "forcing" means operationally, and the source states why it exists: *"Congestion control
  normally hands the encoder `min(estimate, max_bitrate)`. When the estimate is the smaller term,
  the configured maximum stops being the independent variable — a bitrate sweep whose cells all
  get the same estimate is measuring the link, not the cells."* It also states the price:
  *"This deliberately disables the mechanism that keeps a sender inside the link's capacity… It is
  a measurement mode, not a control law."*
- **`--degradation locked`** stops the encoder trading resolution or frame rate away instead of
  bitrate, so the forced rate is delivered as forced *pixels at frame rate*.

So the pin is kept for precisely the reason it was written: an unpinned sweep measures the link.
Two things make this safer than 17 Sep. The approved bitrates are the ones that held — pinned
h264 at 2000k ran at **0.0%** retransmission and at 5000k completed the full 599 s at **4.4%**;
the catastrophic cells were the 8000k ones, which are cancelled. And with packet capture at both
ends, a cell that collapses now yields the packet-level reason instead of an unexplained gap.

**When a forced cell collapses, it must still produce data.** Yesterday four cells failed and left
almost nothing to analyse. Every forced cell therefore runs with:

- pcap armed *before* the epoch and stopped *after* it, at both ends, so the collapse itself is
  captured — the queue building, the retransmission burst, and the signalling timeout;
- `qdisc_backlog_pkts` and `rx_dropped` sampled throughout (already in `hops.csv`);
- every signalling `ping timeout` / resume / re-pin timestamped into the cell's timeline, since
  that sequence is the collapse signature;
- **no early abort.** A collapsed cell is a result. Let it run the full window and record it.

## Cells

Four cells, plus one repeat. Identical instrumentation on every one. **Bitrate forced
(`LK_PIN_BITRATE_TO_MAX=1` + `--max-bitrate` + `--degradation locked`) on every cell except the
explicitly unpinned control.**

1. `h264-2000k` — the known-good configuration; 17,395 frames last time.
2. `h264-2000k-repeat` — **a repeat, not a new bitrate.** We have no repeated cell in any
   campaign, so we cannot separate a codec or bitrate effect from ordinary cell-to-cell
   variation. Cell 2 of the last matrix differed from its neighbour by 3× on p99 for
   reasons unrelated to its label.
3. `h264-5000k` — the bufferbloat case, deliberately included now that the mechanism is
   understood. Expect the standing queue; the point is to observe it with packets.
4. `av1-2000k` — **forced, and no longer gated on anything.** There is no Host B AV1 bug; the
   gate this cell used to carry was based on a verdict I retracted (see below). This is the cell
   that failed at this exact pin on 17 Sep while h264 at the same pin was clean, so it is the
   campaign's most informative cell either way: if it collapses again, the paired captures say
   whether Host A's packets left, and that is the question the whole re-run exists to answer.
5. `h264-2000k-unpinned` — **the one unforced cell, kept deliberately.** It is the only
   operator-representative latency figure (every number we have is from a pinned publisher,
   which costs ~6–7 ms of decode on Host B and ~0.35 ms of encode on Host A), and it is the
   baseline that makes the four forced cells interpretable — without it, a forced cell's latency
   has nothing to be a delta *from*. If a slot is needed for another forced cell, this is the one
   to trade, and the cost is losing that baseline.

**No 8000k cells.** The link bufferbloats above ~5 Mbps and an 8000k pin measures the pin.

## What the 17 Sep AV1 failures actually were

Written 2026-09-17 after two wrong verdicts. Recorded here because the first version of this
document gated cell 4 on a Host B bug that does not exist.

**Host B's AV1 path is exonerated by direct evidence, not by elimination.**

- **Same binary both dates.** `target/release/subscriber` was built 2026-09-08 16:45, no
  webrtc-sys object was rebuilt after it, and no commit touched `webrtc-sys/` between 9 and
  18 Sep. So nothing on B's receive side changed between the working and failing runs.
- **All thirteen unpinned AV1 cells on 10 Sep delivered** — 4,150–4,272 frames received each,
  `decoder=dav1d`, across the full 200k–8000k ladder. That same binary decodes AV1 fine.
- **Nothing arrived on 17 Sep.** B's `wwan0` counters show the downlink **idle**: 4.9 MB over the
  692 s av1-2000k window ≈ 0.057 Mbps, the ICMP/signalling baseline. The working h264-2000k cell
  moved 188 MB (2.15 Mbps) over the same span. `received=0` was correct.
- **The AV1 packet trailer round-trips.** This fork splices the trailer into the AV1 *bitstream*
  as a metadata OBU (`packet_trailer_av1.cpp`, type 31), unlike H.264 which appends behind magic
  bytes. The 10 Sep cells attached the same handler and every row carries a non-zero `frame_id`.

**What the numbers show.** Media onset at Host B, measured on the wire:

| | h264 | AV1 |
|---|---|---|
| 2000k | +1.3 s | +43.7 s |
| 5000k | +1.3 s | +291.0 s |
| 8000k | +1.2 s | never |

and Host A's retransmission share tracks the **pin** on the h264 ladder — 2000k → 0.0%,
5000k → 4.4%, 8000k → 29.1% — with all three AV1 cells at 32–54%.

**The pin is the prime suspect and the codec path is cleared.** Every 17 Sep cell ran
`LK_PIN_BITRATE_TO_MAX=1` with `degradation=locked`; the 10 Sep ladder ran unpinned
(`--max-bitrate` only). av1-2000k's sequence: pin at 17:56:13 → `signal client closed: ping
timeout` 32 s later → 4.5 min failed resume → re-pin → second ping timeout. That is the
bufferbloat-to-signalling-timeout collapse already known from the 5000k case. **Why h264-2000k
tolerated the pin and av1-2000k did not is not established** — that is the open question, and it
is what paired packet capture is for. Not claimed as proven; claimed as the suspect.

## Two instruments that lied, and must not be trusted from the old runs

- **`probes_lost` / `distinct_seq_received`.** `probes_lost` reads **99.7% in the flawless
  h264-2000k cell** and `distinct_seq_received` is 0 in every cell: the control-path probe was
  dead for the whole campaign. It says nothing about the uplink. Fix it before the re-run or drop
  it from the reports — a broken instrument that reads plausibly is worse than no instrument.
- **`frame_width=0` on the first row is normal.** The healthy 10 Sep cells show it too. It is not
  a symptom, and I cited it as one.
- Also, `hops.csv` for av1-8000k contains a 259 Mbps sample, which is a counter/timing artefact.
  Never quote a peak throughput without checking it against the mean.

## Instrumentation, held identical across all cells

Instrument load itself moves latency: a bare publisher versus a publisher carrying eight
instruments differed by ~7 ms, which is the same order as the effects being measured. So
the configuration is fixed for the whole campaign, and no cell is compared against a cell
run with a different set.

- **Both hosts:** packet capture (headers only), full-mask DIAG, qdisc/driver counters,
  5 ms driver-counter sampler, 5 Hz ICMP to the SFU, per-cell SNTP clock offset measured
  by median of three servers.
- **Host B:** 5 ms `wwan0` interface counters, per-frame subscriber log, and now pcap.
- **Pacing:** ≥120 s lead per cell, ≥120 s between cells so the DIAG port lock clears.
- **Every instrument must be checked against a known-good cell before the campaign is trusted.**
  The control-path probe read 99.7% loss in a cell that was demonstrably perfect and nobody
  noticed for a week. One healthy cell, every instrument read, before the rest of the campaign.
- **Host B stays analysis-quiet during each cell**; reports generate between cells.

## Reports

Each cell gets the paired report plus a **radio panel**:

- **signal strength** — RSRP/RSRQ per serving cell and per beam, ~6 Hz, decoded from the
  DIAG capture with SCAT (free; verified working on our own `.dlf` files)
- **scheduling cadence** — inter-arrival of the UL scheduling-report code, measured from
  raw record timestamps (2.9 / 6.1 / 11.8 ms p10/p50/p90 in the one cell measured so far)
- **RRC events** as markers, including the `RRCReconfiguration` messages carrying the RLC
  `t-Reassembly` and PDCP `t-Reordering` values we have been guessing at all campaign
- all on the same timeline as camera-to-screen latency, with late frames ticked

## What this will still not answer

**Grant sizes.** Every free route is closed and was tested, not assumed:

- SCAT decodes ML1, RRC, MIB, serving cell and NAS — **no NR L2 uplink**. Its *LTE*
  parser does decode `grant`, `ul_tb`, `dl_tbs`, `bsr_event`; the NR equivalent has
  simply never been written. That makes a custom parser a bounded job against a working
  reference, not blind reverse-engineering.
- MobileInsight: v2 record layouts only; ours are v3.
- QCSuper → pcap → Wireshark NR MAC: QCSuper knows six log codes, all LTE/RRC-class.

So grants need **XCAL/XCAP or QCAT**, both vendor-licensed and Windows-only. Our captures
are already in the native format (`.dlf`), so nothing about capture needs to change — the
blocker is the tool. Worth asking Accuver or T-Mobile one question first: *can XCAP import
existing Qualcomm DLF captures taken with a third-party tool?* If yes, ~35 GB of captures
already taken become readable and no re-capture is needed.

Until a decoder exists, **do not capture DIAG on every cell** — it costs 6.4 GB a side to
record payloads nothing can read. Capture it on the cells whose radio behaviour actually
matters.
