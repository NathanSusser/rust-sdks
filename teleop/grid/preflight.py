"""Pre-flight gates (ARCHITECTURE §6, thresholds in config/gates.yaml). Standard library + PyYAML.

Each gate is a function `gate_<name>(ctx) -> {"name", "pass", "detail"[, "data"]}`. `run()`
executes a host's gates in parallel and every gate returns within 15 s (a gate that does not
is reported as a FAIL "timed out", never waited for).

Gates check the THING, not a proxy for it. The old PTP check read "servo s2" from the journal
and passed while the cable was unplugged, so the PTP gates read the cable's carrier and speed
from /sys, ptp4l's port state from pmc, and count FRESH servo lines. External commands go
through `ctx.run`, so tests inject their output.
"""
from __future__ import annotations

import glob
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import capture
from .capture import diag as _diag
from .capture import hops as _hops

GATE_TIMEOUT_S = 15.0
TELEOP_DIR = Path(__file__).resolve().parent.parent
GATES_PATH = TELEOP_DIR / "config" / "gates.yaml"


def load_gates(path=GATES_PATH) -> dict:
    import yaml  # noqa: PLC0415
    with open(path) as f:
        return yaml.safe_load(f) or {}


def result(name: str, ok: bool, detail: str, **data) -> dict:
    r = {"name": name, "pass": bool(ok), "detail": detail}
    if data:
        r["data"] = data
    return r


