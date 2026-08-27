# 5G slice steering — device reference implementation

Three traffic classes (telemetry, audio, video) onto three network slices, on a Linux
host attached to a Quectel 5G module, using URSP as the steering mechanism.

Target hardware: Quectel RG650V-NA (SDx72) or RM520N-GL (SDx62).
Host: Linux with systemd, nftables, cgroup v2.

AT commands are tagged:

- `[VERIFIED]` — syntax confirmed against the vendor AT command manual
- `[VERIFY]` — plausible but must be confirmed on hardware before relying on it

---

## 1. Slice map

The contract between the network team, the device integrator, and the application team.
Every value here appears in at least two places, and nothing enforces that the copies
agree. Treat this table as the source of truth and generate the rest from it.

| Class | DSCP name | DSCP | TOS byte | Mask | SST | SD | DNN | Profile |
|---|---|---|---|---|---|---|---|---|
| telemetry | CS1 | 8 | 0x20 | 0xFC | 1 | 0x000001 | telemetry.tmobile.com | 1 |
| audio | EF | 46 | 0xB8 | 0xFC | 1 | 0x000002 | voice.tmobile.com | 2 |
| video | AF41 | 34 | 0x88 | 0xFC | 1 | 0x000003 | video.tmobile.com | 3 |

TOS byte = DSCP << 2. The mask 0xFC ignores the low two ECN bits, which vary with
congestion. Matching without the mask produces rules that pass in the lab and fail
under load.

**Default rule.** URSP requires a lowest-precedence match-all rule for unclassified
traffic. Do not point it at telemetry: telemetry is the bootstrap and management channel,
and misclassified bulk traffic landing there removes observability at the moment it is
most needed. Point it at a general-purpose slice.

---

## 2. How this works, and what it costs

### 2.1 URSP runs at connection setup, not per packet

3GPP designed URSP as a flow-setup-time policy. On a phone the sequence is:

```
app requests a connection
  → framework asks telephony
    → URSP evaluates, selects slice
      → framework binds the socket to that session
        → app sends packets already carrying that session's source address
```

The address is assigned **before** the first packet is written. Three sessions, three
real addresses, no address translation, applications aware of their own routable address.

### 2.2 What is missing on Linux

Not URSP — the binding step. Android has an API for step 4 (the framework receives a
network handle from telephony and binds a socket to it). Linux has no equivalent. Nothing
lets a process ask the modem which network a flow belongs on and receive a socket on it.

Without that step the decision has nowhere to live at setup time, so it moves downstream
to the only place left: inside the modem, after the packet is written.

```
phone:   decide → bind → write packet        no address translation
linux:   write packet → decide → rewrite     address translation required
```

### 2.3 What that entails

Because the decision happens after the host has written the packet:

- The host must present **one interface** with one private address. If it held the three
  carrier addresses, choosing a source address would mean choosing the session, and URSP
  would be bypassed.
- The modem must **rewrite the source address** when it selects the session. That is NAT,
  and it is not a design preference — it is the only mechanism that can change a source
  address on a packet that has already been written.
- The host can observe **intent, never outcome**. A capture shows the DSCP mark the packet
  left with. It cannot show which session the modem chose. A rule that failed to match
  produces a byte-identical capture.
- Applications advertising their own address in a message body (SDP being the common case)
  will advertise the private address. See section 8.

None of this is caused by URSP. It is what remains when the half of URSP's design that
lives above the modem is absent.

### 2.4 Target architecture

The version worth working toward, which removes every cost above:

1. Three sessions as three netdevs, each holding its real carrier address — no NAT.
2. A host daemon reads the modem's active URSP rules.
3. The daemon translates them into host binding and routing policy.
4. Applications bind normally.

Network-authored policy, real addresses, working SDP, per-interface capture, and T-Mobile
still owns the mapping. Steps 1, 3 and 4 are available today. The architecture is blocked
entirely on step 2 — there is no host-readable interface to the modem's URSP rule set.
See section 9.

---

## 3. Modem initialisation

Run once at provisioning; settings persist in modem NV. Re-run on firmware change.

### 3.1 USB mode and radio

```
AT+QCFG="usbnet",1        # 0 RMNET · 1 ECM · 2 MBIM · 3 RNDIS      [VERIFIED]
AT+QNWPREFCFG="mode_pref",NR5G        # standalone only             [VERIFY]
AT+C5GREG=2                            # slice-list URCs on         [VERIFIED]
```

ECM gives the host a single Ethernet interface served by the modem's DHCP, which is the
addressing model section 2.3 requires. `usbnet` writes NVM and needs a reboot.

`AT+C5GREG=2` enables the unsolicited report that fires on cell change in 5GS and whenever
the network provides an allowed NSSAI. This is the device's only native slice-availability
event and the input to the monitor daemon.

Optionally set the host-facing subnet:

```
AT+QMAP="LANIP"                                                      [VERIFIED]
```

### 3.2 Seed the fallback slice list

Covers cold boot against a PLMN the module has no stored context for.

