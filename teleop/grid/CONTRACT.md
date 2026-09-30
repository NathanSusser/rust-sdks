# teleop.grid — the contract every module is written against

This file is the interface between the parts of `teleop/grid/`. Modules are built
in parallel; they agree on nothing except what is written here. If you need
something not listed, add it here first.

Python 3.12. `teleop/grid/{agent,preflight,capture,clock,hostcfg,stats,grid}.py`
must be **standard-library only** — they run on Host B, which has no numpy or
matplotlib. `reduce/`, `metrics.py` and `report/` run on Host A only and may use
numpy, matplotlib, and shell out to `tshark`.

## Hosts

Both hosts run the same checkout at the same commit. Each host has, outside the repo:

```
~/.config/teleop/sfu.env        TELEOP_SFU_HOST=<sfu hostname>          (already exists on both)
~/.config/teleop/host.yaml      see config/host.example.yaml
~/teleop-runs/                  results root (RESULTS_ROOT), never in git
```

`host.yaml` fields (hostcfg.load() returns a dict with exactly these keys):

| key | A | B |
|---|---|---|
| `role` | `a` | `b` |
| `peer` | `192.168.99.2` | `192.168.99.1` |
| `wwan_iface` | `wwan0` | `wwan0` |
| `ptp_iface` | `enp5s0` | `eno2` |
| `ptp_role` | `master` | `slave` |
| `modem_index` | `1` | `0` |
| `diag_tty` | `/dev/ttyUSB0` | `/dev/ttyUSB0` |
| `diag_venv_python` | path to the python that has qcsuper | same |
| `credentials_env` | `<repo>/.livekit-demo/.env` | `<repo>/.livekit-demo/.env` (B's own) |
| `tcpdump` | `sudo -n /usr/bin/tcpdump` | `/usr/bin/tcpdump` (setcap) |
| `results_root` | `~/teleop-runs` | `~/teleop-runs` |
| `display` | — | `:0` (the subscriber renders to screen) |

Credentials are never read by the orchestrator and never appear in any command
line, file or log the orchestrator writes. The agent on each host sources
`credentials_env` itself immediately before exec'ing the harness or subscriber.

## Grid file (`config/grids/*.yaml`)

```yaml
id: g0930a                      # short, unique; becomes the results directory name
description: codec x kbps, steady frames
control:                        # runs first and every `every` cells; thresholds abort the grid
  every: 8
  cell: {codec: h264, kbps: 2500, duration_s: 120}
  thresholds: {owd_p99_ms: 100, packets_lost: 0}
defaults:                       # any declared variable; overridden by the grid axes
  duration_s: 300
  repeats: 3
  fps: 30                       # 30, 25, 20, 15, 12, 10 (the 30 fps clip is decimated by ffmpeg -r)
  resolution: auto              # or WxH; `geometry` is a deprecated alias (warned, read as resolution)
  vbv_frames: 1
  padding: on
  target_quality: off
  intra_refresh: 0
  pin_bitrate: on
  clip: /home/nsusser/teleop-media/depal-face-lower-20260904/depal-face-lower-src-30s.mp4
  lead_s: 60
  cooldown_s: 30
  screenshots: 0                # 0 (off) .. 20 decoded frames sampled on B per cell; not in the label
axes:                           # cross product; OR `pairs:` a list of explicit dicts
  codec: [h264, av1]
  kbps: [512, 2500, 8000]
order: shuffle                  # shuffle | sequential ; seed recorded in grid.yaml copy
expect:                         # gates compare against these (optional)
  band: n41
```

**Rate model.** The operator sets `resolution`, `fps` and `bpp`; `kbps` is derived. Per
cell, after expansion (`grid.resolve_rate()`):

| resolution | bpp | kbps | result |
|---|---|---|---|
| `WxH` | set | — | `kbps = round(W*H*fps*bpp/1000)` |
| `WxH` | — | set | `bpp = kbps*1000/(W*H*fps)`, recorded |
| `WxH` | set | set | rejected: "set bpp or kbps, not both" |
| `WxH` | — | — | rejected |
| `auto` | optional (0.10) | set | W×H from `grid.derive_geometry()` (unchanged: keep 1600:1300, floor to a multiple of 16, cap at 1600×1300, floor at 160×128) |
| `auto` | any | — | rejected |

"Set" means given in `defaults`, `axes`/`pairs` or `control.cell`; neither variable has a
default in variables.yaml. A control cell that names exactly one of `kbps`/`bpp` replaces
the other one inherited from `defaults`. A fixed `WxH` has each side in 128..1920 and even;
a side not a multiple of 16, or an aspect more than 2% off the clip's 1600:1300 (the harness
scales/stretches the clip to W×H), is a warning in `Grid.warnings`, not an error. A derived
kbps outside 128..20000 rejects the grid. Every Cell's `values` carries the resolved
`resolution`, `width`, `height`, `kbps` and `bpp` (float).