def _default_run(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as e:
        return 126, "", str(e)


def _default_read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


@dataclass
class Ctx:
    cfg: dict
    role: str
    cell: dict = field(default_factory=dict)     # Cell.to_dict() (or {} for a cell-less check)
    expect: dict = field(default_factory=dict)
    gates: dict = field(default_factory=dict)
    prev_clock: dict | None = None
    sfu_host: str | None = None
    run: Callable = _default_run
    read: Callable = _default_read
    procs: Callable = capture.list_procs
    clock: Callable | None = None
    now: Callable = time.time

    @property
    def variables(self) -> dict:
        return self.cell.get("variables", {}) if self.cell else {}

    def g(self, name: str) -> dict:
        return self.gates.get(name) or {}


# ---------------------------------------------------------------- PTP

def _carrier(ctx: Ctx, iface: str, want_mbps: int) -> tuple[bool, str, dict]:
    carrier = (ctx.read(f"/sys/class/net/{iface}/carrier") or "").strip()
    speed = (ctx.read(f"/sys/class/net/{iface}/speed") or "").strip()
    data = {"carrier": carrier, "speed_mbps": speed}
    if carrier != "1":
        return False, f"{iface} carrier DOWN (cable unplugged or peer off)", data
    try:
        sp = int(speed)
    except ValueError:
        return False, f"{iface} speed unreadable ({speed!r})", data
    if sp < want_mbps:
        return False, f"{iface} at {sp} Mb/s, want {want_mbps}", data
    return True, f"{iface} carrier up at {sp} Mb/s", data


def _pmc(ctx: Ctx, what: str) -> tuple[str | None, str]:
    """pmc output or None. Unprivileged pmc needs a writable client socket path; if the
    server socket is root-only it gets no reply, so sudo -n is tried second."""
    sock = f"/tmp/teleop-pmc.{os.getpid()}.{threading.get_ident()}"
    attempts = [["pmc", "-u", "-b", "0", "-i", sock, what],
                ["sudo", "-n", "pmc", "-u", "-b", "0", what]]
    why = []
    for argv in attempts:
        rc, out, err = ctx.run(argv, 5)
        try:
            os.unlink(sock)
        except OSError:
            pass
        if rc == 0 and "RESPONSE MANAGEMENT" in out:
            return out, " ".join(argv[:2])
        why.append(f"{' '.join(argv[:2])}: {(err or out).strip().splitlines()[-1] if (err or out).strip() else 'no reply'}")
    return None, "; ".join(why)


def parse_pmc_field(out: str, key: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(key)}\s+(\S+)", out, re.M)
    return m.group(1) if m else None


SERVO_RE = re.compile(r"(ptp4l|phc2sys)\[[\d.]+\]:.*?(?:master offset|offset)\s+(-?\d+)\s+s(\d)")
RMS_RE = re.compile(r"ptp4l\[[\d.]+\]:.*?rms\s+(\d+)\s+max\s+(\d+)")
PORT_STATE_RE = re.compile(r"ptp4l\[[\d.]+\]:.*port \d+ \(([^)]+)\): (\w+) to (\w+) on")


def count_servo_lines(journal: str) -> dict:
    ptp_lines, phc_states, ptp_states, last_off = 0, [], [], None
    for line in journal.splitlines():
        m = SERVO_RE.search(line)
        if m:
            if m[1] == "ptp4l":
                ptp_lines += 1
                ptp_states.append(int(m[3]))
                last_off = int(m[2])
            else:
                phc_states.append(int(m[3]))
            continue
        m = RMS_RE.search(line)
        if m:
            ptp_lines += 1
            last_off = int(m[1])
    return {"ptp4l_servo_lines": ptp_lines, "ptp4l_last_servo": ptp_states[-1] if ptp_states else None,
            "phc2sys_lines": len(phc_states), "phc2sys_last_servo": phc_states[-1] if phc_states else None,
            "last_offset_ns": last_off}


def last_port_state(journal: str, iface: str) -> str | None:
    st = None
    for line in journal.splitlines():
        m = PORT_STATE_RE.search(line)
        if m and m[1] == iface:
            st = m[3]
    return st


def gate_ptp_b(ctx: Ctx) -> dict:
    g = ctx.g("ptp_b")
    iface = ctx.cfg["ptp_iface"]
    ok, detail, data = _carrier(ctx, iface, int(g.get("link_speed_mbps", 1000)))
    parts, fails = [detail], [] if ok else [detail]
    want = g.get("ptp4l_state", "SLAVE")
    out, how = _pmc(ctx, "GET PORT_DATA_SET")
    state = parse_pmc_field(out, "portState") if out else None
    offset_ns = None
    if out:
        cur, _ = _pmc(ctx, "GET CURRENT_DATA_SET")
        if cur and parse_pmc_field(cur, "offsetFromMaster"):
            offset_ns = float(parse_pmc_field(cur, "offsetFromMaster"))
    rc, journal, err = ctx.run(["journalctl", "--since", "-30s", "--no-pager", "-o", "short-unix"], 10)
    sv = count_servo_lines(journal if rc == 0 else "")
    if state is None:
        # No pmc: take the port state from the journal, SAY SO, and rely on the fresh
        # servo lines + carrier, which are what actually catch an unplugged cable.
        _, jb, _ = ctx.run(["journalctl", "-b", "--no-pager", "-o", "short-unix", "-t", "ptp4l"], 10)
        state = last_port_state(jb, iface)
        parts.append(f"pmc unavailable ({how}); journal port state {state}")
    else:
        parts.append(f"pmc portState {state}")
    if state != want:
        fails.append(f"ptp4l port state {state}, want {want}")
    need = int(g.get("min_servo_lines_30s", 20))
    parts.append(f"{sv['ptp4l_servo_lines']} ptp4l servo lines in 30 s")
    if rc != 0:
        fails.append(f"journalctl failed: {err.strip()[:120]}")
    elif sv["ptp4l_servo_lines"] < need:
        fails.append(f"only {sv['ptp4l_servo_lines']} fresh ptp4l servo lines in 30 s, want >= {need}")
    if sv["ptp4l_last_servo"] is not None and sv["ptp4l_last_servo"] != 2:
        fails.append(f"ptp4l servo s{sv['ptp4l_last_servo']} (not locked)")
    if sv["phc2sys_lines"] and sv["phc2sys_last_servo"] != 2:
        fails.append(f"phc2sys servo s{sv['phc2sys_last_servo']} (still acquiring; may step the clock)")
    if offset_ns is None and sv["last_offset_ns"] is not None:
        offset_ns = float(sv["last_offset_ns"])
    max_off = float(g.get("max_offset_ns", 10000))
    if offset_ns is None:
        fails.append("offset unknown (no pmc, no servo line)")
    else:
        parts.append(f"offset {offset_ns:.0f} ns")
        if abs(offset_ns) >= max_off:
            fails.append(f"|offset| {abs(offset_ns):.0f} ns >= {max_off:.0f}")
    data.update(state=state, servo_lines_30s=sv["ptp4l_servo_lines"], offset_ns=offset_ns,
                phc2sys_servo=sv["phc2sys_last_servo"])
    return result("ptp", not fails, "; ".join(fails) if fails else "; ".join(parts), **data)


def gate_ptp_a(ctx: Ctx) -> dict:
    g = ctx.g("ptp_a")
    iface = ctx.cfg["ptp_iface"]
    ok, detail, data = _carrier(ctx, iface, int(ctx.g("ptp_b").get("link_speed_mbps", 1000)))
    fails = [] if ok else [detail]
    want = g.get("ptp4l_state", "MASTER")
    out, how = _pmc(ctx, "GET PORT_DATA_SET")
    state = parse_pmc_field(out, "portState") if out else None
    src = "pmc"
    if state is None:
        rc, jb, err = ctx.run(["journalctl", "-b", "--no-pager", "-o", "short-unix", "-t", "ptp4l"], 10)
        state = last_port_state(jb, iface)
        src = f"journal (pmc unavailable: {how})"
        if rc != 0:
            fails.append(f"journalctl failed: {err.strip()[:120]}")
    rc, act, _ = ctx.run(["pgrep", "-x", "ptp4l"], 5)
    if rc != 0:
        fails.append("ptp4l is not running")
    if state != want:
        fails.append(f"ptp4l port state {state} ({src}), want {want}")
    if state == "FAULTY":
        fails.append("port FAULTY")
    data.update(state=state, state_source=src)
    return result("ptp", not fails, "; ".join(fails) if fails else f"{detail}; ptp4l {state} via {src}", **data)


# ---------------------------------------------------------------- clock

def gate_clock_offset(ctx: Ctx) -> dict:
    from . import clock  # noqa: PLC0415
    g = ctx.g("clock_offset")
    measure = ctx.clock or clock.measure
    m = measure()
    min_servers, max_spread = int(g.get("min_servers", 2)), float(g.get("max_spread_ms", 25))
    if (m["servers"] < min_servers or (m["spread_ms"] or 0) > max_spread) and ctx.clock is None:
        m = measure()   # one retry: a single noisy exchange should not skip a cell
    fails = []
    if m["host_minus_utc_s"] is None or m["servers"] < min_servers:
        fails.append(f"{m['servers']} of {len(m.get('asked', []))} NTP servers answered, want >= {min_servers}")
    elif (m["spread_ms"] or 0) > max_spread:
        fails.append(f"servers disagree by {m['spread_ms']} ms > {max_spread}")
    detail = (f"host-UTC {m['host_minus_utc_s']} s, {m['servers']} servers, spread {m['spread_ms']} ms"
              if m["host_minus_utc_s"] is not None else "no offset")
    prev = (ctx.prev_clock or {}).get("host_minus_utc_s")
    if prev is not None and m["host_minus_utc_s"] is not None:
        change = m["host_minus_utc_s"] - prev
        if abs(change) > float(g.get("warn_change_s", 1.0)):
            detail += f"; WARN changed {change:+.3f} s since the last cell"
    return result("clock_offset", not fails, "; ".join(fails) if fails else detail, clock=m)


# ---------------------------------------------------------------- DIAG + leftovers

def gate_diag_port(ctx: Ctx) -> dict:
    tty = ctx.cfg["diag_tty"]
    fails = []
    lock = _diag.lock_path(ctx.cfg)
    ok, why = _diag.lock_free(ctx.cfg)
    if not ok:
        fails.append(why)
    rc, out, err = ctx.run(["fuser", tty], 10)
    if rc == 0:
        fails.append(f"{tty} held by pid(s) {(out + err).split(':')[-1].strip()}")
    elif rc == 127:
        fails.append("fuser not installed; cannot prove the port is free")
    qs = [p for p in ctx.procs() if capture.is_qcsuper(p["argv"])]
    if qs:
        fails.append("qcsuper already running: " + ", ".join(str(p["pid"]) for p in qs))
    if not os.path.exists(tty):
        fails.append(f"{tty} does not exist")
    elif not (os.access(tty, os.R_OK) and os.access(tty, os.W_OK)):
        fails.append(f"{tty} not readable+writable (dialout?)")
    return result("diag_port", not fails, "; ".join(fails) if fails else f"{tty} free, lock {lock} free")


LEFTOVER_COMMS = {"tcpdump", "teleop-harness", "subscriber"}


def find_leftovers(procs: list[dict], names: list[str]) -> list[dict]:
    """Capture/publisher processes from a previous label. comm for binaries; argv for scripts
    (hop-recorder is bash, qcsuper and our own recorders are python) -- and never ourselves
    (list_procs drops our lineage: pgrep -f matching its own shell is the classic trap)."""
    out = []
    for p in procs:
        comm, argv = p["comm"], p["argv"]
        joined = " ".join(argv)
        hit = None
        if comm in LEFTOVER_COMMS and comm in names:
            hit = comm
        elif "qcsuper-noroot" in names and capture.is_qcsuper(argv):
            hit = "qcsuper"
        elif "hop-recorder" in names and comm in ("bash", "sh") and "hop-recorder" in joined:
            hit = "hop-recorder"
        elif (len(argv) >= 3 and os.path.basename(argv[0]).startswith("python")
              and argv[1] == "-m" and argv[2] in ("teleop.grid.capture.hops", "teleop.grid.capture.diag")):
            hit = argv[2].rsplit(".", 1)[-1] + "-recorder"
        elif len(argv) >= 4 and argv[1:4] == ["-m", "teleop.grid.agent", "_launch"]:
            hit = "scheduled-publisher"
        if hit:
            out.append({"pid": p["pid"], "what": hit, "argv": joined[:160]})
    return out


def gate_capture_leftovers(ctx: Ctx) -> dict:
    names = list(ctx.g("capture_leftovers").get("processes", ["tcpdump", "qcsuper-noroot", "hop-recorder",
                                                               "teleop-harness", "subscriber"]))
    left = find_leftovers(ctx.procs(), names)
    if left:
        return result("capture_leftovers", False,
                      "still running: " + "; ".join(f"{x['what']} pid {x['pid']}" for x in left), leftovers=left)
    return result("capture_leftovers", True, "no recorder, tcpdump, qcsuper, publisher or subscriber alive")


# ---------------------------------------------------------------- disk

def gate_disk(ctx: Ctx, statvfs=os.statvfs) -> dict:
    g = ctx.g("disk")
    root = ctx.cfg["results_root"]
    p = Path(root)
    while not p.exists() and p != p.parent:
        p = p.parent
    st = statvfs(str(p))
    free = st.f_bavail * st.f_frsize
    v = ctx.variables
    secs = int(v.get("duration_s", 300)) + int(v.get("lead_s", 60)) + 30
    need = int(g.get("min_free_multiple_of_cell", 3)) * int(g.get("cell_bytes_estimate_per_s", 7_000_000)) * secs
    gb = 1e9
    return result("disk", free > need, f"{free / gb:.1f} GB free under {p}, need > {need / gb:.1f} GB",
                  free_bytes=free, need_bytes=need)


# ---------------------------------------------------------------- whole-grid disk (Host A)
#
# Host A keeps every byte of every cell (its own captures and B's, pulled), so before the first
# cell the orchestrator projects the whole grid against A's free space (CONTRACT.md, control
# plane v2):
#   per cell  = (A DLF + B DLF) x span x 1.1  +  both hosts' pcaps
#   grid      = sum over the cells still to run, x 0.75 when compress_raw is on
#   refuse if grid > free - 20 GB
# The DLF rate dominates (4.5 MB/s per host whatever the video does); the pcaps are header-only.

DLF_BYTES_PER_S = 4.5e6          # per host: the modem DIAG log rate measured on this rig
DISK_MARGIN = 1.1                # on the DLF bytes: its rate varies, and logs/reduced/reports ride on it
DISK_HEADROOM_BYTES = 20e9       # the plan never fills the disk: the budget is free - 20 GB
COMPRESSED_FACTOR = 0.75         # zstd -3 on DLF + pcap (measured 1.37x on DLFs, 1.5x on pcaps)
PCAP_RECORD_BYTES = 16 + 128     # pcap record header + capture/pcap.py's 128-byte snaplen
PCAP_PAYLOAD_BYTES = 1100        # a typical RTP video payload per packet
PCAP_OTHER_PPS = 500             # control 200 Hz each way, probes, RTCP, other UDP: a generous floor


def _cell_values(cell) -> dict:
    """A Cell's values, or a Cell.to_dict()'s variables (a dict has a .values method too)."""
    return (cell.get("variables") or {}) if isinstance(cell, dict) else cell.values


def _cell_repeat(cell) -> int:
    return int((cell.get("repeat") if isinstance(cell, dict) else cell.repeat) or 1)


def cell_span_s(cell) -> int:
    v = _cell_values(cell)
    return int(v["lead_s"]) + int(v["duration_s"]) + 30


def pcap_bytes_estimate(cell) -> float:
    """One host's header-only pcap for one cell: every UDP packet over the capture span, 144
    bytes a record -- video packets from the cell's kbps, plus a floor for everything else."""
    pps = int(_cell_values(cell)["kbps"]) * 1000 / 8 / PCAP_PAYLOAD_BYTES + PCAP_OTHER_PPS
    return pps * PCAP_RECORD_BYTES * cell_span_s(cell)


def grid_disk_projection(cells, compress_raw: bool = True) -> dict:
    """Bytes the cells will leave on Host A (Cell objects or Cell.to_dict()s)."""
    cells = list(cells)
    dlf = sum(2 * DLF_BYTES_PER_S * cell_span_s(c) * DISK_MARGIN for c in cells)
    pcap = sum(2 * pcap_bytes_estimate(c) for c in cells)
    unc = dlf + pcap
    factor = COMPRESSED_FACTOR if compress_raw else 1.0
    per_cell = [(2 * DLF_BYTES_PER_S * cell_span_s(c) * DISK_MARGIN + 2 * pcap_bytes_estimate(c)) * factor
                for c in cells]
    reps = [_cell_repeat(c) for c in cells]
    last = max(reps) if reps else 0
    return {"cells": len(cells), "dlf_bytes": dlf, "pcap_bytes": pcap, "uncompressed_bytes": unc,
            "compress_raw": bool(compress_raw), "factor": factor, "projected_bytes": unc * factor,
            "last_repeat_round_bytes": sum(b for b, r in zip(per_cell, reps) if r == last and last > 1),
            "lead_10s_bytes": sum(2 * DLF_BYTES_PER_S * 10 * DISK_MARGIN * factor for _ in cells)}


def _free_bytes(root, statvfs=os.statvfs) -> tuple[int, Path]:
    p = Path(root).expanduser()
    while not p.exists() and p != p.parent:
        p = p.parent
    st = statvfs(str(p))
    return st.f_bavail * st.f_frsize, p


def gate_grid_disk(cells, results_root, compress_raw: bool = True, statvfs=os.statvfs) -> dict:
    """The whole-grid gate: refuse to start when the projection exceeds free - 20 GB, saying by
    how much and what to change."""
    proj = grid_disk_projection(cells, compress_raw)
    free, where = _free_bytes(results_root, statvfs)
    budget = free - DISK_HEADROOM_BYTES
    gb = 1e9
    ok = proj["projected_bytes"] <= budget
    detail = (f"{proj['cells']} cells project {proj['projected_bytes'] / gb:.1f} GB on Host A "
              f"({'zstd x' + str(COMPRESSED_FACTOR) + ' of ' if compress_raw else 'compress_raw off, '}"
              f"{proj['uncompressed_bytes'] / gb:.1f} GB uncompressed: DLF {proj['dlf_bytes'] / gb:.1f} + pcap "
              f"{proj['pcap_bytes'] / gb:.1f}); free {free / gb:.1f} GB under {where}, budget free - "
              f"{DISK_HEADROOM_BYTES / gb:.0f} GB = {budget / gb:.1f} GB")
    advice = ""
    if not ok:
        short = proj["projected_bytes"] - budget
        opts = []
        if proj["last_repeat_round_bytes"]:
            opts.append(f"fewer repeats (one repeat round is {proj['last_repeat_round_bytes'] / gb:.1f} GB)")
        opts.append(f"shorter lead_s (every 10 s of lead is {proj['lead_10s_bytes'] / gb:.1f} GB over the grid)")
        if not compress_raw:
            opts.append(f"compress_raw: true (x{COMPRESSED_FACTOR})")
        opts.append(f"free space under {where}")
        advice = f"; SHORT by {short / gb:.1f} GB: " + ", or ".join(opts)
    return result("grid_disk", ok, detail + advice, free_bytes=free, budget_bytes=budget, path=str(where),
                  headroom_bytes=DISK_HEADROOM_BYTES, **proj)


# ---------------------------------------------------------------- modem

RF_BAND_RE = re.compile(r"Active Band Class:\s*'([^']+)'")
RF_CHAN_RE = re.compile(r"Active Channel:\s*'(\d+)'")
PCI_RE = re.compile(r"(?:Physical Cell ID|PCI):\s*'?(\d+)")


def parse_rf_band_info(text: str) -> list[dict]:
    """qmicli --nas-get-rf-band-info -> [{band: 'n41', arfcn}] for NR entries.
    Prefer the "(Extended)" section: the legacy channel field is 16-bit, so NR-ARFCN 501390
    reads as 42638 there. Band class is 'nr5g-41' on current qmicli ('nr5g-band-41' older)."""
    sections = re.split(r"^Band Information", text, flags=re.M)[1:]
    ext = [s for s in sections if s.lstrip().startswith("(Extended)")]
    use = ext or [s for s in sections if not s.lstrip().startswith("(Extended)")]
    out = []
    for sec in use:
        body = sec.split("Bandwidth:")[0]
        bands, chans = RF_BAND_RE.findall(body), RF_CHAN_RE.findall(body)
        for i, b in enumerate(bands):
            m = re.search(r"nr5g-(?:band-)?(\d+)", b)
            if m:
                out.append({"band": f"n{m[1]}", "arfcn": int(chans[i]) if i < len(chans) else None})
    return out


def gate_modem(ctx: Ctx) -> dict:
    g = ctx.g("modem")
    fails, parts = [], []
    rc, lst, err = ctx.run(["mmcli", "-L"], 10)
    idx, note = _hops.resolve_modem(int(ctx.cfg["modem_index"]), lst if rc == 0 else "")
    if idx is None:
        return result("modem", False, f"{note} ({err.strip()[:80]})" if err.strip() else note)
    if "not listed" in note:
        parts.append(note)
    rc, js, err = ctx.run(["mmcli", "-m", str(idx), "-J"], 10)
    try:
        m = json.loads(js)["modem"]
        gen = m["generic"]
    except (ValueError, KeyError, TypeError):
        return result("modem", False, f"mmcli -m {idx} -J unreadable: {err.strip()[:120]}")
    state = gen.get("state")
    tech = gen.get("access-technologies") or []
    current = [b for b in (gen.get("current-bands") or []) if b.startswith("ngran-")]
    allowed_nr = [f"n{b.split('-')[1]}" for b in current]
    if g.get("require_attached", True) and state not in ("connected", "registered"):
        fails.append(f"modem state {state}, not attached")
    if g.get("require_nr5g", True) and "5gnr" not in tech:
        fails.append(f"access tech {tech}, not 5G NR")
    parts.append(f"{state}, {'+'.join(tech) or '?'}, NR bands allowed {allowed_nr or '?'}")
    exp = ctx.expect.get("band")
    live = []
    qdev = _hops.qmi_device(js)
    if qdev:
        for argv in (["sudo", "-n", "qmicli", "-d", qdev, "-p", "--nas-get-rf-band-info"],
                     ["qmicli", "-d", qdev, "-p", "--nas-get-rf-band-info"]):
            rc2, out2, _ = ctx.run(argv, 8)
            if rc2 == 0 and out2:
                live = parse_rf_band_info(out2)
                break
    pci = None
    if live and qdev:
        rc3, out3, _ = ctx.run(["sudo", "-n", "qmicli", "-d", qdev, "-p", "--nas-get-cell-location-info"], 8)
        if rc3 == 0:
            nr = out3[out3.find("5GNR"):] if "5GNR" in out3 else ""
            mm = PCI_RE.search(nr)
            pci = int(mm[1]) if mm else None
    if live:
        parts.append("live " + ", ".join(f"{x['band']}/{x['arfcn']}" for x in live) + (f" pci {pci}" if pci else ""))
    else:
        parts.append("live band UNVERIFIED (qmicli not permitted here; reduce reads it from the DLF)")
    if exp and g.get("band_must_match_expect", True):
        # A persistent band lock that excludes the expected band is visible without privilege:
        # B was locked to n25 under the tests by another session.
        if allowed_nr and exp not in allowed_nr:
            fails.append(f"modem band config allows {allowed_nr}, grid expects {exp} (band-locked?)")
        if live and exp not in [x["band"] for x in live]:
            fails.append(f"serving NR band(s) {[x['band'] for x in live]}, grid expects {exp}")
    if ctx.expect.get("arfcn") and live and int(ctx.expect["arfcn"]) not in [x["arfcn"] for x in live]:
        fails.append(f"ARFCN {[x['arfcn'] for x in live]}, grid expects {ctx.expect['arfcn']}")
    if ctx.expect.get("pci") and pci is not None and int(ctx.expect["pci"]) != pci:
        fails.append(f"PCI {pci}, grid expects {ctx.expect['pci']}")
    band = live[0] if live else {}
    return result("modem", not fails, "; ".join(fails) if fails else "; ".join(parts),
                  modem=idx, state=state, access_tech=tech, allowed_nr=allowed_nr,
                  band=band.get("band"), arfcn=band.get("arfcn"), pci=pci, live=live)


# ---------------------------------------------------------------- A only

def gate_sfu(ctx: Ctx) -> dict:
    """Set, resolves, routed over the wwan interface, and answers HTTPS. The hostname itself is
    never put in a detail (details are logged)."""
    from . import hostcfg  # noqa: PLC0415
    g = ctx.g("sfu")
    try:
        host = ctx.sfu_host or hostcfg.sfu_host()
    except hostcfg.HostConfigError as e:
        return result("sfu", False, str(e))
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)})
    except OSError as e:
        return result("sfu", False, f"TELEOP_SFU_HOST does not resolve: {e}")
    iface = ctx.cfg["wwan_iface"]
    rc, out, _ = ctx.run(["ip", "-4", "route", "get", addrs[0]], 5)
    m = re.search(r"\bdev (\S+)", out)
    dev = m[1] if m else None
    sm = re.search(r"\bsrc (\S+)", out)
    src = sm[1] if sm else None
    if dev != iface:
        return result("sfu", False, f"SFU route leaves via {dev}, not {iface}", route_dev=dev)
    t = float(g.get("https_probe_timeout_s", 8))
    try:
        c = http.client.HTTPSConnection(host, 443, timeout=t, context=ssl._create_unverified_context(),  # noqa: S323
                                        source_address=(src, 0) if src else None)
        c.request("GET", "/")
        status = c.getresponse().status
        c.close()
    except (OSError, http.client.HTTPException) as e:
        return result("sfu", False, f"HTTPS to the SFU over {iface} failed: {e}", route_dev=dev)
    return result("sfu", True, f"resolves ({len(addrs)} addr), routed via {iface}, HTTPS status {status}",
                  route_dev=dev, https_status=status)


