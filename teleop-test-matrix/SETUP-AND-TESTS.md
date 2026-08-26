# Teleoperation Test Matrix — Setup and Test Guide

A practical guide to what this harness is, what you need to run it, and what
each tier unlocks. Start at Tier 0 — it works on a bare MacBook and is how you
find out whether any of this is wired up correctly before touching netem or
hardware you don't have yet.

---

## What this is

The matrix answers six open questions from the Teleoperation PRD (Confluence
556974310), plus a zeroth validation gate that must run first. It works by
sweeping network conditions and LiveKit-specific settings (codec, encoder,
buffering mode, control transport), running a real session against a LiveKit
SFU with `teleop-harness`, and scoring the session's own stats against
thresholds in `matrix.yaml`.

| Piece | What it does |
|---|---|
| `docs/MEASUREMENT-DESIGN.md` | Why each metric is measured the way it is. Read this before changing scoring logic. |
| `matrix.yaml` | Axes, thresholds, suites. Change a threshold here and nowhere else. |
| `run_matrix.py` | Expands the matrix, applies shaping (Tier 1+), runs the harness, writes run records. |
| `src/` (`teleop-harness` binary) | Rust session client. Publishes video + control + audio, samples stats, writes one JSON snapshot per poll interval. |
| `parse_runs.py` | Reads run records + snapshots, scores against `matrix.yaml`, writes a markdown report. |

All three tools are usable from clone: `run_matrix.py --plan` and `--dry-run`
need no network access at all, and Tier 0 needs only `.livekit-demo`
credentials and a MacBook.

---

## Before you start: the clock-sync precondition

Read this before anything else, because it changes what a Tier 0 run can
prove. **Five of the six question-answering suites — T-1, T-2, T-3, T-4, and
Q-7 — score every run INVALID if the harness's clock-sync confidence is
`none`.** Only T-5 (availability) survives without it.

This isn't a harness bug — it's `MEASUREMENT-DESIGN.md` §1g rule (i), computed
mechanically: a suite invalidates on unsynchronized clocks whenever its
`primary` metric set includes anything derived from the OWD/theta
calibration (`control_late_pct`, `control_owd_p50_ms`, `control_owd_p99_ms`,
`g2g_p50_ms`, `g2g_p99_ms`). Those five suites all carry at least one of those
as a **blocking** metric, and a blocking threshold can't be evaluated against
a null value. RTT itself is clock-skew-immune (four-timestamp probe, doesn't
need external sync) and stays valid regardless — it's specifically the
one-way and glass-to-glass numbers that need it.

**What this means for Tier 0 on a bare MacBook:** you have no `chrony`/PTP and
no root, so you cannot achieve external clock sync. The harness's probe-based
fallback still runs and reports a confidence level — check it on the per-poll
`probe` object in the snapshot file (`clock_sync_confidence` field; it is
**not** on the terminal `run_metadata` record — see §4 below for the exact
command). If it reads anything other than `none`, the five gated suites will score normally
(with the usual OWD residual caveat). If it reads `none`, expect T-1, T-2,
T-3, T-4, and Q-7 to come back INVALID/`clock_unsynchronized` — that is
correct scoring, not a broken harness, and it's exactly the situation this
runbook's smoke test below is designed to surface early rather than let you
discover after burning hours on a full sweep.

---

## Tier 0 — MacBook, live SFU, no shaping

