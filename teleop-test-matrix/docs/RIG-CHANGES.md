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
was never made, because the rig keeps producing plausible numbers.

Two columns, because they are different risks with different remedies. A
non-persistent setting with a run-time guard degrades **loudly** — someone sees
a warning. Without one it degrades **silently**: a missing `CUDA_HOME` yields a
working binary that is quietly slower, and a missing CA bundle yields a
connection error that reads as a network fault.

| Change | Host | Survives reboot? | Fails loudly? |
|---|---|---|---|
| CPU governor `performance` | A | Yes — `cpufrequtils`, `GOVERNOR="performance"` | Yes — guard in `run_publisher_test.sh` |
| CPU governor `performance` | B | **No** — `cpufrequtils` absent | **No** — no guard on this side |
| EPP `performance` | A | **No** — `cpufrequtils` does not manage EPP | Partly — the guard checks governor, not EPP |
| EPP `performance` | B | **No** | **No** |
| `ptp4l` / `phc2sys` running | B | Yes — systemd units, `enabled` | Yes — pre-flight checks `s2` and faults |
| `ptp4l` / `phc2sys` running | A | **No** — foreground process, no units exist | **No** — and see the warning below |
| `systemd-timesyncd` disabled | both | Yes | — |
| NetworkManager profile edits | both | Yes — persisted by definition | — |
| `/etc/linuxptp/ptp4l-B.conf` | B | Yes — on disk | — |
| TLS bundle for the SFU | B | Yes — `.livekit-demo/corp-ca.pem`, on disk | — |
| TLS bundle for the SFU | A | **No** — in a session-temporary directory | **No** — reads as a network fault |
| `CC` / `CXX` for builds | both | **No** — environment only, per shell | Yes — build fails outright |
| `CUDA_HOME=/usr` | A | **No** — in no shell profile | **No** — NVENC compiles out with only a `cargo:warning` |

*Host A rows reported by Host A from their own audit; their section below is
theirs to write.*

> **Host A's grandmaster has no systemd unit.** `ptp4l` there is a foreground
> process started by hand. If that terminal closes, sync dies for **both**
> machines — and Host B's `phc2sys` would go on reporting `s2` against a clock
> that has stopped being disciplined. Host B's units protect it from its own
> terminal, not from Host A's.

> **Host A's TLS bundle is in a session-temporary directory.** When that
> session ends the file is deleted, and Host A can no longer reach the SFU:
> `invalid peer certificate: UnknownIssuer`. The T-Mobile enterprise root is
> not in `/usr/local/share/ca-certificates/` there. This is the most urgent
> item on either machine.

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

### TLS root for the SFU — ⚠ NOT PERSISTED, MOST URGENT ITEM ON EITHER MACHINE

The SFU presents a chain rooted in `T-Mobile USA KF ENT Root CA01`, which is not
in this machine's trust store. Only `figure-ai-root.crt` is installed there, and
it belongs to a different PKI entirely. Without the T-Mobile root every
connection fails at the certificate check:

```
Error: engine: signal failure: transport connection error:
IO error: invalid peer certificate: UnknownIssuer
```

The current workaround is a bundle — the system CA file concatenated with the
extracted root — pointed at by `SSL_CERT_FILE`. `rustls-native-certs` 0.8.4
honours that variable, so it works, but **the bundle lives in a session
temporary directory and is deleted when that session ends.** At that point Host
A cannot reach the SFU at all, and the error reads as a network fault rather
than a missing file.

Fix, which removes the dependency on `SSL_CERT_FILE` entirely:

```bash
sudo cp tmobile-ent-root.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
```

Install the **root only** — the self-signed cert at the top of the chain — not
the 123-certificate bundle. Verify before trusting it, sha256:

```
D8:20:6A:4F:8A:66:31:EB:05:37:B7:36:5E:BD:20:A9:79:5B:C9:CC:28:1B:D8:78:4D:1D:AA:ED:DD:24:E5:40
```

That fingerprint was taken from the chain the server itself presented, which is
circular — it proves the bundle matches what we connected to, not that what we
connected to is genuine. Confirm it against a value published by T-Mobile IT
before installing it as a trust anchor.

### CPU governor `performance` — persisted; EPP is not

```bash
sudo cpupower frequency-set -g performance
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference
sudo apt install cpufrequtils
echo 'GOVERNOR="performance"' | sudo tee /etc/default/cpufrequtils
```

Default was `powersave`. The capture loop renders for a few milliseconds then
sleeps until the next 30 fps tick — roughly a 7% duty cycle, which never
convinces the governor to ramp. Most cores sat at 800 MHz against a 5.8 GHz
ceiling. Measured directly: identical work in a tight loop ran **4.85× faster**
than the same work on a 30 fps duty cycle; after the change that penalty fell to
1.11×.

Effect on the pipeline was larger than anything else found in this program:
`capture_to_buffer` went **14.89 → 0.17 ms p50 (85.6×)**, and p95 fell from
20.81 to 1.61 ms. Most of the publisher's measured variance was power
management rather than the pipeline.