def gate_clip(ctx: Ctx) -> dict:
    clip = ctx.variables.get("clip")
    if not clip:
        return result("clip", False, "cell has no clip")
    p = Path(clip)
    if not p.is_file():
        return result("clip", False, f"clip missing: {clip}")
    if not os.access(p, os.R_OK):
        return result("clip", False, f"clip not readable: {clip}")
    size = p.stat().st_size
    digest = capture.sha256_file(p) if size < 512 * 1024 * 1024 else None
    return result("clip", True, f"{p.name} {size / 1e6:.1f} MB", path=str(p), bytes=size, sha256=digest)


def gate_room(ctx: Ctx) -> dict:
    return result("room", True, "SKIPPED: teleop-harness has no list-participants command; the harness warns "
                  "at join ('already has N participant(s)') and the agent records it in run.json", skipped=True)


def gate_encoder(ctx: Ctx) -> dict:
    rc, out, err = ctx.run(["lsmod"], 5)
    if rc != 0:
        return result("encoder", False, f"lsmod failed: {err.strip()[:80]}")
    mods = {line.split()[0] for line in out.splitlines()[1:] if line.strip()}
    if "nvidia" not in mods:
        return result("encoder", False, "nvidia kernel module not loaded; NVENC unavailable (no software fallback)")
    rc, out, _ = ctx.run(["ldconfig", "-p"], 5)
    if rc == 0 and "libnvidia-encode.so" not in out:
        return result("encoder", False, "libnvidia-encode not in the linker cache; NVENC unavailable")
    return result("encoder", True, "nvidia module loaded, libnvidia-encode present",
                  modules=sorted(m for m in mods if m.startswith("nvidia")))


