# Teleop E2E test system — solution architecture

Status: proposal, 2026-09-30. Nothing in this document is built yet.

## 1. What this replaces

Today a test is hand-typed on each host, results land in ad-hoc folders under
`results/` on A and `~/teleop/cells/` on B, pairing the two sides is a manual
copy, and comparison across runs is a table someone writes in chat. The tooling
is spread over five directories on A (123 files) and an untracked `~/diag-capture`
on B, and the two hosts have drifted: B's checkout is behind A's and carries 17
local edits.

Failures in the last week that this design is built to make impossible:

| failure | cause |
|---|---|
| A and B ran different labels; labels disagreed with the cap (`vbv-512kbps` ran at 2500 k) | labels typed by hand, twice |
| PTP silently unlocked while pre-flight said "locked" | pre-flight checked servo state, not carrier or fresh servo lines |
| A had no modem capture for a cell | an earlier recorder still held the DIAG port |
| B's capture copied while still being written | collection did not wait for capture close |
| a run went to a retired SFU with the wrong clip | unset variable, silent fallback |
| B's modem band-locked to n25 under the tests | another session changed persistent modem state |
| paired report missing A's modem page | A's reduction never produced, B could not know |
| A's clock offset to UTC jumped 16 s → 24 s | no NTP; offset only measured, never checked between cells |

## 2. Requirements

1. **Define a grid quickly.** A grid is a set of variables and their values; a
   cell is one combination and one repeat. Defined in one file, in a form, or
   by Claude in chat. Never one script per cell.
2. **Coordinated execution.** One trigger runs every cell on both hosts in
   lockstep, unattended, with identical auto-generated labels and pre-flight
   gates before each cell. A failed cell is marked and skipped, never mixed in.
3. **Organized, comparable results.** Same layout on both hosts, a manifest per
   cell of exactly what ran, pairing automatic, and the output a comparison of
   metrics against variables — not a folder of PDFs.
4. **Richer metrics, tail-first.** Every metric reported as mean, median (p50),
   p95, p99, max, min, n. Frame size, packets per frame and per second, fps
   delivered, drops before and after the encoder, QP, encode and decode time,
   latency by segment, jitter, loss, retransmits, modem and radio context.

KPIs of record: **quality (QP), latency, jitter**, judged at p95 / p99 / max.

Constraints kept: API keys never leave A. Measurement data never enters git;
the repo is public, so no hostnames or modem identifiers in code or docs. The
operator starts every grid. Each host's agent acts only on its own machine.
Raw captures are never deleted automatically.

## 3. One folder

Everything lives in **`teleop/`** at the repository root. Both hosts run the
same checkout at the same commit; the orchestrator refuses to start a grid
unless the commit, the harness binary hash and the Python package version match
on A and B.

```
teleop/
  ARCHITECTURE.md              this document
  README.md                    how to run a grid in five lines
  harness/                     Rust crate (moved from teleop-test-matrix/)
                               one binary: `teleop-harness publish|subscribe`
  grid/                        Python package `teleop` (the control plane)
    cli.py                     `teleop grid new|check|run|status|stop|report`
    grid.py                    grid file schema, expansion, label generation
    orchestrator.py            runs on A; drives both hosts through a cell
    agent.py                   runs on B (and A); one command per call over SSH
    preflight.py               the gates, each a function returning pass/fail+why
    capture/                   diag.py pcap.py hops.py clock.py — replace the shell
    reduce/                    dlf_rates.py ml1.py segjoin.py frames.py — raw → tables
    metrics.py                 tables → metrics.json (the canonical schema, §7)
    report/                    cell.py (per-cell PDF/HTML), grid.py (comparison)
    ui/                        local web UI served from A (§9)
  config/
    variables.yaml             every variable, its type, range and how it is applied
    gates.yaml                 pre-flight thresholds
    grids/                     grid definitions (small YAML files, tracked)
  docs/
    RUNBOOK.md  METRICS.md  (moved and rewritten as the code stabilises)
  requirements.txt             pinned; identical venv on both hosts
```

**`archive/`** at the repository root receives everything else, moved with
`git mv` so history is kept: `diag-capture/`, `teleop-test-matrix/scripts/`
and `docs/`, `examples/local_video/scripts/` and `scripts/tools/`. Nothing in
`archive/` is on any path or called by anything in `teleop/`. B's untracked
`~/diag-capture` is retired once `teleop/grid/capture` covers it; its scripts
are copied into `archive/hostb-diag-capture/` first so nothing is lost.

Results live **outside the repo**, in the same place on both hosts:

```
~/teleop-runs/<grid-id>/
  grid.yaml                    the definition, as expanded, with the git commit
  grid.log
  cells/<label>/
    manifest.json              variables, negotiated geometry, epoch, offsets, gates, status
    hosta/                     A's raw + reduced files (same names on both hosts)
    hostb/                     B's raw + reduced files
    metrics.json               §7
    report.pdf  report.html
  comparison/                  §8 — regenerated after every cell
```

`hosta/` and `hostb/` are written by their own host and mirrored to the other
after the cell closes, with a sha256 manifest checked on arrival. Each host
keeps its own copy; nothing is moved.

## 4. Variables

Declared once in `config/variables.yaml`; the grid file may only use names
declared there. Each entry says how the variable is applied (harness flag,
encoder env, geometry rule) so a cell cannot be mislabelled relative to what
ran — the label is *generated from* the applied values.

| variable | values | applied via |
|---|---|---|
| `codec` | h264, av1 (h265 admitted, not tuned) | `--codec` |
| `resolution` | `auto` (from kbps at `bpp`), or `WxH` (128..1920 per side); `geometry` is a deprecated alias | `--width --height` |
| `fps` | 30 (or 25, 20, 15, 12, 10) | `--fps` |
| `bpp` | 0.01..0.5; derives kbps on a fixed resolution (0.10 for `auto`) | kbps / geometry rule |
| `kbps` | integer; optional, derived from resolution x fps x bpp | `--max-bitrate`, `LK_MAX_START_BITRATE_KBPS` |
| `vbv_frames` | 1, 5 | `LK_NVENC_VBV_FRAMES` |
| `padding` | on, off | `LK_NVENC_FILLER` |
| `target_quality` | off, or QP 1–51 | `LK_NVENC_TARGET_QUALITY` |
| `intra_refresh` | 0 or frames | `LK_NVENC_INTRA_REFRESH_FRAMES` |
| `pin_bitrate` | on, off (GCC on) | `LK_PIN_BITRATE_TO_MAX` |
| `duration_s` | 300 default | `--duration-s` |
| `clip` | path | `--camera-source` |
| `repeats` | 3 default | expansion |

Network-side state (band lock, carrier) is **not a grid variable**: it is
persistent modem state and changes under other sessions. It is *recorded* per
cell from the DIAG log (band, ARFCN, PCI on both hosts) and gated in pre-flight
(§6): a cell whose measured band differs from the grid's declared expectation
fails its gate.

Label: `<grid-id>-c<NN>-<codec>-<kbps>k-<WxH>-v<vbv>-p<0|1>-r<rep>`, e.g.
`g0930a-c07-av1-2500k-1008x816-v1-p1-r2`. It is the room name, the directory
name, and the file prefix on both hosts, and it is written by the orchestrator,
never typed.

Expansion: full cross product unless the grid file says `pairs:` (explicit
list). Cell order is **interleaved across repeats and shuffled with a recorded
seed**, so time-of-day and radio drift do not align with one variable. A
**control cell** (a fixed reference configuration) runs first and after every
N cells; a control that fails its own thresholds stops the grid.

## 5. Cell lifecycle

One cell, driven by the orchestrator on A. Every step has a timeout and writes
its outcome into `manifest.json`.

```
 0  match      commit + binary hash + venv equal on A and B, else refuse the grid
 1  preflight  both hosts in parallel (§6); any FAIL → cell status=SKIPPED, reason recorded
 2  arm B      agent: start pcap, DIAG, hops; start subscriber (joins room=label); report armed
 3  arm A      start pcap, DIAG, hops; record clock offset; compute epoch = now + lead
 4  publish    at epoch, harness publish; manifest gets the epoch actually fired
 5  run        duration_s; orchestrator polls both agents (subscriber alive, captures growing)
 6  close      A stops publish; B agent stops subscriber, runs diag-log-off, waits for pcap
              and DLF to stop growing; both agents write hosta/ or hostb/ sha256 manifests
 7  mirror     A pulls hostb/ over the cable and verifies; A pushes hosta/ to B and verifies
 8  reduce     on A only (§7): DLF rates + band read for both hosts, per-frame join, tables
 9  report     metrics.json, per-cell report, comparison regenerated
10  cooldown   configurable gap; then the next cell
```

Control channel: SSH from A to B over the PTP cable, `BatchMode`, one
`teleop agent <cmd>` per call; no daemon on B. Long-running processes on B
(subscriber, captures) are started detached with a pidfile under the cell
directory; `agent status` reads them. Credentials: B's subscriber uses B's own
local config; the orchestrator never carries a key or secret in a command,
environment or file. `agent stop` acts only on processes recorded in that cell's
pidfiles — never a bare `pkill`.