`cpufrequtils` restores the **governor** on reboot but has no concept of
`energy_performance_preference` — that is an `intel_pstate` knob outside its
scope. So the governor persists here and EPP does not.

*Open item: persist EPP, via a systemd unit or a tmpfiles rule. Until then,
check it after any reboot.*

### Build environment — ⚠ NOT PERSISTED, AND FAILS SILENTLY

Every build must be invoked as:

```bash
CUDA_HOME=/usr CC=clang-21 CXX=clang++-21 cargo build --release -p local_video \
  --features desktop --bin publisher --bin subscriber
```

None of those three are in `.bashrc`, `.profile` or `/etc/environment`. Two
reasons they are mandatory, with very different failure modes:

**`CC`/`CXX` fail loudly.** `webrtc-sys/build.rs` rejects GCC outright —
`libwebrtc.a` is built against Chromium's hermetic libc++, whose `trivial_abi`
annotations GCC ignores, which silently breaks the calling convention for
`unique_ptr` and `shared_ptr`. It also requires clang **≥ 21**; clang 18 fails
with a message about the hermetic libc++. `/usr/bin/clang++` still points at
llvm-18, so the versioned binaries must be named explicitly.

**`CUDA_HOME` fails silently, and this one has already cost us a wrong result.**
`build.rs` enables `USE_NVIDIA_VIDEO_CODEC` only when `$CUDA_HOME/include/cuda.h`
exists, defaulting to `/usr/local/cuda`, which does not exist here — `cuda.h`
comes from the `nvidia-cuda-dev` package and lives at `/usr/include/cuda.h`. If
the check fails, every NVIDIA encoder and decoder source is skipped and the only
symptom is a `cargo:warning` that scrolls past. The binary builds, runs, and
encodes in software. A rebuild without it produced an encode time that was
reported as an improvement before the cause was found.

Confirm after any build that the log reports `encoder=NVIDIA H264 Encoder` and
that the backend list includes `nvenc`. If it reads `libaom` or the list is
`auto, software, unknown`, the gate failed.

**`build.rs` does not declare `cargo:rerun-if-env-changed=CUDA_HOME`**, so cargo
replays a cached build-script result and the variable appears to do nothing. Run
`touch webrtc-sys/build.rs` first when changing it.

*Open item: a fallback to `/usr/include/cuda.h` in `build.rs` plus the
`rerun-if-env-changed` declaration would remove this whole class of error. Not
done — it is a build-script change affecting the entire workspace.*

### PTP grandmaster — ⚠ NOT PERSISTED, FOREGROUND PROCESS

```bash
sudo ptp4l -f /etc/linuxptp/ptp4l-A.conf -m
```

`/etc/linuxptp/ptp4l-A.conf` persists. The **process does not**: there are no
`ptp4l-A.service` or `phc2sys-A.service` units on this machine, and `ptp4l` has
been running as a hand-started foreground process since Phase 5.

This is worse here than the equivalent on Host B, because **Host A is the
grandmaster**. If that terminal closes, sync dies for both machines, and Host
B's `phc2sys` will go on reporting `s2` against a PHC that nothing is
disciplining. Host B's Phase 7 units protect Host B from its own terminal and
do nothing about this.

No `phc2sys` runs here, and that is correct rather than an omission: this NIC
reports `PTP Hardware Clock: none`, so there is no PHC to bridge from and
`ptp4l` disciplines `CLOCK_REALTIME` directly under `time_stamping software`.
The rig is therefore software-tier end to end, capped by this side.

*Open item: install the Phase 7 units. The runbook presents them as optional;
they should be the documented path.*

### Run-time guards in `run_publisher_test.sh`

Two additions, both aimed at the failure modes above:

- **Governor guard.** Reads `scaling_governor` and prints a loud warning when it
  is not `performance`. It warns rather than aborts, so a deliberate measurement
  under `powersave` is still possible, and it fails safe — an unreadable file
  yields an empty string, which does not equal `performance` and still warns.
- **`SHOW_PREVIEW=0`** drops `--display-video`, mirroring the subscriber's
  existing `SHOW_TIMING`. Measured cost of the preview is small — 0.24 ms at the
  median — but it adds tail jitter, so it is worth dropping when the tail is
  under study.

### Persisted without further work

| Change | Detail |
|---|---|
| `systemd-timesyncd` disabled | `timedatectl` reports NTP inactive; chrony was never installed |
| PTP link addressing | NetworkManager profile `ptp-link`, `192.168.99.1/30`, `autoconnect yes`, `ipv4.never-default yes` |
| Packages | `clang`, `clang-21`, `libclang-common-18-dev`, `nvidia-cuda-dev`, `cpufrequtils` |

The `never-default` setting is load-bearing: without it the direct cable can
become the default route and video leaves over the PTP link instead of 5G,
which would invalidate the measurement and be invisible in the results.

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