def gate_link_mtu(ctx: Ctx) -> dict:
    from . import hostcfg  # noqa: PLC0415
    g = ctx.g("link_mtu")
    want = int(g.get("min_path_mtu", 1280))
    iface = ctx.cfg["wwan_iface"]
    try:
        mtu = int((ctx.read(f"/sys/class/net/{iface}/mtu") or "0").strip())
    except ValueError:
        mtu = 0
    if mtu < want:
        return result("link_mtu", False, f"{iface} MTU {mtu} < {want}", iface_mtu=mtu)
    try:
        host = ctx.sfu_host or hostcfg.sfu_host()
        addr = socket.getaddrinfo(host, 443, socket.AF_INET)[0][4][0]
    except (OSError, hostcfg.HostConfigError) as e:
        return result("link_mtu", False, f"cannot resolve the SFU for a path probe: {e}", iface_mtu=mtu)
    rc, out, err = ctx.run(["ping", "-M", "do", "-s", str(want - 28), "-c", "1", "-W", "2", "-I", iface, addr], 6)
    text = out + err
    if re.search(r"message too long|frag needed|mtu\s*=\s*\d+", text, re.I):
        m = re.search(r"mtu\s*=\s*(\d+)", text)
        return result("link_mtu", False, f"path MTU below {want}" + (f" ({m[1]})" if m else ""), iface_mtu=mtu)
    if rc == 0:
        return result("link_mtu", True, f"{iface} MTU {mtu}; {want}-byte DF probe answered", iface_mtu=mtu,
                      path_mtu_at_least=want)
    return result("link_mtu", True, f"{iface} MTU {mtu}; path unverified (no ICMP reply to DF probe)",
                  iface_mtu=mtu, path_mtu_at_least=None)


