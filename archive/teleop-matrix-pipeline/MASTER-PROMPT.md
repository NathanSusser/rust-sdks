# MASTER PROMPT — LiveKit Rust Teleoperation Test Matrix

Paste everything below the line into Claude Code, opened at the root of this
repository (`rust-sdks/`). Install the agent definitions first:

```bash
mkdir -p .claude/agents && cp teleop-matrix-pipeline/agents/*.md .claude/agents/
```

---

You are the engineering lead for a small, senior team. Build a teleoperation
network test matrix for the **LiveKit Rust SDK** in this repository, matching the
rigor of an existing C++ implementation that already shipped.

The complete specification follows. Read it all, then work. Do not ask me to
confirm the plan before starting — everything you need to decide is either stated
below or discoverable from the sources listed. Ask only if two readings of a
requirement would lead to materially different work.

---

## 1. What this is, and what it is not

This is a **measurement harness**, not a configuration recipe. Its job is to sweep
conditions and produce defensible numbers that settle six open questions from a PRD.

But it must also carry a **reference configuration**: the single set of settings that
LiveKit's robotics documentation recommends for lowest-latency teleoperation. Every
suite runs that configuration as its baseline cell, and every result is reported as a
delta from it. Without a named baseline, a matrix produces a pile of numbers that
nobody can act on. Define it explicitly in `matrix.yaml` as
`reference_config`, derive it from §4 rather than inventing it, and state it on page
one of the report.

The likely shape of that baseline, to be confirmed against the docs, not assumed:
per-camera named video tracks, hardware encoder where available, room-level playout
delay at its floor **or** subscriber-side forced zero jitter buffer, control on a
data track with `buffer_size: 1`, and `max_bitrate` at the §8.0b 5 Mbps ceiling.

## 2. Context: what already exists

A prior effort built this same test matrix against a **C++ / libwebrtc native
client**. That work is complete and is your reference implementation. It lives at:

```
~/code/Livekit Native Nathan/
├── test_matrix/
│   ├── matrix.yaml                    # axes, metrics, thresholds, 6 suites — the source of truth
│   ├── run_schema.json                # the run-record contract
│   ├── run_matrix.py                  # --plan / --dry-run / --run  (429 lines)
│   ├── parse_runs.py                  # extraction, scoring, breakpoints, report  (406 lines)
│   ├── fixtures/                      # synthetic + malformed known-answer inputs
│   ├── SETUP-AND-TESTS.md             # operator guide: netem, clock sync, per-suite runbook
│   └── Teleoperation Test Matrix.docx # the published spec document
├── stats.h / stats.cpp                # bespoke RoomStats: probe RTT, OWD calibration,
│                                      # loop latency (glass-to-glass), DC counters,
│                                      # poll budget, join milestones, resume history
└── room_client.cpp / .h               # the session client
```

Read `matrix.yaml`, `run_schema.json`, `SETUP-AND-TESTS.md`, and `stats.h` before
designing anything. They encode hard-won decisions — INVALID-vs-FAIL separation,
warmup exclusion, conditions-as-applied — that you should carry forward.

**Your job is to redo that work for the Rust SDK.** Not a port. A reimplementation
that expresses the same measurement design through LiveKit Rust APIs, follows
LiveKit's own robotics guidance where the C++ version predates it, adds the axes the
Rust SDK makes possible — **AV1, encoder tier, and buffering mode above all** — and
honestly drops the metrics it does not expose.

## 3. Requirements source

The governing test specification is Confluence page **579869319** in space
**KEAIT**, "Teleoperation Test Matrix":

<https://t-mobile.atlassian.net/wiki/spaces/KEAIT/pages/579869319/Teleoperation+Test+Matrix>

It derives from the Teleoperation PRD, Confluence **556974310**, same space. The
Solution Architecture page, **574818569**, is useful supporting context.

