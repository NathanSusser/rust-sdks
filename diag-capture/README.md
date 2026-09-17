# Modem DIAG capture on the RM520N-GL

How to capture Qualcomm baseband logs from the Quectel RM520N-GL on Host A, what the
tooling in this directory does, and the traps that cost real debugging time.

**Why we capture at all.** The granted uplink bitrate collapsed to 35 kbps on a link that
measured 50 Mbps. The 5G NR MAC/ML1 records in a DIAG log carry the scheduler's actual
grants — the first instrument that can separate *"the network stopped granting"* from
*"our sender stopped asking"*. Nothing available over AT commands answers that.

---

## 1. Hardware and the DIAG port

| | |
|---|---|
| Module | Quectel RM520N-GL, USB `2c7c:0801` |
| Chipset | Qualcomm SDX62, 3GPP Release 16 |
| Firmware | `RM520NGLAAR03A04M4G` |
| DIAG port | `/dev/ttyUSB0` |

**Identify the DIAG port by USB descriptor, never by number.** The interface signature is
`bInterfaceClass=ff`, `bInterfaceSubClass=ff`, `bInterfaceProtocol=30` (hex; 48 decimal),
`bNumEndpoints=02`:

```bash
for i in /sys/bus/usb/devices/1-10.1:1.*; do
  echo "intf=$(cat $i/bInterfaceNumber) class=$(cat $i/bInterfaceClass) \
sub=$(cat $i/bInterfaceSubClass) proto=$(cat $i/bInterfaceProtocol) \
eps=$(cat $i/bNumEndpoints) tty=$(ls $i | grep ^ttyUSB)"
done
```

On Host A interface `00` matches and maps to `ttyUSB0`. The port layout is:

```
cdc-wdm8 (qmi)   ttyUSB0 (ignored)   ttyUSB1 (gps)   ttyUSB2 (at)   wwan0 (net)
```

ModemManager must show the DIAG port as **`(ignored)`** — check with
`mmcli -m 0 | grep -A3 'ports:'`. If MM claims it, you need a udev rule to make it let go.

> `wwan0` carries this host's default route. DIAG capture is read-only with respect to the
> data path and does not disturb the bearer, but anything that re-registers the modem will
> drop the link.

---

## 2. Prerequisites

**`dialout` membership, and no sudo.**

```bash
sudo usermod -aG dialout "$USER"   # then log out and back in — a running shell keeps its old groups
id -nG | tr ' ' '\n' | grep -x dialout
```

That is the only privilege needed. See §4 for why sudo is *not* required despite QCSuper
trying very hard to demand it.

`tcpdump` and `qmicli` are NOPASSWD on this host, which `hop-recorder.sh` uses for the wire
and QMI hops. They are unrelated to DIAG capture; a host without them still records the
modem log.

---

## 3. Install

QCSuper is not on PyPI — install from the repo into the venv beside these scripts:

```bash
cd /home/nsusser/code/rust-sdks/diag-capture
python3 -m venv venv                       # needs python3.12-venv if ensurepip is missing
./venv/bin/pip install --upgrade pip setuptools wheel
./venv/bin/pip install 'qcsuper @ git+https://github.com/P1sec/QCSuper.git'
```

Installed here: qcsuper 2.1.3, plus `pycrate`, `pyserial`, `pyusb`, `crcmod`. `py-spy` and
`scat` are also in the venv for profiling and for reading converted captures.

Verify:

```bash
./venv/bin/python -c 'import qcsuper, os; print(os.path.dirname(qcsuper.__file__))'
```

---

## 4. Rootless capture — `qcsuper-noroot`

**`qcsuper-noroot` is a symlink to `qcsuper-noroot-fast2`.** Always invoke the symlink, and
record which build a run used with `readlink qcsuper-noroot` (the drivers already do).

### Why the wrapper exists

Stock QCSuper escalates to root in exactly two cases, both in
`venv/.../qcsuper/inputs/usb_modem_pyserial.py`:

