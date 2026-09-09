# PROMPT — Module bring-up and DSCP tagging on the modem host

For the Claude Code session on the **Linux host wired to the 5G module**. Paste
everything below the line.

Read `5g-teleop-qos-device-reference.md` in this repo first — it is the design and the
source of truth for every DSCP and 5QI value. This document does not restate it. It says
what to **find out**, in what order, and what counts as proof.

---

## 0. The objective, in one paragraph

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
Assume WebRTC media leaves this host **unmarked** and plan around it. §4 tells you how to
make progress anyway, and why that is not a blocker.

---

## 1. Read this before you touch the module

Three facts that decide whether anything you measure means anything.

**1.1 Every silent failure in this system fails the same way.** A wrong slice string, a
missing DSCP mark, an unmarked IPv6 flow, a mismatched packet filter — all of them land
traffic on the session's **default QoS flow** with no error anywhere. The session comes up,
traffic flows, throughput looks fine. It only shows as degraded behaviour under load, weeks
later. **Nothing here is verified by absence of an error message.** Every step has a
positive check and you run it.

**1.2 You have no device-side QFI visibility.** This is listed as an open item in the
reference doc and it is the hard constraint on this whole effort. The module does not expose
which QoS flow a packet was assigned to. So you cannot inspect your way to correctness —
you can only prove it by **differential behaviour under congestion**. Design for that from
the start (§5).

**1.3 A negative from a search is not evidence of absence.** This programme has already paid
for that mistake — see `teleop-test-matrix/docs/RUN-DISCIPLINE.md`, which is required
reading and was written from two days lost to exactly this class of error. If an AT command
returns nothing, that is one piece of evidence of one kind. Corroborate with a different
*kind* — a URC, a counter, a packet capture — not with the same argument restated.

---

## 2. Module bring-up — what to confirm, in order

The reference doc §3 has the commands. Tags matter: `[VERIFIED]` means the syntax was
checked against the vendor manual; `[VERIFY]` means plausible and unconfirmed. **Treat every
`[VERIFY]` as an open question you are closing.** Report which ones you closed and how.

Work in this order and stop at the first thing that does not check out — later steps produce
plausible garbage on top of an earlier failure.

### 2.1 Identify the hardware and firmware first

Before any configuration. Target is Quectel RG650V-NA (SDx72) or RM520N-GL (SDx62); confirm
which you actually have, the firmware revision, and whether it matches what the reference doc
was written against. Record it. Firmware revision is the first thing to suspect when a
documented command behaves differently, and NV settings persist across reboots but not
necessarily across firmware updates.

### 2.2 Radio and USB mode

`AT+QCFG="usbnet",0` — RMNET raw-IP, so the host gets the **real carrier-assigned address**
on its interface. This is load-bearing: without it the modem translates addresses, ICE
advertises an address that does not exist on the network, and packet capture stops meaning
anything. Confirm the host interface actually holds a carrier address afterward, not a
private one.

`AT+QNWPREFCFG="mode_pref",NR5G` is tagged `[VERIFY]`. Confirm you are on **5G standalone**,
not NSA. QoS flows as designed are an SA concept; on NSA this design does not apply and you
need to say so loudly rather than proceed.

### 2.3 The S-NSSAI comma count — highest-risk step in the document

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

### 2.4 Both sessions up

Per §3.3 — MPDN rules, auto-connect, then `AT+QMAP="MPDN_status"` to confirm. Auto-connect
matters because it establishes both sessions at power-on, so nothing at runtime has to decide
whether to build one.

Then §7's checks: registered on 5G SA, teleop slice **authorised** (check allowed *and*
check rejected causes — a rejection reason is far more useful than an absence), contexts
carrying the right slice identifier, both sessions up with distinct interfaces and addresses.

---

## 3. Host — interface separation

Reference doc §4. Two sessions, two interfaces, each with a real address. Route teleop to
one and management to the other.

This is ordinary Linux routing and fully observable, so verify it properly: confirm the
teleop process's traffic actually egresses the teleop interface. The failure mode to guard
against is the one Host B already hit on the video rig — **a second interface quietly taking
the default route**, so traffic goes out the wrong path while being recorded as the right
one. See the `never-default` discussion in `teleop-test-matrix/docs/RIG-CHANGES.md`. An
outright outage is easy to spot; a quiet reroute is not.

---

## 4. DSCP marking — and the split you must respect

Reference doc §5 is explicit that there are **two marking sources** and that they must not
fight. This is the part most likely to be got wrong, so be precise about which is which.

### 4.1 WebRTC marks its own media — except it currently does not

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

1. Get everything **except** WebRTC media marked correctly and proven (§4.2). That is real,
   independently valuable work.
2. For media, use nftables as a **temporary stand-in** so the network-side rules can be
   tested end to end before the SDK lands. Mark it clearly as scaffolding in the commit and
   in the runbook, with the condition for removing it: once `enable_dscp` ships, the host
   rule must come out, or the two marking sources will fight exactly as §5 warns.
3. Report what the SDK gap costs, so it can be prioritised against real evidence.

### 4.2 nftables marks everything else

Reference doc §5.2 and §5.3. Telemetry, control, and anything non-WebRTC. Use cgroup v2
service isolation per §5.3 rather than matching on ports where you can — ports are
reassignable and a rule that matches the wrong process is invisible.

### 4.3 IPv6 parity is not optional

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

## 5. Proof — and the honest limit of it

### 5.1 On the wire, from this host

Four marks, present **simultaneously on one 5-tuple**: same source address, same source
port, four code points. Values from reference doc §2 — CS5/40, EF/46, AF41/34, CS1/8.

A capture showing four marks across four connections proves nothing about this design. The
whole claim is per-packet differentiation *inside* one session.

### 5.2 That the network honoured them — the part you cannot inspect

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

Take the measurement discipline from `teleop-test-matrix/docs/RUN-DISCIPLINE.md` — that
document exists because this programme has already published a mechanism it had to withdraw
in full. Specifically: pre-register what you expect before the run, record the exact argv,
and treat n=1 as a hypothesis.

---

## 6. What to report back

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
9. **What you could not verify.** This is a required section, not a courtesy. An honest gap
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

## 7. Working agreement

- Report the §2.3 S-NSSAI finding **before** configuring anything downstream of it.
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
