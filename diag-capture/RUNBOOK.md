# Modem logging and disk management runbook

Two-host 5G teleoperation rig. Both hosts run a **Quectel RM520N-GL** on `wwan0` with the
Qualcomm DIAG port at `/dev/ttyUSB0`, PTP-synced to each other.

| | Host A | Host B |
|---|---|---|
| Hostname | `matt-dedonato-CORSAIR-ONE-i500` | `MZ0126SD` |
| Role | publisher | subscriber |
| Tooling root | `~/code/rust-sdks/diag-capture/` | `~/diag-capture/` |
| tcpdump privilege | scoped NOPASSWD sudo | file capabilities |

> **The tooling roots differ.** A's scripts live under the repo; B's live in `$HOME`. `~/diag-capture`
> exists on A but holds only `sysrec.sh`. Checking the wrong one has already produced a "the script
> does not exist" conclusion that was false.

---

## 0. Setup — install, the DIAG port, and the rootless wrapper

Done once per host. `dialout` membership (§1) is the only privilege any of this needs.

### Install QCSuper

Not on PyPI — install from the repo into the venv beside these scripts:

```bash
cd ~/code/rust-sdks/diag-capture
python3 -m venv venv                       # needs python3.12-venv if ensurepip is missing
./venv/bin/pip install --upgrade pip setuptools wheel
./venv/bin/pip install 'qcsuper @ git+https://github.com/P1sec/QCSuper.git'
./venv/bin/python -c 'import qcsuper, os; print(os.path.dirname(qcsuper.__file__))'
```

qcsuper 2.1.3, plus `pycrate`, `pyserial`, `pyusb`, `crcmod`. `py-spy` and `scat` are also
in the venv. The venv, `logs/`, `up4m.bin` and the lock file are gitignored
(`.gitignore:74-80`).

### Identify the DIAG port by descriptor, never by number

`bInterfaceClass=ff`, `bInterfaceSubClass=ff`, `bInterfaceProtocol=30` (hex; 48 decimal),
`bNumEndpoints=02`:

```bash
for i in /sys/bus/usb/devices/1-10.1:1.*; do
  echo "intf=$(cat $i/bInterfaceNumber) class=$(cat $i/bInterfaceClass) \
sub=$(cat $i/bInterfaceSubClass) proto=$(cat $i/bInterfaceProtocol) \
eps=$(cat $i/bNumEndpoints) tty=$(ls $i | grep ^ttyUSB)"
done
```

Interface `00` matches and maps to `ttyUSB0`. ModemManager must show it as **`(ignored)`**
(`mmcli -m $IDX | grep -A3 'ports:'`); if MM claims it, a udev rule has to make it let go.

> **Never hardcode the modem index.** It is **1** on Host A and **0** on Host B, and
> `mmcli -m 0` on Host A fails with a bare `error: couldn't find modem` — a wrong-index
> command that looks like a missing modem rather than a wrong argument. Resolve it first:
> ```bash
> IDX=$(mmcli -L | sed -n 's|.*/Modem/\([0-9]*\).*|\1|p' | head -1)
> ```
> Identity fields, no sudo needed: `mmcli -m $IDX | grep -iE 'model|firmware|carrier config|h/w'`.
> Both rigs read **RM520N-GL / RM520NGLAAR03A04M4G / Commercial-TMO 0A01050F / h-w 20000** —
> byte-identical, which is why the 0xB97F record-layout differences between the hosts are
> **not** firmware skew. See the 0xB97F note in §4b.

> `wwan0` carries the host's default route. DIAG capture does not disturb the bearer, but
> anything that re-registers the modem drops the link.

### Why `qcsuper-noroot` exists

Stock QCSuper escalates to root in two places, both in
`venv/.../qcsuper/inputs/usb_modem_pyserial.py`:

1. `access(device, W_OK)` fails — **`dialout` already satisfies this**.
2. `detect_diag_interference()` (line 104) walks `/proc`, matches any cmdline containing
   `modemmanager` or `qc`, and calls `_try_escalate()` when it cannot read that process's
   `fd` directory. **ModemManager runs as root, so this fires unconditionally.**

With `DISPLAY` set, `_try_escalate` prefers `pkexec`, which throws a **graphical password
dialog onto the physical console**. From a terminal: no output, no error, no traceback, and
the serial port never opens. It looks exactly like a hung modem and is not one.

