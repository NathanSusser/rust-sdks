# Host B (Subscriber / PTP Slave) — Setup Runbook

**You are the agent operating HOST B.** Host B subscribes to video, slaves its
clock to Host A over the direct cable, and produces the final PDF report.

Host A has its own runbook. Where the two must agree, this file says so. **Two
verifications in this runbook are authoritative for the whole rig** — the PTP
offset (Phase 8) and the clock-skew check on real data (Phase 10). Host A cannot
perform either; you must.

---

## What is being built and why

Two Linux machines measure one-way video latency over 5G. The publisher stamps a
frame as it hands it to WebRTC; you stamp the same frame as its first packet
arrives. The difference is transport latency.

That subtraction spans two machines' clocks, so **its error is the offset between
them.** Public NTP is routinely off by tens to hundreds of milliseconds; transport
here is roughly 10–60 ms. NTP error would exceed the signal.

So a **dedicated Ethernet cable carries PTP only**, while video stays on 5G.
Keeping the timing path off the measured path is deliberate: sharing them would let
congestion move clocks and latency together, and correlated error is much harder to
spot than obvious error.

```
   ┌──────────────┐                          ┌──────────────┐
   │   HOST A     │   direct Ethernet        │   HOST B     │
   │  publisher   │═══ PTP only ════════════▶│  subscriber  │
   │  grandmaster │   192.168.99.1/30        │  192.168.99.2/30
   └──────┬───────┘                          └──────┬───────┘
          │ 5G modem                                │ 5G modem
          └────────────► LiveKit SFU ◄──────────────┘
                        (video path)
```

**The invariant:** the direct link carries PTP and nothing else — never video,
never the default route. Several checks exist only to enforce that.

---

## Phase 0 — Identify the hardware

```bash
ip -br link
```

Distinguish the 5G/WWAN interface from the wired Ethernet one. **The wired one is
the direct link.**

```bash
export ETH_LINK=enp1s0     # <-- your actual wired interface
export WAN_LINK=wwan0      # <-- your actual 5G interface
```

Confirm you have them the right way round:

```bash
ip route get 8.8.8.8
```

The interface named there is your **WAN**. If it equals `$ETH_LINK`, they are
reversed — stop and re-identify.

### 0.1 Timestamping capability

```bash
sudo ethtool -T "$ETH_LINK"
```

| `PTP Hardware Clock:` | Tier | Expect |
|---|---|---|
| a number (`0`, `1`, …) | **hardware** | < 1 µs |
| `none` | **software** | 10–50 µs |

**Ask Host A which tier it reported, and use the same one.** Mismatched tiers still
sync, but the defensible accuracy becomes the weaker of the two.

Either tier is vastly better than NTP and sufficient for a 10 ms measurement.

---

## Phase 1 — Physical

Cable runs directly between the two hosts; no switch. Then:

```bash
ip -br link show "$ETH_LINK"
sudo ethtool "$ETH_LINK" | grep -E "Speed|Duplex|Link detected"
```

**Pass:** `Link detected: yes`, full duplex. If `no`, reseat the cable and confirm
Host A's NIC is up.

---

## Phase 2 — Addressing

A `/30` holds exactly two hosts, which structurally prevents a third device or a
wider misroute.

**Host B is `192.168.99.2`. Host A is `192.168.99.1`.** Do not swap.

### NetworkManager

```bash
sudo nmcli connection add type ethernet ifname "$ETH_LINK" con-name ptp-link \
  ipv4.method manual \
  ipv4.addresses 192.168.99.2/30 \
  ipv4.never-default yes \
  ipv6.method disabled \
  connection.autoconnect yes

sudo nmcli connection up ptp-link
```

`ipv4.never-default yes` forbids this link from becoming the default route, so
video cannot silently leave via the cable. No gateway is configured, by design.

### systemd-networkd

`/etc/systemd/network/10-ptp-link.network`:

```ini
[Match]
Name=enp1s0

[Network]
Address=192.168.99.2/30
# No Gateway= line, deliberately.
IPv6AcceptRA=no
LinkLocalAddressing=no

[Link]
RequiredForOnline=no
```

```bash
sudo systemctl restart systemd-networkd
```

### Verify