Failure handling: a cell is `OK`, `SKIPPED` (gate), `INCOMPLETE` (ran, but a
capture or the mirror failed — kept, flagged, excluded from comparison by
default) or `ABORTED` (operator stop). The grid continues past `SKIPPED` and
`INCOMPLETE`. Two consecutive `SKIPPED` for the same gate pauses the grid and
notifies, since the cause is environmental.

## 6. Pre-flight gates

Each gate is a function with a threshold in `config/gates.yaml`; each result is
stored in the manifest so a report can say which gates a cell passed.

| gate | host | pass condition |
|---|---|---|
| code match | both | same commit, harness hash, requirements hash |
| PTP | B | cable carrier up at 1 Gbps; ptp4l in SLAVE with ≥ N servo lines in the last 30 s; `pmc` offset < 10 µs |
| PTP | A | ptp4l MASTER on the cable, port not FAULTY |
| clock offset | both | host-minus-UTC measured now, recorded; warn if changed > 1 s since last cell |
| DIAG port free | both | no lock file, no process on the DIAG tty |
| capture leftovers | both | no recorder, tcpdump or qcsuper from a previous label alive |
| disk | both | free space > 3 × expected cell size |
| modem | both | attached, 5G NR, band/ARFCN/PCI read live and equal to the grid's expectation |
| SFU | A | `TELEOP_SFU_HOST` set, resolves, HTTPS reachable over wwan0 |
| clip | A | exists, matches the grid |
| room | A | no stale participant already in the room (or the operator's `--evict`) |
| encoder | A | NVENC available (driver loaded), else fail — never fall back to software |
| B decoder | B | can decode the cell's codec (AV1 check before an AV1 cell) |
| link MTU | A | path MTU to the SFU ≥ 1280 (records the value) |

## 7. Reduction and the metrics schema

Reduction runs on A after the mirror, from both hosts' raw files. Inputs and
what each yields:

| input | yields |
|---|---|
| A `pub.csv` | capture→packetize per frame, encode ms, frame ids, drops before encode |
| A `wwan0` pcap | per-frame bytes and packet count, per-second bytes/packets, emission spread, NACKs from the SFU, retransmits |
| B `wwan0` pcap | per-frame arrival spread, per-second arrival, in-flight (with A's) |
| B `subscriber.csv` | receive, decode, render timestamps, `packets_lost`, freezes |
| A/B `.jsonl` (WebRTC stats) | encoder implementation, QP sum, frames encoded/sent, resolution, delivered fps, quality-limitation reason |
| A/B DLF | band/ARFCN/PCI per record, per-second record rates by code (modem activity index) |
| A/B `hops.csv` | kernel queue, drops, requeues, RSRP/SNR |
| B `timeline.txt`, A recorder log | PTP state, clock offsets, capture windows |

`metrics.json` is the one file every report reads. Every distribution metric
carries the same seven fields:

```json
{"mean": 41.8, "p50": 41.8, "p95": 61.0, "p99": 95.1, "max": 233.8, "min": 22.0, "n": 8649}
```

Groups:

- `config` — the variables, the negotiated geometry, encoder implementation
  actually used, epoch, clock offsets, PTP lock state, band/ARFCN/PCI on both
  hosts, gates passed. (A cell whose `encoder_implementation` is not NVENC is
  flagged, whatever the grid said.)
- `frame` — size kB, packets per frame, delivered fps, frames captured /
  encoded / sent / received / rendered, drops before encode, drops after,
  keyframes, spread (sd/mean).
- `rate` — bytes/s and packets/s on A's wire and B's wire, target kbps vs
  achieved, padding share (bytes in filler NALs).
- `encoder` — QP (mean, p50, p95, p99, max — higher is worse), encode ms,
  quality-limitation seconds by reason.
- `latency` — per segment: `app_to_wire_A`, `emission_A`, `in_flight`,
  `arrival_B`, `wire_to_app_B`, `decode`, `render`, `e2e`; and `owd`
  (packetize → receive) with **jitter** as both sd and RFC 3550 interarrival.
- `tail` — for owd and e2e: count and share > 100 ms and > 150 ms, episodes
  (start, end, n, max, dominant segment).
- `network` — packets lost at B, loss events, NACKs SFU→A, NACKs B→SFU,
  duplicates, in-flight packets, path MTU.
- `modem` — per host: activity index in the cell vs its own steady state,
  0x19EF presence, kernel queue max, RSRP/SNR mean and min.
- `integrity` — captures complete, mirror verified, reduction resyncs, any
  reason the cell is excluded from comparison.

## 8. Reports

**Per cell** (`report.pdf`, `report.html`): the current paired report's four
pages, plus: frame size and packets-per-frame over time; bytes/s and packets/s
on both wires; QP over time; a segment breakdown of every frame over 100 ms;
and a header block stating gates, PTP state, band on both hosts and the
encoder actually used.

**Grid comparison** (`comparison/`), regenerated after every cell so it is
useful while a grid is still running:

- `metrics.csv` — long format: one row per cell × metric × statistic, with the
  variables as columns. Loads into anything.
- `comparison.html` — interactive: pick a KPI and a statistic (default p99),
  choose the x variable and a facet variable, and see one panel per facet with
  one point per cell and repeats shown individually; a table beneath with
  mean/p50/p95/p99/max per cell; excluded cells listed with reasons.
- `comparison.pdf` — the same as fixed pages: one page per KPI of record (QP,
  owd, e2e, jitter, frame size, fps delivered, loss), each showing the tail
  statistics against every variable in the grid.

Repeats are never averaged away silently: the table shows each repeat and a
summary row; the plot shows the spread.

## 9. Interfaces

- **CLI** (`teleop …`) is the only path into the system; the UI and Claude both
  call it. `grid new` writes a grid file from flags or a template; `grid check`
  expands it and runs pre-flight without running cells; `grid run` runs it;
  `grid status` and `grid stop`; `grid report` regenerates reports.
- **Web UI**, served locally on A: Grid (form → grid file, or paste YAML),
  Run (per-cell progress, gate results, live log tail, stop button), Results
  (the comparison page, drill-down into any cell, downloads). Plain Python
  standard library server plus static pages, no framework, so it runs on B too.
- **Claude in chat**: writes the grid file, runs `grid check`, reports what will
  happen, and — only when the operator says go — runs `grid run` and watches it.

## 10. What changes in the harness

- `teleop-harness` gains a `subscribe` subcommand so B runs the same binary as
  A; the `examples/local_video` subscriber logic moves into `teleop/harness`.
  Until then B keeps its current subscriber and the agent wraps it.
- The harness writes its negotiated geometry, encoder implementation and
  bitrate cap into a small `run.json` at start, so the manifest is filled from
  what ran rather than from what was asked.
- libwebrtc's own log messages reach Rust through one log target, `libwebrtc`,
  so there is no per-encoder module to raise to `info`. The sink used to forward
  everything at `debug`; it now forwards libwebrtc's warning and error severities
  as `warn` and `error`, and the NVENC encoders log their "frame-size cap" and
  "rate control" lines at warning. Those lines are therefore in the log at the
  default `RUST_LOG=warn`, without the volume of full info logging.

## 11. Migration

Each phase leaves a working system.

| phase | work | done when |
|---|---|---|
| 0 | create `teleop/`; `git mv` the five directories to `archive/`; move the crate to `teleop/harness`; fix workspace `Cargo.toml`; B pulls, builds, gets the venv; B's `~/diag-capture` copied to `archive/hostb-diag-capture/` | both hosts at one commit, harness builds on both, a manual cell still runs from `archive/` scripts |
| 1 | `grid.py`, `orchestrator.py`, `agent.py`, `preflight.py`, `capture/` (port of the shell) | a one-cell grid runs end to end on both hosts from one command, with gates and mirror |
| 2 | `reduce/`, `metrics.py`, per-cell report | `metrics.json` and `report.pdf` for every cell, with the new metrics |
| 3 | comparison report, `metrics.csv`, `comparison.html`; UI | a 2×2×3 grid runs unattended and the comparison page answers a variable question |
| 4 | `subscribe` in the harness; retire B's local scripts and the `archive/` shell for good | B runs nothing that is not in `teleop/` |

Phase 0 and 1 are the ones that remove the manual work; 2 and 3 are the
reporting you asked for; 4 is cleanup.

## 12. Open decisions

1. **Repeats and duration defaults**: proposed 3 × 300 s per combination. A
   2 × 4 × 2 grid at that is 48 cells ≈ 5 hours plus cool-down.
2. **Storage**: a 300 s cell is ~2 GB per host (DLFs). 48 cells ≈ 100 GB on
   each host. A has room; B has ~50 GB free and needs either a prune policy for
   *B's mirrored copy of A's files* (never raw captures) or a larger disk.
3. **Control cell definition**: proposed `h264 2500k auto-geometry vbv1
   padding-on 120 s`, thresholds owd p99 < 100 ms and loss = 0.
4. **UI**: build it in phase 3 as proposed, or drive everything from the CLI
   and Claude and skip the UI for now.