1. `access(device, W_OK)` fails — **`dialout` membership already satisfies this**.
2. `detect_diag_interference()` (line 104) walks `/proc`, matches any process whose cmdline
   contains `modemmanager` or `qc`, and calls `_try_escalate()` when it cannot read that
   process's `fd` directory. **ModemManager runs as root, so this fires unconditionally.**

When `DISPLAY` is set, `_try_escalate` prefers `pkexec`, which throws a **graphical password
dialog onto the physical console**. From a terminal you see no output, no error, no
traceback — it just hangs forever, without even opening the serial port. This cost roughly
an hour before it was diagnosed.

The wrapper unsets `DISPLAY` and replaces `detect_diag_interference` with a no-op. MM
already ignores our DIAG port, so there is genuinely nothing for that check to find.

> **Do not install a NOPASSWD sudoers rule for qcsuper.** An earlier draft of this setup
> suggested one; it is a privilege-escalation hole. The venv is user-writable, so NOPASSWD
> on `venv/bin/qcsuper` lets anything running as that user rewrite the script — or any
> module it imports — and get root. `NOPASSWD: /bin/kill` is worse still. Use `dialout`
> plus this wrapper, and `timeout -s INT` where a script needs a bounded stop.

### What `fast2` adds over the plain rootless wrapper

1. **Reader speed.** Stock QCSuper reads the serial port **one byte per call** and grows the
   frame with `bytes +=`, pinning a core at full mask (99.7% even on an idle link) and
   losing ~30% of frames to truncation. `fast2` reads whatever is waiting and splits on
   `0x7e`, and drops the eager `repr()` formatting done for every bad frame.
2. **Opcode 158.** This firmware sends a disjoint half of the NR5G log set wrapped in
   opcode 158 (inner record at payload offset 19). `fast2` decodes it and feeds each record
   to the normal log path so it lands in the DLF like any other.
3. **DLF record splitting.** One `DIAG_LOG_F` frame can carry several records back to back.
   QCSuper wrote them as *one* record under the first record's length — the
   `Dismissing log type ... indicating size X instead of Y` warning — leaving misaligned
   bytes in the DLF. `fast2` splits on declared lengths and never writes a record whose
   length does not fit.
4. **Deterministic shutdown.** QCSuper's own SIGINT path races a deinit thread against the
   daemon reader and sometimes never exits. Worse, a `&` job in non-interactive bash starts
   with SIGINT *ignored*, so Python never installs its handler. `fast2` installs its own
   SIGINT/SIGTERM handler: stop DLF writes, flush and close, print counters, exit. A second
   signal exits immediately.
5. **`QCSUPER_NR5G_ONLY=1`** limits the log mask to codes `0xB800–0xB9FF`.

On exit it prints a counter line — `frames`, `bad_crc`, `op158_*`, `dlf_records`,
`dlf_split_frames`, `dlf_dropped_bytes` — worth reading after every capture.

### Full mask vs. narrow mask

`DlfDumper` sets no `limit_registered_logs`, so it enables **every log bit the baseband
offers**, including the NR5G ML1/MAC scheduler items. `PcapDumper` *does* restrict the mask
to RRC/NAS, and the mask is global device state — so **never ask for `--pcap-dump` and
`--dlf-dump` together**; the pcap module can clamp away exactly the records you want.
Capture DLF only and regenerate a pcap offline if you need one:

```bash
./venv/bin/qcsuper --dlf-read FILE.dlf --pcap-dump FILE.pcap
```

---

## 5. Taking a capture

### Around one publish cell (the normal case)

```bash
./capture-around-cell.sh <label> <epoch> <room> <cap_kbps> <codec> <outdir> [extra harness args...]
```

Starts the capture, waits for the DLF to actually grow, publishes the cell at `epoch`,
captures until the harness exits, then stops cleanly and prints the per-category record
summary. It verifies the capture is *live* (not merely launched) before publishing, and
warns if the capture dies mid-cell.

### A long stream with every hop recorded

```bash
./long-run.sh <label> <epoch> <duration_s> <cap_kbps> <codec> [extra...]
```

