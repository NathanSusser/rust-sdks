# Teleop traffic differentiation — device reference implementation

Differentiated treatment for teleop control, audio, video and telemetry over a single
PDU session on the teleop slice, using DSCP marking mapped to 5QI QoS flows.

Target hardware: Quectel RG650V-NA (SDx72) or RM520N-GL (SDx62).
Host: Linux with systemd, nftables, cgroup v2. Media stack: LiveKit / WebRTC.

AT commands are tagged:

- `[VERIFIED]` — syntax confirmed against the vendor AT command manual
- `[VERIFY]` — plausible but must be confirmed on hardware before relying on it

---

## 1. Architecture

Slices separate **workloads**. QoS flows separate **treatment within a workload**.

```
SLICE 3002 — teleop                      default slice — corporate / management
  one PDU session                          one PDU session
  one source address                       one source address
  one ICE candidate pair                   OTA updates, SSH, fleet management

  UPLINK   vehicle → operator   heavy, four classes
    ├── DSCP CS5   → 5QI 3    control acks
    ├── DSCP EF    → 5QI 1    audio
    ├── DSCP AF41  → 5QI 2    video
    └── DSCP CS1   → 5QI 9    telemetry

  DOWNLINK operator → vehicle   light, one class
    └── 5-tuple from SFU → 5QI 3    control commands + operator voice
```

Two slices, not four. Everything teleop shares one session, one address, one PeerConnection.
Differentiation happens per packet inside that session, where it costs nothing.

**Asymmetric by design.** Contention lives on uplink — several video streams competing with
audio and telemetry from the vehicle. Downlink is control commands and one operator voice
stream, both small, with nothing heavy to compete against. So uplink gets fine-grained
per-class treatment and downlink gets a single elevated flow. This also avoids a hard
dependency: see section 8.3.

### Why this works where per-slice steering did not

Both URSP traffic descriptors (TS 24.526) and QoS rule packet filters (TS 24.501) can match
on type-of-service. The difference is what they select.

URSP selects a **PDU session**. Three sessions means three IP addresses, so the host must
hold a private address and the modem must rewrite it after choosing — address translation,
no per-class visibility, and a broken ICE negotiation because the media stack advertises an
address that does not exist on the network.

QoS rules select a **flow within a session**. All flows share the session's address, so
classification is per packet and nothing downstream has to be corrected. The host keeps its
real routable address, ICE works normally, and packet capture still shows everything.

### Firmware risk

This is the material change. URSP matching on type-of-service is exotic and undocumented in
both vendor manuals. QoS rules with packet filters are core session management, exercised by
every VoNR call on the network. If the module can carry voice, it can classify into QoS flows.

---

## 2. Traffic class map

Source of truth. Every value appears in at least two places and nothing enforces that the
copies agree.

### 2.1 Uplink — vehicle to operator

| Class | DSCP | Value | TOS byte | Mask | 5QI | GBR | PDB | Priority | Marked by |
|---|---|---|---|---|---|---|---|---|---|
| teleop control | CS5 | 40 | 0xA0 | 0xFC | 3 | yes | 50 ms | 30 | nftables |
| audio | EF | 46 | 0xB8 | 0xFC | 1 | yes | 100 ms | 20 | WebRTC |
| video | AF41 | 34 | 0x88 | 0xFC | 2 | yes | 150 ms | 40 | WebRTC |
| telemetry | CS1 | 8 | 0x20 | 0xFC | 9 | no | 300 ms | 90 | nftables |

TOS byte = DSCP << 2. The 0xFC mask ignores the low two ECN bits, which vary with
congestion. Filters written without the mask pass in the lab and fail under load.

Lower 5QI priority value wins. Control beats audio beats video beats telemetry, which is
the correct ordering for a teleoperation control loop.

**Notes on the choices**

- `5QI 2` is conversational video, 150 ms. Not `5QI 4`, which is buffered streaming at
  300 ms and tuned for a player with a jitter buffer rather than a control loop.
- Control is split out from telemetry deliberately. If steering, throttle and brake
  commands ride in the telemetry class at priority 90, the most latency-critical traffic in
  the system sits behind the video that exists only so an operator can generate it. Under
  congestion that degrades exactly backwards.
- `5QI 3` is specified for real-time gaming and V2X — the closest standard profile to
  teleoperation. If T-Mobile supports it, `5QI 80` (10 ms budget, non-GBR, low-latency eMBB)
  is worth asking about for control.

### 2.2 Downlink — operator to vehicle

One QoS flow. Classified at the UPF by 5-tuple from the SFU's media address, not by DSCP.

