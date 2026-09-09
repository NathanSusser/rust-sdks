# PROMPT — Module bring-up and DSCP tagging on the modem host

For the Claude Code session on the **Linux host wired to the 5G module**. Paste
everything below the line.

Read `5g-teleop-qos-device-reference.md` in this repo first — it is the design and the
source of truth for every DSCP and 5QI value. This document does not restate it. It says
what to **find out**, in what order, and what counts as proof.

---

## 1. Why this work now has a measured motive

As of 2026-09-08 the video programme reached a ruling: **Google Congestion Control is not
tracking this link and has been removed from the rig.** That is the reason this QoS work
matters, and it changes what success looks like, so read this before §3.

Three things were overriding the requested video configuration, and only one was the
estimator (`660f5664`):

| Override | What it did |
|---|---|
| `MAX_START_BITRATE_KBPS = 1000` | hardcoded in the SDK — ask for 10 Mbps, start at 1 and probe for the rest. Now configurable via `LK_MAX_START_BITRATE_KBPS`, default unchanged |
| `x-google-min-bitrate` unpinned | the estimator could lower the allocation at will. `LK_PIN_BITRATE_TO_MAX` pins it to the cap |
| **degradation preference** | the SDK defaults a `Camera` source to `MaintainFramerate` — hold frame rate, **sacrifice resolution**. Nobody chose it. This was the resolution staircase |

With all three applied: **10 Mbps offered, 147.2 MB over 152 s at 6.8–9.4 Mbps, 1600×1300
throughout, 29 fps, zero resolution changes.** The comparable run with GCC intact ran at
**300×240 and 0.03 Mbps.**

The number that should shape your work: **four runs minutes apart differed 7.3× in the grant
the estimator handed the encoder**, while each spent essentially all of what it was given
(`cbd27033`, `239a295e`). The binding constraint on this link is not capacity — it is an
estimator that cannot distinguish 5G radio scheduling jitter from congestion, on an
unmanaged bearer.

**So the point of mapping video to its own 5QI is not headroom. It is a stable, predictable
grant**, so the estimator converges somewhere useful instead of tracking scheduling noise.
That gives you a better success criterion than throughput — see §7.3.

Two honest limits on the above, both recorded by the hosts rather than inferred:

- The pin is a **measurement mode, not a control law.** It drowned four cells: 31–38% of
  packets retransmitted on a 0.63–1.28 Mbps uplink at 5–10 Mbps caps, one keyframe then
  silence (`f40296fc`). Forcing the cap past what the link carries removes the adaptation
  that would otherwise degrade measurably. Worse, a drowned cell **passes** the admission
  gate, because the gate checks the grant held at the cap and the pin guarantees exactly
  that.
- This is **not** a reinstatement of the mechanism withdrawn on 4 September. That one —
  "the estimator lowered its target and the encoder shed pixels to match" — was refuted
  because the bitrate never fell (`b46b4b43`), and the collapse was the encoder hitting the
  H.264 quantiser ceiling on near-incompressible noise (`b499a5db`). Content-driven, not
  CC-driven. **Do not cite the A-series as evidence for anything here.** The current finding
  is on real content and rests on different measurements.

---

## 2. The objective, in one paragraph

A teleoperation client publishes control, audio, video and telemetry from this host over
**one PDU session** on the teleop slice. All four classes share one IP address, one ICE
candidate pair, one PeerConnection. They must leave this host carrying **four different
DSCP marks**, so the network can map each to its own 5QI QoS flow and treat them
differently under congestion. Slices carry tenancy; QoS flows carry media classes. Your job
is the device half: get the module up on the right slice, get the marks onto the packets,
and prove both on the wire.

**What you are NOT doing.** You are not patching the LiveKit SDK. That is a separate branch
(`enable-dscp`) with its own prompt (`enable-dscp-PROMPT.md`), and as of this writing it has
not been done — `grep -rni dscp libwebrtc webrtc-sys/src livekit` returns **zero** matches.
Assume WebRTC media leaves this host **unmarked** and plan around it. §6 tells you how to
make progress anyway, and why that is not a blocker.

---

## 3. Read this before you touch the module

Three facts that decide whether anything you measure means anything.