`grid.expand()` yields `Cell` objects: one per (combination × repeat), ordered per
`order`, plus control cells inserted. Every declared variable (except the deprecated
`geometry`) has a value in every Cell (defaults filled; `kbps`/`bpp` resolved as above),
plus `width` and `height`. `Cell.label` is:

```
<id>-c<NN>-<codec>-<W>x<H>-<fps>fps-b<bpp>-<kbps>k-v<vbv>-p<0|1>-r<rep>
```

where W×H is the resolution the cell will request, `b<bpp>` is bpp to three decimals with
the dot dropped (0.100 → `b0100`, 0.04 → `b0040`) and `<kbps>` is the applied cap, e.g.
`bpp_sweep-c03-h264-1600x1300-30fps-b0040-2496k-v1-p1-r1`. `c<NN>` is the index in run
order (control cells are `x<NN>`). The label is the room name, the cell directory
name and the file prefix on both hosts.

`Cell.harness_env()` → dict of `LK_*` env for the publisher.
`Cell.harness_args()` → list of `teleop-harness` args (no url, no room, no output paths: the agent adds those).

## Cell directory — identical on both hosts

```
$RESULTS_ROOT/<grid-id>/
  grid.yaml                    expanded copy: resolved defaults, order, seed, git commit
  grid.log
  cells/<label>/
    manifest.json
    hosta/                     written by A's agent, mirrored to B
      <label>.pub.csv  <label>.jsonl  <label>.log  run.json
      <label>.wwan0.pcap  <label>.dlf  <label>.qcsuper.log  <label>.hops.csv
      clock.json  captures.json  SHA256SUMS
    hostb/                     written by B's agent, mirrored to A
      subscriber.csv  subscriber.log
      frames-qp.csv            decoder per-frame log (LK_DECODER_FRAME_LOG); absent from an older subscriber
      frames/index.csv  frames/<frame_id:08>.i420     only when screenshots > 0 (~3 MB each at 1600x1300)
      <label>.wwan0.pcap  <label>.dlf  <label>.qcsuper.log  <label>.hops.csv
      clock.json  captures.json  SHA256SUMS
    reduced/                   A only
      dlf-rates-a.csv  dlf-rates-b.csv  band-a.csv  band-b.csv
      frames.csv  seconds.csv  spikes.csv
      screens.csv  screens/<frame_id>.png                only when hostb/frames/ exists
    metrics.json
    report.pdf  report.html
  comparison/
    metrics.csv  comparison.html  comparison.pdf
```

`clock.json`: `{"host_minus_utc_s": -15.8, "measured_at": <unix>, "servers": 3, "spread_ms": 7.9}`.
`captures.json`: per capture `{"pcap": {"path","pid","started","closed","bytes"}, "dlf": {...}, "hops": {...}}`.
`SHA256SUMS`: `sha256  bytes  relative-path` for every file in that host dir, written on close.
`run.json` (written by the harness — until it does, the agent writes it from the harness log):
`{"encoder_implementation","width","height","fps","max_bitrate_bps","codec","started_at"}`.

## manifest.json

```json
{
  "label": "...", "grid_id": "...", "index": 7, "kind": "cell|control", "repeat": 2,
  "variables": {codec, kbps, fps, resolution, width, height, bpp, vbv_frames, padding,
                target_quality, intra_refresh, pin_bitrate, duration_s, clip, lead_s,
                screenshots, sample_every (only when screenshots > 0)},
  "requested": {"width": 1008, "height": 816},
  "negotiated": {"width", "height", "encoder_implementation", "codec"},
  "epoch": 1790384410,                       # host-clock unix second the publisher fired
  "epoch_iso": "2026-09-26T01:00:10Z",
  "commit": "8cafb496", "harness_sha256": "...",
  "clock": {"a": <clock.json>, "b": <clock.json>},
  "ptp": {"a": {"state": "MASTER"}, "b": {"state": "SLAVE", "servo_lines_30s": 30, "offset_ns": 2176}},
  "band": {"a": {"band","arfcn","pci","share"}, "b": {...}},   # filled by reduce
  "gates": {"a": [{"name","pass","detail"}], "b": [...]},
  "status": "OK|SKIPPED|INCOMPLETE|ABORTED",
  "status_reason": "",
  "timeline": {"preflight","armed_b","armed_a","published","closed","mirrored","reduced","reported"},
  "excluded_from_comparison": false, "exclusion_reason": ""
}
```

## metrics.json

Every distribution is a `Summary`:
`{"mean","p50","p95","p99","max","min","n"}` from `stats.summ(values)`.
Empty input → all null, n=0. Never omit a field.