**Fetch all three from the live Confluence REST API before you design.** An Atlassian
API token is already provisioned; the usage reference is at
`~/.claude/reference/reference_atlassian_api_token.md` — read it first and follow
whatever auth pattern it documents. The site is `t-mobile.atlassian.net` and the
account is `nathan.susser1@t-mobile.com`. Storage-format body, Confluence Cloud:

```
GET https://t-mobile.atlassian.net/wiki/api/v2/pages/{id}?body-format=storage
# v1 fallback:
GET https://t-mobile.atlassian.net/wiki/rest/api/content/{id}?expand=body.storage,version
```

Pull `579869319` (Test Matrix — governing), `556974310` (PRD — normative; where the
two differ, **the PRD wins**), and `574818569` (Solution Architecture — context).
Record each page's `version.number` and fetch date in `matrix.yaml` under `meta`, the
way the C++ `matrix.yaml` records `prd_version_fetched`. That is how the matrix stays
traceable when the pages move under it.

Two operational notes: the corporate TLS proxy re-signs outbound HTTPS, so if a
request fails on certificate validation, point it at `.livekit-demo/corp-ca.pem`
(`curl --cacert`, or `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` for Python). And
`~/code/Livekit Native Nathan/architecture/confluence-body.xml` is a worked example
of Confluence storage format from this same space if you need to parse or produce it.

Only if the API is genuinely unreachable, fall back to
`test_matrix/Teleoperation Test Matrix.docx` — content as of 2026-08-13 — and say so
explicitly in your first status update. Do not treat the offline copy as equivalent.

The page defines, and you must not invent your own versions of:

- **The four concurrent streams** — video UL ≤5 Mbps / 20–90 ms / loss-tolerant;
  body-state DL ~0.5 Mbps @ 200 Hz / 20–90 ms / near-zero loss; force feedback UL
  ~0.5 Mbps / near-zero loss; **audio both ways ~0.25 Mbps / ≤50 ms / tolerant**.
- **The pass/fail rules** — RTT p95 ≤ 90 ms (§8.1a), RTT p50 ≤ 50 ms, glass-to-glass
  p50 ≤ 100 ms (§8.1b), control delivered ≥ 99.9% (§8.3a), control late ≤ 0.1%
  (§8.2b), video bitrate ≤ 5 Mbps (§8.0b), video ≥ 27 fps (§7.1a), session drops = 0
  (§8.6a), client poll overbudget ≤ 5% (validity, invalidates rather than fails).
- **The six suites** — T-1 video floor, T-2 loss collapse, T-3 jitter tolerance,
  T-4 concurrency/capacity, T-5 availability and recovery, Q-7 latency definition.
- **Out of scope** — manipulation sufficiency (Figure), per-cell capacity (real RAN),
  operator takeover timing.

⚠️ **Known gap you must close.** The PRD names audio as one of four concurrent
streams with a ≤50 ms budget, but the C++ matrix measured **no audio metric and ran
no audio suite**. Do not silently inherit that omission. Either add audio metrics
(inbound/outbound audio RTP, `MediaPlayout` stats, audio level, concealment) and an
audio-under-load cell to the relevant suites, or state in `MEASUREMENT-DESIGN.md`
that audio is deliberately deferred and why. An unmeasured PRD stream is a hole in
the report, and it should be a visible one.

## 4. Required reading — LiveKit robotics documentation

This matrix must be tuned to LiveKit's robotics guidance, not to generic WebRTC
folklore. Read the **whole** robotics subtree before the design document is written:

**Publishing video**
- <https://docs.livekit.io/robotics/media/video/> and `/video-tracks/` — named
  per-camera tracks, `NativeVideoSource`, `TrackPublishOptions`, `VideoEncoding`.
- <https://docs.livekit.io/robotics/media/video/encoders/> — **hardware encoder
  support matrix. Binding on §7.**
- <https://docs.livekit.io/robotics/media/video/metadata/> — timestamps and frame
  metadata, which is how glass-to-glass is meant to be carried.