This is not a dry run. It exercises session lifecycle, codec negotiation and
fallback, encoder-tier detection, bitrate/fps/resolution control,
glass-to-glass timing, zero-jitter buffering, control transport, and the full
stats pipeline against a real LiveKit Cloud room. Every axis except the
netem-only ones (`loss_pct`, `owd_ms`, `jitter_ms`, `uplink_mbps`,
`concurrency` beyond 1, most of T-5's fault injection) is live here.

### 1. Prerequisites

- LiveKit Cloud credentials in `.livekit-demo/.env`:
  ```bash
  cp .livekit-demo/env.example .livekit-demo/.env
  # then edit .livekit-demo/.env and fill in LIVEKIT_URL, LIVEKIT_API_KEY,
  # LIVEKIT_API_SECRET from the LiveKit Cloud dashboard (Settings -> Keys)
  ```
  If you're inside the corporate network, the TLS proxy re-signs outbound
  HTTPS, so `export SSL_CERT_FILE="$(pwd)/.livekit-demo/corp-ca.pem"` before
  anything else, from the repo root — this is required, not optional, and a
  failure here looks like a LiveKit connectivity bug if you skip it.
- Rust toolchain able to build this workspace (`cargo --version`).
- Python 3.10+ with no extra packages for `run_matrix.py`, `parse_runs.py`,
  and `fixtures/make_fixtures.py` — all three are stdlib-only by design.
  Running the harness's own test suite (§"Regenerating fixtures" below) does
  need `pytest` (`pip install pytest`); it isn't required for anything else
  in this runbook.

### 2. Build the harness

```bash
cd rust-sdks
export SSL_CERT_FILE="$(pwd)/.livekit-demo/corp-ca.pem"   # corporate network only
cargo build --release -p teleop-test-matrix
ls -la target/release/teleop-harness
```

If this doesn't produce a binary, stop here — nothing downstream will work.
`cargo test -p teleop-test-matrix` should also be green; it's the harness's
own unit-test gate and doesn't touch the network.

### 3. See what Tier 0 can run before running anything

```bash
cd teleop-test-matrix
python3 run_matrix.py --plan --tier0
```

This prints per-suite run counts, a wall-time estimate, and the Tier 0 subset
separately from the full matrix — no network access needed. Expect
`T1_video_floor`, `T2_loss_collapse`, and `Q7_latency_definition` to
contribute Tier 0 runs — 69 in total across 23 cells and 3 process batches.
T-2's shaping-free subset is small and easy to miss — it's just the required
`video_codec=av1` cell held at `loss_pct=0` (`matrix.yaml`'s `required_cells`
never get filtered out even though the rest of T-2 is `tier0: false`), so
don't be surprised by a handful of T-2 runs in the Tier 0 plan.
`T3_jitter_tolerance`, `T4_capacity`, and `T5_availability` contribute
nothing at Tier 0 (they need `jitter_ms`, multi-host concurrency, or
link-manipulation fault injection — all Tier 1+).

There is **no validation gate**. `V0_playout_units` and the playout-delay
units question it settled are retired along with the room-level playout-delay
hint modes; `buffering_mode` is locked to `zero_jitter` for every cell. See
the amendment at the top of `docs/MEASUREMENT-DESIGN.md` for the measurements
and the LiveKit docs behind that. If `--plan` ever prints a `VALIDATION GATE`
section again, a gate has been re-added and must be run before anything
else is scored.

```bash
python3 run_matrix.py --dry-run --tier0
```

This prints the exact `teleop-harness` CLI invocation for every Tier 0 run,
still with no network access. Use it to sanity-check flags before spending
real session time. Note the process-batching banner:
`enable_zero_playout_delay` is process-global in the Rust SDK (it can't be
toggled after the LiveKit runtime initializes), so `buffering_mode` groups
runs into separate `teleop-harness` process invocations — one process per
`(suite, buffering_mode)` pair, never mixed. Since every cell is now
`zero_jitter`, that's one process per suite, but the grouping is still
enforced rather than assumed. This is why `--plan` reports a `procs` column
distinct from the run count. Note also that **no `--playout-delay-*` flags
are emitted**: `zero_jitter` is applied via `enable_zero_playout_delay`
before runtime init, not through the room API.

### 4. Live smoke test — confirm the harness actually works

Run one cell for real before trusting a sweep. `Q7_latency_definition`'s
h264 cell is a good first choice: it needs no shaping and exercises the
session, stats and G2G pipeline end to end. The cell's real `duration_s` is 120 (`matrix.yaml`'s default); the
invocation below shortens that to 60s purely to make the first smoke test
faster — that's a runbook convenience, not a matrix value, so don't read
`matrix.yaml` looking for "60".