The wrapper unsets `DISPLAY` and makes `detect_diag_interference` a no-op. MM already
ignores the DIAG port, so the check has nothing legitimate to find.

> **Do not add a NOPASSWD sudoers rule for qcsuper.** The venv is user-writable, so
> `NOPASSWD: venv/bin/qcsuper` lets anything running as that user rewrite the script — or any
> module it imports — and get root. `NOPASSWD: /bin/kill` is worse. `dialout` plus this
> wrapper is sufficient; use `timeout -s INT` where a script needs a bounded stop.

### What `qcsuper-noroot-fast2` adds

1. **Reader speed.** Stock QCSuper reads the port **one byte per call** and grows the frame
   with `bytes +=`, pinning a core at full mask (99.7% even on an idle link) and losing ~30%
   of frames to truncation. `fast2` reads what is waiting and splits on `0x7e`.
2. **Opcode 158.** This firmware sends a disjoint half of the NR5G log set wrapped in opcode
   158 (inner record at payload offset 19). `fast2` decodes it into the normal log path.
3. **Record splitting.** One `DIAG_LOG_F` frame can carry several records back to back.
   QCSuper wrote them as one record under the first record's length (`Dismissing log type
   ... indicating size X instead of Y`), leaving misaligned bytes in the DLF.
4. **Deterministic shutdown.** QCSuper's SIGINT path races a deinit thread against the
   reader and sometimes never exits; worse, a `&` job in non-interactive bash starts with
   SIGINT *ignored*, so Python never installs a handler. `fast2` installs its own.

It prints a counter line on exit — `frames`, `bad_crc`, `op158_*`, `dlf_records`,
`dlf_split_frames`, `dlf_dropped_bytes`. Read it after every capture.

> **Never pass `--pcap-dump` alongside `--dlf-dump`.** `DlfDumper` sets no
> `limit_registered_logs`, so it enables every log bit the baseband offers, scheduler items
> included. `PcapDumper` *does* restrict the mask to RRC/NAS, and the mask is global device
> state — so asking for both can clamp away exactly the records you want. Capture DLF only
> and regenerate a pcap offline:
> `./venv/bin/qcsuper --dlf-read FILE.dlf --pcap-dump FILE.pcap`

Benchmark a candidate build before an experiment depends on it — start latency, CPU%,
record/NR5G/`0xB9xx` content, resyncs, and seconds from SIGINT to exit:

```bash
./test-qcsuper-build.sh <qcsuper-noroot-file> <seconds> <label>
```

---

## 1. Modem / DIAG capture

Nothing configures the modem beforehand — **no AT commands, no `mmcli`, no QMI, no mode switch.**
QCSuper sets the log mask itself over the DIAG port. The only prerequisite is `dialout` membership.

```bash
# one-off, both hosts — then log out and back in
sudo usermod -aG dialout "$USER"
id -nG | tr ' ' '\n' | grep -x dialout        # verify
ls -l /dev/ttyUSB0                            # crw-rw---- root:dialout
```

### Host A

```bash
cd ~/code/rust-sdks
# via the cell wrapper (normal path) — starts DIAG + pcap + hop counters together
DIAG=1 ./diag-capture/hop-recorder.sh <label> <duration_s> <outdir>

# the qcsuper line it runs, for reference:
sleep infinity | diag-capture/venv/bin/python3 diag-capture/qcsuper-noroot \
  --usb-modem /dev/ttyUSB0 --dlf-dump <out>/<label>.dlf
```

`sleep infinity |` is load-bearing: backgrounded with stdin at EOF, QCSuper stops after ~4 s with
no error.

### Host B

```bash
~/diag-capture/capture.sh <seconds> [label]      # -> ~/diag-logs/<label>-<stamp>.dlf

# the qcsuper line it runs:
PYTHONUNBUFFERED=1 timeout -s INT -k 30 "$left" \
  ~/diag-capture/qcsuper-noroot --usb-modem /dev/ttyUSB0 --dlf-dump "$out.dlf" \
  > >(trap '' INT TERM HUP; exec sed -u -E 's/(Wrong CRC).*/\1/' >>"$out.log") 2>&1 &
