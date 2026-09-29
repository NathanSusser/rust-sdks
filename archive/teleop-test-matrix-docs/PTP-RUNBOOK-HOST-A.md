# Host A (Publisher / PTP Grandmaster) — Setup Runbook

**You are the agent operating HOST A.** Host A publishes video and serves time to
Host B. Work top to bottom; every phase ends in a check you must pass before moving on.

Your peer, Host B, has its own runbook. Where the two must agree, this file says so.

---

## What is being built and why

Two Linux machines measure one-way video latency over 5G. The publisher stamps a
frame as it hands it to WebRTC; the subscriber stamps the same frame as the first
packet lands. Subtracting the two gives transport latency — the number the whole
test exists to produce.

That subtraction spans two machines' clocks, so **its error is the clock offset
between them.** NTP on a public server is routinely off by tens to hundreds of
milliseconds; the transport figure being measured is roughly 10–60 ms. NTP is
therefore not merely imprecise here, it is larger than the signal.

The fix is a **dedicated Ethernet cable carrying PTP only**. Video keeps flowing
over 5G. Timing and measurement travel separate paths on purpose: if they shared a
path, congestion would move the clocks and the latency together, and a correlated
error is far harder to detect than an obvious one.

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

**The invariant, stated once:** the direct link carries PTP and nothing else. It
must never carry video and must never become the default route. Several checks
below exist only to enforce this.

---

## Phase 0 — Identify the hardware

```bash
ip -br link
```

You will see the 5G/WWAN interface (often `wwan0`, `usb0`, or an `enp*` behind a
modem) and a wired Ethernet interface. **The wired one is the direct link.**

Record its name. This runbook writes `ETH_LINK`; substitute your real name everywhere.

```bash
export ETH_LINK=enp1s0     # <-- set to your actual wired interface
export WAN_LINK=wwan0      # <-- set to your actual 5G interface
```

Confirm you did not pick the 5G interface by mistake:

```bash
ip route get 8.8.8.8
```

The interface in that output is your **WAN**. If it equals `$ETH_LINK`, you have
them backwards — stop and re-identify.

### 0.1 Timestamping capability (decides your accuracy tier)

```bash
sudo ethtool -T "$ETH_LINK"
```

Read `PTP Hardware Clock:`

| Output | Tier | Expect | Action |
|---|---|---|---|
| `PTP Hardware Clock: 0` (or any number) | **hardware** | < 1 µs | Use the hardware path below |
| `PTP Hardware Clock: none` | **software** | 10–50 µs | Use the software path below |

Either tier is a 1000×+ improvement on NTP and is sufficient for a 10 ms
measurement. Do not block on getting hardware timestamping.

**Report the tier to the operator before continuing.** Host B must use the same
tier — mismatched tiers still sync, but the accuracy claim becomes the weaker of
the two, and the operator needs to know which they can defend.

---

## Phase 1 — Physical

1. Connect a Cat5e (or better) cable **directly between Host A and Host B**. No
   switch. Auto-MDIX on gigabit means a crossover cable is not needed.
2. Confirm link:

```bash
ip -br link show "$ETH_LINK"
sudo ethtool "$ETH_LINK" | grep -E "Speed|Duplex|Link detected"
```

**Pass:** `Link detected: yes` and a full-duplex speed. If `no`, the cable is not
seated or the peer NIC is down — fix before continuing; nothing later works without it.

---

## Phase 2 — Addressing

A `/30` subnet, which holds exactly two hosts. That is not a cosmetic choice: a
`/30` makes it structurally impossible to attach a third device or misroute onto a
wider network.

**Host A is `192.168.99.1`. Host B is `192.168.99.2`.** Do not swap these.

### If the machine uses NetworkManager (most desktop distros)

```bash
sudo nmcli connection add type ethernet ifname "$ETH_LINK" con-name ptp-link \
  ipv4.method manual \
  ipv4.addresses 192.168.99.1/30 \
  ipv4.never-default yes \
  ipv6.method disabled \
  connection.autoconnect yes

sudo nmcli connection up ptp-link
```

`ipv4.never-default yes` is the load-bearing setting: it forbids this link from
ever becoming the default route, so video cannot silently leave via the cable.
There is no gateway on this connection, by design.

### If the machine uses systemd-networkd

`/etc/systemd/network/10-ptp-link.network`:

```ini
[Match]
Name=enp1s0

[Network]
Address=192.168.99.1/30
# No Gateway= line, deliberately: this link must never carry default traffic.
IPv6AcceptRA=no
LinkLocalAddressing=no

[Link]
RequiredForOnline=no
```

```bash
sudo systemctl restart systemd-networkd
```

### Verify addressing

```bash
ip -br addr show "$ETH_LINK"
```