**Publishing data**
- <https://docs.livekit.io/robotics/media/data/data-tracks/> — data tracks for
  high-frequency structured data.
- <https://docs.livekit.io/transport/data/rpc/> — RPC for discrete operations.

**Performance**
- <https://docs.livekit.io/robotics/media/performance/stats/> — room- and track-level
  stats. Note the doc's own warning: counters are **cumulative since subscription
  start**, so an interval value requires differencing two readings.
- <https://docs.livekit.io/robotics/media/performance/low-latency/> and its
  `/playout-delay/` and `/zero-jitter/` children.

**Teleoperation**
- <https://docs.livekit.io/robotics/teleop/>, `/teleop/robot/`, `/teleop/operator/` —
  the robot and operator app patterns and the control schema.

**Other**
- <https://docs.livekit.io/reference/other/roomservice-api/> — `minPlayoutDelay` /
  `maxPlayoutDelay`.
- <https://docs.livekit.io/agents/start/testing/test-framework/> — borrow its
  assertion vocabulary only. It tests agent *behavior*, not transport; do **not**
  adopt LLM judges for numeric transport thresholds.
- <https://docs.rs> for every third-party crate API you touch, at the pinned version.

Also read `/AGENTS.md` at this repo root. It is binding on all Rust you write.

**Control path correction.** LiveKit's robotics guidance puts robot control on a
**data track** subscribed with `DataTrackSubscribeOptions::new().with_buffer_size(1)`,
with lossy delivery, applying the latest valid command and tolerating gaps — "stale
commands are usually worse than dropped commands for continuous teleoperation
input." The C++ matrix predates this and modeled control as a reliable-vs-lossy
**data channel**. Follow the current guidance: make `control_transport` an axis
covering the data-track path (`livekit-datatrack`, `examples/basic_data_track`) and
the legacy reliable/lossy channel, so the report can state what the migration buys.
The reliable-channel head-of-line blocking finding from the C++ work is still worth
reproducing — it is the evidence *for* the data-track design.

## 5. Ground truth from LiveKit engineering

From David Chen (GM Robotics, LiveKit) by email, 20–21 Aug 2026. Authoritative, and
in places it **contradicts the public docs**. Where they conflict, resolve it
empirically in this checkout and record the finding.

**The SFU is not a latency source.** No buffer delays packets; benchmarked dwell time
is typically under one millisecond. Do not model SFU queuing.

**Two non-equivalent ways to control the jitter buffer:**

1. **Room-level playout delay hints** — `minPlayoutDelay` / `maxPlayoutDelay` at room
   creation. Room-wide: sets the PlayoutDelayHint RTP extension for *every*
   subscriber. Per David, **values are in units of 10 ms and cannot both be zero**,
   so the floor is `min=0, max=1` → an effective 10 ms buffer. A true 0/0 is planned.
   ⚠️ **Unresolved conflict:** the docs show `--min-playout-delay 0
   --max-playout-delay 10` and `create_room_with_playout_delay(name, opts, 0, 10)`,
   reading as milliseconds, while David describes 10 ms units where `10` means
   100 ms. Resolve against `livekit-api` source here and confirm by measuring
   `jitter_buffer_delay / jitter_buffer_emitted_count`. Do not let the ambiguity
   reach `matrix.yaml`.
2. **Subscriber-side forced zero** — `WebRTC-ForcePlayoutDelay/min_ms:0,max_ms:0/`,
   exposed as `livekit::webrtc::enable_zero_playout_delay()`. Per-client, true 0/0,
   **Rust SDK only**. This is what David used for AV1 testing. Wired to
   `--low-latency` at `examples/local_video/src/subscriber.rs:1678`.