Wraps `hop-recorder.sh`, which samples every hop on this host at 1 Hz on one clock — WebRTC
stats, fq_codel queue, driver counters, QMI packet stats, 5G RSRP/RSRQ/SNR, a header-only
`wwan0` pcap — and with `DIAG=1` (default here) the modem DLF alongside.

### Recorder only, no publisher

```bash
DIAG=1 ./hop-recorder.sh <label> <duration_s> <outdir>
```

### Campaign drivers

`overnight-driver.sh`, `overnight-driver2.sh`, `depal9-driver.sh`, `s1-arm.sh`,
`s2-arm.sh`, `s3-arm.sh`. These schedule cells and capture in lockstep with Host B. Note
that driver2 captures only every *N*th cycle by design — the DLF budget on Host B is
fixed, so capture is placed where the answer is, and off-cycles separate modem-log load
from network effects.

### Bare manual capture

```bash
mkdir -p logs
sleep infinity | ./venv/bin/python ./qcsuper-noroot \
  --usb-modem /dev/ttyUSB0 --dlf-dump logs/manual-$(date -u +%Y%m%dT%H%M%SZ).dlf
# Ctrl-C, then ALWAYS:
./venv/bin/python ./diag-log-off /dev/ttyUSB0
```

### Validating a new wrapper build

```bash
./test-qcsuper-build.sh <qcsuper-noroot-file> <seconds> <label>
```

Reports start latency, CPU% while capturing, record/NR5G/`0xB9xx` content, resyncs, CRC
lines, and seconds from SIGINT to exit. Run this before letting an experiment depend on a
candidate build.

---

## 6. Always turn logging back off

**This is the single most important operational rule.**

QCSuper's `on_deinit` tries to clear the log mask, but under full-mask load its reply is
lost in the inbound log stream (`unmatched response received`) and **the mask stays on**.
The modem then streams megabytes per second forever, loading the baseband during every
later run — including runs that are measuring something else entirely. Host B measured
13.4 MB in 4 s from a modem left in this state after a clean SIGINT exit.

Every script here runs `diag-log-off` from an `EXIT` trap, so it happens however the script
exits. After any manual capture, run it yourself:

```bash
./venv/bin/python ./diag-log-off /dev/ttyUSB0
```

It sends `DIAG_LOG_CONFIG_F` / `DISABLE` three times, drains, then **confirms silence rather
than assuming it**, printing `logging off` or `STILL STREAMING` and exiting non-zero if the
modem is still talking.

### Checking whether logging is currently on

With no capture running, read the port and see whether anything arrives:

```bash
fuser -v /dev/ttyUSB0          # must be empty first — see §7
./venv/bin/python - <<'PY'
import serial, time
s = serial.Serial('/dev/ttyUSB0', 115200, timeout=1); s.reset_input_buffer()
n, t = 0, time.time()
while time.time() - t < 4: n += len(s.read(65536))
print(n, 'bytes ->', 'LOGS ON' if n else 'logs off')
PY
```

---

## 7. One reader per DIAG port

Two DIAG clients corrupt each other's streams. Every script takes an exclusive `flock` on
`.ttyUSB0.lock` and additionally refuses to start if `fuser` shows the port already held.

Where `~/diag-capture` exists (Host B), the scripts share *that* lock file so the two
toolchains can never collide; elsewhere the lock lives beside these scripts. Override with
`DIAG_LOCK`.

A script that already holds the lock passes `DIAG_LOCK_HELD=1` to `diag-log-off` so the
child does not deadlock against its parent.

---

## 8. Shell traps that bit us for real

Each of these was hit in practice on 2026-09-14:

- **Backgrounded with stdin at EOF, QCSuper stops after ~4 s with no error.** Hence the
  `sleep infinity |` prefix on every invocation — it holds stdin open.
- **`pkill -f PATTERN` matches the shell whose own command line contains PATTERN**, and so
  SIGINTs the caller mid-script. Everything is stopped **by PID**, never by pattern. Where
  a pattern is unavoidable, it is scoped: `pkill -P $$ -x sleep`.