**Pass:** shows `192.168.99.1/30`.

```bash
ip route | grep -c "^default.*$ETH_LINK"
```

**Pass:** prints `0`. Any other number means the direct link became a default
route — remove it before continuing, or video will try to leave over the cable.

---

## Phase 3 — Routing safety

The single most important verification in this runbook.

```bash
echo "--- default route (must be the 5G interface) ---"
ip route show default

echo "--- where does SFU traffic go? ---"
ip route get 8.8.8.8
```

**Pass:** both name `$WAN_LINK`, not `$ETH_LINK`.

```bash
echo "--- where does peer traffic go? ---"
ip route get 192.168.99.2
```

**Pass:** names `$ETH_LINK`. Only the `/30` peer should use the cable.

If the default route points at `$ETH_LINK`, delete it:

```bash
sudo ip route del default dev "$ETH_LINK"
```

and fix the source (`never-default` for NetworkManager, or a stray `Gateway=`).

---

## Phase 4 — Disable NTP

`chrony`/`timesyncd` and `ptp4l` both discipline the system clock. Left running
together they fight, and the clock slews unpredictably — which looks exactly like
the measurement error you are trying to remove.

```bash
sudo systemctl disable --now chronyd 2>/dev/null || true
sudo systemctl disable --now chrony 2>/dev/null || true
sudo systemctl disable --now systemd-timesyncd 2>/dev/null || true
timedatectl set-ntp false 2>/dev/null || true
```

**Verify:**

```bash
timedatectl | grep -i "NTP service"
```

**Pass:** `inactive`.

> Set the clock roughly right **before** disabling NTP (`sudo chronyd -q`), so PTP
> starts from a small offset. PTP will correct any offset, but starting minutes
> away makes the first convergence slow and the logs confusing.

### 4.1 CPU governor and EPP — do not skip

Not a clock setting, but it belongs here because it is the other host-level
default that silently corrupts timings.

A bursty 30 fps duty cycle never convinces the `powersave` governor to ramp up.
Measured cost: capture→buffer went from 0.17 ms to 14.89 ms, with no symptom
other than the latency itself.

```bash
sudo cpupower frequency-set -g performance
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference
```

**Verify both:**

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor              # performance
cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference # performance
```

To persist the governor across reboots, `sudo apt install cpufrequtils` and set
`GOVERNOR="performance"` in `/etc/default/cpufrequtils`.

> **EPP is not covered by `cpufrequtils`.** It is an `intel_pstate` knob outside
> its scope and reverts to `balance_performance` on reboot. A freshly booted
> machine can therefore read governor `performance` while the setting that
> matters has silently gone back. Both run scripts warn on both values; heed the
> warning rather than assuming the persisted governor covers you.

---

## Phase 5 — PTP grandmaster

```bash
sudo apt update && sudo apt install -y linuxptp ethtool
```

Write `/etc/linuxptp/ptp4l-A.conf`:

```ini
[global]
# priority1 < B's 255 makes A grandmaster by the BMCA deterministically, rather
# than leaving it to a MAC-address tiebreak that could silently flip roles.
priority1               127
clockClass              248
# Direct cable, exactly one peer, no switch: E2E is correct and P2P adds nothing.
delay_mechanism         E2E
network_transport       L2
# 8 Hz sync and delay-request. On a dedicated link the traffic is trivial and
# faster servo convergence is worth far more than the bandwidth.
logSyncInterval         -3
logMinDelayReqInterval  -3
tx_timestamp_timeout    50
summary_interval        0

[enp1s0]
```

Set the `[enp1s0]` section to your real `$ETH_LINK`.

**Software-timestamping tier only** — add to `[global]`:

```ini
time_stamping           software
```

**Start it:**

```bash
sudo ptp4l -f /etc/linuxptp/ptp4l-A.conf -m
```

**Pass:** within ~30 s the log says `assuming the grand master role`.

Leave it running; open a new terminal for the next phase.

---

## Phase 6 — Discipline the system clock

**Do not skip this. It is the step that makes PTP visible to the application.**

`ptp4l` disciplines the **NIC's** PTP hardware clock. Your application calls
`clock_gettime()`, which reads the **system** clock. Without `phc2sys` the two are
unrelated: PTP locks perfectly, `pmc` reports beautiful offsets, and every
timestamp in the CSV is still on unsynchronized time. The failure is silent and
looks like success.

**Hardware tier:**

```bash
sudo phc2sys -s "$ETH_LINK" -w -m -O 0
```

`-w` waits for ptp4l to lock before slewing. `-O 0` because PHC and system clock
are both TAI-referenced here.

**Software tier:** skip `phc2sys` — `ptp4l` already disciplines the system clock
directly when `time_stamping software` is set.

**Pass (hardware tier):** offsets settle to tens or hundreds of **nanoseconds**:

```
phc2sys[123.4]: CLOCK_REALTIME phc offset  -34 s2 freq  -1234 delay 987
```

`s2` means locked servo. `s0`/`s1` mean still converging — wait.

---

## Phase 7 — Make it persistent

> Previously labelled optional. It is not, for anything beyond a single
> afternoon: these units are what make the rig survive a reboot, and a
> multi-day programme *will* reboot. The "skip for a one-off test" framing
> invited skipping it and then losing a day's configuration silently.

```bash
sudo tee /etc/systemd/system/ptp4l-A.service >/dev/null <<'EOF'
[Unit]
Description=PTP grandmaster on the direct link
After=network-online.target

