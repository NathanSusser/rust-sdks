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

## Cells

Four cells, plus one repeat. Identical instrumentation on every one.

1. `h264-2000k` — the known-good configuration; 17,395 frames last time.
2. `h264-2000k-repeat` — **a repeat, not a new bitrate.** We have no repeated cell in any
   campaign, so we cannot separate a codec or bitrate effect from ordinary cell-to-cell
   variation. Cell 2 of the last matrix differed from its neighbour by 3× on p99 for
   reasons unrelated to its label.
3. `h264-5000k` — the bufferbloat case, deliberately included now that the mechanism is
   understood. Expect the standing queue; the point is to observe it with packets.
4. `av1-2000k` — **only after the AV1 receive bug is fixed.** Otherwise it produces
   3 frames of 16,372 sent, as it did today.
5. `h264-2000k-unpinned` — the operator-representative figure. Every latency number we
   have is from a pinned publisher, which we measured as costing ~6–7 ms of decode on
   Host B and ~0.35 ms of encode on Host A.

**No 8000k cells.** The link bufferbloats above ~5 Mbps and an 8000k pin measures the pin.

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