```

`qcsuper-noroot` is a **symlink** (both hosts) currently to `qcsuper-noroot-fast2`,
sha256 `1be99b70e92c32dbf464dd7f1d276239cbf0008113827b2c720dce95ba0f2ea1`.
Switch builds atomically with `ln -sfn` + `mv -T`; `QCSUPER_BIN=<path>` overrides for testing.

### One reader per port

Two DIAG clients interleave and corrupt both captures.

```bash
exec 9>"$DIAG_DIR/.ttyUSB0.lock"; flock -n 9 || exit 1   # A and B both
fuser -v /dev/ttyUSB0                                    # A additionally
pgrep -f '^[^ ]*python[0-9.]* [^ ]*qcsuper'              # B: matches the interpreter, not shells
```

> `pgrep -f <pattern>` matches the shell whose own command line contains the pattern. This has
> killed a run. Match the binary (`pgrep -x`) or a pattern that cannot appear in your own argv.

### Turning DIAG logging OFF — do not skip

```bash
# Host A (from the recorder's cleanup, or by hand)
DIAG_LOCK_HELD=1 diag-capture/venv/bin/python3 diag-capture/diag-log-off /dev/ttyUSB0

# Host B — called explicitly, twice: after the capture loop and before each relaunch
(trap '' INT TERM HUP; exec ~/diag-capture/diag-log-off /dev/ttyUSB0)
```

QCSuper clears the mask on exit, but **under full-mask load the reply is lost**
(`unmatched response received`) and the modem keeps streaming at full rate with nobody reading —
loading the baseband on every later run. A runs it from an EXIT trap; B calls it explicitly.

---

## 2. Packet capture

### Host A — scoped NOPASSWD sudo

```bash
sudo -n -l          # (root) NOPASSWD: /usr/bin/tcpdump, /usr/bin/qmicli, /usr/sbin/nft, /usr/bin/mmcli

sudo -n tcpdump -i wwan0 -nn -s 128 -Z "$(id -un)" --time-stamp-precision=nano \
  -G <duration> -W 1 -w <out>.pcap udp
```

### Host B — file capabilities, no sudo at run time

```bash
sudo setcap cap_net_raw,cap_net_admin=eip /usr/bin/tcpdump   # ONE-OFF, needs sudo
getcap /usr/bin/tcpdump                                      # verify before every campaign
# -> /usr/bin/tcpdump cap_net_admin,cap_net_raw=eip          (a package upgrade drops it)

