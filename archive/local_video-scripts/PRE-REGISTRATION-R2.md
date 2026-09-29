# Pre-registration — R2 (5G A3 repeat) and R3 (local-SFU ethernet run)

Written **before** either run, and committed before the publisher starts, so
that neither host can pick the reading that fits afterwards. Both hosts recorded
their predictions independently; where they differ, that is left visible.

The programme's dominant failure was interpreting a result after seeing it. This
file exists to make that impossible for these two runs.

---

## R2 — A3's configuration over 5G, on a recovered uplink

**This is not a replication of A3.** Same parameters, roughly three times the
uplink. It is a new condition that happens to share A3's config, and the
writeup must say so.

| | A3 (4 Sep, afternoon) | R2 (6 Sep) |
|---|---|---|
| Uplink at run time | ~10 Mbps (spot, unrecorded at the time) | 28.1 / 29.7 / 33.5 Mbps, recorded in manifest |
| Offered / capacity | ≈ 100% | ≈ 33% |
| Encoder columns | none | QP, `quality_limitation_reason`, encoded resolution |

Config, otherwise identical to A3: 1920×1080, 30 fps, `--test-pattern 1`,
`--codec h264`, `--max-bitrate 10000000`, `--stats-interval-ms 100`, 120 s.

### Host A's prediction: the collapse reproduces

Reasoning, stated so it can be judged wrong for the right reason: the encoder's
own condition is **identical** in both runs. It is granted 10 Mbps by
`--max-bitrate` and, in A3, that 10 Mbps was fully delivered with zero packet
loss. Extra headroom above an unchanged, fully-satisfied grant is invisible to
the encoder — it cannot spend bitrate it was never offered. If the picture
stepped down in A3 while its full grant was flowing, nothing about a larger
uplink changes what the encoder sees, so it should step down again.

The corollary is what makes this falsifiable: **if the collapse does not
reproduce, then something about link capacity was reaching the encoder in A3
through a channel we did not measure** — and "the delivered bitrate was flat, so
bandwidth was not the constraint" is wrong in a way we have not identified.

### Host B's prediction: the collapse reproduces, and QP is HIGH before each step

Same call as Host A, and their reasoning is the stronger one — a satisfied grant
makes headroom invisible. But "the staircase happens" is a weak thing to predict
when both of us expect it, so here is a sharper one that can be wrong on its own.

**QP will be high before each down-step, not low — and specifically it will be
high at 1080p while the full 10 Mbps is flowing.**

The arithmetic behind it. 10 Mbps at 1080p30 is **0.16 bits per pixel**. In A3
the encoder stepped down until it reached 1.4–2.2 bpp and then stopped, in A2 and
A2-off it oscillated around the rung that gave roughly 2 bpp, and it did that
while spending its whole grant. If 1.4–2.2 bpp is what this content costs at an
acceptable quantiser, then 0.16 bpp at 1080p is **an order of magnitude short**,
and the encoder is quantising hard to fit — which is exactly the condition
WebRTC's quality scaler is built to respond to.

That would make the staircase the scaler working correctly on content that is
far more expensive than "animated colour bars" suggests: the moving checkerboard
produces residuals everywhere, so the flat-region intuition is wrong.

This corrects something I asserted earlier in the programme. I wrote that CBR at
a 10 Mbps target on trivially compressible content should drive the quantiser
*low*, and called it strange that the picture shrank anyway. The bpp figures were
in front of me and say the opposite: the encoder's own revealed preference across
four runs is 1.4–2.2 bpp, and it never once settled at a rung offering less.

**How this is wrong.** If QP reads low before the steps, this prediction fails and
the scaler is not driving the staircase — the trigger is somewhere else entirely
(`quality_limitation_reason` of `Cpu`, the bitrate allocator, or encoder-internal
adaptation below libwebrtc). That would be the more interesting result and I would
rather it than a confirmation.

**Secondary prediction, recorded to be scored:** `quality_limitation_reason` reads
`Bandwidth` for most of the run despite ~30 Mbps of uplink and zero loss — because
the field reports the encoder being unable to meet quality within its *grant*, not
the link being full. If it reads `Cpu` or `None`, I am wrong about the mechanism
and not merely about the label.