[Service]
ExecStart=/usr/sbin/ptp4l -f /etc/linuxptp/ptp4l-A.conf
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ptp4l-A
```

### Install `phc2sys-A` only on the hardware tier

**Check first — on the software tier this unit cannot work and must not be
installed:**

```bash
ethtool -T "$ETH_LINK" | grep "PTP Hardware Clock"
```

`PTP Hardware Clock: none` means there is no PHC to synchronise *from*.
`phc2sys -s "$ETH_LINK"` has no source, and Phase 6 has already said so:
`ptp4l` disciplines the system clock directly when `time_stamping software` is
set. Installing the unit anyway leaves a permanently failing service and a
checklist item that can never pass.

Only if the link reports a real PHC:

```bash
sudo tee /etc/systemd/system/phc2sys-A.service >/dev/null <<'EOF'
[Unit]
Description=Sync system clock to PHC
After=ptp4l-A.service
Requires=ptp4l-A.service

[Service]
ExecStart=/usr/sbin/phc2sys -s enp1s0 -w -O 0
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now phc2sys-A
```

Substitute your interface in the `phc2sys` line.

> This rig's Host A runs `time_stamping software` on a NIC reporting
> `PTP Hardware Clock: none`. **Absent `phc2sys` is the correct state here.** The
> runbook previously installed the unit unconditionally in this phase while
> Phase 6 said to skip it, and then required its output in the Definition of
> Done — three places disagreeing about the same host.

---

## Phase 8 — Joint verification (coordinate with Host B)

Host B runs the authoritative offset check. Your job here is to confirm A is
grandmaster and the paths are still separated.

```bash
echo "--- A should be grandmaster ---"
sudo pmc -u -b 0 'GET PARENT_DATA_SET' | grep -E "parentPortIdentity|grandmasterIdentity"

echo "--- routing unchanged ---"
ip route get 8.8.8.8 | head -1        # must be the 5G interface
ip route get 192.168.99.2 | head -1   # must be the direct link

echo "--- peer reachable ---"
ping -c 3 -I "$ETH_LINK" 192.168.99.2
```

**Then have Host B report `offsetFromMaster`.** Targets:

- hardware tier: **< 1000 ns**
- software tier: **< 50000 ns** (50 µs)

Either satisfies a 10 ms measurement with orders of magnitude to spare.

---

## Phase 9 — Run the test

Host B (subscriber) starts **first**. Once it is waiting, start the publisher:

```bash
cd ~/code/rust-sdks/examples/local_video/scripts
export LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...
./run_publisher_test.sh wss://your.livekit.server round4-mso 120
```

This publishes the animated test pattern with the capture time burned into the
video, opens a local preview, and writes `results/publisher.csv` together with
`results/publisher.manifest.json`, which records the run's provenance —
invocation, git sha, governor and EPP, encoder actually selected, uplink
capacity at both ends, and how the run ended.

It ends its own run at the end frame and releases the room.

> This was previously stated as "it exits by itself" and was **false** until
> `6f25679`. Shutdown tested equality against one specific frame ID, so it
> depended on that exact frame surviving the whole capture→packetize pipeline.
> At 30 fps with no drops it nearly always does; in a run encoding one frame in
> twenty-six it did not, and the publisher ran 19 minutes past its window still
> publishing into the room — which silently corrupted the next run. If you are
> on an older build, confirm the process actually exited before starting
> anything else in that room.

**Frame window:** `START_FRAME` defaults to 0. Do not raise it to skip
encoder ramp-up without checking what you are discarding — see the warning
under Phase 9.1.

When finished, copy the CSV **and its manifest** to Host B, which generates the
report:

```bash
scp results/publisher.csv results/publisher.manifest.json \
    userB@192.168.99.2:~/code/rust-sdks/examples/local_video/scripts/results/
