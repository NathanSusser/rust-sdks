# Re-run design: finding the bitrate breakpoint, and attributing it

Written 2026-09-17 after the codec×bitrate matrix produced one usable cell in six.

Two operator questions, both verbatim, and the design serves them in this order:

1. *"I want to pin it so I know when the network has issues at what bitrate. then I can
   configure based on that bitrate."* — the **capacity-breakpoint sweep**, below.
2. *"confirm whether network is dropping packets or is taking too long etc or if its on the
   modem or host side."* — the **attribution** that paired packet capture provides, which is
   what turns a breakpoint into a *reason* for the breakpoint.

**One warning that governs how the result gets used.** A breakpoint from a single sweep is a
sample of one hour on one link, not a constant. Host A has documented recurring sub-2 Mbps
episodes, and the archive's own note is that capacity figures here are *samples, not levels*.
So the sweep tells you where the knee was **today**; configuring at that exact rate leaves no
margin for the hours when the link is worse. Configure below the highest clean rate, and re-run
the ladder at a different time of day before treating any number as a setting.

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

**Both hosts can capture, by two different mechanisms. Neither needs anything from the operator.**

- **Host B: file capabilities.** `getcap /usr/bin/tcpdump` → `cap_net_admin,cap_net_raw=eip`,
  granted 2026-09-17 (see below). No sudo involved, and sudo here needs a password anyway.
- **Host A: scoped NOPASSWD sudo.** `sudo -n -l` → `(root) NOPASSWD: /usr/bin/tcpdump,
  /usr/bin/qmicli, /usr/sbin/nft, /usr/bin/mmcli`. Its `getcap` is empty and always will be;
  it captured 43 MB of real packets per cell on 17 Sep this way.

**So capability must be probed three ways, and the trap is symmetrical.** This cost real time
twice in one day, in mirror image:

| probe | wrong on | why |
|---|---|---|
| `sudo -n tcpdump --version` | **Host B** | sudo needs a password there; the binary needs no sudo |
| `sudo -n true` | **Host A** | NOPASSWD is granted *per command*, so a blanket probe fails |

Each host tested the other with the probe suited to its own mechanism, and both concluded the
other was incapable. A correct gate tries **scoped sudo, then file capabilities, then a
one-packet probe**, and reports incapable only if all three fail. `hop-recorder.sh` on Host A
has been fixed to do exactly that.

**A paired run must never proceed half-instrumented.** The attribution table below needs both
ends; a run with one end capturing would look complete and answer nothing. So each side probes
*itself* before the epoch and the run **refuses, or marks every affected cell single-ended**.

To be precise about the history, because an earlier version of this paragraph got it wrong:
B's inability was **real, not a measurement artefact**. Its `tcpdump` genuinely carried no
capabilities until the `setcap` above was run on 2026-09-17, so the premise in "Why the last
campaigns could not attribute anything" was true when written.

The file metadata settles it, and is recorded here because this claim was stated three different
ways in one day and memory clearly is not enough:

```
$ stat /usr/bin/tcpdump
Modify: 2025-09-10 08:28:48   <- packaged binary, contents never altered
Change: 2026-09-17 11:55:25   <- capability granted, today
```

`setcap` rewrites a security xattr, which bumps `ctime` while leaving `mtime` alone — so that
pair *is* the signature of the grant, and it lands **393 seconds after** commit `33224fc`, the
one that listed this `setcap` as prerequisite #1. Before 11:55:25 on 2026-09-17, B could not
capture. Host A's long-standing comment gating B to h264 cells was correct at the time. The `sudo -n` gate is a
*forward-looking* bug: now that B has the capability, that gate would still report it as
incapable and silently skip B's capture. Both things are worth fixing; only one of them explains
the past.

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

## The goal: find the bitrate where the network starts having issues

**Operator's purpose, 2026-09-17, verbatim: *"I want to pin it so I know when the network has
issues at what bitrate. then I can configure based on that bitrate."*** This is a
**capacity-breakpoint sweep**, not a latency comparison, and it changes the cell list.

The pin is the correct instrument for it, and the only one: an unpinned sender backs off before
it stresses the link, so it can never show you where the link breaks. Forcing the rate is what
makes the link answer.

**The knee is already bracketed between 2 and 5 Mbps**, so that is where the steps go:

| h264, 17 Sep | retx | e2e p99 | verdict |
|---|---|---|---|
| 2000k | 0.0% | 94.5 ms | clean |
| 5000k | 4.4% | 565 ms | degraded, but completed 599 s |
| 8000k | 29.1% | — | collapsed |

Steps at the edges of that range tell us nothing new. Steps *inside* it locate the knee.

### What counts as "the network has issues"

Stated numerically so the sweep produces an actionable bitrate rather than a pile of numbers.
Thresholds are anchored on the 17 Sep clean cell, not invented. A cell is:

- **CLEAN** — retx < 0.5%, qdisc backlog 0, media onset < 2 s, p99 within 1.5× the anchor's p99.
- **QUEUEING** (the knee — this is the number you want) — retx ≥ 0.5%, **or** any sustained
  qdisc backlog, **or** p99 ≥ 1.5× anchor. The link is now carrying the rate by *delaying* it.