```
AT+C5GNSSAI=12,"01000001:01000002:01000003"                          [VERIFY]
```

Length is in octets: three S-NSSAIs at 4 octets each. Confirm the module's expected string
form by reading back with `AT+C5GNSSAIRDP=0,"310260"`. The encoding of the colon-separated
list is the field most likely to differ from the example.

### 3.3 Define the three contexts

`<S-NSSAI>` is the 17th optional parameter of `+CGDCONT`. The comma count is unforgiving
and is the most common source of silent misconfiguration in this build.

```
AT+CGDCONT=1,"IPV4V6","telemetry.tmobile.com","",0,0,,,,,,,,,,,1,"01000001"
AT+CGDCONT=2,"IPV4V6","voice.tmobile.com","",0,0,,,,,,,,,,,1,"01000002"
AT+CGDCONT=3,"IPV4V6","video.tmobile.com","",0,0,,,,,,,,,,,1,"01000003"
```

`[VERIFY]` — do not trust the comma positions. Set them, then immediately:

```
AT+CGDCONT?
```

Confirm the slice string landed in the S-NSSAI field and not in `<SSC_mode>` or
`<Pref_access_type>`. If it landed wrong the context still activates — on the default
slice — and nothing reports an error.

### 3.4 Bring up three data calls behind one interface

All three bind to the default LAN (VLAN ID 0) so the host sees a single interface.
Parameters are `<rule_num>,<profileID>,<VLAN_ID>,<IPPT_mode>,<auto_connect>`. Passthrough
must be off; the modem needs to NAT.

```
AT+QMAP="MPDN_rule",0,1,0,0,1                                        [VERIFIED]
AT+QMAP="MPDN_rule",1,2,0,0,1
AT+QMAP="MPDN_rule",2,3,0,0,1

AT+QMAP="auto_connect",0,1,1                                         [VERIFIED]
AT+QMAP="auto_connect",1,1,2
AT+QMAP="auto_connect",2,1,3

AT+QMAP="SFE",1                        # forwarding acceleration     [VERIFY]
```

Rule numbers are 0–3, so four concurrent data calls are available.

**Auto-connect is the most valuable single setting here.** The modem establishes all three
sessions at power-on before any application runs. This removes the establishment race
entirely: by the time a packet exists, all three destinations exist, and the runtime
problem collapses from "decide whether to build a session, build it, then use it" to
"select one of three."

---

## 4. Host — service isolation

One cgroup per class. This is what lets the firewall tell traffic apart without modifying
application code.

`/etc/systemd/system/telemetry.service`

```ini
[Unit]
Description=Telemetry agent
After=slice-monitor.service

[Service]
ExecStart=/usr/local/bin/telemetryd
Slice=telemetry.slice
Restart=always

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/telemetry.slice`

```ini
[Unit]
Description=Telemetry traffic class
```

Repeat for `audio` and `video`. The slice unit gives a stable cgroup path for nftables to
match on. Confirm the actual paths before writing firewall rules:

```bash
systemd-cgls | grep -A2 -E 'telemetry|audio|video'
cat /proc/$(pidof telemetryd)/cgroup
```

---

## 5. Host — packet marking

`/etc/nftables.d/slice-marking.nft`

```
table inet slicemark {
    chain output {
        type route hook output priority mangle; policy accept;

        socket cgroupv2 level 2 "telemetry.slice" ip  dscp set cs1
        socket cgroupv2 level 2 "telemetry.slice" ip6 dscp set cs1

        socket cgroupv2 level 2 "audio.slice"     ip  dscp set ef
        socket cgroupv2 level 2 "audio.slice"     ip6 dscp set ef

        socket cgroupv2 level 2 "video.slice"     ip  dscp set af41
        socket cgroupv2 level 2 "video.slice"     ip6 dscp set af41
    }
}
```

Three things that matter:

- **Both address families.** `ip dscp` only touches IPv4. An IPv6 socket with no matching
  `ip6` rule goes out unmarked and falls to the default rule. This is the most common way
  this design fails, and it fails silently.
- **`level 2`** matches `<name>.slice` under the root. Verify against the real cgroup path
  from section 4; the level depends on slice nesting.
- Applications may mark themselves instead. Both socket options are required:

```c
int tos = 46 << 2;
setsockopt(fd, IPPROTO_IP,   IP_TOS,      &tos, sizeof(tos));
setsockopt(fd, IPPROTO_IPV6, IPV6_TCLASS, &tos, sizeof(tos));
```

Load and confirm:

```bash
nft -f /etc/nftables.d/slice-marking.nft
nft list table inet slicemark
```

Confirm the modem preserves the marking through NAT. It should — translation touches
addresses and ports, not the class field — but it is a two-minute check.

---

## 6. Host — slice monitor daemon

Tracks which slices the network currently permits and alarms when the device is running
degraded. This is the only device-side visibility that exists in this architecture.

`/usr/local/bin/slice-monitor`