**Open question worth a suite.** T-Mobile's RAN treats all traffic alike with high
discard timers, so video over UDP gets retransmissions of already-stale packets —
latency and throughput spent on frames nobody can use. The candidate fix is aligning
the RAN with the application: RLC **UM** rather than AM, appropriate AQM, shorter
PDCP discard / PDCP reordering / RLC reassembly timers. David's honest position is
that he is *not sure* the network side needs to match the application side, since
the jitter buffer sits well above raw IP. **That uncertainty is the most valuable
thing this matrix can settle for T-Mobile.** The falsifiable prediction: under forced
zero jitter buffer, a retransmitted late packet is strictly worse than a dropped one
— the deadline has passed — so RLC-AM with long discard timers should show *higher*
glass-to-glass and *no* freeze benefit versus RLC-UM. Note the app-layer echo of the
same principle in LiveKit's own `buffer_size: 1` guidance. Design the runs that
confirm or falsify it.

## 6. What makes this a Rust project, not a port

### 6a. The stats surface is different

`room.get_stats()` returns `SessionStats`; per-track `track.get_stats()` returns
`Vec<RtcStats>` with `InboundRtp`, `OutboundRtp`, `RemoteInboundRtp`, `CandidatePair`,
`Transport`, `DataChannel` (`libwebrtc/src/stats.rs`). Some C++ metrics map cleanly.
Others — four-timestamp probe RTT, one-way delay calibration, per-frame arrival
interval, poll overbudget — have **no** WebRTC-stats equivalent and must be built
application-side or dropped. Resolving every one is the architect's first
deliverable, citing real symbols from this checkout.

### 6b. Glass-to-glass is already solved — reuse it

`examples/local_video` is a working publisher/subscriber pair with the timing
machinery built:

| File | What it gives you |
|---|---|
| `src/publisher.rs` | Camera capture, codec + encoder selection, bitrate/resolution/fps, `FrameMetadataFeatures` |
| `src/timestamp_burn.rs` | `TimestampOverlay`, `TextBurner`, `format_timestamp_us` |
| `src/subscriber_timing.rs` | Receive-side timing |
| `src/codec_display.rs` | Live codec / implementation readout |
| `src/clock.rs` | Millisecond on-screen clock for camera-pointed-at-display G2G |
| `src/subscriber.rs` | `--low-latency`, `--display-timestamp`, `--participant` |

`--attach-timestamp` / `--attach-frame-id` carry timing **in-band as frame
metadata** — the clean G2G path. `--burn-timestamp` is the pixel fallback for
measuring through a physical display. Extract or depend on this code. A third
timestamping scheme in this repo is a defect.

**Working credentials and runner scripts already exist** at `.livekit-demo/`:
`run-video.sh`, `run-data.sh`, `mint_token.py`, `env.example`, and a `corp-ca.pem`
that `SSL_CERT_FILE` must point at because the corporate TLS proxy re-signs outbound
HTTPS and plain rustls native roots do not pick it up on macOS. Reuse this. Do not
write a second token minter, and make sure the harness honors `SSL_CERT_FILE` or it
will fail on the corporate network in a way that looks like a LiveKit bug.

### 6c. Codec is a first-class axis, and AV1 is the point of it

`PublisherCodec` in `examples/local_video/src/publisher.rs:46–61` supports
`H264, H265, VP8, VP9, AV1`, mapped to `livekit::options::VideoCodec`. CLI default is
H264 (`publisher.rs:231`). David's verified AV1 invocation:

```bash
# Publish
RUST_LOG=info cargo run -p local_video --features="desktop" --bin publisher -- \
  --room-name ROOM_NAME --identity IDENTITY \
  --url URL --api-key xx --api-secret xx \
  --camera-index 0 --burn-timestamp --attach-timestamp --attach-frame-id \
  --height 720 --width 1280 --fps 30 --max-bitrate 5000000 --codec av1

# Subscribe  (--participant MUST be the publisher's identity)
RUST_LOG=info cargo run -p local_video --bin subscriber -- \
  --url URL --api-key xx --api-secret xx \
  --room-name test --identity SUBSCRIBER_IDENTITY \
  --display-timestamp --participant PUBLISHER_IDENTITY --low-latency
```

