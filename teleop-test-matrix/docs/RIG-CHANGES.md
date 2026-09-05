# Rig changes — two-host teleoperation latency test bench

Every deliberate change made to the two test machines, why it was made, and
**whether it survives a reboot**. This is a machine-state document: it records
what was configured, not what the measurements showed. Findings live in the
run reports; keeping them separate means this file only changes when a machine
does.

Two machines:

| | Host A | Host B |
|---|---|---|
| Role | publisher | subscriber |
| Hostname | `matt-dedonato-CORSAIR-ONE-i500` | `MZ0126SD` |
| PTP link | `192.168.99.1` | `192.168.99.2` |
| PTP role | grandmaster | slave |
| GPU | RTX 5070 (NVENC) | Intel UHD 730 (software decode) |
| 5G uplink | 10.0 Mbps measured | 33.4 Mbps measured |

## How to edit this file

It is the only file both hosts own, so it is partitioned and **neither host
edits the other's section**:

- `## Host A` — Host A only
- `## Host B` — Host B only
- `## Both hosts` — joint, changed only in a coordinated commit

Pushes are serialized with an explicit handoff: commit, `git pull --ff-only`,
push, tell the other host the sha. Never a bare `git pull`, never `--force`. A
rejected push means the other host holds the token — pull and retry.

## Does it survive a reboot?

The column that matters. A change that reverts silently is worse than one that
was never made, because the rig keeps producing numbers.

| Change | Host | Survives reboot? |
|---|---|---|
| CPU governor `performance` + EPP `performance` | B | **No** — see below |
| CPU governor `performance` + EPP `performance` | A | *Host A to confirm* |
| `ptp4l` / `phc2sys` running | B | Yes — systemd units, `enabled` |
| `ptp4l` running | A | *Host A to confirm — foreground process as of last check* |
| `systemd-timesyncd` disabled | B | Yes — `disabled` |
| NetworkManager profile edits | B | Yes — persisted by definition |
| `/etc/linuxptp/ptp4l-B.conf` | B | Yes — on disk |
| `corp-ca.pem` | B | Yes — on disk, gitignored |
| `CC` / `CXX` for builds | both | **No** — environment only, per shell |

---

## Host B (subscriber)

### CPU governor — ⚠ NOT PERSISTED

```bash
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference
```

Default was `powersave` on all 16 cores with EPP `balance_performance`. The
video pipeline runs a ~7% duty cycle — a few milliseconds of work per 33 ms
frame — which never convinces the governor to ramp, so cores sat at 800 MHz
against a 4500 MHz ceiling. Correcting it cut subscriber decode time by 73%
and moved median core frequency from 1737 to 2880 MHz.

**This reverts on reboot.** `cpufrequtils` is not installed and
`/etc/default/cpufrequtils` does not exist, so nothing restores it. Until that
is fixed, verify the governor before every run — a rebooted machine produces
plausible, wrong numbers with no warning. Host A's `run_publisher_test.sh`
carries a run-time guard that warns when the governor is not `performance`;
Host B has no equivalent yet.

*Open item: install `cpufrequtils` and set `GOVERNOR="performance"` in
`/etc/default/cpufrequtils`. Note it restores the governor but **not** EPP, so
EPP still needs setting or checking separately after a reboot.*

### 5G must hold the default route

```bash
sudo nmcli connection modify "T-Mobile MSO" ipv4.never-default no
sudo nmcli connection modify "FigNet" connection.autoconnect no
sudo nmcli device disconnect wlo1
```

The `T-Mobile MSO` profile shipped with `ipv4.never-default yes`, so the modem
came up, got an address, and then declined to carry any general traffic — the
radio reports `connected` while applications behave as though 5G is dead.

Wi-Fi is disabled rather than deprioritized. It connected on its own when the
Ethernet cable was moved and took the default route; if that happened during a
run, video would silently traverse Wi-Fi while being recorded as 5G. An
outright outage is easy to spot, a quiet reroute is not.

### PTP link

```bash
sudo nmcli connection add type ethernet ifname eno2 con-name ptp-link \
  ipv4.method manual ipv4.addresses 192.168.99.2/30 \
  ipv4.never-default yes ipv6.method disabled connection.autoconnect yes
```

A `/30` holds exactly two hosts, which structurally prevents a third device.
`never-default` stops the cable stealing the default route back from 5G. The
link carries PTP and nothing else — keeping the timing path off the measured
path means congestion cannot move the clocks and the latency together, and
correlated error is far harder to detect than obvious error.

Transfers between hosts (`scp` of CSVs) do use this link, but **only between
runs, never during one**. Both hosts saw transfers drop and succeed on retry.

### `sanity_freq_limit 0` — required by this NIC

`/etc/linuxptp/ptp4l-B.conf`, on `eno2` (Intel I219-LM, `e1000e`, hardware
timestamping, `PTP Hardware Clock: 0`):

```ini
priority1               255     # B must never win the BMCA
clockClass              248
delay_mechanism         E2E
network_transport       L2
logSyncInterval         -3
logMinDelayReqInterval  -3
sanity_freq_limit       0       # see below
step_threshold          1.0
```

This NIC's PHC needs roughly **+375,000 ppb** of correction. `ptp4l` defaults
`sanity_freq_limit` to 200,000,000 ppb, so at the default it raises
`SYNCHRONIZATION_FAULT` once per second and never holds `SLAVE`. The NIC's
`max_adjustment` is 999,999,999 ppb, so the correction is within hardware
range — the sanity checker simply refuses to believe a clock this far off is
legitimate. Setting it to 0 disables that check.