```bash
ip -br addr show "$ETH_LINK"           # expect 192.168.99.2/30
ip route | grep -c "^default.*$ETH_LINK"   # expect 0
ping -c 3 -I "$ETH_LINK" 192.168.99.1      # expect replies
```

If ping fails but the link is up, Host A has not finished Phase 2 — wait.

---

## Phase 3 — Routing safety

```bash
echo "--- default route (must be 5G) ---"
ip route show default

echo "--- SFU traffic path ---"
ip route get 8.8.8.8

echo "--- peer traffic path ---"
ip route get 192.168.99.1
```

**Pass:** the first two name `$WAN_LINK`; the third names `$ETH_LINK`.

If a default route sits on the direct link:

```bash
sudo ip route del default dev "$ETH_LINK"
```

and fix the cause (`never-default`, or a stray `Gateway=`).

---

## Phase 4 — Disable NTP

chrony/timesyncd and ptp4l both discipline the system clock; together they fight
and the clock slews unpredictably — indistinguishable from the error being removed.

```bash
sudo systemctl disable --now chronyd 2>/dev/null || true
sudo systemctl disable --now chrony 2>/dev/null || true
sudo systemctl disable --now systemd-timesyncd 2>/dev/null || true
timedatectl set-ntp false 2>/dev/null || true
timedatectl | grep -i "NTP service"    # expect: inactive
```

> Set the clock roughly right first (`sudo chronyd -q`) so PTP starts from a small
> offset. It will converge regardless, but a large initial gap is slow and confusing.

---

## Phase 5 — PTP slave

```bash
sudo apt update && sudo apt install -y linuxptp ethtool
```

`/etc/linuxptp/ptp4l-B.conf`:

```ini
[global]
# 255 is the lowest priority: B must never win the BMCA and become grandmaster.
# If B ever does, the two clocks are syncing to each other in a loop and the
# offset is meaningless.
priority1               255
clockClass              248
delay_mechanism         E2E
network_transport       L2
logSyncInterval         -3
logMinDelayReqInterval  -3
tx_timestamp_timeout    50
summary_interval        0

[enp1s0]
```

Set `[enp1s0]` to your real `$ETH_LINK`. **Software tier only** — add
`time_stamping software` under `[global]`.

```bash
sudo ptp4l -f /etc/linuxptp/ptp4l-B.conf -m
```

**Pass:** log shows `new foreign master`, then `s0` → `s1` → `s2` (locked).
`master offset` should fall toward zero.

**Fail:** if B says `assuming the grand master role`, it is not seeing A. Check the
cable and that A's ptp4l is running. Do not proceed — a self-referential sync
produces confident, meaningless numbers.

Leave running; new terminal for the next phase.

---

## Phase 6 — Discipline the system clock

**Do not skip this.**

`ptp4l` disciplines the **NIC's** hardware clock. The subscriber calls
`clock_gettime()`, which reads the **system** clock. Without `phc2sys` they are
unrelated: PTP locks perfectly, `pmc` reports excellent offsets, and every CSV
timestamp is still unsynchronized. The failure looks exactly like success — it is
the single most common way this setup silently produces wrong data.

**Hardware tier:**

```bash
sudo phc2sys -s "$ETH_LINK" -w -m -O 0
```

**Software tier:** skip it; `ptp4l` already disciplines the system clock.

**Pass:** offsets settle to tens/hundreds of **nanoseconds** with `s2`:

```
phc2sys[123.4]: CLOCK_REALTIME phc offset  -27 s2 freq  -1102 delay 942
```

---

## Phase 7 — Persistence (optional)

```bash
sudo tee /etc/systemd/system/ptp4l-B.service >/dev/null <<'EOF'
[Unit]
Description=PTP slave on the direct link
After=network-online.target

[Service]
ExecStart=/usr/sbin/ptp4l -f /etc/linuxptp/ptp4l-B.conf
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/phc2sys-B.service >/dev/null <<'EOF'
[Unit]
Description=Sync system clock to PHC
After=ptp4l-B.service
Requires=ptp4l-B.service

[Service]
ExecStart=/usr/sbin/phc2sys -s enp1s0 -w -O 0
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ptp4l-B phc2sys-B
```

---

## Phase 8 — Offset verification (**authoritative — you own this**)