**Why AV1 changes the answer, not just adds a column.** T-1 asks which video profile
fits under the §8.0b 5 Mbps ceiling at ≥27 fps. AV1's efficiency may put a *higher*
resolution rung inside that budget than H264 can reach. If so the video floor moves
and T-1's answer to Figure changes. Sweep codec against `video_profile` and report
the profile→codec→encoder→bitrate→fps table, not a single floor.

**What AV1 costs, and must therefore be measured alongside it:**

- **Encode CPU.** Record `codec_implementation` (`publisher.rs:521`) and the
  `--encoder` selection in every run. Treat `qualityLimitationReason == Cpu` in
  `OutboundRtp` as a first-class metric. A bitrate produced by a CPU-starved encoder
  is INVALID, not a pass.
- **Decode latency.** AV1 decode adds to G2G. Q-7's three-column table (network RTT /
  one-way / glass-to-glass) must be produced **per codec**, because the ratio between
  columns is codec-dependent and that ratio *is* Q-7's answer.
- **Keyframe cost and recovery.** Measure keyframe service time and PLI rate per
  codec — it drives how fast video recovers from a T-2 loss burst.
- **A known failure mode already in this code.** `publisher.rs:464` raises "Encoder
  produced frames but no RTP packets were sent; the AV1 bitstream may be malformed."
  Make that an explicit INVALID reason, not a silent zero-bitrate FAIL.
- **Negotiation is not guaranteed.** `publisher.rs:1131–1211` retries with a
  different codec on publish failure (H265 → H264). **Record the actual negotiated
  codec, never the requested one.**
- **Hold simulcast off for the core matrix.** `--simulcast` / `--dynacast` exist
  (`publisher.rs:1165`, `1939–1947`) and change bitrate behavior substantially. Fix
  off, note in the environment block, treat as a follow-on question.

### 6d. New axes

- `video_codec: [av1, vp9, vp8, h264]` — crossed with `video_profile` for T-1,
  swept for Q-7.
- `encoder_tier` — see §7.
- `buffering_mode: [default, playout_hint_floor, playout_hint_smooth, zero_jitter]`,
  hint units per the §5 resolution.
- `control_transport: [data_track_buf1, dc_reliable, dc_lossy]` — see §4.
- `ran_profile` — see §8.

**AV1 × zero_jitter is the cross that matters most.** Zero jitter buffer plus larger,
more variable frames is exactly where frame-assembly sensitivity shows up. Do not
leave that cell as a hole.

## 7. Encoder tier — and why your MacBook cannot answer the AV1 question alone

Per LiveKit's hardware encoder page, encoder selection is **automatic**: the SDK uses
a hardware encoder when the platform provides one for the negotiated codec, and falls
back to software otherwise. The support matrix:

| Platform | Encoder API | Codecs |
|---|---|---|
| AMD CPUs and GPUs | VAAPI | H.264 |
| Intel CPUs | VAAPI | H.264 |
| Nvidia discrete GPUs | NVENC | H.264, H.265, **AV1** |
| Nvidia Jetson | Jetson MMAPI | H.264, H.265, **AV1** |
| Apple Silicon | VideoToolbox | H.264, H.265 — **no AV1** |

NVENC AV1 requires an Nvidia GPU with hardware AV1 support; Jetson AV1 requires
Orin-class hardware.

**The consequence, which must be stated in the report and enforced in the schema:**
on Apple Silicon, `--codec av1` runs **software AV1**. Its bitrate efficiency is
representative; its encode latency, CPU load, and achievable fps are **not**
representative of a robot with NVENC or a Jetson Orin. Therefore:

- `encoder_tier` is a required run-record field: `sw`, `videotoolbox`, `vaapi`,
  `nvenc`, `jetson`.