~/diag-capture/pcap.sh <seconds> [label] [iface=wwan0]
# runs: tcpdump -i wwan0 -n -s 96 -U -G <dur> -W 1 -w <out>.pcap udp >>"$out.log" 2>&1 9<&-
```

> **Never signal tcpdump on Host B.** Raising capabilities clears the dumpable flag, so the kernel
> demands `CAP_KILL` from the sender; `kill -TERM` and `-KILL` both return `EPERM` to the uid that
> launched it. `-G <dur> -W 1` is its own deadline. The `9<&-` is load-bearing — without it tcpdump
> inherits the lock fd and holds the flock until reboot.

> `-G` closes on the first packet **after** the deadline, so elapsed ≥ nominal on a quiet link.

**Timestamp resolution differs between the hosts.** A writes nanosecond pcaps
(`--time-stamp-precision=nano`, magic `a1b23c4d`), B writes microsecond (`a1b2c3d4`). Dividing both
by 1e6 puts them ~1000 s apart. Read the magic per file.

---

## 3. System pressure (PSI)

```bash
~/diag-capture/sysrec.sh <seconds> <label>      # both hosts -> ~/sys-logs/<label>-<stamp>.csv, 1 Hz
```

Reads `/proc/pressure/{cpu,io,memory}` — the `total=` field only, which is **cumulative microseconds
stalled**, so a one-second delta is directly comparable to a render gap in the same second. Plus
`/proc/diskstats`, `/proc/loadavg`, `/proc/meminfo`.

```bash
DEV=$(basename "$(readlink -f "$(findmnt -no SOURCE /)")")   # /dev/mapper/... -> dm-1
grep -qE " $DEV " /proc/diskstats || DEV=""
```

> `/proc/diskstats` names the **kernel** device. Matching the mapper alias silently yields zeros in
> every disk column, which looks exactly like an idle disk.

---

## 4. Log mask — what we record and what it costs

**We currently record every log code the modem declares.** QCSuper asks the modem for its highest
valid code per subsystem (`LOG_CONFIG_RETRIEVE_ID_RANGES_OP`) and then sets *every bit* of *every*
subsystem it reports: 1X, WCDMA, GSM, UMTS, DTV, APPS/LTE/WIMAX, TDSCDMA. The 2G/3G subsystems
produce zero records on a 5G-only link — that is not a gap, they are simply not in use.

Measured on `cell15m-a` (899 s, Host A, 14,482,208 records):

| mask | share of volume | rate | status |
|---|---|---|---|
| full (current) | 100% | **35.3 GB/hour** | default |
| `MASK=nr5g` (`0xB800–0xB9FF`) | 86.6% | **30.6 GB/hour** | **already implemented, never used** |
| the 28 codes our analysis reads | 17.1% | **6.1 GB/hour** | would need a new mask |

```bash
MASK=nr5g ~/diag-capture/capture.sh <seconds> <label>    # Host B
QCSUPER_NR5G_ONLY=1 ...                                  # the env var both builds honour
```

> **`MASK=nr5g` saves only 13%**, because the non-NR codes are just 13.4% of the volume. It is not
> the lever it looks like.
>
> **And it would drop `0x1C0D`**, which is outside `0xB800–0xB9FF`, is in our analysis set, and is
> the undecoded code that bottoms out on *both* hosts during the cell5m-a event. Do not enable
> `MASK=nr5g` without deciding to lose it.

The 28-code mask is a **proposal, not built.** It is the only option that meaningfully changes the
disk maths (6.1 vs 35.3 GB/hour), and it carries the risk that a QCAT operator later wants a code we
excluded.

---

## 4b. Radio metrics without QCAT

RSRP is recoverable from the raw DLF — no licensed tool needed. `0xB97F` (ML1 measurement)
records decode structurally with `dlf_records.iter_file(..., emit_offset=True)`; the payload is a
seek and read at the emitted offset, and **byte 72 read as little-endian int32 / 128** is an RSRP
field at ~6 Hz (162 ms cadence), against QMI's 1 Hz.

```bash
python3 ~/diag-capture/ml1-extract.py <dlf> <out.json>     # one pass, ~5 s per 3.3 GB
```

Validated on Host A, which has both instruments, over the cell5m-a media window:

| property | ML1 byte 72 | QMI |
|---|---|---|
| level | −91.08 dBm | −89.77 dBm (mean diff 1.32 dB) |
| tracking | r = +0.745 over 297 matched samples | — |
| the +8 dB step | +8.85 dB | +8.0 dB |

> **Validated, not identified.** A per-beam measurement of the serving beam would pass all three
> tests. QCAT settles it; until then call it "the field that tracks QMI RSRP", not "RSRP".

> **QMI lags by ~2.1 s.** It polls at 1 Hz and the value is cached, so it reports a change later
> than it happened. On cell5m-a the 1 Hz series placed a beam change four seconds *after* an
> outage; at 6 Hz it begins inside the outage's last 100 ms. Do not use a 1 Hz timestamp as the
> pivot when testing a 6 Hz series — the lag between them is part of what differs.

---

## 4c. Analysing a DLF

```bash
./venv/bin/python ./inspect_dlf.py FILE.dlf      # log IDs by category + verdict
./dlf-check FILE.dlf [--codes]                   # strict walk: records, resyncs, skipped bytes
./dlf-rates.py FILE.dlf <probe_start> <probe_end> <host_minus_utc_s> out.csv [margin_s]
```

`inspect_dlf.py` buckets records into LTE RRC/NAS, NR5G RRC/NAS, NR5G MAC and NR5G ML1
(rank, MCS, grants). Ranges are matched **narrowest-first** — NR5G MAC (`0xB880–0xB8BF`)
sits *inside* the NR5G RRC/NAS span (`0xB800–0xB8FF`), and a first-match scan filed every
MAC record as RRC/NAS, making the verdict wrongly report "no MAC".

### Two readers, deliberately

`dlf_records.py` (used by `inspect_dlf.py` and `dlf-rates.py`) and `dlf-check` have
**different acceptance rules and do not always agree**. On the 107 MB S1 capture:
`dlf-check` 390,687 records / 2 resyncs / 130 bytes skipped; `dlf_records` 390,037 / 5 /
1,988 — `dlf-check` accepts 650 records the other rejects. On a clean capture they agree
exactly, so the split appears only where malformed records exist. **Do not call either
"the" reader.** Anything shared across hosts comes from `dlf_records.iter_file`, so that is
the equivalence that matters.

Both resync on malformed records rather than stopping. A reader that walks sequentially and
stops at the first bad length **silently truncates the capture** — on S1 that made a
16-minute capture look as though it ended before the cell began. `iter_file` streams in
chunks; a multi-GB DLF must not be read whole.

### Never compute a capture span from record timestamps

Captures carry a few records whose timestamp field is garbage, **at both ends**. On the
107 MB S1 capture the maximum decodes to year 13086 and the minimum to 1980, so naive
max-minus-min reads as tens of thousands of days on a 6-minute capture. A handful of records
in ~390,000 — harmless to counts, fatal to any span or axis derived from them, and
reader-independent. **Clip to the cell window first**, and remember §8: DLF timestamps are
true UTC while the hosts are not.

After a resync the walk has just crossed garbage, so the record following it is the least
reliable in the file. Byte offsets are trustworthy where timestamps are not — ask "are the
dropped bytes clustered at the events?" in **offsets**, not timestamps.

### Vendor tools

- **QXDM / QCAT** open `.dlf` natively — the path for MAC-layer scheduler decode. Capture
  here, hand the DLF to whoever holds the licence. (XCAL is a *different* product, from
  Accuver, producing `.drm`; there is no Linux path to XCAL logs.)
- **SCAT** reads QMDL: `./dlf2qmdl.py FILE.dlf FILE.qmdl` re-wraps each record in a
  `DIAG_LOG_F` response with CRC-16/X-25 and HDLC escaping.
- `package-for-qcat.sh <celldir> <label> [out_dir]` cuts labelled, time-bounded slices around
  the disturbed seconds on both hosts — the form a QCAT operator will actually open, instead
  of a 6.6 GB file.

---

## 5. Disk management

| | Host A | Host B |
|---|---|---|
| DLF + qcsuper log | `results/<cell>/` | `~/diag-logs/` |
| pcap | `results/<cell>/` | `~/pcap-logs/` |
| PSI csv | `~/sys-logs/` | `~/sys-logs/` |
| Cell artefacts | `results/<cell>/` | `~/teleop/cells/<room>/` |

### Preflight — refuse rather than fill the disk

Enforced inside each capture script on Host B:

```bash
# capture.sh  — 10 MB/s + 5 GB spare
need_kb=$(( dur * 10 * 1024 + 5 * 1024 * 1024 ))
# pcap.sh     —  1 MB/s + 5 GB spare
need_kb=$(( dur * 1024 + 5 * 1024 * 1024 ))
avail_kb=$(df -Pk <dir> | awk 'NR==2 {print $4}')
[ "$avail_kb" -ge "$need_kb" ] || exit 1
```

Rule of thumb at full mask: **~9 GB per host per 15-minute cell.**

### Retention — TWO HONEST GAPS

**No retention policy is implemented anywhere.** There is no deletion logic in `run-cell-b.sh`,
`capture.sh`, `pcap.sh`, `hop-recorder.sh` or `long-run.sh`. Raw DLFs accumulate indefinitely —
Host B's `~/diag-logs` is 74 GB across 16 files as of 2026-09-22.

**Gap 1 — the 2026-09-16 deletion is not recorded.** 109 GB of raw DLF was deleted (18
`overnight-2026-09-15` cycles plus 6 S1–S3) on the operator's explicit decision. The command and the
selection criteria are not in any script or shell history. Only the QCSuper `.log` files and the
per-second `dlf-rates` summaries survive. **Ask the operator; do not reconstruct it.**

**Gap 2 — reduce-then-discard works but is not policy.** Reducing a DLF to `dlf-rates.csv` preserves
everything the event-screening statistic needs: the overnight reductions survived that deletion and
were enough to screen 19 cycles for disturbed seconds afterwards. But it has never been written down
as a step or automated, so it is a **proposal**, not current practice.

```bash
# the reduction that makes a raw DLF discardable for screening purposes
python3 diag-capture/dlf-rates.py <dlf> <probe_start_s> <probe_end_s> <host_minus_utc_s> <out.csv> [margin_s]
# ~1 MB per cell, against 3–10 GB of raw
```

---

## 6. Preflight gates (Host B, all before the subscriber starts)

```bash
# PTP servo must be LOCKED — phc2sys steps CLOCK_REALTIME by seconds while acquiring
servo=$(journalctl --since "-60 sec" --no-pager | grep -oE "phc2sys.*s[0-9]" | tail -1 | grep -oE "s[0-9]$")
[ "$servo" = s2 ] || exit 1                       # FORCE=1 overrides