- **`$!` of a pipeline is its *last* element.** The stderr filter is attached with a
  *process substitution*, not a pipe — a pipe would make `$!` the `sed` and leave QCSuper
  holding the port when cleanup signalled it.
- **QCSuper prints the whole bad frame on every CRC failure**: 277 MB of log for a 53 MB DLF.
  The filter keeps the event and drops the hexdump.
- **`tcpdump` keeps root's credentials after `-Z`** and cannot be signalled by this user or
  by sudo. It stops itself with `-G <duration> -W 1`; a SIGINT-based design once left an
  unkillable capture behind.

---

## 9. Analysing a DLF

```bash
./venv/bin/python ./inspect_dlf.py FILE.dlf      # log IDs by category + verdict
./dlf-check FILE.dlf [--codes]                   # strict walk: records, resyncs, skipped bytes
./dlf-rates.py FILE.dlf <probe_start> <probe_end> <host_minus_utc_s> out.csv [margin_s]
```

`inspect_dlf.py` buckets records into LTE RRC/NAS, NR5G RRC/NAS, NR5G MAC, NR5G ML1
(PHY: rank, MCS, grants) and reports whether scheduling data is present at all. Ranges are
matched **narrowest-first** — NR5G MAC (`0xB880–0xB8BF`) sits *inside* the NR5G RRC/NAS span
(`0xB800–0xB8FF`), and a naive first-match scan filed every MAC record as RRC/NAS, which
made the verdict wrongly report "no MAC".

`dlf-rates.py` produces per-second, per-code counts so hosts can compare captures without
moving multi-GB DLFs across the cable.

### Two readers, deliberately

`dlf_records.py` (used by `inspect_dlf.py` and `dlf-rates.py`) and `dlf-check` have
**different acceptance rules and do not always agree**. On the 107 MB S1 capture: `dlf-check`
390,687 records / 2 resyncs / 130 bytes skipped; `dlf_records` 390,037 / 5 / 1,988 —
`dlf-check` accepts 650 records the other rejects. On a clean capture they agree exactly, so
the split appears only where malformed records exist. **Do not describe either as "the"
reader.** Anything shared across hosts comes from `dlf_records.iter_file`, so that is the
equivalence that matters.

Both resync on malformed records rather than stopping. A reader that walks sequentially and
stops at the first bad length **silently truncates the capture** — on S1 that made a
16-minute capture look as though it ended before the cell began.

`iter_file` streams in chunks; a multi-GB DLF must not be read whole (one S2 capture is
~9 GB).

### Never compute a capture span from record timestamps

Captures carry a few records whose timestamp field is garbage, **at both ends**. On the
107 MB S1 capture the maximum decodes to year 13086 and the minimum to 1980, so naive
max-minus-min reads as tens of thousands of days on a 6-minute capture. It is a handful of
records in ~390,000 — harmless to counts, fatal to any span, axis or header derived from
them, and reader-independent. **Clip to the cell window first.**

After a resync the walk has just crossed garbage, so the first record following it is the
least reliable in the file. Byte offsets are trustworthy where timestamps are not — ask
"are the dropped bytes clustered at the events?" in **offsets**, not timestamps.

### Clock

Host times are PTP-synchronised between hosts but run several seconds behind UTC (A measured
−8.98 s on 2026-09-17). DLF timestamps are **network time**. Put them on the host clock with
`host = modem + (host − UTC)`, trusting to about ±2 s. `dlf-rates.py` takes that offset as
an argument and records it in the CSV header.

---

## 10. Reading DLFs in vendor tools

- **QXDM / QCAT** open `.dlf` natively. This is the path for MAC-layer scheduler decode —
  capture on Linux, hand the DLF to whoever holds the licence. QXDM is Qualcomm's, Windows
  only, licensed via Qualcomm or your module vendor. (XCAL is a *different* product, from
  Accuver, producing `.drm`; there is no Linux path to XCAL logs.)
- **SCAT** reads QMDL. Convert with `./dlf2qmdl.py FILE.dlf FILE.qmdl`, which re-wraps each
  DLF record in a `DIAG_LOG_F` response with CRC-16/X-25 and HDLC escaping.