Anyone rebuilding this rig on the same I219-LM will hit this.

### NTP off, and `phc2sys` is mandatory

```bash
sudo systemctl disable --now systemd-timesyncd
sudo timedatectl set-ntp false
```

`chrony`/`timesyncd` and `ptp4l` both discipline the system clock; together
they fight and the clock slews unpredictably — indistinguishable from the error
being removed.

Host B uses **hardware** timestamping, so `ptp4l` disciplines the NIC's clock
while the subscriber reads the *system* clock via `clock_gettime()`. Without
`phc2sys` the two are unrelated: PTP locks perfectly, `pmc` reports excellent
offsets, and every CSV timestamp is unsynchronized. The failure looks exactly
like success.

Both run as systemd units (`ptp4l-B.service`, `phc2sys-B.service`, both
`enabled`) rather than foreground processes. They were foreground initially and
PTP died three times when terminals closed.

### SFU certificate

The LiveKit SFU is a T-Mobile edge endpoint (`10.1.20.21`, reached over the 5G
static route) presenting a certificate from `T-Mobile USA KF ENT Root CA01`,
which is not in the system trust store. `.livekit-demo/corp-ca.pem` holds the
system roots plus that CA; `SSL_CERT_FILE` must point at it.

`run-video.sh` sets this automatically; **`run_subscriber_test.sh` does not**,
so it has to be exported by hand:

```bash
export SSL_CERT_FILE=~/code/rust-sdks/.livekit-demo/corp-ca.pem
```

Root fingerprint, sha256:
`D8:20:6A:4F:8A:66:31:EB:05:37:B7:36:5E:BD:20:A9:79:5B:C9:CC:28:1B:D8:78:4D:1D:AA:ED:DD:24:E5:40`

### Packages and toolchain

- `linuxptp` 4.0, `ethtool`, `python3-reportlab` 4.1.0 (the report generator
  needs it; `pip` is refused here under PEP 668, so use apt)
- **`clang++` is not on PATH — only `clang++-21`.** `webrtc-sys/build.rs`
  rejects GCC outright, because `libwebrtc.a` is built against Chromium's
  hermetic libc++ whose `trivial_abi` annotations GCC ignores. Every build
  needs:

  ```bash
  CC=clang-21 CXX=clang++-21 cargo build --release -p local_video --features desktop
  ```

- SSH key auth to Host A is configured, so CSV transfer needs no password.

### Not changed, deliberately

- `run_subscriber_test.sh` is unmodified. It cannot pass `--low-latency`, so
  those runs invoke the binary directly using the script's own argument list
  plus the flag, rather than editing a runbook script.
- Hardware decode is not available and was not pursued: no NVIDIA GPU, and
  `webrtc-sys` ships a VAAPI *encoder* but no VAAPI decoder. Software decode is
  structural here, not a misconfiguration. It has never been the bottleneck —
  1.27 ms at 640×360, 3.65 ms at 1080p, against a 33.3 ms frame budget.

---

## Host A (publisher)

*Host A to complete. Suggested coverage, based on what came up during testing:
CPU governor and whether it is persisted; `CUDA_HOME=/usr` for the NVENC/NVDEC
build gate, and whether `webrtc-sys/build.rs` needs touching to defeat the
cached build result; `clang-21` toolchain; whether `ptp4l` runs as a systemd
unit or still as a foreground process; the `SHOW_PREVIEW` toggle and the
run-time governor guard in `run_publisher_test.sh`.*

---

## Both hosts

### Before every run

1. Governor is `performance` on both hosts — **check, don't assume**, it is not
   persisted on Host B
2. `ptp4l` in `SLAVE` on B, `phc2sys` at `s2`, zero `SYNCHRONIZATION_FAULT`
3. Default route on the 5G interface, no default route on the PTP link
4. Previous publisher confirmed **stopped** before the next subscriber starts —
   otherwise the subscriber latches onto the leftover stream and the run mixes
   two publishers
5. Row growth in the CSV is the only trusted liveness signal. The `rendered=`
   field in the decode-health log is a libwebrtc counter that reads 0 during a
   perfectly healthy run

### Before every push

Run the **full package suite, unfiltered**:

```bash
CC=clang-21 CXX=clang++-21 cargo test --release -p local_video --features desktop
```

The publisher and subscriber binaries share `frame_log.rs` and
`subscriber_timing.rs`, so a change on either side can break the other's tests.
A filtered run produces a green result indistinguishable from a real one, and a
green result carries no timestamp relative to your edits — the only run that
proves anything is the one *after* your last change, unfiltered. This is not
hypothetical: a filtered pass taken before a later edit put a red commit on the
shared branch.

### Known-wrong things in the runbooks

Not yet fixed; to be corrected in a single joint commit.

1. Paths say `~/rust-sdks/...`; the repo is at `~/code/rust-sdks/...`
2. "Exits by itself at the end frame" — both binaries can hang in teardown.
   `pkill -TERM` works; Ctrl-C only sets an `AtomicBool` and does not terminate
3. The "subscriber.csv only a header" troubleshooting entry names the wrong
   causes. It can also mean the renderer never painted while decode was healthy
4. Phase 7 systemd units are presented as optional. They should be the
   documented path — PTP died three times in foreground terminals
5. No mention of the resolution/bitrate CSV columns
6. The governor is not mentioned at all. It belongs in Phase 4 beside disabling
   NTP: both are cases of the machine's default configuration silently
   invalidating measurements
7. No mention that the two binaries share modules, or that the full suite is
   the gate before pushing