- **FAILING** — retx ≥ 10%, media onset > 5 s, or any frame loss at Host B.
- **COLLAPSED** — any signalling `ping timeout` or resume. Hard failure; the rate is unusable.

**Configure below the highest CLEAN rate, not at the QUEUEING rate.** A queueing link still
delivers video, which is exactly why it is dangerous: it looks like it works and it is silently
spending hundreds of milliseconds of latency to do so.

## Cells: an anchored ladder

**Forced (`LK_PIN_BITRATE_TO_MAX=1` + `--max-bitrate` + `--degradation locked`) on every cell
except the final unforced control.** 300 s per cell, ≥120 s between cells.

### Why the same 2000k cell is run four times

**Plain version: we re-run one test we already know is good, at intervals, to prove the link
didn't change underneath us while we were sweeping.**

The ladder takes about 90 minutes. Suppose we ran 2000k, 2500k, 3000k … straight upward and
4000k came back bad. There would be two possible explanations and no way to choose between them:

1. **4000k is past what the link can carry** — the answer we're after; or
2. **the link simply got worse** in the 50 minutes since we started — nothing to do with bitrate.

Host A's uplink does exactly that: it drops below 2 Mbps for stretches and then recovers, seen
on four separate dates. So capacity here is a *sample, not a level*.

The fix is a yardstick. 2000k is the rate we already know runs clean (0.0% retransmission), so
we re-run **that same cell** after every few ladder steps and read it like a spirit level:

- **all four anchors come back the same** → the link held steady for the whole 90 minutes, so
  differences between the ladder steps really are caused by bitrate. The sweep is valid.
- **an anchor comes back degraded** → the link itself changed at that moment. The ladder steps
  run near it are **void** and get re-run later, rather than being published as a breakpoint
  that is really just a clock reading.

This is not a theoretical worry — it is precisely the mistake made on 17 Sep. The h264 cells ran
first and passed, the AV1 cells ran later and failed, and I concluded AV1 was broken. It wasn't.
One 2000k anchor immediately before the AV1 cells would have caught that in minutes instead of
producing two retracted verdicts.

So: drop ladder steps to save time. **Never drop an anchor** — without them the sweep cannot
tell a bitrate limit from a bad half-hour.

| # | cell | purpose |
|---|---|---|
| 1 | `h264-2000k-anchor-a` | reference; establishes the CLEAN baseline for every threshold above |
| 2 | `h264-2500k` | ladder |
| 3 | `h264-3000k` | ladder |
| 4 | `h264-2000k-anchor-b` | **time control** — did the link change, or did the bitrate? |
| 5 | `h264-3500k` | ladder |
| 6 | `h264-4000k` | ladder |
| 7 | `h264-2000k-anchor-c` | **time control** |
| 8 | `h264-4500k` | ladder |
| 9 | `h264-5000k` | the known-degraded rate; confirms the ladder reproduces 17 Sep |
| 10 | `h264-2000k-anchor-d` | **time control**, closes the ladder |
| 11 | `av1-2000k` | the breakpoint is **codec-dependent** — this exact pin collapsed AV1 on 17 Sep while h264 was clean at it. Configuring off an h264-only number would be wrong for an AV1 stream. |
| 12 | `av1-3000k` | second AV1 point; two points give a direction, one gives nothing |
| 13 | `h264-2000k-unpinned` | operator-representative latency, and a final check the link is still healthy |

≈13 cells × 420 s ≈ **91 minutes**. Drop cells 2, 5 or 8 to shorten — never an anchor.

**No 8000k cells.** Cancelled by the operator, and 29.1% retx already tells us the answer.

**300 s, not 600 s.** Retx share and p99 stabilise within a minute; the 17 Sep collapse announced
itself 32 s after the pin. But `av1-5000k`'s media onset was +291 s, so **any cell that collapses
or shows a late onset is re-run at 600 s** to characterise it properly.

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

### The ladder curve is the deliverable

A sweep's answer lives *across* cells, not inside any one of them. Thirteen per-cell PDFs do not
answer "at what bitrate", so the campaign's primary output is one **ladder page**:

- x-axis **forced bitrate**; one line each for **retx %**, **e2e p99**, **media onset**, and
  **peak qdisc backlog**, each on its own panel rather than a shared scale (they differ by orders
  of magnitude, and a dual-axis chart would misread).
- every **anchor cell plotted at 2000k in run order**, so anchor drift is visible as spread at a
  single x position. Anchors that separate are the signal that the ladder measured the hour.
- each cell annotated **CLEAN / QUEUEING / FAILING / COLLAPSED** per the thresholds above, and
  the **highest CLEAN rate called out as the headline number** — that is the configuration input.
- h264 and AV1 as separate series, since the breakpoint is codec-dependent.
- cells voided by anchor drift drawn but struck through, never silently dropped.

### Per cell

Each cell also gets the paired report plus a **radio panel**:

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