**3.1 Every silent failure in this system fails the same way.** A wrong slice string, a
missing DSCP mark, an unmarked IPv6 flow, a mismatched packet filter — all of them land
traffic on the session's **default QoS flow** with no error anywhere. The session comes up,
traffic flows, throughput looks fine. It only shows as degraded behaviour under load, weeks
later. **Nothing here is verified by absence of an error message.** Every step has a
positive check and you run it.

**3.2 You have no device-side QFI visibility.** This is listed as an open item in the
reference doc and it is the hard constraint on this whole effort. The module does not expose
which QoS flow a packet was assigned to. So you cannot inspect your way to correctness —
you can only prove it by **differential behaviour under congestion**. Design for that from
the start (§7).

**3.3 A negative from a search is not evidence of absence.** This programme has already paid
for that mistake — see `teleop-test-matrix/docs/RUN-DISCIPLINE.md`, which is required
reading and was written from two days lost to exactly this class of error. If an AT command
returns nothing, that is one piece of evidence of one kind. Corroborate with a different
*kind* — a URC, a counter, a packet capture — not with the same argument restated.

---

## 4. Module bring-up — what to confirm, in order

The reference doc §3 has the commands. Tags matter: `[VERIFIED]` means the syntax was
checked against the vendor manual; `[VERIFY]` means plausible and unconfirmed. **Treat every
`[VERIFY]` as an open question you are closing.** Report which ones you closed and how.

Work in this order and stop at the first thing that does not check out — later steps produce
plausible garbage on top of an earlier failure.

### 4.1 Identify the hardware and firmware first

Before any configuration. Target is Quectel RG650V-NA (SDx72) or RM520N-GL (SDx62); confirm
which you actually have, the firmware revision, and whether it matches what the reference doc
was written against. Record it. Firmware revision is the first thing to suspect when a
documented command behaves differently, and NV settings persist across reboots but not
necessarily across firmware updates.

### 4.2 Radio and USB mode

`AT+QCFG="usbnet",0` — RMNET raw-IP, so the host gets the **real carrier-assigned address**
on its interface. This is load-bearing: without it the modem translates addresses, ICE
advertises an address that does not exist on the network, and packet capture stops meaning
anything. Confirm the host interface actually holds a carrier address afterward, not a
private one.

`AT+QNWPREFCFG="mode_pref",NR5G` is tagged `[VERIFY]`. Confirm you are on **5G standalone**,
not NSA. QoS flows as designed are an SA concept; on NSA this design does not apply and you
need to say so loudly rather than proceed.

### 4.3 The S-NSSAI comma count — highest-risk step in the document

`<S-NSSAI>` is the **17th optional parameter** of `+CGDCONT`. The reference doc says plainly
that the comma count is unforgiving and is the most common source of silent
misconfiguration. If the slice string lands in `<SSC_mode>` or `<Pref_access_type>` instead,
**the context still activates — on the default slice — and nothing reports an error.**

So: set it, then immediately `AT+CGDCONT?` and confirm the string is in the S-NSSAI field
specifically. Not that the command returned OK. Not that the session came up.

**You need the real `sst.sd` hex from the network team.** The reference doc offers
`"01000BBA"` as what SST 1 + differentiator 3002 decimal *would* be, and explicitly says do
not assume — ask. This is an open item with the network team as owner. If you do not have it,
say so and do not substitute a guess; a guessed slice ID is the single most expensive silent
failure available here, because everything downstream will look like it works.

### 4.4 Both sessions up

Per reference doc §3.3 — MPDN rules, auto-connect, then `AT+QMAP="MPDN_status"` to confirm. Auto-connect
matters because it establishes both sessions at power-on, so nothing at runtime has to decide
whether to build one.

Then §7's checks: registered on 5G SA, teleop slice **authorised** (check allowed *and*
check rejected causes — a rejection reason is far more useful than an absence), contexts
carrying the right slice identifier, both sessions up with distinct interfaces and addresses.

---

## 5. Host — interface separation

Reference doc §4. Two sessions, two interfaces, each with a real address. Route teleop to
one and management to the other.

This is ordinary Linux routing and fully observable, so verify it properly: confirm the
teleop process's traffic actually egresses the teleop interface. The failure mode to guard
against is the one Host B already hit on the video rig — **a second interface quietly taking
the default route**, so traffic goes out the wrong path while being recorded as the right
one. See the `never-default` discussion in `teleop-test-matrix/docs/RIG-CHANGES.md`. An
outright outage is easy to spot; a quiet reroute is not.