```
config      variables, requested, negotiated, epoch, commit, clock offsets, ptp, band (both), gates_passed (bool), encoder_is_nvenc (bool)
frame       size_kb:Summary, packets_per_frame:Summary, fps_delivered (float), frames_captured, frames_encoded, frames_sent,
            frames_received, frames_rendered, dropped_pre_encode, dropped_post_encode, keyframes, size_spread_pct
rate        bytes_per_s_a:Summary, packets_per_s_a:Summary, bytes_per_s_b:Summary, packets_per_s_b:Summary,
            kbps_target, kbps_achieved, padding_bytes_share
encoder     qp:Summary (higher is worse), qp_per_frame:Summary, encode_ms:Summary, quality_limitation_s {none,bandwidth,cpu,other}
latency     app_to_wire_a, emission_a, in_flight, arrival_b, wire_to_app_b, decode, render, e2e : Summary each
            owd:Summary (packetize -> webrtc_receive)
jitter      owd_sd_ms (float), interarrival_rfc3550_ms:Summary, frame_interval_b_ms:Summary
tail        owd_over_100 {count, share}, owd_over_150 {count, share}, e2e_over_100 {...}, e2e_over_150 {...},
            episodes: [{start_s, end_s, n, max_ms, dominant_segment}]
network     packets_lost (int), loss_events (int), nacks_sfu_to_a, nacks_b_to_sfu, duplicates_a,
            in_flight_packets:Summary, path_mtu
modem       a {activity_index, x19ef_records, kernel_queue_max_bytes, rsrp_dbm:Summary, snr_db:Summary},
            b {...}
integrity   captures_complete (bool), mirror_verified (bool), reduce_resyncs (int), ptp_locked (bool),
            excluded (bool), reasons [str]
```

Segment definitions (all ms, per frame, joined by RTP timestamp and frame order as in
`reduce/segjoin.py`): `app_to_wire_a` = first packet leaves A's kernel − packetize;
`emission_a` = last − first packet leaves A; `in_flight` = last packet arrives B − last
leaves A; `arrival_b` = last − first arrives B; `wire_to_app_b` = webrtc_receive − last
arrives B; `decode` and `render` from subscriber.csv; `e2e` = `e2e_to_gpu_complete_ms`.

## Entry points

```
teleop.grid.stats.summ(values: Iterable[float]) -> dict                 # the only Summary implementation
teleop.grid.hostcfg.load() -> dict                                       # ~/.config/teleop/host.yaml, validated
teleop.grid.grid.load(path) -> Grid ; Grid.expand() -> list[Cell] ; Grid.write_expanded(dir)
teleop.grid.clock.measure() -> dict                                      # clock.json content (NTP median, stdlib only)
teleop.grid.capture.Captures(cfg, cell_dir, label).arm(span_s) / status() -> dict / close() -> dict
teleop.grid.preflight.run(cfg, cell, role) -> list[dict{name,pass,detail}]
teleop.grid.agent           CLI: python -m teleop.grid.agent <cmd> [--json args]  -> JSON on stdout, exit 0/1
    cmds: identity | preflight | arm | status | publish | close | checksums | stop | pull
teleop.grid.orchestrator.run_grid(grid_path, *, resume=False) ; stop_grid(grid_id)
teleop.grid.reduce.reduce_cell(cell_dir) -> None                         # writes reduced/, band into manifest
teleop.grid.metrics.build(cell_dir) -> dict                              # writes metrics.json
teleop.grid.report.cell.render(cell_dir) -> Path                         # report.pdf + report.html
teleop.grid.report.grid.render(grid_dir) -> Path                         # comparison/
teleop.grid.cli                CLI `python -m teleop.grid.cli` == `teleop`:
    grid new|check|run|status|stop|report ; cell reduce|metrics|report <cell_dir> ; agent ...
```

Agent transport: the orchestrator runs `ssh -o BatchMode=yes <peer> 'cd <repo> && python3 -m teleop.grid.agent <cmd> --json <base64 json>'`
and parses one JSON object from stdout. Every agent command is idempotent and
takes `--cell-dir` and `--label`. Long-running processes started by `arm` and
`publish` are detached (`setsid`), with pid and start time recorded in
`captures.json` / `run.json`; `stop` kills only pids recorded there, by pid and
verified command line, never by pattern.

The subscriber on B is `target/release/subscriber` from `examples/local_video`
(it renders to the screen; keep `DISPLAY` from host.yaml, unset `WAYLAND_DISPLAY`).
The publisher on A is `target/release/teleop-harness`. Both take `--url wss://$TELEOP_SFU_HOST`.
Reference invocations: `archive/teleop-test-matrix-scripts/publish-cell.sh` and
`archive/local_video-scripts/tools/run-cell-b.sh`.