---

## 11. Volume and disk

Full-mask DIAG on a 5G modem is heavy — tens of MB per minute, and captures here reach
multiple GB. The archive at `~/teleop-archive-2026-09/modem-logs-dlf/` is **116 GB across 24
DLFs**. Host A currently has 614 GB free of 913 GB.

The USB DIAG link can also saturate and drop frames under load; `fast2`'s reader rewrite
exists precisely because the stock one-byte reader lost ~30% of frames. Check the
`bad_crc` and `dlf_dropped_bytes` counters after every capture, and carry the caveat
wherever scheduler statistics derived from a lossy capture get used. If drop rates are
unacceptable, `QCSUPER_NR5G_ONLY=1` narrows the mask.

---

## 12. File inventory

| File | Purpose |
|---|---|
| `qcsuper-noroot` → `qcsuper-noroot-fast2` | **The capture wrapper.** Rootless, fast reader, opcode 158, record splitting, deterministic exit |
| `diag-log-off` | Force the log mask off and confirm silence. Run after every capture |
| `capture-around-cell.sh` | Capture around one publish cell |
| `hop-recorder.sh` | Per-second all-hop recorder; `DIAG=1` adds the DLF |
| `long-run.sh` | Long stream + full hop recording |
| `overnight-driver.sh`, `overnight-driver2.sh`, `depal9-driver.sh` | Campaign drivers |
| `s1-arm.sh`, `s2-arm.sh`, `s3-arm.sh` | Queue-location experiment arms |
| `probe-timed.sh` | Uplink capacity probe with a millisecond event log |
| `test-qcsuper-build.sh` | Benchmark a candidate wrapper build |
| `inspect_dlf.py` | Log IDs by category + scheduling-data verdict |
| `dlf-check` | Strict standalone DLF walk (own acceptance rule) |
| `dlf_records.py` | Resyncing record iterator shared by the analysis tools |
| `dlf-rates.py` | Per-second per-code counts for cross-host sharing |
| `dlf2qmdl.py` | DLF → QMDL for SCAT |
| `hop-ttl-probe.py`, `tcp-rtt-probe.py`, `qdisc-10hz.sh` | Path probes used by the arms |
| `.ttyUSB0.lock` | The one-reader-per-port lock |

### Superseded

- **`capture.sh` is stale — do not use it.** It still invokes `sudo` (unnecessary since §4)
  and its header still describes writing a pcap alongside the DLF (harmful per §4). Nothing
  in this directory calls it. The `capture.sh` referenced in `capture-around-cell.sh` and
  `hop-recorder.sh` comments is **Host B's** `~/diag-capture/capture.sh`, a different file.
- `qcsuper-noroot.current`, `qcsuper-noroot-fast`, `*.staged`, `*.bak` are retained build
  history. `qcsuper-noroot.current` is the original plain rootless wrapper, kept because it
  is the minimal readable statement of the escalation fix.

---

## 13. What this cannot tell you

- **`UECapabilityInformation` is sent once, at registration**, in response to a network UE
  Capability Enquiry. A capture started mid-session will never contain it. Getting it means
  forcing a re-attach *while capturing* — which drops the bearer carrying this host's default
  route, so do it with console access and never during a live run.
- **MAC-layer scheduling records are not RRC**, so they never appear in a GSMTAP pcap no
  matter how it was produced. Scheduler analysis is DLF → QCAT.
- **UL 2×2 MIMO on this module is SA-only and TDD-only** (n38/n41/n48/n77/n78/n79); the NSA
  row of the hardware design lists no UL MIMO at all. Both Tx chains are on **ANT0 and
  ANT2** — ANT1 and ANT3 are receive-only, so a board populated ANT0+ANT3 has one Tx
  connected and cannot do 2-layer uplink regardless of configuration.
- **UL carrier-aggregation combinations are in no public document.** They are in
  `Quectel_RM520N-GL_CA&EN-DC_Features`, available from a Quectel FAE, or visible per-firmware
  as `supportedBandCombinationList` inside `UECapabilityInformation`.