---

## 6. DSCP marking — and the split you must respect

Reference doc §5 is explicit that there are **two marking sources** and that they must not
fight. This is the part most likely to be got wrong, so be precise about which is which.

### 6.1 WebRTC marks its own media — except it currently does not

The design intends libwebrtc to mark audio/video/data itself via RFC 8837, keyed on
`RtpEncodingParameters.priority`. **Verified in this tree, today:**

- `libwebrtc/src/rtp_parameters.rs:136` declares `priority`
- `:173` defaults it to `Priority::Low`, which maps to **DF (0)** — best-effort
- nothing in `livekit/src` sets `priority` at all (`grep -rn "priority" livekit/src` → empty)
- `enable_dscp` does not exist anywhere in the workspace (0 matches)

So **all four classes are currently marked identically as best-effort** and all land on the
default flow. That is the state of the world; do not assume otherwise because the design
describes something better.

**This does not block you.** It scopes you. Do not patch the SDK from this host — that is
`enable-dscp`'s job and touching the same files from two branches is how they stop being
mergeable. Instead:

1. Get everything **except** WebRTC media marked correctly and proven (§6.2). That is real,
   independently valuable work.
2. For media, use nftables as a **temporary stand-in** so the network-side rules can be
   tested end to end before the SDK lands. Mark it clearly as scaffolding in the commit and
   in the runbook, with the condition for removing it: once `enable_dscp` ships, the host
   rule must come out, or the two marking sources will fight exactly as reference doc §5 warns.
3. Report what the SDK gap costs, so it can be prioritised against real evidence.

### 6.2 nftables marks everything else

Reference doc §5.2 and §5.3. Telemetry, control, and anything non-WebRTC. Use cgroup v2
service isolation per reference doc §5.3 rather than matching on ports where you can — ports are
reassignable and a rule that matches the wrong process is invisible.

### 6.3 IPv6 parity is not optional

This is a listed open item and it fails **silently**. The contexts are `IPV4V6`, so IPv6 is
live. If the marking path sets `IP_TOS` but not `IPV6_TCLASS`, IPv6 flows go out unmarked and
land on the default flow — and a capture filtered only on `ip[1]` looks *identical* to a
working one.

Check both explicitly, with both filters:

```bash
# IPv4, EF (46).  0xb8 = 46 << 2
tcpdump -i <teleop-if> -n -c 50 'ip[1] & 0xfc == 0xb8'

# IPv6, EF (46)
tcpdump -i <teleop-if> -n -c 50 'ip6[0:2] & 0x0fc0 == 0x0b80'
```

The `0xfc` mask ignores the low two ECN bits, which vary with congestion. Filters written
without it work in the lab and fail under load.

If IPv6 cannot be marked, **that is a finding, not a failure.** Report it plainly — it may
be an argument for `IPV4`-only contexts on the teleop session, which is a design decision
above your scope.

---

## 7. Proof — and the honest limit of it

### 7.1 On the wire, from this host

Four marks, present **simultaneously on one 5-tuple**: same source address, same source
port, four code points. Values from reference doc §2 — CS5/40, EF/46, AF41/34, CS1/8.

A capture showing four marks across four connections proves nothing about this design. The
whole claim is per-packet differentiation *inside* one session.

### 7.2 That the network honoured them — the part you cannot inspect

Marks leaving this host prove the host works. They say nothing about whether the network
mapped them to distinct 5QIs. With no device-side QFI visibility, the only available proof
is **differential behaviour under congestion**:

- saturate the uplink so the session is genuinely contended
- confirm the high-priority classes hold latency/loss while the low-priority class degrades
- then flip the marks — deliberately mark control as CS1 and telemetry as CS5 — and confirm
  the degradation follows the **mark**, not the traffic type

That inversion is the test that distinguishes real classification from coincidence. Without
it, "control stayed smooth" is equally explained by control simply being small. **Design the
inversion arm in before you run anything**, not after a good result.

### 7.3 The success criterion that matters most: grant stability

Per §1, the estimator's grant swung **7.3× across four runs minutes apart** on the
unmanaged bearer. If a dedicated 5QI for video is doing its job, that variance should
**fall** — a predictable grant is the whole product, more than any peak number.

So make this an explicit question, and report it whether the answer is good or bad:

> Does the variance of `available_outgoing_bitrate` drop when video is on its own 5QI,
> compared with the same content on the default flow?

This is measurable today and needs no SDK change. `examples/local_video` already logs both
sides of it per stats tick — see `PUBLISHER_CSV_HEADER` at `publisher.rs:661`, which carries
`target_bitrate_mbps` and `available_outgoing_bitrate_mbps` (the latter read off the
**candidate-pair** stat, not outbound-rtp — `find_available_outgoing_bitrate`,
`publisher.rs:729`).

One real gap, if you use the other harness: **`teleop-test-matrix` samples only the
*subscriber's* candidate pair** (`snapshot.rs:248`,
`subscriber_available_outgoing_bitrate_bps`) and has no publisher-side grant field at all.
The publisher's grant is the one that matters here. Either use `local_video`, which has it,
or add the publisher-side field — but do not assume the matrix harness is recording it.

Report grant **against cap** per run. A run is only interpretable when the cap was binding;
where the grant came in lower, congestion control set the rate and you measured the link
rather than your configuration. That rule is already in `e2_sweep_report.py` — reuse it
rather than reinventing it.

**Run with GCC intact for this comparison.** The pin exists to remove the estimator from
the loop, which is exactly the variable you are trying to observe here. Pinning it would
make grant variance trivially zero and prove nothing.

Take the measurement discipline from `teleop-test-matrix/docs/RUN-DISCIPLINE.md` — that
document exists because this programme has already published a mechanism it had to withdraw
in full. Specifically: pre-register what you expect before the run, record the exact argv,
and treat n=1 as a hypothesis.

---

## 8. What to report back

Structure the report around **what is now known that was not before**, not around what you
did.

1. **Hardware and firmware** actually present, versus what the doc assumed.
2. **Every `[VERIFY]` tag you closed**, with the evidence, and every one still open.
3. **The `sst.sd` question** — did you get a real value, or is it still blocked on the
   network team?
4. **Whether the slice string landed in the S-NSSAI field**, shown from `AT+CGDCONT?` output
   rather than asserted.
5. **SA versus NSA**, stated plainly.
6. **Which classes are marked, which are not, and by what** — separating "WebRTC marks it"
   from "nftables stands in for it".
7. **IPv4 versus IPv6 parity**, both filters run.
8. **Any congestion result**, with the inversion arm, or an explicit statement that you could
   not create real contention.
9. **Grant stability (§7.3)** — the variance of `available_outgoing_bitrate` on a dedicated
   5QI versus the default flow, with GCC intact, and grant against cap per run. If the
   variance does not improve, that is the single most valuable negative result available
   from this work: it would mean a dedicated QoS flow does not fix what is actually broken,
   and the effort belongs elsewhere. Report it as prominently as a positive.
10. **What you could not verify.** This is a required section, not a courtesy. An honest gap
   is worth more than a confident inference — this programme lost two days to a claim that
   was argued three times and measured zero times.

### Answers the network team needs from you

These are blocked on device-side data and are on the critical path:

- **The SFU's stable media address and port range** for the downlink 5-tuple rule (§8.2).
  LiveKit in UDP mux mode presents a single port, which makes it a one-line filter — confirm
  that is how it is deployed here.
- **Whether the module can carry four distinct uplink QoS flows at all** on this firmware.
  The reference doc's argument is that QoS rules with packet filters are core session
  management, exercised by every VoNR call — if the module can carry voice, it can classify
  into flows. Confirm or refute that on this hardware.
- **GBR versus non-GBR** for control/audio/video, which changes client failure handling.

---

## 9. Working agreement

- Report the §4.3 S-NSSAI finding **before** configuring anything downstream of it.
- Small commits, each leaving the tree in a working state.
- Touch only device-side and host-side files. **Do not patch `libwebrtc/`, `webrtc-sys/` or
  `livekit/`** — that is `enable-dscp`'s territory and file-disjointness is what keeps the
  branches mergeable.
- Values in reference doc §2 are the source of truth. §10 warns that the DSCP/5QI numbers
  appear in the host firewall rules, the client config and the network rules, with nothing
  enforcing that the three agree. **Regenerate from §2; never edit a copy independently.**
- If a capture contradicts the reference doc, **the capture wins.** Correct the doc and state
  what you observed.
- If two readings of this document would lead to materially different work, ask. Otherwise
  proceed.