```python
#!/usr/bin/env python3
import json, re, sys, time, serial

PORT = "/dev/ttyUSB2"
PLMN = "310260"
EXPECTED = {"01000001": "telemetry", "01000002": "audio", "01000003": "video"}
STATE = "/run/slice-state.json"

reg = re.compile(r"\+C5GREG:\s*(.*)")


def parse_allowed(payload):
    """Pull the colon-separated S-NSSAI list out of a +C5GREG line."""
    for field in reversed(re.findall(r'"([^"]*)"', payload)):
        if field and (":" in field or len(field) >= 8):
            return [s.strip().lower() for s in field.split(":") if s.strip()]
    return []


def publish(allowed):
    present = {EXPECTED[s]: True for s in allowed if s in EXPECTED}
    missing = [n for n in EXPECTED.values() if n not in present]
    state = {
        "ts": int(time.time()),
        "allowed": allowed,
        "available": sorted(present),
        "missing": missing,
        "degraded": bool(missing),
    }
    with open(STATE, "w") as fh:
        json.dump(state, fh)
    if missing:
        print(f"DEGRADED missing={','.join(missing)}", file=sys.stderr, flush=True)
    else:
        print("OK all three slices allowed", flush=True)


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

Run in order. Each step is meaningless until the previous one passes.

```
# 1  registered on 5G standalone
AT+QNWINFO
AT+C5GREG?

# 2  all three slices authorised — check allowed, and check rejected for causes
AT+C5GNSSAIRDP=3,"310260"

# 3  contexts carry the right slice identifiers
AT+CGDCONT?

# 4  all three data calls up
AT+QMAP="MPDN_status"
```

Host side:

```bash
# marking applied, both families
tcpdump -i wwan0 -v -n 'ip[1] & 0xfc == 0xb8'
tcpdump -i wwan0 -v -n 'ip6[0:2] & 0x0fc0 == 0x0b80'

cat /run/slice-state.json
```

**This confirms intent only.** It shows the packet left asking for the audio class. It
cannot show which session the modem chose.

Network side — the only place ground truth exists:

- per-slice byte counters at the gateway show traffic on the expected slice
- deliberately unmarked traffic appears on the default slice, not nowhere

---

## 8. Media traversal

Independent of slicing, and capable of sinking the project on its own.

Because the modem NATs, an application that advertises its own address inside a message
body — SDP being the common case — will advertise the private address. Signalling succeeds,
the call connects, and media goes nowhere. Classic one-way audio.

In order of preference:

1. **Confirm the media path terminates on a T-Mobile SBC or media gateway.** Carrier voice
   infrastructure is built expecting NATed clients and typically latches onto the source
   address seen on the wire rather than trusting the body. If this is already true, the
   problem may already be solved. Check this first.
2. STUN so the application learns its public address, ICE for candidate negotiation, or a
   TURN relay.
3. The module's SIP ALG. Last resort — ALGs are fragile and cause stranger failures than
   they fix.

---

## 9. Open questions for Quectel and Qualcomm

Two questions, in the same conversation with field engineering. The second is worth far
more than the first.

**1. Does the firmware support type-of-service / traffic-class traffic descriptors in URSP
matching?**

Everything in this build rests on it. If the answer is no, the modem rejects the policy
outright — the observed failure is a `Manage UE Policy Command Reject` with a protocol
error cause — and this architecture does not exist on this hardware.

**2. Is there, or can there be, a host-readable interface to the active URSP rule set over
AT or QMI?**

Traffic descriptors, route selection descriptors, precedence, current validity. Qualcomm's
policy engine already holds a parsed rule set and surfaces it to the Android framework
through the HAL; what is missing is a path to a non-Android host.

A yes here unlocks section 2.4: three real addresses, no NAT, working SDP, per-interface
capture, and network-authored policy at the same time. It is the difference between Linux
being a workaround platform for slicing and a first-class one.

Frame it as what a carrier needs to deploy slicing on devices that are not phones. That is
a market both vendors want, and T-Mobile has leverage an individual integrator does not.

---

## 10. Known gaps

| Gap | Impact | Owner |
|---|---|---|
| Traffic-class matching in firmware unproven | architecture does not exist if unsupported | device + Quectel |
| Policy steering across sessions in router mode unproven | same | device + Quectel |
| No host-readable URSP rule set | blocks the no-NAT target architecture | Quectel + Qualcomm |
| NAT traversal for audio and video | media may fail independently of slicing | app team + network |
| No device-side per-flow slice visibility | correctness cannot be verified from the device | ops |
| Failure behaviour when a slice drops mid-session | undefined; device cannot detect it | network + app |
| Default rule target not yet chosen | must not be the management channel | network |

---

## 11. Change control

The DSCP values, SST/SD pairs and DNN strings in section 1 appear in the modem contexts,
the host firewall rules, and the network policy rules. Nothing enforces that the three
copies agree, and every mismatch fails the same way: traffic falls to the default rule,
silently. Regenerate the other three from section 1 rather than editing them independently.