### Agreed in advance

`quality_limitation_reason` and per-frame QP are read **as the primary outcome**,
not as supporting detail. A3 had neither column; that absence is why its
mechanism is still open.

---

## R3 — local SFU over the 1 Gbps PTP cable

`livekit-server` bound to `192.168.99.1`, both hosts connecting over the direct
cable. `enp5s0` is 1000 Mb/s full duplex, RTT 0.494 ms, zero loss.

### The decisiveness is asymmetric, and this is recorded before the run

This run changes **two** things — the link *and* the SFU (a local Go binary, not
T-Mobile's deployment). Therefore:

| Outcome | What it licenses |
|---|---|
| Staircase **still happens** | **Decisive.** 1 Gbps, sub-ms RTT, zero loss, three orders more headroom than A3. No bandwidth account survives. Resolution-decision problem, settled |
| Staircase **does not happen** | **Ambiguous.** Could be the link, could be the different SFU. Settles little, and must be reported as settling little |

### Constraints, in the manifest rather than in memory

- **No e2e, transport, or latency figure may be reported from R3, at all.** Video
  on the timing cable degrades the clock that produces those numbers, so any
  latency reading would be measuring our own interference.
- The outcome is read entirely from publisher-side encoder stats, which need no
  cross-host clock.
- Host B's sync reading below is the known-good baseline; it is re-read *after*
  R3 so the damage is measured rather than assumed.

### Known-good sync baseline (Host B, slave side, pre-R3)

```
ptp4l rms (last 5)   1417 / 2042 / 3051 / 4109 / 4289 ns
path delay           72-76 us (mixed-tier, unchanged since 4 Sep)
phc2sys servo        s2, offsets ±4 us
MASTER excursions    zero since 19:19:28 on 4 Sep
state transitions    none — continuously SLAVE for ~42 h
```

Host A cannot verify any of this from its own side: a grandmaster has no master
to measure against and emits no `rms` lines. That asymmetry is documented in
`RUN-DISCIPLINE.md` §6 and this is its first use in production.

---

## Two known measurement gaps, recorded as gaps

Neither is fixed before these runs, and both are stated so no reader infers they
were considered and dismissed.

1. **`phc2sys` on Host B crashes chronically** — 97 restarts since 15:19 on
   4 Sep, `ioctl PTP_SYS_OFFSET_PRECISE: Connection timed out`, one to six times
   an hour. Every sync check either host runs passes anyway, because `ptp4l`
   stays SLAVE throughout. Bounded: 2–3 s undisciplined per restart, excursion
   peaking near 20 µs, which is two to five orders below the transport figures.
   No prior run overlapped a restart — all eight windows checked by hand. The
   manifest now records `phc2sys_restarts_start` / `_end` so this never needs
   reconstructing again. The crash itself needs root and stays on the operator
   list.

2. **The manifests have been probing the wrong direction on the subscriber.** A
   subscriber's uplink carries RTCP and little else; its *downlink* is the video
   path. Host B's uplink spread was 2.13× — outside our own comparability
   threshold — while its downlink is 212–224 Mbps at a 1.05× spread, 22× the
   offered load. The publisher's uplink probe is the correct direction; the
   subscriber needs a downlink probe added.

---

# OUTCOMES — scored 6 Sep, after the runs

Recorded against the predictions above and against R4's, which was registered by
message before that run started. Scored strictly: a prediction whose falsifying
condition never arose is **untested**, not confirmed.

## R2 and the paired run — bars, 1080p, 10 Mbps cap

Both ran `--test-pattern 1`. Neither was a repeat of A3, which is the whole
story of this section: **the A-series ran `--test-pattern 2`, noise.** The
comparison these runs were designed to make was invalid from the start.

| Prediction | Outcome |
|---|---|
| Host A: the collapse reproduces | **WRONG.** 1080p held, zero steps, both runs |
| Host A's premise — "granted 10 Mbps and fully delivered, so headroom is invisible" | **ALSO WRONG.** The grant was 6.093 then 3.264 Mbps, never the cap |
| Host B: QP high before each down-step | **WRONG** here — QP 12 then 17, near-lossless. Right about A3, tested in R4 |
| Host B: `quality_limitation_reason` = Bandwidth | **WRONG** here — `None`, 3596/3596 and 3594/3594 |

What they did establish, and it stands independently of the source confusion:

- The encoder **undershoots its grant** on cheap content — 0.443 and 0.521 Mbps
  against 6.093 and 3.264 Mbps targets. Corroborated by Host B at 0.54 Mbps from
  a *software* encoder on different silicon.
- **No padding.** `transport_bytes_sent / media_bytes_sent` = 1.039, i.e. RTP
  headers and RTCP. The pacer adds nothing.
- **Cross-validation at 100.2%**: publisher outbound 0.521 Mbps against
  subscriber inbound 0.521 Mbps, opposite ends of a two-hop path, different
  codebases. The first time both legs were measured in one run.

## R4 — noise, 1080p, 10 Mbps cap, on 2.5× A3's uplink

| Host B's prediction | Outcome |
|---|---|
| The staircase persists | **CORRECT** |
| Steps land in the first seconds | **CORRECT** — all three inside 12.08 s |
| 5.00 s scaler cadence | **CORRECT** to a hundredth: 2.00 / 5.00 / 5.00 |
| Settles where bpp reaches 1.4–2.2 | **CORRECT** — 1.419 |
| Terminal rung *below* 640×360, conditional on a ~6 Mbps grant | **UNTESTED.** The grant was the full 10 Mbps cap, so the falsifying condition never arose. The model predicted the observed rung once the grant was known, but in a form registered only after the fact |

Host A registered no prediction for R4, having been wrong twice that day.

**The model is unfalsified here, not confirmed**, and the distinction is Host B's
own insistence. In a programme with a twelve-row withdrawal ledger it is the
difference that matters.

## What R4 settled

- **The mechanism**: 1080p noise costs ~1.4 bpp; a 10 Mbps grant at 1080p30
  offers 0.16. QP pins at the H.264 ceiling of **51 before every down-step** and
  relaxes to 38 only on reaching an affordable rung, at which point stepping
  stops. Read directly from the encoder, not inferred.
- **Bandwidth excluded by measurement**: 10 Mbps granted, 9.8 taken, **15 Mbps of
  uplink never asked for**, and the collapse happened anyway. 2.5× the headroom
  changed neither the rungs nor the destination.
- **Replication**: A3 vs R4 agree on rungs, order, terminal resolution
  (640×360), delivered bitrate (9.59 vs 10.02 Mbps), bpp (1.39 vs 1.45), decode
  p50 (3.65 vs 3.63 ms) and zero loss.

## Two mechanisms proposed *after* the fact, flagged as such

Neither is registered, both are well supported, and both are the kind of claim
this programme has withdrawn before. Recorded so a later reader can weigh them
accordingly rather than finding them stated as findings.

1. **Why the estimator "wandered" on bars.** A bars encoder sends 0.44 Mbps and
   gives GCC almost nothing to probe with, so the estimate drifts (6.093, then
   3.264). A noise encoder consumes its allocation, so the estimate is exercised
   and sits at the cap (10.000). Both hosts reached this independently after
   seeing R4. It retires an earlier claim that the estimator was unstable.
2. **Why bars are so cheap.** Each frame is the previous one translated
   horizontally, which motion compensation captures almost exactly. Checkable in
   the source independently of any run, which is what distinguishes it from a
   rationalisation.

## Still not measured, and it is the only thing that matters to the deliverable

**Real camera video.** Every number in this programme comes from a source chosen
to be the hardest possible case. Noise at 1.4 bpp is not teleoperation footage,
which compresses one to two orders of magnitude better. The mechanism is content
cost against grant — so the honest expectation is that the production feed does
**not** behave like this.

Host A has no camera: no `/dev/video*`, no UVC device on USB. The run needs
hardware from the operator and no new code — omitting `--test-pattern` already
selects a UVC source.