| Traffic | Classified by | 5QI |
|---|---|---|
| control commands + operator voice | source address and port of the SFU | 3 |

Everything else inbound falls to the session's default flow.

**Why not DSCP here.** The SFU terminates each participant's media session and re-originates
new packets toward the vehicle — new source address, new keys, new IP headers. The operator's
markings are on packets that die at the SFU, so only what the SFU sets on its own egress
matters. LiveKit's server uses Pion, where per-track DSCP marking is an open feature request
rather than a shipped capability: bundled connections share one UDP mux socket, so socket-level
marking cannot separate audio from video, and packet-filter workarounds break under UDP mux.

Since downlink carries no heavy stream, one elevated flow classified by 5-tuple gets the
protection that matters and removes the dependency entirely. Revisit only if downlink grows a
bandwidth-competitive stream.

**Open decision — GBR or not.** `5QI 1`, `2` and `3` are guaranteed bitrate, which means
admission control: the network reserves capacity and can *reject* a flow rather than degrade
it. That needs guaranteed and maximum bitrate values provisioned per flow, and a defined
client behaviour when a flow is refused. The non-GBR alternative (`5QI 7` at priority 70 for
live media, `5QI 9` for the rest) gives priority differentiation with no reservation and no
rejection path. Settle this with the network team before provisioning — it changes the
client's failure handling.

---

## 3. Modem initialisation

Run once at provisioning; settings persist in NV. Re-run on firmware change.

### 3.1 USB mode and radio

```
AT+QCFG="usbnet",0        # RMNET raw-IP — host receives the carrier address  [VERIFIED]
AT+QNWPREFCFG="mode_pref",NR5G        # standalone only                       [VERIFY]
AT+C5GREG=2                            # slice availability URCs              [VERIFIED]
```

RMNET raw-IP gives the host the real carrier-assigned address directly on the network
interface. No modem-side translation. This is what makes ICE work and what keeps packet
capture meaningful.

### 3.2 Define the two contexts

`<S-NSSAI>` is the 17th optional parameter of `+CGDCONT`. The comma count is unforgiving and
is the most common source of silent misconfiguration.

```
AT+CGDCONT=1,"IPV4V6","teleop.tmobile.com","",0,0,,,,,,,,,,,1,"<SST.SD>"
AT+CGDCONT=2,"IPV4V6","corp.tmobile.com","",0,0,,,,,,,,,,,1,""
```

`<SST.SD>` for SLICE 3002 — confirm the hex with the network team. If it is SST 1 with slice
differentiator 3002 decimal, that is `"01000BBA"`. Do not assume; ask.

`[VERIFY]` — do not trust the comma positions. Set them, then immediately:

```
AT+CGDCONT?
```

Confirm the slice string landed in the S-NSSAI field and not in `<SSC_mode>` or
`<Pref_access_type>`. If it landed wrong the context still activates, on the default slice,
and nothing reports an error.

### 3.3 Bring up both sessions

```
AT+QMAP="MPDN_rule",0,1,0,0,1                                        [VERIFIED]
AT+QMAP="MPDN_rule",1,2,0,0,1

AT+QMAP="auto_connect",0,1,1                                         [VERIFIED]
AT+QMAP="auto_connect",1,1,2

AT+QMAP="MPDN_status"                  # confirm both up             [VERIFIED]
```

Auto-connect establishes both sessions at power-on before any application runs, so nothing
at runtime has to decide whether to build a session.

---

## 4. Host — interface separation

Two sessions produce two interfaces, each with its own real address. Route teleop to one and
management to the other. This is ordinary Linux routing and is fully observable.

```bash
ip addr show                       # confirm two carrier addresses
ip route show table all
```

Bind the teleop stack to the teleop interface — either `SO_BINDTODEVICE`, or by source
address in the LiveKit client configuration, or with a routing rule:

```bash
ip rule add from <teleop-carrier-addr> table 100
ip route add default dev wwan0 table 100
```

Keep management traffic on the default route. Nothing here interacts with QoS classification;
the two mechanisms operate at different layers and do not conflict.

---

## 5. Host — DSCP marking

There are **two marking sources**. Keep them from fighting.

### 5.1 WebRTC marks its own media

With DSCP enabled, libwebrtc marks audio `EF` and video `AF41` automatically per media type.
That is exactly the mapping in section 2, so the media needs no external marking.

Enable it in the LiveKit client — the `enable_dscp` media config flag. Verify with a capture
before assuming it took effect; it is off by default in many builds.