## Reference implementations to port (read, do not import)

| new module | ported from |
|---|---|
| capture/diag.py | archive/diag-capture/capture-around-cell.sh (A), archive/hostb-diag-capture/capture.sh (B), qcsuper-noroot-fast2, diag-log-off |
| capture/pcap.py | hop-recorder.sh (pcap part), archive/hostb-diag-capture/pcap.sh |
| capture/hops.py | archive/diag-capture/hop-recorder.sh, archive/hostb-diag-capture/hop-recorder-b.sh |
| clock.py | archive/diag-capture/clock-offset.py |
| preflight.py | archive/diag-capture/paired-cell.sh (guards), preflight-check.sh, run-cell-b.sh pre-flight |
| reduce/dlf_rates.py, dlf_records.py | archive/diag-capture/dlf-rates.py, dlf_records.py |
| reduce/ml1.py | archive/diag-capture/ml1-ca.py |
| reduce/segjoin.py, frames.py | /home/nsusser/teleop-share-20260925/tools/{segjoin.py,proof.py,framesize-check.py} |
| report/cell.py | archive/local_video-scripts/generate_frame_report.py (keep its four pages, add the new ones) |
| report/grid.py | new |

## Reduced tables — columns (added by report)

<!-- added by report. The canonical names are the ones reduce/frames.py already writes
     (FRAME_COLS, SECONDS_COLS, SPIKE_COLS); this section records which of them the report
     reads, and the few OPTIONAL extra columns the report uses when present. report/cell.py
     also accepts the `_ms`-suffixed spellings (owd_ms, size_bytes, t_s in seconds.csv, ...),
     see ALIASES in report/cell.py. A missing optional column is tolerated: the chart that
     needs it says "no data" and the page is still emitted. -->

Time base everywhere: seconds since the manifest `epoch` (host clock; A and B share it
when PTP is locked). Empty string = not measured. Booleans `0`/`1`. Values may be negative
(before the publisher fired).

`reduced/frames.csv` — one row per published frame. Report reads:
`t_s, frame_id, bytes, packets_a, packets_b, received, rendered, app_to_wire_a, emission_a,
in_flight, arrival_b, wire_to_app_b, owd, e2e, decode, render, encode_ms` (ms, no suffix).
Optional, used when present: `keyframe` (0/1; keyframes drawn in a second colour on the
frame-size page), `qp` (per-frame QP), `capture_to_packetize_ms`.