```bash
sudo pmc -u -b 0 'GET CURRENT_DATA_SET'
```

Read `offsetFromMaster` (nanoseconds):

| Tier | Target | Meaning for a ~10 ms measurement |
|---|---|---|
| hardware | < 1000 ns | error ≈ 0.00001 % — negligible |
| software | < 50000 ns | error ≈ 0.0005 % — negligible |

Also confirm B is **not** grandmaster:

```bash
sudo pmc -u -b 0 'GET PARENT_DATA_SET' | grep grandmasterIdentity
```

That identity must be **Host A's**. If it is B's own, sync is invalid — return to
Phase 5.

**Report the offset to the operator.** This number is what makes the transport
figure defensible; it belongs in the test record.

---

## Phase 9 — Run the test

**Start first**, before Host A's publisher.

```bash
cd ~/rust-sdks/examples/local_video/scripts
export LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...
./run_subscriber_test.sh wss://your.livekit.server round4-mso 120
```

Opens the video window plus a diagnostics window with live per-stage timing, and
writes `results/subscriber.csv`. Exits by itself at the end frame.

**A display is required.** The subscriber writes a CSV row on GPU render
completion, so a headless run yields an empty file. Over SSH:
`DISPLAY=:0 ./run_subscriber_test.sh ...`

**If frames stall or the CSV stops growing**, drop the diagnostics window — its GPU
work competes with decode on the machine being measured:

```bash
SHOW_TIMING=0 ./run_subscriber_test.sh wss://your.livekit.server round4-mso 120
```

Tell Host A to start its publisher once you are waiting.

---

## Phase 10 — Report (**authoritative — you own this**)

Copy Host A's CSV in, then:

```bash
cd ~/rust-sdks/examples/local_video/scripts
./run_report.sh ./results "MSO 5G — Round 4 (PTP synced)"
```

This prints a clock check and then writes `results/report.pdf`.

**Read the clock check before trusting the PDF:**

```
clock check: paired=3541  negative=0 (0.0%)
             transport min=8.42  p50=11.60  max=64.30 ms
```

- **`negative=0`** — required. Any negatives mean your clock trails A's and the
  transport row is shifted. Note that `generate_frame_report.py` **silently drops
  negative samples**, so a skewed run still renders a clean-looking PDF. That is
  precisely why this check runs separately.
- **plausible `min`** — a sub-1 ms minimum over 5G means your clock leads A's and
  transport is understated.

With PTP healthy both should be clean. **If they are not, PTP is not reaching the
system clock — check `phc2sys` (Phase 6) before anything else.**

### What the PDF contains

Per-stage mean/P50/P95: publisher exposure→buffer, encode, **transport**,
subscriber assembly, decode, render, and end-to-end — same format as Round 3.

Only **transport** and **end-to-end** depend on cross-host sync. Every other stage
is measured on a single clock and was always trustworthy; PTP is what makes those
two defensible.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Link detected: no` | cable/NIC | reseat; `sudo ip link set $ETH_LINK up` |
| ping to `.1` fails | A not configured | A must finish Phase 2 |
| B claims grandmaster | not seeing A | check cable, A's ptp4l, `priority1 255` |
| offset in milliseconds, not settling | NTP still running | re-check Phase 4 |
| Offset perfect, but negatives in Phase 10 | `phc2sys` missing | Phase 6 — the classic failure |
| subscriber.csv only a header | no display, or no video | check `DISPLAY`; confirm A is publishing |
| Frames stall mid-run | diagnostics window GPU contention | `SHOW_TIMING=0` |
| Video dies when cable plugged | link became default route | Phase 3 |

---

## Definition of done

- [ ] `Link detected: yes`; ping to `192.168.99.1` succeeds
- [ ] `192.168.99.2/30` assigned; no default route on `$ETH_LINK`
- [ ] `ip route get 8.8.8.8` → 5G interface
- [ ] `timedatectl` → NTP inactive
- [ ] `ptp4l` in `s2`; grandmaster identity is **Host A**
- [ ] `phc2sys` running, sub-µs (hardware tier)
- [ ] `offsetFromMaster` within target — **reported to operator**
- [ ] `run_report.sh` clock check: **0% negatives**, plausible min
- [ ] `results/report.pdf` generated