# clock offset — MEASURED, never estimated
python3 ~/diag-capture/clock-offset.py            # median of 3 STABLE SNTP servers -> host_minus_utc_s
```

**The server list is `time.google.com` / `time.cloudflare.com` / `time.apple.com` on BOTH hosts,
and the two must always match.** `pool.ntp.org` was dropped on 2026-09-22: it resolves to a
different server on every lookup, so it contributes a fresh network path each time, and it was the
worst server on both hosts by a wide margin (A: 23 ms range; B: 16.9 ms stdev, 3–4× the next
worst). Dropping it took the per-measurement stdev from 3.4 → 1.6 ms on A and 6.3 → 4.2 ms on B.

Before changing that list, run `ntp-server-check.py` **on both hosts** and compare on **stdev**,
never on range — range can only grow as samples are added, so ranges from different n are not
comparable. Three traps, all of which we walked into:

| Trap | What happened |
|---|---|
| RTT predicts stability | It does not. `time.apple.com` is the slowest server on both hosts (~90 ms) and the steadiest on both; `time.nist.gov` is slow *and* near-worst. A least-RTT estimator therefore optimises the wrong quantity. |
| A result from one host transfers | A least-RTT estimator measured 3× better on A and ~10% on B. On A one server won the RTT race 28/30, so it was really "always ask the same server"; on B no server dominates and pool *won* 3 times in 12, so least-RTT selected the server we were removing. |
| A small sample is enough | At n=8 the ranking inverts and the pool set looks best. Use n≥30. |

`time.apple.com` misses roughly 1 lookup in 8 (4/30 on B, 3/30 on A — a server property, since it
reproduces on both paths). At three servers a miss degrades the median to a mean of two, which is
tolerable; if the rate climbs, `time.nist.gov` is the drop-in (0.47 ms worse on B, never misses)
and **both hosts switch together**.

A split server list is the one configuration to avoid: A and B measuring with different sets
reintroduces exactly the inter-host discrepancy that the 1 ms same-cell agreement rules out.

> The offset drifts **seconds per day** (−14.733 → −25.595 over four days). An *estimated* offset
> once misaligned a whole modem timeline by 2.5 s with every record count intact. Measure it per
> cell and pass it to `dlf-rates.py`.

### Arm verification — the check that actually matters

```bash
ARM_T=$(date +%s)                                  # BEFORE launching anything
# born()  : creation time from the filename stamp <room>-YYYYMMDDTHHMMSSZ, `stat -c %W` fallback
# fresh() : newest file whose born() >= ARM_T
# growing(): size increases across a 3 s sleep
```

> **Do not gate on mtime.** A stale capture that is still writing has an mtime of "just now" — an
> mtime gate accepted exactly the file it was written to reject, and cost a whole cell that recorded
> nothing while reporting success.
>
> `flock -n 9 9>f` only **tests** the lock: the redirection is scoped to the command, so it reserves
> nothing and races. That race is why the arm verification exists.

---

## 7. Stopping safely

- **Nothing here needs sudo at run time.** The only sudo is the one-off `setcap` on Host B and the
  scoped NOPASSWD rule on Host A.
- **Stop things by PID, never by pattern.** `pkill -f <pattern>` matches the calling shell.
- **Never signal Host B's tcpdump** (see §2). Let `-G` expire.
- **`diag-log-off` will stop a running capture.** Do not run it during a cell.
- Starting a second `capture.sh` or `pcap.sh` will refuse, correctly, rather than interfere.
- After any abnormal exit, confirm the modem is quiet:
  ```bash
  fuser -v /dev/ttyUSB0            # should be empty
  <diag-log-off path> /dev/ttyUSB0 # prints "0 bytes in 2s -> logging off"
  ```

---

## 8. Known traps, each paid for once

| Trap | Symptom | Defence |
|---|---|---|
| `pgrep -f` self-match | "still running" when nothing is | match the binary, `pgrep -x` |
| mtime freshness gate | accepts a stale still-writing file | use creation time from the filename |
| `flock -n 9 9>f` | reserves nothing, races | verify the artefact, not the lock |
| Unkillable tcpdump (caps) | `EPERM` on TERM and KILL | `-G`/`-W 1` self-deadline |
| Lock fd inherited by tcpdump | flock held until reboot | `9<&-` on the invocation |
| ns vs µs pcap magic | delays ~1000 s wrong | read the magic per file |
| mapper alias in diskstats | every disk column zero | resolve to `dm-N` |
| Estimated clock offset | timeline 2.5 s out, counts fine | measure per cell |
| **DLF timestamps are true UTC; the HOSTS are wrong** | a series 15-25 s out of register; conclusions invert | convert with `host_minus_utc_s` before comparing to anything host-clocked |
| **A `...Z` filename we wrote is not Z** | an external reader opens the file and the records are 25 s away | label shipped artefacts in true UTC, and say the offset has been applied |
| A claim with an unstated filter | "first sample above X" that had a hidden time guard | state the selection, or give two windows and a difference |
| **One host's measurement stated as the pair's** | a correct number with a fabricated scope; nothing reads as uncertain | before stating a property of the pair, ask which host it was measured on — then measure it on the other |
| `MASK=nr5g` | drops `0x1C0D` | decide explicitly |
| DIAG on the render host | render stalls, frames superseded | see below |

> **The two hosts are not interchangeable.** Different sudo model (scoped NOPASSWD vs file
> capabilities), different tooling root (`~/code/rust-sdks/diag-capture` vs `~/diag-capture`),
> different clock offset, different pcap timestamp resolution (ns vs µs), different disk
> behaviour under identical write load — and **different 5G carriers**: Host A on ARFCN 521310
> (2606.55 MHz), Host B on 501390 (2506.95 MHz), 99.6 MHz apart in n41, sharing PCI 85 because
> PCI is reused across frequencies. A capture directory holds both hosts' `ping-*.txt` and
> `hops.csv` under identical names, distinguished only by the `hostb/` path segment; the tell
> for a B-side `hops.csv` is the absence of `qmi_` columns.
>
> **Neither host is synced to UTC.** Host A has no time discipline at all — `NTP service:
> inactive`, no chrony or ntpd installed, `systemd-timesyncd` disabled — and it is the PTP
> grandmaster, so Host B inherits its drift through `ptp4l`/`phc2sys`. `timedatectl` reporting
> "System clock synchronized: yes" on B means only that *something* is disciplining it, and that
> something is A. Measured drift **−2.79 s/day, 32.3 ppm**: −14.733 s on 18 Sep, −25.601 s on
> 22 Sep. PTP gives excellent *relative* sync and no *absolute* time.
>
> **So the modem is right and we are wrong.** DLF record timestamps are network-disciplined and
> are true UTC; the host clock runs 15–25 s behind. Verified on Host A to a 60 ms residual: the
> recorder's start, converted with the measured offset, lands on the first DLF record.
>
> Fixing it means running NTP on **Host A** (B follows A; NTP on B would fight `phc2sys`), needs
> sudo, and steps the clock ~25 s — so it belongs between campaigns, with the offset recorded
> immediately before and after so existing captures stay convertible. **The post-step offset
> should measure ~0; a non-zero one means the anchor did not take** and the hosts are still
> free-running with a new starting point.
>
> `timedatectl`'s "System clock synchronized" flag is not a UTC check — it reports only that
> *something* is disciplining the clock. Host B reads `yes` while following a grandmaster with no
> reference at all; Host A, the grandmaster, honestly reads `no`. Use `clock-offset.py`.
> `capture_timestamp_us`, `hops.csv` and every media artefact are on the HOST clock.
> `dlf-rates.py` takes `--host-minus-utc` for exactly this reason; reading payloads straight out
> of `dlf_records.iter_file()` bypasses it. Three separate wrong conclusions in this campaign came
> from mixing the two: a stale EPOCH file put a reduction 48 s out, a freshly measured offset
> applied to a three-day-old capture put a slice 8 s out, and a media window subtracted from raw
> modem timestamps put an RSRP series 15 s out and briefly "disproved" a validated field.
>
> **A cell with DIAG running cannot be used to characterise render timing.** On `cell15m-a` the
> single 650 ms render stall sat in the one second of 900 where the disk blocked hardest — 904 ms of
> `io_full` while writing 259 MB, which was the DIAG capture. In the nine seconds before it the DIAG
> record rate fell to 19% of median (the recorder starving), then flushed. Host A, writing the same
> volume, never blocked above 21 ms.
>
> A **`DIAG=0`** cell is the outstanding test.

---

## 9. File inventory

| File | Purpose |
|---|---|
| `qcsuper-noroot` → `qcsuper-noroot-fast2` | **The capture wrapper.** Rootless, fast reader, opcode 158, record splitting, deterministic exit |
| `diag-log-off` | Force the log mask off and confirm silence. Run after every capture |
| `test-qcsuper-build.sh` | Benchmark a candidate wrapper build |
| `hop-recorder.sh` / `hop-recorder-b.sh` | Per-hop recorder, A (privileged) and B (unprivileged receive side) |
| `capture-around-cell.sh` | Capture around one publish cell |
| `paired-cell.sh` | One command: paired A/B cell, every instrument on both hosts, pulls B's data |
| `live-cell.sh` | One live cell with the full instrument stack; sets the reduction anchor |
| `long-run.sh` | Long stream plus full hop recording |
| `pcap.sh` | RTP/RTCP header capture on the 5G interface |
| `preflight-check.sh` | Read every instrument against a known-good cell, one named pass condition each |
| `clock-offset.py` | Measure this host's offset from true UTC → `host_minus_utc_s` |
| `ntp-server-check.py` | Score candidate NTP servers/estimators before changing `SERVERS`; run on BOTH hosts |
| `ml1-ca.py` | 0xB97F → per-carrier PCI/ARFCN/BRSRP, discovering each carrier block (stride is NOT fixed) |
| `sweep-driver.sh` | Anchored capacity-breakpoint sweep |
| `overnight-driver.sh`, `overnight-driver2.sh`, `depal9-driver.sh`, `restart-to-driver2.sh` | Campaign drivers |
| `s1-arm.sh`, `s2-arm.sh`, `s3-arm.sh` | Queue-location experiment arms |
| `probe-timed.sh` | Uplink capacity probe with a millisecond event log |
| `qdisc-10hz.sh`, `driver-counters-5ms.sh` | High-rate queue and driver counter samplers |
| `hop-ttl-probe.py`, `tcp-rtt-probe.py` | Path probes used by the arms |
| `udp-ladder-send.py`, `udp-ladder-recv.py` | UDP loss ladder, no SFU in the path |
| `inspect_dlf.py` | Log IDs by category plus a scheduling-data verdict |
| `dlf-check` | Strict standalone DLF walk (its own acceptance rule) |
| `dlf_records.py` | Resyncing record iterator shared by the analysis tools |
| `dlf-rates.py` | Per-second per-code counts for cross-host sharing |
| `dlf-slice.staged` | Cut a time window (and optionally a code set) out of a DLF — **live code**, despite the name |
| `dlf2qmdl.py` | DLF → QMDL for SCAT |
| `package-for-qcat.sh` | Labelled event slices for a QCAT operator |
| `rtp-join.py`, `paired-report.py` | Join both hosts' captures on RTP; paired A/B report on one time base |

---

## 10. What DIAG cannot tell you

- **`UECapabilityInformation` is sent once, at registration.** A capture started mid-session
  will never contain it. Getting it means forcing a re-attach *while capturing*, which drops
  the bearer carrying the host's default route — console access, never during a live run.
- **MAC-layer scheduling records are not RRC**, so they never appear in a GSMTAP pcap however
  it was produced. Scheduler analysis is DLF → QCAT.
- **UL 2×2 MIMO on this module is SA-only and TDD-only** (n38/n41/n48/n77/n78/n79); the NSA
  row of the hardware design lists no UL MIMO at all. Both Tx chains are on **ANT0 and
  ANT2** — ANT1 and ANT3 are receive-only, so a board populated ANT0+ANT3 has one Tx
  connected and cannot do 2-layer uplink regardless of configuration.
- **UL carrier-aggregation combinations are in no public document.** They are in
  `Quectel_RM520N-GL_CA&EN-DC_Features` from a Quectel FAE, or visible per-firmware as
  `supportedBandCombinationList` inside `UECapabilityInformation`.

---

*Assembled 2026-09-22 from Host A's tooling and Host B's, each read out of the files rather than
recalled. Sections 5 gap 1 and gap 2 are open questions, not documented practice.
Sections 0, 4c, 9 and 10 were folded in from `README.md` (2026-09-22), which this file replaces.*