```

### 9.1 Startup exclusions are not free

An earlier version of these scripts excluded the first 60–90 frames of every run
to keep encoder ramp-up out of the statistics. Applied uniformly, defensible in
isolation — and it removed the evidence differentially. Share of each run's
worst-1% frames that fell inside the excluded window:

| Run | In window | Share |
|---|---|---|
| control | 0 / 35 | 0% |
| A2-off | 3 / 33 | 9% |
| A1 | 9 / 34 | 26% |
| A2 | 29 / 34 | **85%** |
| A3 | 29 / 34 | **85%** |

Zero from the control, 85% from the two runs whose behaviour was under
investigation. Every comparison drawn across that filter was biased toward
making the loaded runs look calmer than they were.

> **A filter that discards more evidence from the treatment than from the
> control is removing signal, not noise.** Before applying any exclusion, check
> its effect *per arm*, not just its justification.

> Copying over the direct link is fine — the test is over by then, so the transfer
> cannot perturb a measurement.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Link detected: no` | cable/NIC down | reseat; `sudo ip link set $ETH_LINK up` |
| ptp4l: `port 1: link down` | peer not configured | Host B finishes Phase 2 first |
| Both hosts claim grandmaster | cable not actually between them, or a switch in path | verify with `ping -I $ETH_LINK` |
| phc2sys `offset` in millions | NTP still running | re-check Phase 4 |
| Sync looks perfect, CSV timestamps still wrong | `phc2sys` not running — **hardware tier only** | Phase 6. On the software tier absent `phc2sys` is correct; check `ethtool -T` before chasing this |
| Video stops when cable is plugged | link became default route | Phase 3; add `never-default` |
| CSV contains the header and **no rows** | *Not* permissions — the frame-ID range matched nothing | See below |
| CSV file missing entirely | `File::create` failed | Genuinely a path or permissions problem |
| `ptp4l` restarted; its own sync check passes immediately | A fresh daemon has no `rms` history | See below |

### A header-only CSV has already ruled out permissions

The header is written at logger construction — `create_dir_all`, `File::create`,
write header, flush — before a single frame arrives. So the artifact tells you
which branch you are on:

| On disk | What it proves |
|---|---|
| No file at all | `File::create` failed. Path or permissions, genuinely |
| Header, zero rows | `File::create` **succeeded**. Permissions are ruled out by the file's own existence; your `--log-start-frame-id` / `--log-end-frame-id` window matched no frames |

A header-only CSV is positive evidence *against* the permissions hypothesis.
Read the file before chasing the cause it appears to suggest.

### After a `ptp4l` restart, the restarted host cannot check its own sync

A grandmaster has no master to measure itself against, so `ptp4l` emits no `rms`
lines at all while holding that role. On the **slave** this is loud and the
existing check catches it — here is the whole event from Host B's journal when
Host A's daemon went down:

```
19:19:24  port 1 (eno2): SLAVE to MASTER on ANNOUNCE_RECEIPT_TIMEOUT_EXPIRES
19:19:24  port 1 (eno2): assuming the grand master role
19:19:28  selected best master clock 345a60.fffe.5a6e7b
19:19:29  port 1 (eno2): UNCALIBRATED to SLAVE on MASTER_CLOCK_SELECTED
19:19:29  rms 5831 max 12953 freq +374977984 delay 70362
```

It self-healed in five seconds with no operator action, and the residue was a
single 13 µs excursion — three orders below anything the transport figures
measure.

> The host that **cannot** detect this is the one that just restarted. It has no
> `rms` history, so a check counting samples over a window passes on a few
> seconds of convergence — and it is also the host that believes it is the
> grandmaster and therefore never doubts itself.
>
> **After any `ptp4l` restart, wait for the full averaging window to elapse
> before trusting that host's own sync-quality check.**

---

## Definition of done

- [ ] `Link detected: yes` on `$ETH_LINK`
- [ ] `192.168.99.1/30` assigned; no default route on `$ETH_LINK`
- [ ] `ip route get 8.8.8.8` → 5G interface
- [ ] `timedatectl` → NTP inactive
- [ ] CPU governor **and** EPP both read `performance`
- [ ] `ptp4l` running, log shows grandmaster role
- [ ] **Hardware tier only:** `phc2sys` running with `s2` and sub-µs offsets.
      On the software tier (`PTP Hardware Clock: none`), `phc2sys` absent is the
      correct state — do not tick this and do not install the unit
- [ ] If `ptp4l` was restarted, its averaging window has fully elapsed since the
      restart before its sync check is believed
- [ ] Host B reports `offsetFromMaster` within target
- [ ] Timestamping tier reported to the operator
- [ ] Full unfiltered test suite passes before any push that touches shared
      code: `cargo test -p local_video --features desktop`. A *filtered* run
      passed while the shared branch was broken — the filter excluded the test
      the change had invalidated