```bash
tcpdump -i wwan0 -n 'ip[1] & 0xfc == 0xb8'    # audio, EF
tcpdump -i wwan0 -n 'ip[1] & 0xfc == 0x88'    # video, AF41
```

### 5.2 nftables marks everything else

Only control and telemetry. Do **not** write rules that would overwrite the WebRTC marks —
scope the rules to the cgroups of the non-media services.

`/etc/nftables.d/teleop-marking.nft`

```
table inet teleopmark {
    chain output {
        type route hook output priority mangle; policy accept;

        socket cgroupv2 level 2 "control.slice"   ip  dscp set cs5
        socket cgroupv2 level 2 "control.slice"   ip6 dscp set cs5

        socket cgroupv2 level 2 "telemetry.slice" ip  dscp set cs1
        socket cgroupv2 level 2 "telemetry.slice" ip6 dscp set cs1
    }
}
```

Three things that matter:

- **Both address families.** `ip dscp` only touches IPv4. An IPv6 socket with no matching
  `ip6` rule goes out unmarked and lands on the session's default QoS flow. This is the most
  common way this design fails, and it fails silently.
- **`level 2`** matches `<name>.slice` under the root. Verify against the real path.
- If an application marks itself instead, both socket options are required:

```c
int tos = 40 << 2;                    /* CS5 */
setsockopt(fd, IPPROTO_IP,   IP_TOS,      &tos, sizeof(tos));
setsockopt(fd, IPPROTO_IPV6, IPV6_TCLASS, &tos, sizeof(tos));
```

### 5.3 Service isolation

One cgroup per non-media class, so the firewall can tell traffic apart without modifying
application code.

`/etc/systemd/system/control.service`

```ini
[Unit]
Description=Teleop control channel

[Service]
ExecStart=/usr/local/bin/controld
Slice=control.slice
Restart=always

[Install]
WantedBy=multi-user.target
```

Repeat for `telemetry`. Confirm the paths:

```bash
systemd-cgls | grep -A2 -E 'control|telemetry'
```

---

## 6. Host — slice monitor daemon

Tracks whether the teleop slice is currently permitted. Smaller job than before, but still
the only push notification the modem gives you.

`/usr/local/bin/slice-monitor`

```python
#!/usr/bin/env python3
import json, re, sys, time, serial

PORT = "/dev/ttyUSB2"
PLMN = "310260"
TELEOP = "01000bba"          # confirm against the network team's value
STATE = "/run/slice-state.json"

reg = re.compile(r"\+C5GREG:\s*(.*)")


def parse_allowed(payload):
    for field in reversed(re.findall(r'"([^"]*)"', payload)):
        if field and (":" in field or len(field) >= 8):
            return [s.strip().lower() for s in field.split(":") if s.strip()]
    return []


def publish(allowed):
    ok = TELEOP in allowed
    with open(STATE, "w") as fh:
        json.dump({"ts": int(time.time()), "allowed": allowed, "teleop": ok}, fh)
    if ok:
        print("OK teleop slice allowed", flush=True)
    else:
        print("DEGRADED teleop slice not in allowed nssai", file=sys.stderr, flush=True)


def main():
    port = serial.Serial(PORT, 115200, timeout=1)

    def send(cmd):
        port.write((cmd + "\r\n").encode())
        time.sleep(0.4)
        return port.read(port.in_waiting or 1).decode(errors="ignore")

    send("ATE0")
    send("AT+C5GREG=2")
    publish(parse_allowed(send("AT+C5GREG?")))

    last_poll = time.time()
    while True:
        line = port.readline().decode(errors="ignore").strip()
        if line:
            m = reg.match(line)
            if m:
                publish(parse_allowed(m.group(1)))
        if time.time() - last_poll > 300:
            publish(parse_allowed(send("AT+C5GREG?")))
            last_poll = time.time()


if __name__ == "__main__":
    main()
```

`parse_allowed` is deliberately defensive. Capture a real `+C5GREG` line from the test SIM
before trusting it — the field ordering and quoting of the allowed-NSSAI string is the part
most likely to differ from the manual's abstract syntax.

---

## 7. Verification

```
# 1  registered on 5G standalone
AT+QNWINFO
AT+C5GREG?

# 2  teleop slice authorised — check allowed, and check rejected for causes
AT+C5GNSSAIRDP=3,"310260"

# 3  contexts carry the right slice identifier
AT+CGDCONT?

# 4  both sessions up
AT+QMAP="MPDN_status"
```

Host side:

```bash
ip addr show                                   # two real carrier addresses
tcpdump -i wwan0 -n 'ip[1] & 0xfc == 0xb8'     # audio marked EF
tcpdump -i wwan0 -n 'ip[1] & 0xfc == 0x88'     # video marked AF41
tcpdump -i wwan0 -n 'ip[1] & 0xfc == 0xa0'     # control marked CS5
tcpdump -i wwan0 -n 'ip[1] & 0xfc == 0x20'     # telemetry marked CS1
cat /run/slice-state.json
```

Repeat each capture for IPv6 if the stack is dual-stack. Unmarked IPv6 is the failure this
build is most likely to hit.

**What the host cannot see:** which QoS flow a packet was assigned to. There is no AT command
reporting QFI on these modules. The failure mode is gentler than before — wrong priority
rather than wrong slice and wrong gateway — but it still means the behavioural test is a
congestion test, not an inspection.

**Behavioural verification.** Saturate the link and confirm the ordering holds: control
latency stays flat, audio holds, video degrades before audio, telemetry degrades first. That
is the test that actually proves the QoS rules are installed and matching.

Network side: per-flow counters on the teleop slice should show four QFIs carrying traffic in
roughly the expected proportions.

---

## 8. The ask to the network team

Routine provisioning, not a special request. Two directions, different mechanisms.

### 8.1 Uplink — four flows, classified by DSCP

> On SLICE 3002, provision uplink QoS rules mapping these DSCP values to these 5QIs:
>
> | DSCP | Value | 5QI |
> |---|---|---|
> | CS5 | 40 | 3 |
> | EF | 46 | 1 |
> | AF41 | 34 | 2 |
> | CS1 | 8 | 9 |
>
> Packet filters to match on type-of-service / traffic class with mask 0xFC.
> Unmatched traffic to the session's default QoS flow.

### 8.2 Downlink — one flow, classified by 5-tuple

> On SLICE 3002, provision one downlink QoS rule mapping traffic from the SFU's media
> address and port to 5QI 3. Unmatched traffic to the session's default flow.

Needs the SFU's stable media address and port range. LiveKit in UDP mux mode presents a
single port, which makes this a one-line filter.

### 8.3 SFU placement and DSCP integrity

Not needed for the downlink rule above, but required if downlink is ever split into multiple
classes:

- AWS Direct Connect **preserves DSCP** — AWS does not manage QoS on DX and passes markings
  through. The cloud transit path is not the problem.
- **Check T-Mobile's own ingress policy.** Carriers commonly bleach or re-mark DSCP on
  traffic arriving from external peers by default. Intact bits over DX do not help if the
  edge zeroes them on arrival.
- **DX capacity.** No managed QoS on the link means no protection against oversubscription.
  If the DX port congests, marked and unmarked traffic degrade together.

### 8.4 Also confirm

1. **GBR or non-GBR** for control, audio and video — see section 2.1. If GBR, the guaranteed
   and maximum bitrate values per flow, and the expected client behaviour on rejection.
2. **The `sst.sd` hex** for SLICE 3002, and the DNN for the teleop session.
3. **Is `5QI 3` available**, or `5QI 80` if a tighter delay budget is supported for control.

---

## 9. Open items

| Item | Impact | Owner |
|---|---|---|
| Control channel currently unsplit from telemetry | control loop at lowest priority under congestion | app team |
| GBR vs non-GBR undecided | changes client failure handling and provisioning | network + app |
| `enable_dscp` not yet enabled in the LiveKit client | uplink media unmarked, everything lands on default flow | app team |
| IPv6 marking parity | silent fallback to default flow | device |
| No device-side QFI visibility | correctness proven by congestion test, not inspection | ops |
| `sst.sd` for SLICE 3002 unconfirmed | context activates on the wrong slice, silently | network |
| T-Mobile edge DSCP ingress policy unchecked | only matters if downlink is later split | network |
| SFU media address and port range not yet fixed | downlink rule needs a stable 5-tuple | app team |

**Deferred, not open.** Per-track DSCP marking on the LiveKit server is an open Pion feature
request and is deliberately not on the critical path. The downlink design in section 2.2
requires nothing from it. Revisit only if a bandwidth-competitive downlink stream appears —
at which point the Pion issue is the lever, and carrier weight behind it will move faster
than a packet-filter workaround.

---

## 10. Change control

The DSCP values and 5QI mappings in section 2 appear in the host firewall rules, the WebRTC
client configuration, and the network QoS rules. Nothing enforces that the three copies
agree, and every mismatch fails the same way: traffic falls to the session's default flow,
silently, and only shows up as degraded behaviour under load. Regenerate the others from
section 2 rather than editing them independently.