First, a zero-network sanity check on the invocation itself — `--validate-args`
parses every flag and exits without touching the network, so a typo or a CLI
contract drift is caught before you burn session time on it. Still from
`teleop-test-matrix/` (§3's `cd`), so the binary is one level up:

```bash
../target/release/teleop-harness --validate-args \
  --url "$LIVEKIT_URL" --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --room-name teleop-smoke-0 --duration-s 60 \
  --codec h264 --encoder auto --width 1920 --height 1080 --fps 30 --max-bitrate 5000000 \
  --attach-timestamp --attach-frame-id \
  --buffering-mode zero_jitter \
  --control-transport data_track_buf1 --control-rate-hz 200 --control-buffer-size 1 \
  --stats-poll-hz 1 --video-poll-hz 1 --warmup-s 15 --poll-overbudget-multiplier 1.5 \
  --concurrency 1 \
  --snapshots-out snapshots/smoke-0.jsonl --publisher-seq-log snapshots/smoke-0.seq.jsonl \
  --audio --audio-source synthetic_tone --audio-bitrate 250000
```

Should print `ok: ...` and exit 0. Then run it for real — same flags, minus
`--validate-args`, from the repo root (**not** `teleop-test-matrix/`, since
the binary path below is relative to the workspace target directory):

```bash
cd ..   # back to repo root if you followed §3's `cd teleop-test-matrix`
mkdir -p teleop-test-matrix/runs teleop-test-matrix/snapshots
set -a; source .livekit-demo/.env; set +a
./target/release/teleop-harness \
  --url "$LIVEKIT_URL" --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --room-name teleop-smoke-0 --duration-s 60 \
  --codec h264 --encoder auto --width 1920 --height 1080 --fps 30 --max-bitrate 5000000 \
  --attach-timestamp --attach-frame-id \
  --buffering-mode zero_jitter \
  --control-transport data_track_buf1 --control-rate-hz 200 --control-buffer-size 1 \
  --stats-poll-hz 1 --video-poll-hz 1 --warmup-s 15 --poll-overbudget-multiplier 1.5 \
  --concurrency 1 \
  --snapshots-out teleop-test-matrix/snapshots/smoke-0.jsonl \
  --publisher-seq-log teleop-test-matrix/snapshots/smoke-0.seq.jsonl \
  --audio --audio-source synthetic_tone --audio-bitrate 250000
echo "exit: $?"
```

This invocation self-hosts both ends: the harness process joins the room
twice internally (publisher + subscriber participants under the same
process, since G2G, control delivery, and RTT calibration all need a
joinable send/receive pair). Exit code should be 0; the process runs for
roughly the full 60s (it isn't instant — if it returns in under a few
seconds, that's a connection or auth failure, not a fast success).

Then inspect what actually ran, from the repo root:

```bash
tail -1 teleop-test-matrix/snapshots/smoke-0.jsonl | python3 -m json.tool
```

The last line of any snapshot file is the `run_metadata` record — its
absence means the harness never reached the end of the run (scored as
`session_lost_mid_run` by `parse_runs.py`). Check specifically:

- `negotiated_codec` vs `requested_codec` — confirm they match. AV1 has **no
  fallback path**: a failed AV1 publish is a hard error, not a silent
  substitution to another codec. H264/VP8/VP9 also shouldn't silently
  diverge in this matrix (H265 is deliberately excluded from `matrix.yaml`
  entirely, since it's the one codec with automatic fallback to H264 and
  would silently mislabel a cell).
- `encoder_tier` — on Apple Silicon this should read `videotoolbox` for
  H264/H265 or `sw` for AV1 (VideoToolbox has no AV1 encoder). This is the
  single most important field to check before trusting any AV1 latency or
  CPU number: software AV1 on a MacBook is not representative of NVENC or
  Jetson hardware, and every report generated from Tier 0/1 data must carry
  that caveat forward.
- `scored_window_start_unix_us` / `_end_unix_us` — spans exactly
  `duration_s - warmup_s` seconds by construction; their presence at all
  confirms the run completed rather than being cut short.

`clock_sync_confidence` is **not** on this record — it's per-poll, on the
`probe` object of every non-terminal line:

```bash
grep -o '"clock_sync_confidence": *"[a-z]*"' teleop-test-matrix/snapshots/smoke-0.jsonl | tail -1
```

See the precondition section above for what to do with the result.

### 5. Run a real Tier 0 cell through `run_matrix.py` and score it

From `teleop-test-matrix/` (`cd teleop-test-matrix` from repo root if step 4
left you elsewhere):

```bash
python3 run_matrix.py --run --suite Q7_latency_definition --tier0 \
  --harness ../target/release/teleop-harness --url "$LIVEKIT_URL" \
  --clock-source none --path cloud
```

`--harness` is relative to `teleop-test-matrix/`, one level up from the
workspace `target/` directory — get this wrong and `run_matrix.py` currently
surfaces it as an unhandled `FileNotFoundError` traceback rather than a
clean error message, which reads like a crash in the tool rather than a bad
path on the command line.

`--clock-source none` and `--path cloud` are honest defaults for a bare
MacBook against LiveKit Cloud with no external time sync — they're recorded
in the run's `environment` block, not inferred, so the report can state the
boundary rather than assume it. `--tier {0,1,2}` is inferred from `--tier0`
when omitted.

`run_matrix.py` writes run records under `teleop-test-matrix/runs/` and
snapshot/seq-log files under `teleop-test-matrix/snapshots/` — **siblings
under the same root**, not nested inside each other. This matters for the
next step:

```bash
python3 parse_runs.py --runs runs/ --report report.md
```

`parse_runs.py --runs <path>` resolves relative snapshot paths against a
`base_dir` that defaults to: `<path>`'s parent if `<path>` is a directory, or
`<path>`'s grandparent if `<path>` is a single run-record file (since a
single file's parent is the `runs/` directory itself, and `snapshots/` is
that directory's *sibling*, not its child). If you relocate run records away
from their snapshots, pass `--base-dir` explicitly. A missing snapshot file
is treated as an operator error and raises immediately with the resolved
path named — it will not silently score as a lost session.

Read `report.md`. The header states the reference configuration, the run
counts by verdict, and the dimensions results are never pooled across. Every
run is `zero_jitter`, so there is no buffering delta anywhere in the report
and nothing in it should be read as one.

### 6. What Tier 0 cannot tell you

- Whether AV1 is viable on real robot hardware (software AV1 here; NVENC/
  Jetson numbers need Tier 2).
- The loss/jitter breakpoints for T-2/T-3, or T-4's capacity ceiling — no
  shaping or multi-host concurrency without root and a second machine.
- Real availability/recovery behavior for most of T-5's fault classes.
- Anything the clock-sync precondition above blocks.

What it *can* tell you, cheaply: whether the session/codec/stats pipeline
works at all, which encoder tier your hardware actually selects, whether the
playout-delay units question is resolved, and the T-1/Q-7 shaping-free
cells (codec/profile bitrate-fps table, and the network-RTT/one-way/G2G
three-column comparison at zero injected delay).

---

## Tier 1 — Linux host with root, netem

Unlocks `loss_pct`, `owd_ms`, `jitter_ms`, `uplink_mbps`, and T-4's load
generators — the full matrix.

### Additional prerequisites

- A Linux host with `root` (netem/`tc` requires it) and `iproute2` installed.
  `matrix.yaml`'s `shaping` block gives the literal `tc qdisc replace ... netem`
  and `tbf` command templates; `run_matrix.py` applies one qdisc command per
  cell (delay+jitter+loss combined into one `netem` invocation — never stack
  two `netem` calls, the second silently replaces the first) plus a separate
  `tbf` layer for rate limiting.
- Two hosts joined to the same SFU room for most suites (a robot-side host
  and an operator-side host), on the interface named in `shaping.iface_default`
  in `matrix.yaml` (override with `--iface`).
- T-4 (capacity) needs load-generator sessions on a host **separate** from
  the one being measured — if the generator and the measured session share a
  NIC, `poll_overbudget_pct` rises and you'll misattribute client-side
  saturation to the SFU.
- Clock sync approaching NTP-grade — `chrony` or PTP on both hosts, verify
  `chronyc tracking` shows stratum ≤ 2 with sub-5ms offset. This is what
  unlocks the five clock-gated suites (T-1, T-2, T-3, T-4, Q-7) for real —
  without it you're limited to what Tier 0 already gave you plus loss/jitter
  sweeps scored only on RTT.

### Running the full matrix

```bash
python3 run_matrix.py --plan            # full matrix, all suites, wall-time estimate
python3 run_matrix.py --dry-run         # full invocation list, still no network
python3 run_matrix.py --run             # the real thing — check --plan's wall-time estimate first
```

Verify shaping was actually applied as requested, not just requested:
`matrix.yaml`'s `shaping.verify` template (`tc qdisc show dev {iface}`) should
be run after any netem application and its output recorded — netem clamps
and rounds, and the run record must carry what was *applied*, not what was
asked for. Every run record's `environment` block should reflect this.

### Reproducing the T-2 reliable-vs-lossy finding

T-2 deliberately sweeps `control_transport` across `data_track_buf1`,
`dc_reliable`, and `dc_lossy` at every loss step. Expect opposite failure
signatures: on `dc_reliable`, SCTP retransmits, so loss shows up as
**latency** (`control_delivered_pct` stays near 100% while
`control_late_pct` climbs) — head-of-line blocking. On `dc_lossy` and
`data_track_buf1`, samples vanish outright and `control_delivered_pct` drops
directly. This divergence is the evidence for LiveKit's `buffer_size: 1`
data-track guidance ("stale commands are usually worse than dropped commands"),
so don't average across transports — `matrix.yaml`'s `never_pool_across`
rule enforces this in scoring, but it's worth understanding why while reading
the report.

---

## Tier 2 — external camera, external GPU with NVENC (or Jetson)

Unlocks hardware AV1 encoding and encode latency/CPU numbers representative
of real robot hardware. Every `encoder_tier: sw` or `videotoolbox` result
from Tier 0/1 involving AV1 is **provisional** until re-run here — Apple
Silicon has no AV1 hardware encoder, so software AV1 numbers describe
correctness (bitrate efficiency) but not the encode latency, CPU load, or
achievable fps ceiling a Jetson Orin or an NVENC-class GPU would produce.

### Additional prerequisites

- An external USB/CSI camera (the harness's video source needs to come from
  real capture hardware to be representative — the `--width`/`--height`/
  `--fps` flags apply regardless of source, but built-in laptop cameras are
  fine for the pipeline smoke test and not for AV1 timing numbers).
- An Nvidia discrete GPU with hardware AV1 support (NVENC AV1 needs specific
  GPU generations — check LiveKit's hardware encoder support matrix) or an
  Nvidia Jetson Orin-class board.
- Re-run the same Tier 0/1 cells that involved `video_codec: av1`, this time
  confirming `encoder_tier` in the `run_metadata` record reads `nvenc` or
  `jetson`, not `sw`. If it still reads `sw`, the hardware encoder wasn't
  selected — encoder choice is automatic in the SDK, not something this
  harness forces, so a `sw` reading here means either the GPU lacks AV1
  hardware support or the driver/runtime isn't exposing it, not a harness
  bug.
- Never pool `sw`/`videotoolbox` results with `nvenc`/`jetson` results in any
  comparison — `matrix.yaml`'s `never_pool_across` includes `encoder_tier`
  for exactly this reason.

### Video source: local device or IP camera

`--camera-source` takes three kinds of value, and every one of them is opt-in.
The matrix default is `test_pattern` and stays that way; see
`docs/MEASUREMENT-DESIGN.md` for why a lens is never a cell default.

| Value | Source |
|---|---|
| `test_pattern` | The generated pattern. The default for every matrix cell. |
| `0`, `FaceTime HD Camera` | A local capture device, by enumeration index or by a case-insensitive substring of its name. |
| `rtsp://…`, `rtsps://…` | An IP camera, decoded by an `ffmpeg` subprocess. |

**The Tier 2 camera is a Pegatron "Muscat" IP camera on Ethernet.** Its working
URLs are:

```
rtsp://192.168.100.123/full1080p    # H.264 1920x1080, ~10 fps, + AAC audio
rtsp://192.168.100.123/4k
```

A minimal run against it:

```bash
./target/release/teleop-harness \
  --url ws://127.0.0.1:7880 --room-name rtsp-check --duration-s 60 \
  --codec h264 --width 1920 --height 1080 --fps 30 \
  --attach-timestamp --attach-frame-id \
  --camera-source rtsp://192.168.100.123/full1080p \
  --snapshots-out snapshots/rtsp-check.jsonl
```

Or through the runner, which passes it to every run in the sweep:

```bash
python3 run_matrix.py --dry-run --tier0 \
  --camera-source rtsp://192.168.100.123/full1080p
```

Notes for whoever runs this first — the code was written without a reachable
camera, so these are the things to check before suspecting the harness:

- **`ffmpeg` must be on `PATH`.** It is the decoder; there is no built-in one and
  no fallback to the pattern. `ffmpeg -version` first.
- **The camera needs a static IP on its subnet**, which needs admin rights on the
  measuring host. Confirm with
  `ffplay -rtsp_transport tcp rtsp://192.168.100.123/full1080p` before involving
  the harness at all — if ffplay cannot open it, neither can this.
- **Audio from the camera is discarded** (`-an`). The harness publishes its own
  synthetic tone; the AAC track is irrelevant.
- **TCP transport is the default** (`--rtsp-transport tcp|udp`). Only reach for
  UDP if you have a reason: UDP RTSP drops media silently on a filtered path and
  that reads as a broken camera rather than a network problem.
- **Every frame read is bounded** by `--rtsp-stall-timeout-s` (15 s, from
  `matrix.yaml` `meta.parameters.rtsp_stall_timeout_s`). A stall, a truncated
  frame and a clean end of stream are three distinct errors, and each one carries
  ffmpeg's own last stderr lines — that output is the diagnosis, so read it
  rather than the harness's wrapper text.
- **Credentials in the URL are stripped** from logs and from the run record. Both
  `rtsp://user:pass@host/path` forms work; the record shows `rtsp://***@host/path`.
- **The Muscat runs ~10 fps at 1080p while the matrix targets 30 fps**, so ffmpeg
  duplicates frames to reach the requested rate and `negotiated_fps` records what
  the encoder was fed, not what the sensor produced. RTSP runs are realism
  spot-checks; they are **not matrix cells** and must not be read as evidence
  about the 27 fps bar.

---

## Regenerating fixtures / running the analysis layer standalone

`parse_runs.py` and its known-answer fixtures need no live session at all —
useful for testing report changes or verifying your `matrix.yaml` edits
don't silently change a score:

```bash
python3 fixtures/make_fixtures.py     # writes fixtures/runs/ and fixtures/snapshots/
python3 -m pytest test_parse_runs.py -v
python3 parse_runs.py --runs fixtures/runs/ --report /tmp/fixture_report.md
```

A clean clone containing only `parse_runs.py`, `test_parse_runs.py`,
`matrix.yaml`, and `fixtures/make_fixtures.py` regenerates and scores
identically (`run_schema.json` isn't needed — nothing in the test suite
reads it). The fixtures directory itself is gitignored — size varies with
whatever `make_fixtures.py` currently generates, dominated by the 200Hz
control sequence logs — and is not the source of truth; regenerate rather
than trust a stale copy.

---

## Known boundary: what this runbook has and hasn't verified

Everything above involving `--plan`, `--dry-run`, and the fixture/test
pipeline has been run and its output checked directly while writing this
document. **A live execution against a real SFU (`run_matrix.py --run`, or
the harness invoked directly against LiveKit Cloud) has not been performed by
the team that built this harness** — per this project's scope, that's the
first thing to do with a live MacBook, and it's the reason §4 above opens
with a smoke test rather than assuming the full Tier 0 subset just works.
If the smoke test in step 4 fails, that is exactly the kind of finding this
runbook exists to surface early, cheaply, and on one cell — not after
launching a 78-run Tier 0 sweep.