`reduced/seconds.csv` — one row per whole second. Report reads:
`t, a_bytes, a_packets, b_bytes, b_packets, qp` (qp = Δqp_sum/Δframes_encoded).
Optional: `padding_bytes_a` (bytes of padding within a_bytes), `target_kbps` (encoder
target from A's jsonl, drawn as a step line on the rate page).

`reduced/spikes.csv` — every frame with owd > 100 ms. Report reads:
`t, owd, dominant, kind, bytes, size_ratio`. Optional: `frame_id`, `e2e`, `keyframe`.
`kind` is printed as-is. If spikes.csv is absent the report derives the rows from
frames.csv (dominant = the largest segment in ms; kind = `key`/`large`/`path`/`host`).

`reduced/dlf-rates-{a,b}.csv` — the dlf-rates.py format: a leading `#` line with
`probe_start=<unix s>` (or `probe_start_ms=`) and `host_minus_utc_s=`; then
`second_rel_probe,code,count`. The report plots `probe_start + second_rel_probe − epoch`.
A `t_s` column instead of `second_rel_probe` is also accepted.

`reduced/band-{a,b}.csv` — report reads `band, arfcn, pci` per row and shows each
distinct combination with its share of rows.

`metrics.json` — Summaries may appear where the schema says scalar (e.g.
`modem.a.activity_index`); the report shows p50/p99/max for those. `rate.padding_note`
(string) is shown when `padding_bytes_share` is null.

`comparison/metrics.csv` (written by report/grid.py) columns, in order:
`grid_id,label,index,repeat,kind,status,excluded,<every variable name, sorted>,metric_path,statistic,value`.
`metric_path` is the dotted path in metrics.json (`latency.owd`, `tail.owd_over_100.share`,
`modem.a.rsrp_dbm`); `statistic` is one of `mean,p50,p95,p99,max,min,n` for a Summary
and `value` for a numeric scalar (booleans as 0/1; null kept as an empty value). The
`config` group, strings and lists (`tail.episodes`, `integrity.reasons`) are not
flattened. `excluded` is 1 when the manifest or metrics.integrity excludes the cell, its
status is not `OK`, or it has no metrics.json.

## Added by orchestration (integration notes)
- The harness writes `run.json` atomically via `<path>.tmp` + rename. Mirror/pull must exclude `*.tmp`.
- `--run-json <path>` is the harness flag; the A agent passes `hosta/run.json` and only parses the harness log as a fallback when the file is absent after the first stats poll.

## Added by reduce

<!-- added by reduce. Additive only: nothing above is renamed or removed. -->

**Extra reduced files.** `reduced/episodes.csv` (`start_s,end_s,n,max_ms,dominant_segment,kinds`),
`reduced/inflight.csv` (`t,in_flight`, 10 Hz over [0, duration)), and `reduced/reduce.json`
(scalars metrics.py needs: join diagnostics, totals, encoder counters, DLF stats, `missing`,
`notes`). `dlf-rates-{a,b}.csv` / `band-{a,b}.csv` are written only when that host's DLF exists;
absence means "no modem capture", never "no activity".

**frames.csv** also carries `receive_us, rtp_ts, capture_to_receive, capture_to_packetize_ms,
size_ratio, jitter_rfc3550, interval_b, packets_lost`. **seconds.csv** columns are
`reduce/frames.py SECONDS_COLS` (includes `target_kbps`, `modem_a/b` = per-second activity
index, `x19ef_a/b`, `kq_a_bytes_max`, `kq_b_pkts_max`, `lost_delta`, `nacks_*`, `dup_*`,
`inflight_max`, `rsrp/snr_a/b`). **band-{a,b}.csv**: one row per (ML1 record, carrier):
`modem_ts,t_rel_epoch_s,carrier(pcell|scell1..),block_off,pci,arfcn,band,band_raw,brsrp_dbm`.
`band` resolves raster overlaps to the deployed superset (n2/n25 -> n25, n38/n41 -> n41);
`band_raw` keeps the overlap.

**manifest.json** (written by reduce): `band.{a,b}` = `{band, arfcn, pci, share, scell}` where
`scell` is the same shape for the first secondary carrier or null; a host without a DLF gets
`null`. `integrity.reduce_resyncs` = pub<->A-wire join resyncs + DLF reader resyncs (A + B);
`integrity.reduce` = `{missing, notes, segjoin_shift, order_agreement, join_resyncs, dlf_resyncs:{a,b}}`.
`timeline.reduced` is set.

**Definitions that the schema left open.**
- `owd` is packetize -> webrtc_receive as specified. The legacy paired report's "owd" was
  capture -> webrtc_receive (subscriber `exposure_to_receive_ms`), 1.4-2.8 ms larger (it
  includes encode). It is kept as `latency.capture_to_receive` and
  `tail.capture_to_receive_over_{100,150}` so old numbers stay comparable.
- The pub <-> A-wire order join (segjoin's shift rule) is verified per frame against the
  capture clock (RTP ticks ~= capture_us x 0.09 + K, tolerance 135 ticks: RTP timestamps here
  are quantised to ~1 ms). Rows that disagree are re-joined by timestamp; each run of such
  rows is one join resync.
- `frame.dropped_post_encode` counts published frames not received inside B's receive span
  (first..last received frame); frames before the subscriber joined are not drops.
  `frame.frames_captured` = published + `dropped_pre_encode` (sum of frame_id_gap - 1).
- `rate.*_per_s_*` Summaries cover seconds from the first second a video packet was seen on
  that wire to the end of the cell. `rate.kbps_achieved` = A-wire video UDP bytes in [0, duration)
  x 8 / duration. `rate.padding_note` (string) is always present.
- `encoder.qp` is per-second Δqp_sum/Δframes_encoded from A's jsonl. For AV1 this is the
  0-255 q-index scale, not 0-51; compare QP only within a codec. `encoder.implementation` added.
- `jitter.interarrival_rfc3550_ms` is RFC 3550 §6.4.1 computed per frame with S = packetize (A)
  and R = webrtc_receive (B), i.e. J += (|Δowd| - J)/16; the Summary is over the running J.
  `jitter.frame_interval_b_ms` = consecutive webrtc_receive intervals.
- `network.nacks_sfu_to_a` / `nacks_b_to_sfu` count RTPFB fmt 1 whose RTCP header is in the
  clear. B's RTCP to the SFU is compound SRTCP starting with RR (everything after the first
  header encrypted), so `nacks_b_to_sfu` is **null** unless B's pcap shows dissectable RTCP
  toward the SFU. Added `network.duplicates_b` and `network.in_flight_clean` (true when
  packets_lost = 0). `in_flight_packets` counts only frames seen on both wires (frames sent
  before B subscribed would otherwise sit in the difference forever).
- `modem.{a,b}.activity_index` is a **Summary** of the per-second index (ELEV-code records per
  second / the cell's own steady mean over t >= 15 s) over [0, duration). Added
  `activity_index_first15` (mean index over t in [0,15) — the startup-transient ratio of the spike
  ledger), `activity_steady_per_s`, `kernel_queue_max_pkts` (B's hops has packets, not bytes),
  `dlf_present`. `x19ef_records` counts 0x19EF records in [0, duration).
- `tail.spike_kinds` = count per spikes.csv `kind`. Kinds: `transient` (t < 15 s, in_flight
  dominant, normal-size frame, while A's activity index >= 2 or A's kernel queue is non-empty —
  the pattern the spike ledger labelled by hand), `bigframe` (emission-side segment dominant
  and size_ratio >= 2), `inflight`, `bhost` (wire_to_app_b), `other`, `unjoined` (no wire
  segments). Episodes also carry `kinds`.
- `config` adds `encoder_implementation, label, grid_id, status`; `config.gates_passed` is
  null when no gate results were recorded; `config.clock_offsets = {a, b}` (host_minus_utc_s).
- `integrity.reasons` also lists `note: ...` lines from reduce (informational, not exclusions).
  `integrity.excluded` = manifest `excluded_from_comparison` or status != OK.
- `padding_bytes_share` is null (filler NALs / AV1 padding are invisible through SRTP) unless
  `variables.padding` is off, in which case it is 0.0.

**Legacy cells.** `python3 -m teleop.grid.tools.import_legacy <old results/<label>> <root>`
hard-links an old cell into `<root>/cells/<label>/` with a manifest (`grid_id: "legacy"`,
`integrity.legacy_import: true`, `legacy.notes`). Offsets: A from the dlf-rates.csv header,
B from the last "clock offset measured" line of timeline.txt; when A has none, B's value is
used (PTP-locked hosts) and the manifest says so.

## Added by control-plane

<!-- added by control-plane (hostcfg, grid, clock, preflight, capture/, agent, orchestrator, cli).
     Additive: nothing above is renamed or removed. -->

**host.yaml.** Required keys are the table above **plus `repo`** (the checkout path, already in
`host.example.yaml`). `display` must be present; it may be empty on A and must be an X display
(`:0`) on B. Optional `peer_repo` (the checkout path on the peer, default = `repo`). Unknown keys
are rejected. `hostcfg.sfu_host()` reads `TELEOP_SFU_HOST` from sfu.env and never falls back.
`TELEOP_HOST_YAML` / `TELEOP_SFU_ENV` override the two paths (tests only).

**Grid expansion (details the schema left open).**
- `id`: `^[a-z0-9][a-z0-9_]{0,23}$` — no `-`, which separates label fields.
- A variable that has no default in variables.yaml and no value in the grid is an error, except
  `fps`, which falls back to 30 (ARCHITECTURE §4).
- `c<NN>`/`x<NN>` is the 0-based position in run order, zero-padded to at least 2 digits (wider
  when there are ≥ 100 cells). A control cell's `r<n>` is its occurrence number (1, 2, ...).
- A control cell runs at index 0 and after every `every` regular cells, including after the
  last block when that block is full.
- Order: rounds by repeat (every combination's r1, then every r2, ...); `shuffle` shuffles each
  round with `random.Random(seed)`. `seed:` in the grid file fixes it; otherwise one is drawn and
  recorded in the expanded `grid.yaml`. `grid run --resume` re-expands with the recorded seed and
  refuses if the labels differ.
- `control.thresholds` keys: `owd_p99_ms` (fails when `latency.owd.p99 >=` it) and `packets_lost`
  (fails when `network.packets_lost >` it). `expect` keys: `band` (`n41`), `arfcn`, `pci`.
- `Cell.to_dict()` = `{index, label, kind, repeat, variables, requested, span_s, harness_args,
  harness_env}`; `span_s = lead_s + duration_s + 30` is the capture window.
- The expanded `grid.yaml` holds `id, description, source, expanded_at, git_commit, order, seed,
  defaults, expect, control, definition` (the original file) and `cells` (without harness args/env).

**Per-host files besides those listed above** (all inside `hosta/` or `hostb/`, so they are in
SHA256SUMS and mirrored):
`<label>.tcpdump.log`, `<label>.hops.log`, `<label>.diag-supervisor.log`, `<label>.diag.json`
(DIAG supervisor status: attempts, early exits, `partial`, `log_off {ok, rc, output}`,
`crc_dropped`, `unmatched`), `process.json` (the publisher/subscriber the agent started: pid,
start ticks, cmdline signature, argv, cell env — never a credential — plus `checks` on A, see
below), `fired.json` (`{fired_at, epoch, pid}`, written by the launcher at the instant it fired).
B has `hostb/process.json` and `hostb/fired.json` for the subscriber; B has no `run.json`.

**A DLF that was restarted or died mid-window is renamed `<label>-PARTIAL.dlf`**;
`captures.json` then has `dlf.partial: true` and `dlf.path` pointing at it, and the cell is
INCOMPLETE. Reduce should glob `<label>*.dlf`.

**captures.json** per capture: `path, pid, start_ticks, started, closed, bytes, span_s, deadline,
signature, still_running`, plus `log`/`argv` (pcap, hops), `status`/`qcsuper_log` (dlf) and after
close `partial, log_off, crc_dropped, unmatched` (dlf). Top level: `label, role, span_s, armed_at,
armed, arm_errors, closed_at, complete, close_notes`. `complete` is false if any capture is
missing, still running, empty, partial, or diag-log-off did not confirm the modem went quiet.

**hops.csv columns** (a column a host cannot read is absent, never zero-filled):
- A: `unix_ms,qdisc_sent_pkts,qdisc_dropped,qdisc_overlimits,qdisc_requeues,qdisc_backlog_bytes,
  qdisc_backlog_pkts,sys_tx_packets,sys_tx_dropped,sys_tx_errors,qmi_tx_ok,qmi_tx_dropped,qmi_rx_ok,
  qmi_rx_dropped,nr_rsrp_dbm,nr_rsrq_db,nr_snr_db`
- B: `unix_ms,qdisc_sent_pkts,qdisc_dropped,qdisc_backlog_pkts,sys_rx_packets,sys_rx_dropped,
  sys_rx_errors,sys_rx_missed,sys_tx_packets,sys_tx_dropped,udp_sock_max_rxq,udp_sock_drops,
  udp_indatagrams,udp_inerrors,udp_rcvbuferrors,nr_rsrp_dbm,nr_rsrq_db,nr_snr_db`
Radio columns come from `mmcli -J --signal-get`, 5G section only (polling armed with
`--signal-setup=5` at the start of the recording); empty cells mean "not read", not zero.

**run.json.** The harness owns it (`--run-json`, atomic). The A agent passes `--run-json` only
when the built binary's `--help` lists it. At close the agent writes run.json itself only if the
harness did not (`"source": "agent"`), otherwise it fills keys the harness left null and lists them
in `filled_by_agent`. Cross-checks derived from the jsonl and log go to `process.json.checks`:
`codec_matches, clip_matches, stale_participants_at_join, frame_size_cap_line,
fps_delivered_median, encoder_tier, camera_source`.

**clock.json** also carries `asked, answered, rtts_ms, warnings`; `host_minus_utc_s` is null when
no server answered (never guessed).

**Agent.** `--cell-dir` may be relative to that host's `results_root`
(`<grid-id>/cells/<label>`); its last path component must equal `--label`. Every reply is one JSON
object `{cmd, role, label, ok, ...}` with `error` when `ok` is false; exit status 1 when not ok.
`pull` args: `{remote_dir, peer_role}`; it rsyncs (excluding `*.tmp`) into
`<cell>/host<peer_role>/` and returns `verified {ok, problems, files}`. `identity` returns
`commit, teleop_dirty, harness_sha256, subscriber_sha256, requirements_sha256, package_sha256`
(hash of `teleop/grid/**/*.py` except tests, plus `teleop/config/*.yaml`), `python, pyyaml,
results_root, repo`. `_launch` is internal (the detached at-epoch launcher).

**Gates** (`preflight.run` → list of `{name, pass, detail[, data]}`). A: `binary, credentials, ptp,
clock_offset, diag_port, capture_leftovers, disk, modem, sfu, clip, room, encoder, link_mtu,
cpu_governor`. B: `binary, credentials, ptp, clock_offset, diag_port, capture_leftovers, disk,
modem, decoder_b, display, cpu_governor`. The orchestrator prepends `code_match` to A's list, and
on a skip after pre-flight appends a pseudo-gate (`arm_a`, `arm_b`, `subscriber`, `lead_time`).
`room` is always pass with `data.skipped: true` until the harness can list participants.
`cpu_governor` passes with a `WARN` detail unless `gates.yaml` sets `cpu_governor.strict: true`.
The modem gate fails on a band config that excludes `expect.band` (readable without privilege);
the live serving band/ARFCN/PCI is checked only where qmicli is permitted, else the detail says
`UNVERIFIED`. `ptp` data: `state, servo_lines_30s, offset_ns, carrier, speed_mbps`.

**manifest.json additions:** `steps` (`{<step>: {ok, at, detail}}` for preflight, arm_a, arm_b,
subscriber, publish, live, publisher_exit, close, checksums, mirror), `epoch_fired_at` (float, from
fired.json; > 1 s from `epoch` is an INCOMPLETE "ANCHOR MISMATCH"), `captures {a, b}` (close
results), `modem_preflight {a, b}`, `integrity {captures_complete, mirror_verified, problems}`.
`epoch` is the planned integer second; `clock.{a,b}` is the pre-flight measurement, replaced by the
arm-time clock.json when that succeeds.

**Grid directory:** `state.json` (`status: running|paused|stopped|aborted|refused|done`, `current`),
`STOP` (written by `grid stop`; checked between steps and every 10 s during a cell), `PAUSED`
(reason, after two consecutive SKIPPED cells share a failing gate; `grid run --resume` continues).
`cells/<label>/postprocess-errors.log` collects tracebacks from reduce/metrics/report.

**CLI.** `grid check --local-only` checks this host only (no ssh). `grid report <id> --cells`
re-renders every cell report. `grid status <id> --json`.

**Runtime dependency on archive/ (temporary).** `capture/diag.py` runs
`archive/diag-capture/qcsuper-noroot-fast2` and `archive/diag-capture/diag-log-off` with
`diag_venv_python` (they need pyserial/crcmod/qcsuper, which only that venv has). The DIAG lock is
the legacy one: `~/diag-capture/.<tty>.lock` when `~/diag-capture` exists, else beside the
archived scripts; `DIAG_LOCK` overrides. Move both scripts into `teleop/grid/capture/` to retire
this.

## Added by screenshots

<!-- added by screenshots (grid, agent, reduce/screens.py, metrics, report). Additive only. -->

**Variable `screenshots`** (int 0..20, default 0 = off; META: never in the label or the harness
args). When > 0 every Cell's `values` also carries `sample_every = max(1, floor(fps*duration_s/screenshots))`
(`grid.sample_every()`; 25 fps x 300 s / 6 = 1250). `Cell.subscriber_args()` =
`{duration_s, screenshots, sample_every}` is what the orchestrator passes to B's `publish`.

**B's subscriber** (`agent.subscriber_command()`): env `LK_DECODER_FRAME_LOG=<cell>/hostb/frames-qp.csv`
ALWAYS (a subscriber that predates it ignores the variable); argv adds
`--sample-frames-dir <cell>/hostb/frames --sample-every <sample_every>` only when screenshots > 0.
Both land inside `hostb/`, so they are in SHA256SUMS and the mirror like every other file
(6 frames x ~3 MB at 1600x1300 per cell).

**hostb files.**
- `frames-qp.csv`: `rtp_timestamp,frame_id,capture_timestamp_us,qp,width,height,decode_ms,codec,implementation`,
  one row per decoded frame. `qp` may be empty (h265). Every consumer treats the file as optional.
- `frames/index.csv`: `frame_id,capture_timestamp_us,width,height,stride_y,stride_u,stride_v,bytes_written`;
  `frames/<frame_id:08>.i420` = Y, U, V planes back to back at those strides. Sampling is by frame
  ID (`frame_id % sample_every == 0`), so a lost sampled frame is a hole, not a renumbering.

**reduced/ (reduce/screens.py, run last in `reduce_cell`; a failure is recorded in
`reduce.json["screens"]` and never fails the cell).**
- `screens/<frame_id>.png`: I420 -> RGB, BT.601 limited range, chroma repeated 2x2. The index
  strides are used when they account for the file size, else one padded luma stride is inferred.
- `screens.csv`: `frame_id, t_s, png, width, height, qp, qp_join, bytes, owd, e2e`. `t_s` =
  capture_timestamp_us/1e6 - epoch; `png` is relative to `reduced/`; `qp` joined from frames-qp.csv
  by frame_id, else the nearest capture timestamp within 15 ms, else an exact RTP timestamp
  (`qp_join` = `frame_id|capture|rtp|` empty); `bytes` (A wire), `owd`, `e2e` from frames.csv by frame_id.
- `frames.csv` gains a trailing `qp` column (same join) when frames-qp.csv exists; without it the
  column is absent and reduce.json notes why.

**metrics.json.** `encoder.qp_per_frame`: Summary over every frames-qp.csv `qp` (n=0 when the file
is absent or qp is empty); `encoder.qp` (per second, A's stats) is unchanged. Per-frame QP is the
bitstream's: H.264/H.265 0-51, AV1 q-index 0-255. `config.screenshots` = rows in screens.csv (PNGs
written; 0 when none).

**Reports.** The cell report adds "Screenshots" (2 x 3 per page, each captioned frame_id, t, QP,
frame kB, one-way ms; the HTML embeds the PNGs base64 at <= 640 px wide) when screens.csv exists,
and "QP per frame" (QP vs time with p50/p95/p99/max, histogram; the codec's QP scale in the
subtitle) when per-frame QP exists. `report/grid.py` adds `encoder.qp_per_frame` to the KPIs of
record after `encoder.qp`; comparison/metrics.csv always carries its rows (empty when a cell's
metrics.json predates it).