- The analysis **never pools across encoder tiers**, and a breakpoint derived from
  software AV1 is labeled as such wherever it appears.
- `MEASUREMENT-DESIGN.md` states, per metric, whether it is encoder-tier-sensitive.
  Bitrate-at-quality is largely portable; encode latency, fps ceiling, CPU
  limitation, and the G2G encode share are not.

## 8. RAN parameters: recorded, not controlled

Five settings owned by the T-Mobile network team (Nishant Patel; capacity questions
to Numan Suri / Scott Jacka), not by this harness:

`rlc_mode` (AM/UM) · `aqm_mode` (OFF/Non-GBR/GBR) · `pdcp_discard_timer_ms` ·
`pdcp_reordering_timer_ms` · `rlc_reassembly_timer_ms`

Put them in `environment.ran_profile` as **recorded** values, defaulting to
`unknown`. The harness does not set them. On a lab RAN where they *can* be varied,
`ran_profile` becomes a named axis and the §5 hypothesis is directly testable. Until
then every run states its profile, and the report never compares across profiles
without saying so. On `loopback` and `lan` paths mark `ran_profile: n/a` — those
results do not transfer to cellular and the report must say it.

## 9. Execution tiers — the harness must work at every tier, starting on a MacBook

I will bring this up in three stages. Design for all three from the start; the
difference between them is which axes are available, not which code runs.

**Tier 0 — MacBook, live SFU, no shaping.** macOS cannot run `tc`/`netem`, but it
*can* run real sessions against LiveKit Cloud using `.livekit-demo/`. This tier is
not a dry run: it exercises session lifecycle, codec negotiation and fallback,
encoder tier detection, bitrate/fps/resolution control, glass-to-glass, zero jitter
buffer, control transport, and the whole stats pipeline end to end. Every axis except
the netem ones is live here. **Make this a first-class supported mode**, not an
afterthought: `run_matrix.py` must accept a suite restricted to shaping-free axes and
run it on macOS, and `SETUP-AND-TESTS.md` must open with this tier. It is how I will
find out whether any of this works.

**Tier 1 — Linux host with root, netem.** Unlocks `loss_pct`, `owd_ms`, `jitter_ms`,
`uplink_mbps`, fault injection, and the T-4 load generators. This is the full matrix.

**Tier 2 — external camera and external GPU with NVENC.** Unlocks hardware AV1 and
representative encode latency, and makes the T-1 answer transferable to real robot
hardware. Tier 0 and Tier 1 results at `encoder_tier: videotoolbox` or `sw` are
provisional for any encoder-sensitive metric until re-run here.

The run record's `environment` block must make the tier unambiguous: `path`
(`loopback` / `lan` / `edge_mso` / `cellular` / `cloud`), `encoder_tier`,
`ran_profile`, camera source, and host identification. A reader must never have to
guess which tier produced a number.

## 10. The team

Five subagents are installed in `.claude/agents/`. Use them as specified; each has
its own standing rules that you should not restate:

| Agent | Owns |
|---|---|
| `teleop-architect` | Measurement design, metric→API mapping, gap resolution, axes, thresholds |
| `teleop-rust-engineer` | The `teleop-test-matrix` crate: sampler, control publisher, probes, CLI |
| `teleop-test-engineer` | `matrix.yaml`, `run_schema.json`, `run_matrix.py`, fixtures, Rust unit tests |
| `teleop-data-analyst` | `parse_runs.py`: extraction, scoring, breakpoints, the report |
| `teleop-reviewer` | Independent review of a completed package — never reviews its own work |

**Delegation policy.** Delegate only for a phase-sized, genuinely independent track
of work. Do not delegate anything you can finish in a handful of tool calls, and do
not spawn a subagent to double-check work you just did yourself — the reviewer pass
at each gate is the verification, and one pass per gate is enough. Prefer one agent
over three. Run agents concurrently only when their file sets do not overlap; two
agents editing `matrix.yaml` in parallel is a defect, not throughput.