def gate_binary(ctx: Ctx) -> dict:
    name = "teleop-harness" if ctx.role == "a" else "subscriber"
    p = Path(ctx.cfg["repo"]) / "target" / "release" / name
    if not (p.is_file() and os.access(p, os.X_OK)):
        return result("binary", False, f"{p} missing or not executable (cargo build --release)")
    return result("binary", True, f"{name} present")


def gate_credentials(ctx: Ctx) -> dict:
    """Existence and readability ONLY. The file is never opened here."""
    p = Path(ctx.cfg["credentials_env"])
    if not p.is_file() or not os.access(p, os.R_OK):
        return result("credentials", False, "credentials_env missing or unreadable")
    return result("credentials", True, "credentials_env present (not read)")


def gate_cpu_governor(ctx: Ctx) -> dict:
    """A throttled encoder and a queueing link look alike in the paired split. Warn (or fail
    with cpu_governor.strict) unless every core is performance/performance."""
    strict = bool(ctx.g("cpu_governor").get("strict", False))
    govs = {(ctx.read(f) or "").strip() for f in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")}
    epps = {(ctx.read(f) or "").strip()
            for f in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference")}
    ok = govs <= {"performance"} and epps <= {"performance"}
    detail = f"governor {sorted(govs) or '?'} epp {sorted(epps) or '?'}"
    if ok:
        return result("cpu_governor", True, detail)
    return result("cpu_governor", not strict,
                  ("" if strict else "WARN ") + detail + " -- timings inflated, not comparable to performance runs")


# ---------------------------------------------------------------- B only

def gate_decoder_b(ctx: Ctx) -> dict:
    codec = ctx.variables.get("codec", "h264")
    rc, out, _ = ctx.run(["lsmod"], 5)
    nvidia = rc == 0 and any(line.split()[0] == "nvidia" for line in out.splitlines()[1:] if line.strip())
    rc, cache, _ = ctx.run(["ldconfig", "-p"], 5)
    nvcuvid = "libnvcuvid.so" in cache
    dav1d = "libdav1d.so" in cache
    data = {"codec": codec, "nvidia": nvidia, "nvcuvid": nvcuvid, "dav1d": dav1d}
    if codec == "av1" and not ((nvidia and nvcuvid) or dav1d):
        return result("decoder_b", False, "no AV1 decoder: need NVDEC (nvidia + libnvcuvid) or libdav1d", **data)
    if codec == "h265":
        # libwebrtc itself ships no H.265 decoder. B decodes it through the system FFmpeg
        # (webrtc-sys/src/ffmpeg, "VAAPI H265 Decoder"): on VA-API when a render node and
        # libva are present, otherwise FFmpeg's software HEVC decoder. Either needs the
        # decoder compiled into the subscriber and libavcodec.
        binary = Path(ctx.cfg["repo"]) / "target" / "release" / "subscriber"
        try:
            built = b"VAAPI H265 Decoder" in binary.read_bytes()
        except OSError:
            built = False
        avcodec = "libavcodec.so" in cache
        vaapi = bool(glob.glob("/dev/dri/renderD*")) and "libva.so" in cache
        data |= {"ffmpeg_h265_built": built, "libavcodec": avcodec, "vaapi": vaapi}
        if nvidia and nvcuvid:
            return result("decoder_b", True, "h265 decodable (NVDEC)", **data)
        if not (built and avcodec):
            missing = [n for n, ok in (("FFmpeg H.265 decoder in subscriber", built), ("libavcodec", avcodec)) if not ok]
            return result("decoder_b", False, f"no H.265 decoder: missing {', '.join(missing)}", **data)
        how = "FFmpeg on VA-API" if vaapi else "FFmpeg software (no VA-API render node/libva)"
        return result("decoder_b", True, f"h265 decodable ({how})", **data)
    how = "NVDEC" if nvidia and nvcuvid else "software"
    return result("decoder_b", True, f"{codec} decodable ({how})", **data)


def gate_display(ctx: Ctx) -> dict:
    """The subscriber logs on GPU render completion, so no display = an empty CSV. It runs with
    WAYLAND_DISPLAY unset (Xwayland is not throttled by a locked session); the lock state is
    recorded because render timings under Xwayland are not comparable to native Wayland."""
    disp = ctx.cfg.get("display", "")
    m = re.match(r"^:(\d+)", disp)
    if not m:
        return result("display", False, f"display {disp!r} is not an X display like :0")
    sock = f"/tmp/.X11-unix/X{m[1]}"
    if not os.path.exists(sock):
        return result("display", False, f"no X server socket {sock}")
    rc, out, _ = ctx.run(["loginctl", "list-sessions", "--no-legend"], 5)
    locked = None
    for line in out.splitlines():
        sid = line.split()[0] if line.split() else ""
        if not sid:
            continue
        r2, v, _ = ctx.run(["loginctl", "show-session", sid, "-p", "LockedHint", "--value"], 5)
        if v.strip() in ("yes", "no"):
            locked = v.strip() == "yes"
            break
    return result("display", True, f"{disp} present; session locked={locked}", locked=locked)


# ---------------------------------------------------------------- run

GATES_A = [gate_binary, gate_credentials, gate_ptp_a, gate_clock_offset, gate_diag_port, gate_capture_leftovers,
           gate_disk, gate_modem, gate_sfu, gate_clip, gate_room, gate_encoder, gate_link_mtu, gate_cpu_governor]
GATES_B = [gate_binary, gate_credentials, gate_ptp_b, gate_clock_offset, gate_diag_port, gate_capture_leftovers,
           gate_disk, gate_modem, gate_decoder_b, gate_display, gate_cpu_governor]


def _timed(fn, ctx: Ctx, timeout: float) -> dict:
    box: dict = {}

    def target():
        try:
            box["r"] = fn(ctx)
        except Exception as e:  # noqa: BLE001 -- a crashing gate is a failing gate, with the reason
            box["r"] = result(fn.__name__.removeprefix("gate_"), False, f"gate raised {type(e).__name__}: {e}")
    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, box


def run_gates(ctx: Ctx, gates: list, timeout: float = GATE_TIMEOUT_S) -> list[dict]:
    started = [(fn, *_timed(fn, ctx, timeout)) for fn in gates]
    deadline = time.monotonic() + timeout
    out = []
    for fn, t, box in started:
        t.join(max(0.0, deadline - time.monotonic()))
        if "r" in box:
            out.append(box["r"])
        else:
            out.append(result(fn.__name__.removeprefix("gate_"), False, f"timed out after {timeout:.0f} s"))
    return out


def run(cfg: dict, cell: dict | None, role: str | None = None, *, expect: dict | None = None,
        prev_clock: dict | None = None, gates: dict | None = None, timeout: float = GATE_TIMEOUT_S) -> list[dict]:
    """All of this host's gates for `cell` (Cell.to_dict(), or None for a cell-less check)."""
    role = role or cfg["role"]
    ctx = Ctx(cfg=cfg, role=role, cell=cell or {}, expect=expect or {}, gates=gates if gates is not None
              else load_gates(), prev_clock=prev_clock)
    return run_gates(ctx, GATES_A if role == "a" else GATES_B, timeout)


def code_match(a: dict, b: dict, gates: dict | None = None) -> dict:
    """The cross-host gate, computed by the orchestrator from both `agent identity` replies."""
    g = (gates if gates is not None else load_gates()).get("code_match", {})
    checks = [("commit", g.get("require_same_commit", True)),
              ("harness_sha256", g.get("require_same_harness_sha256", True)),
              ("requirements_sha256", g.get("require_same_requirements_sha256", True)),
              ("package_sha256", True)]
    # The harness binary exists only on the publisher; B runs the subscriber. Compare the
    # harness hash only when both hosts report one, so the gate checks code identity
    # (commit, package, requirements) rather than demanding a binary B never builds.
    diffs = [f"{k}: A {str(a.get(k))[:12]} != B {str(b.get(k))[:12]}" for k, req in checks
             if req and not (k == "harness_sha256" and (a.get(k) is None or b.get(k) is None))
             and (a.get(k) != b.get(k) or not a.get(k))]
    return result("code_match", not diffs, "; ".join(diffs) if diffs else
                  f"commit {str(a.get('commit'))[:8]}, package and requirements identical"
                  + ("" if g.get("require_same_harness_sha256", True) and a.get("harness_sha256")
                     and b.get("harness_sha256") else " (binaries are built per host; not compared)"))
