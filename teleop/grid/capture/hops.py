"""1 Hz hop counters on one clock: kernel queue, interface counters, modem handoff, radio.
Standard library only.

Port of archive/diag-capture/hop-recorder.sh (A, sender: tx side, QMI handoff via NOPASSWD
qmicli) and hostb-diag-capture/hop-recorder-b.sh (B, receiver: rx side, per-socket UDP drops,
no sudo). Rules kept:

* Radio via mmcli needs periodic signal polling ARMED (--signal-setup); a modem at
  "refresh rate: 0 seconds" returns nothing and every radio column is silently EMPTY (B
  shipped that for four days). Arm it, then VERIFY THE VALUE PARSES with the collector's
  own parser (`parse_mmcli_signal`), not a second expression written to look equivalent.
* Columns a host cannot read are ABSENT, not zero: a zero in qmi_rx_dropped would read as
  "dropped nothing" when the truth is "nobody looked". B has no QMI columns.
* /proc/net/udp: data rows have 13+ fields (the header has 15; a NF>=15 guard matched only
  the header). Hex is parsed in Python (mawk has no strtonum).
* The recorder is our own process: stopped by verified pid, never by pattern.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):   # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "teleop.grid.capture"

from . import CODE_ROOT, python_exe, spawn_detached, start_ticks, verify_pid  # noqa: E402

COLUMNS_A = ["unix_ms", "qdisc_sent_pkts", "qdisc_dropped", "qdisc_overlimits", "qdisc_requeues",
             "qdisc_backlog_bytes", "qdisc_backlog_pkts", "sys_tx_packets", "sys_tx_dropped", "sys_tx_errors",
             "qmi_tx_ok", "qmi_tx_dropped", "qmi_rx_ok", "qmi_rx_dropped", "nr_rsrp_dbm", "nr_rsrq_db", "nr_snr_db"]
COLUMNS_B = ["unix_ms", "qdisc_sent_pkts", "qdisc_dropped", "qdisc_backlog_pkts", "sys_rx_packets",
             "sys_rx_dropped", "sys_rx_errors", "sys_rx_missed", "sys_tx_packets", "sys_tx_dropped",
             "udp_sock_max_rxq", "udp_sock_drops", "udp_indatagrams", "udp_inerrors", "udp_rcvbuferrors",
             "nr_rsrp_dbm", "nr_rsrq_db", "nr_snr_db"]


def columns(role: str) -> list[str]:
    return COLUMNS_A if role == "a" else COLUMNS_B


def hops_path(host_dir: Path, label: str) -> Path:
    return Path(host_dir) / f"{label}.hops.csv"


def _run(argv, timeout=5) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------- parsers (shared with preflight)

def parse_tc_qdisc(text: str) -> dict:
    out = {}
    m = re.search(r"Sent \d+ bytes (\d+) pkt \(dropped (\d+), overlimits (\d+) requeues (\d+)\)", text)
    if m:
        out.update(qdisc_sent_pkts=m[1], qdisc_dropped=m[2], qdisc_overlimits=m[3], qdisc_requeues=m[4])
    m = re.search(r"backlog (\d+)b (\d+)p", text)
    if m:
        out.update(qdisc_backlog_bytes=m[1], qdisc_backlog_pkts=m[2])
    return out


def parse_mmcli_signal(text: str) -> dict:
    """`mmcli -m N -J --signal-get` -> {nr_rsrp_dbm, nr_rsrq_db, nr_snr_db} from the 5G section
    only (a text grep for the first 'rsrp:' can pick LTE's under NSA)."""
    try:
        sig = json.loads(text)["modem"]["signal"]
    except (ValueError, KeyError, TypeError):
        return {}
    nr = sig.get("5g") or sig.get("nr5g") or {}
    out = {}
    for src, dst in (("rsrp", "nr_rsrp_dbm"), ("rsrq", "nr_rsrq_db"), ("snr", "nr_snr_db")):
        v = str(nr.get(src, "")).strip()
        try:
            out[dst] = f"{float(v):g}"
        except ValueError:
            pass
    return out


def parse_qmi_packet_stats(text: str) -> dict:
    out = {}
    for pat, key in (("TX packets OK", "qmi_tx_ok"), ("TX packets dropped", "qmi_tx_dropped"),
                     ("RX packets OK", "qmi_rx_ok"), ("RX packets dropped", "qmi_rx_dropped")):
        m = re.search(pat + r":\s*'?(\d+)", text)
        if m:
            out[key] = m[1]
    return out


def parse_proc_net_udp(text: str) -> tuple[int, int]:
    """(max rx_queue across sockets, sum of per-socket drops). Data rows only (NF >= 13)."""
    mx, drops = 0, 0
    for i, line in enumerate(text.splitlines()):
        f = line.split()
        if i == 0 or len(f) < 13 or ":" not in f[4]:
            continue
        try:
            rq = int(f[4].split(":")[1], 16)
            drops += int(f[-1])
        except ValueError:
            continue
        mx = max(mx, rq)
    return mx, drops


def parse_snmp_udp(text: str) -> dict:
    rows = [line.split() for line in text.splitlines() if line.startswith("Udp:")]
    if len(rows) < 2:
        return {}
    d = dict(zip(rows[0][1:], rows[1][1:]))
    return {"udp_indatagrams": d.get("InDatagrams", ""), "udp_inerrors": d.get("InErrors", ""),
            "udp_rcvbuferrors": d.get("RcvbufErrors", "")}


def resolve_modem(cfg_index: int, mmcli_list: str) -> tuple[int | None, str]:
    """The configured index if ModemManager lists it; else the ONLY modem, said out loud.
    Indices change when the modem re-enumerates (A's host.yaml said 1 while it was 0)."""
    idx = [int(m) for m in re.findall(r"/Modem/(\d+)", mmcli_list)]
    if cfg_index in idx:
        return cfg_index, f"modem {cfg_index}"
    if len(idx) == 1:
        return idx[0], f"modem_index {cfg_index} not listed; using the only modem, {idx[0]}"
    return None, f"modem_index {cfg_index} not listed; ModemManager lists {idx or 'none'}"


def qmi_device(mmcli_json: str) -> str | None:
    try:
        port = json.loads(mmcli_json)["modem"]["generic"]["primary-port"]
    except (ValueError, KeyError, TypeError):
        return None
    return f"/dev/{port}" if port and port.startswith("cdc-wdm") else None


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------- sampling

class Sampler:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.role = cfg["role"]
        self.iface = cfg["wwan_iface"]
        self.modem, self.modem_note = resolve_modem(int(cfg["modem_index"]), _run(["mmcli", "-L"], 10))
        self.qmi_dev = None
        self.can_qmi = False
        if self.role == "a" and self.modem is not None:
            self.qmi_dev = qmi_device(_run(["mmcli", "-m", str(self.modem), "-J"], 10))
            self.can_qmi = bool(self.qmi_dev) and bool(_run(["sudo", "-n", "qmicli", "--version"], 10))

    def arm_signal(self) -> str:
        if self.modem is None:
            return "no modem; radio columns will be EMPTY"
        _run(["mmcli", "-m", str(self.modem), "--signal-setup=5"], 10)
        time.sleep(6)
        got = parse_mmcli_signal(_run(["mmcli", "-m", str(self.modem), "-J", "--signal-get"], 10))
        if "nr_rsrp_dbm" not in got:
            return "WARNING: modem reports no 5G rsrp after --signal-setup; radio columns will be EMPTY"
        return f"radio: 5G RSRP reads {got['nr_rsrp_dbm']} dBm"

    def sample(self) -> dict:
        row = {"unix_ms": str(int(time.time() * 1000))}
        row.update(parse_tc_qdisc(_run(["tc", "-s", "qdisc", "show", "dev", self.iface])))
        s = f"/sys/class/net/{self.iface}/statistics"
        row.update(sys_tx_packets=_read(f"{s}/tx_packets"), sys_tx_dropped=_read(f"{s}/tx_dropped"),
                   sys_tx_errors=_read(f"{s}/tx_errors"))
        if self.role == "b":
            row.update(sys_rx_packets=_read(f"{s}/rx_packets"), sys_rx_dropped=_read(f"{s}/rx_dropped"),
                       sys_rx_errors=_read(f"{s}/rx_errors"), sys_rx_missed=_read(f"{s}/rx_missed_errors"))
            mx, drops = parse_proc_net_udp(_read("/proc/net/udp"))
            row.update(udp_sock_max_rxq=str(mx), udp_sock_drops=str(drops))
            row.update(parse_snmp_udp(_read("/proc/net/snmp")))
        elif self.can_qmi:
            row.update(parse_qmi_packet_stats(
                _run(["sudo", "-n", "qmicli", "-d", self.qmi_dev, "-p", "--wds-get-packet-statistics"])))
        if self.modem is not None:
            row.update(parse_mmcli_signal(_run(["mmcli", "-m", str(self.modem), "-J", "--signal-get"])))
        return row


def record(cfg: dict, out: Path, span_s: int) -> int:
    stop = {"flag": False}

    def on_sig(signum, frame):  # noqa: ARG001
        stop["flag"] = True
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    s = Sampler(cfg)
    cols = columns(cfg["role"])
    print(f"hops: {s.modem_note}; qmi={'yes' if s.can_qmi else 'no'}", flush=True)
    with open(out, "x") as f:           # 'x': never append to a previous run's file
        f.write(",".join(cols) + "\n")
        f.flush()
        # First row BEFORE arming the radio (6 s) so the file exists and grows immediately.
        f.write(",".join(s.sample().get(c, "") for c in cols) + "\n")
        f.flush()
        print(s.arm_signal(), flush=True)
        end = time.time() + span_s
        nxt = time.time()
        while time.time() < end and not stop["flag"]:
            t0 = time.time()
            row = s.sample()
            f.write(",".join(row.get(c, "") for c in cols) + "\n")
            f.flush()
            nxt = max(nxt + 1.0, t0)
            time.sleep(max(0.0, nxt - time.time()))
    print(f"hops: wrote {out}", flush=True)
    return 0


# ---------------------------------------------------------------- Captures interface

def start(cfg: dict, host_dir: Path, label: str, span_s: int) -> dict:
    out = hops_path(host_dir, label)
    if out.exists():
        raise RuntimeError(f"{out.name} already exists; refusing to overwrite")
    args = {"cfg": cfg, "out": str(out), "span_s": int(span_s), "label": label}
    b64 = base64.b64encode(json.dumps(args).encode()).decode()
    argv = [python_exe(), "-m", "teleop.grid.capture.hops", "record", "--label", label, "--json", b64]
    log = Path(host_dir) / f"{label}.hops.log"
    started = time.time()
    pid = spawn_detached(argv, log, cwd=CODE_ROOT)
    return {"path": str(out), "pid": pid, "start_ticks": start_ticks(pid), "started": started, "closed": None,
            "bytes": None, "span_s": int(span_s), "deadline": started + int(span_s), "log": str(log),
            "columns": columns(cfg["role"]), "signature": [["teleop.grid.capture.hops", "record", label]]}


def stop(cfg: dict, rec: dict, wait_s: float | None = None) -> dict:
    ok, why = verify_pid(rec)
    if ok:
        try:
            os.kill(rec["pid"], signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + (wait_s if wait_s is not None else 15)
    while time.time() < end and verify_pid(rec)[0]:
        time.sleep(0.3)
    return {"stopped": not verify_pid(rec)[0]}


def radio_check(rec: dict) -> dict:
    """After arming: are the radio columns populated? (warn, not fail: counters still valid)."""
    try:
        lines = Path(rec["path"]).read_text().splitlines()
    except OSError:
        return {"populated": False, "note": "no hops file"}
    if len(lines) < 2:
        return {"populated": False, "note": "no rows yet"}
    hdr = lines[0].split(",")
    i = hdr.index("nr_rsrp_dbm")
    vals = [ln.split(",")[i] for ln in lines[1:] if len(ln.split(",")) > i]
    pop = any(v for v in vals)
    return {"populated": pop, "note": "" if pop else "radio columns EMPTY so far (signal polling arms in ~6 s)"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="teleop.grid.capture.hops")
    ap.add_argument("cmd", choices=["record"])
    ap.add_argument("--label", required=True)
    ap.add_argument("--json", required=True)
    a = ap.parse_args(argv)
    args = json.loads(base64.b64decode(a.json))
    return record(args["cfg"], Path(args["out"]), int(args["span_s"]))


if __name__ == "__main__":
    sys.exit(main())