## 11. Phases and gates

Each gate is a hard stop.

**Phase 0 — Ground truth.** Fetch pages 579869319, 556974310, and 574818569 from the
Confluence API per §3 and record their version numbers. Read the C++ prior art. Read the
**entire** robotics doc subtree from §4. Inventory this checkout: grep
`libwebrtc/src/stats.rs` for stats structs and fields; confirm
`enable_zero_playout_delay` and the playout-delay room API and **settle the units
question from §5**; read `examples/local_video/src/*` end to end; read
`examples/basic_data_track` and the `livekit-datatrack` crate for the control path;
read `.livekit-demo/` for credentials, TLS, and runner conventions.
*Gate: the three Confluence pages fetched live with version numbers recorded, a
written inventory of confirmed API symbols with file:line citations, and a stated
resolution of the playout-delay units conflict.*

**Phase 1 — Design.** `teleop-architect` produces
`teleop-test-matrix/docs/MEASUREMENT-DESIGN.md`: metric→API mapping, gap list with a
resolution for every gap (including the audio decision from §3), axis design
including codec, encoder tier, buffering mode, control transport and `ran_profile`,
the `reference_config` from §1, and the threshold table traced to PRD clauses. It
states which metrics are codec-sensitive and which are encoder-tier-sensitive.
*Gate: `teleop-reviewer` confirms every row names a symbol that exists and every gap
has a stated resolution.*

**Phase 2 — Matrix definition.** `teleop-test-engineer` writes `matrix.yaml`,
`run_schema.json`, `run_matrix.py` with `--plan`, `--dry-run`, and a
shaping-free mode for Tier 0. The run record carries requested and actual codec,
encoder tier, buffering mode, control transport, path, and `ran_profile`.
*Gate: `--plan` prints per-suite run counts and wall-time estimates, including a
separate count for the Tier 0 subset; `--dry-run` completes on macOS.*

**Phase 3 — The harness crate.** `teleop-rust-engineer` builds
`teleop-test-matrix/` as a workspace member, reusing `examples/local_video` timing
code and `.livekit-demo` conventions: session lifecycle, video publisher with
selectable codec, control publisher at 200 Hz over the selectable transport, stats
sampler with its own budget accounting, latency probes, CLI. Writes one JSON snapshot
per interval; exits non-zero on session failure; honors `SSL_CERT_FILE`.
*Gate: `cargo fmt` clean, `cargo clippy --all-targets -- -D warnings` clean,
`cargo test -p teleop-test-matrix` green, changeset present, reviewer has reported.*

**Phase 4 — Analysis.** `teleop-data-analyst` writes `parse_runs.py` and the report
generator, driven by fixtures. Fixtures must include an AV1 run that fell back to
another codec, an AV1 run that hit the malformed-bitstream condition, and a
CPU-limited software-AV1 run; all three score INVALID with the correct reason.
*Gate: known-answer fixtures reproduce hand-computed values; the three failure
fixtures score INVALID rather than FAIL.*

**Phase 5 — Tier 0 readiness and handoff.** Run the pipeline end to end on synthetic
data: plan → dry-run → fixture parse → report. Write
`teleop-test-matrix/SETUP-AND-TESTS.md` opening with the **Tier 0 MacBook runbook** —
`.livekit-demo` setup, the `SSL_CERT_FILE` requirement, David's verified AV1
publish/subscribe smoke test, how to confirm the encoder tier that was actually
selected, and which suites are runnable without netem — then Tier 1 (netem, clock
sync, full matrix) and Tier 2 (external camera, NVENC). Then `HANDOFF.md`: what is
unblocked, what is blocked and on whom, recommended execution order.
*Gate: a reader who has never seen this repo can go from clone to a Tier 0 live smoke
test, and separately to a dry-run report, using only `SETUP-AND-TESTS.md`.*

## 12. Engineering standards

- **`matrix.yaml` is the only home for thresholds, axis values, and rates.** Scoring
  lives in Python so a threshold change never triggers a rebuild.
- **Conditions and metrics travel together in one run record.** A metrics blob
  without its conditions is unanalyzable — the most common way a test matrix rots.
- **Record what was applied, not what was requested.** netem clamps, shapers round,
  codecs fall back, encoders are chosen automatically. Store the real values and the
  verbatim `tc` command.
- **PASS / FAIL / INVALID / OBSERVE.** A run that did not measure the thing is
  INVALID and excluded from breakpoints, never a failure. Codec fallback,
  CPU-limited encoding, and malformed AV1 bitstream are INVALID reasons.
- **Cumulative counters are differenced before any ratio.** State the units.
- **≥3 repeats per cell; report dispersion.** A breakpoint whose repeats disagree is
  a range, not a point.
- **Never pool across codecs, encoder tiers, buffering modes, control transports, or
  RAN profiles.** They are different experiments.
- **Every result is reported as a delta from `reference_config`.**
- **Finish what you start.** No `todo!()`, no placeholder returns, no stubs. If
  something cannot be built because the SDK lacks the capability, say so precisely —
  that is a finding, not a failure.
- **Every claim states its boundary.** Loopback does not transfer to cellular. An SFU
  capacity number is not a site capacity number. Software AV1 on Apple Silicon is not
  NVENC AV1.

## 13. Scope

Build the design, the crate, the matrix, the analysis, and the docs, and prove them
on synthetic data. **Do not run against a live SFU yourself** — I will run Tier 0 on
my MacBook using your runbook, and Tier 1 and Tier 2 when that hardware is ready.
Your job is to make Tier 0 work the first time I try it. Make routine judgment calls
yourself. If you think part of this specification is mistaken or a better approach
exists, say so in a sentence and continue with the task as written rather than
quietly redefining it.

## 14. Communication

Before your first tool call, say in one sentence what you are about to do. While
working, post a brief update only when you clear a gate, find something that changes
the design, or hit a blocker — not before each step. When you finish a phase, lead
with the outcome: what now exists and what it proves.

Match the length of written documents to what the task needs. Cover the substance; do
not pad with filler sections, redundant summaries, or boilerplate. The reference
`SETUP-AND-TESTS.md` is 423 lines and every line does work — use it as your
calibration for density, not as a length target.

## 15. Definition of done

1. `teleop-test-matrix/` exists as a workspace member crate, builds clean under
   `fmt`, `clippy -D warnings`, and `test`, with a changeset.
2. `MEASUREMENT-DESIGN.md` maps every metric to a verified Rust SDK symbol or an
   explicit resolved gap; states the playout-delay units resolution from §5; states
   the audio decision from §3; and names `reference_config`.
3. `matrix.yaml` defines all axes — `video_codec` with AV1, `encoder_tier`,
   `buffering_mode`, `control_transport`, `ran_profile` — all thresholds traced to
   PRD clauses, and all six suites.
4. The AV1 × zero_jitter cell is present and not excluded.
5. `run_matrix.py --plan` and `--dry-run` work on macOS, and a Tier 0 shaping-free
   subset is selectable and correctly excludes every netem-dependent axis.
6. `parse_runs.py` reproduces hand-computed values from known-answer fixtures and
   scores the malformed-AV1, codec-fallback, and CPU-limited fixtures INVALID with
   correct reasons.
7. `SETUP-AND-TESTS.md` opens with the Tier 0 MacBook runbook and covers Tiers 1
   and 2.
8. `HANDOFF.md` states what is unblocked, what is blocked and on whom, and the
   recommended execution order.
9. `teleop-reviewer` has reviewed each package; findings are fixed or listed in
   `HANDOFF.md` with a reason for deferral.
