#!/usr/bin/env python3
"""Map an OLD results cell into the contract layout, so reduce/metrics can run on it.

    python3 -m teleop.grid.tools.import_legacy <old_cell_dir> <target_root> [--label L]

Old layout (results/<label>/ on Host A):
    <label>.pub.csv .jsonl .log .wwan0.pcap .dlf .qcsuper.log .hops.csv  EPOCH  dlf-rates.csv  recorder.out
    hostb/ subscriber.csv subscriber.log timeline.txt dlf-rates-hostb.csv <label>.hops-b.csv
           <label>-<stamp>Z.pcap  <label>-<stamp>Z.dlf  <label>-<stamp>Z.log   (possibly several arms)
New layout: <target_root>/cells/<label>/{manifest.json, hosta/, hostb/} (CONTRACT.md "Cell directory").

Files are hard-linked (copied if the target is on another filesystem); the old cell is never
modified. The manifest is filled from what the old files say ran:
  epoch        EPOCH
  clock.a      host_minus_utc_s from the header of dlf-rates.csv (else a clock-offset file); when A
               has neither, B's value is used and marked (the hosts are PTP-locked)
  clock.b      the LAST "clock offset measured: <x>s" line in hostb/timeline.txt
  B captures   the arm that covers the epoch: the latest <stamp> not after the epoch, preferring the
               files timeline.txt names as RUNNING (an aborted earlier arm leaves stale captures)
  variables    from <label>.log (geometry/cap/codec/duration/clip lines) and the jsonl run_metadata
  status       OK
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

STAMP = re.compile(r"-(\d{8}T\d{6})Z\.(pcap|dlf|log)$")


def _link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _stamp_unix(s: str) -> float:
    return calendar.timegm(time.strptime(s, "%Y%m%dT%H%M%S"))


def _offset_from_rates(p: Path) -> float | None:
    if not p.exists():
        return None
    with open(p) as f:
        for line in f:
            if not line.startswith("#"):
                return None
            m = re.search(r"host_minus_utc_s=([+-]?[0-9.]+)", line)
            if m:
                return float(m.group(1))
    return None


def _pick_b_capture(hb: Path, ext: str, epoch: float, timeline: str) -> Path | None:
    cands = []
    for p in hb.glob(f"*.{ext}"):
        m = STAMP.search(p.name)
        if m:
            cands.append((_stamp_unix(m.group(1)), m.group(1), p))
    if not cands:
        return None
    named = {m.group(1) for m in re.finditer(r"RUNNING -> \S*?-(\d{8}T\d{6})Z\." + ext, timeline)}
    ok = [c for c in cands if c[0] <= epoch]
    pref = [c for c in ok if c[1] in named]
    pool = pref or ok or cands
    return max(pool)[2]


def _parse_log(text: str) -> dict:
    v = {}
    m = re.search(r"geometry: (\d+)x(\d+)@(\d+)", text)
    if m:
        v["width"], v["height"], v["fps"] = int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r"bpp ([0-9.]+)", text)
    if m:
        v["bpp"] = float(m.group(1))
    m = re.search(r"cap=(\d+)k", text)
    if m:
        v["kbps"] = int(m.group(1))
    m = re.search(r"codec=(\w+)", text)
    if m:
        v["codec"] = m.group(1)
    m = re.search(r"duration=(\d+)s", text)
    if m:
        v["duration_s"] = int(m.group(1))
    m = re.search(r"clip=(\S+)", text)
    if m:
        v["clip"] = m.group(1)
    v["pin_bitrate"] = "LK_PIN_BITRATE_TO_MAX=1" in text if "LK_PIN_BITRATE_TO_MAX" in text else None
    return v


def import_cell(old: Path, root: Path, label: str | None = None) -> Path:
    old = Path(old)
    label = label or old.name
    cell = Path(root) / "cells" / label
    ha, hb = cell / "hosta", cell / "hostb"
    ha.mkdir(parents=True, exist_ok=True)
    epoch = int(float((old / "EPOCH").read_text().strip()))

    for suffix in (".pub.csv", ".jsonl", ".log", ".wwan0.pcap", ".dlf", ".qcsuper.log", ".hops.csv"):
        p = old / f"{label}{suffix}"
        if p.exists() and p.stat().st_size > 0:
            _link(p, ha / f"{label}{suffix}")

    log = (old / f"{label}.log").read_text(errors="replace") if (old / f"{label}.log").exists() else ""
    lv = _parse_log(log)
    meta = None
    if (old / f"{label}.jsonl").exists():
        for line in open(old / f"{label}.jsonl"):
            if '"run_metadata"' in line:
                meta = json.loads(line)
    dev = (meta or {}).get("camera_device") or {}
    run = {"encoder_implementation": (meta or {}).get("encoder_implementation"),
           "width": dev.get("negotiated_width") or lv.get("width"), "height": dev.get("negotiated_height") or lv.get("height"),
           "fps": dev.get("negotiated_fps") or lv.get("fps"),
           "max_bitrate_bps": lv["kbps"] * 1000 if "kbps" in lv else None,
           "codec": (meta or {}).get("negotiated_codec") or lv.get("codec"),
           "started_at": ((meta or {}).get("run_origin_unix_us") or epoch * 1e6) / 1e6,
           "source": "legacy import (from harness log + jsonl run_metadata)"}
    if not (ha / "run.json").exists():
        (ha / "run.json").write_text(json.dumps(run, indent=1))

    notes = []
    off_a = _offset_from_rates(old / "dlf-rates.csv")
    if off_a is None:
        for name in ("clock-offset", "clock-offset.txt", "clock.json"):
            p = old / name
            if p.exists():
                m = re.search(r"([+-]?\d+\.\d+)", p.read_text())
                if m:
                    off_a = float(m.group(1))
                    break
    timeline = ""
    off_b = None
    if (old / "hostb").is_dir():
        ob = old / "hostb"
        hb.mkdir(exist_ok=True)
        timeline = (ob / "timeline.txt").read_text(errors="replace") if (ob / "timeline.txt").exists() else ""
        offs = re.findall(r"clock offset measured: ([+-]?[0-9.]+)s", timeline)
        off_b = float(offs[-1]) if offs else _offset_from_rates(ob / "dlf-rates-hostb.csv")
        for name in ("subscriber.csv", "subscriber.log", "timeline.txt"):
            if (ob / name).exists():
                _link(ob / name, hb / name)
        for ext, dst in (("pcap", f"{label}.wwan0.pcap"), ("dlf", f"{label}.dlf")):
            p = _pick_b_capture(ob, ext, epoch, timeline)
            if p is not None:
                _link(p, hb / dst)
                logp = p.with_suffix(".log")
                if ext == "dlf" and logp.exists():
                    _link(logp, hb / f"{label}.qcsuper.log")
        for p in ob.glob("*.hops-b.csv"):
            _link(p, hb / f"{label}.hops.csv")
            break
    if off_a is None and off_b is not None:
        off_a = off_b
        notes.append("A clock offset not recorded in the old cell; B's value used (A and B are PTP-locked)")
    if (old / "NOTE-no-A-modem.txt").exists():
        notes.append("old cell NOTE: " + (old / "NOTE-no-A-modem.txt").read_text().strip().replace("\n", " "))
    if (old / "RENAMED.txt").exists():
        notes.append("old cell RENAMED: " + (old / "RENAMED.txt").read_text().strip().replace("\n", " "))

    def clock(v, src):
        return None if v is None else {"host_minus_utc_s": v, "measured_at": None, "servers": None,
                                       "spread_ms": None, "source": src}

    ptp_b = {"state": None}
    m = re.findall(r"ptp4l locked to A \((\d+) servo lines", timeline)
    if m:
        ptp_b = {"state": "SLAVE", "servo_lines_30s": int(m[-1]), "offset_ns": None}
    w, h = run["width"], run["height"]
    variables = {"codec": lv.get("codec"), "kbps": lv.get("kbps"), "fps": lv.get("fps", 30),
                 "geometry": f"{w}x{h}" if w and h else None, "bpp": lv.get("bpp"), "vbv_frames": None,
                 "padding": None, "target_quality": None, "intra_refresh": None,
                 "pin_bitrate": lv.get("pin_bitrate"), "duration_s": lv.get("duration_s"), "clip": lv.get("clip"),
                 "lead_s": None}
    manifest = {
        "label": label, "grid_id": "legacy", "index": 0, "kind": "cell", "repeat": 1,
        "variables": variables,
        "requested": {"width": lv.get("width"), "height": lv.get("height")},
        "negotiated": {"width": w, "height": h, "encoder_implementation": run["encoder_implementation"],
                       "codec": run["codec"]},
        "epoch": epoch, "epoch_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
        "commit": None, "harness_sha256": None,
        "clock": {"a": clock(off_a, "dlf-rates.csv header" if _offset_from_rates(old / "dlf-rates.csv") is not None
                             else "hostb/timeline.txt (PTP peer; A not recorded)"), "b": clock(off_b, "hostb/timeline.txt")},
        "ptp": {"a": {"state": "MASTER" if ptp_b["state"] else None}, "b": ptp_b},
        "band": {"a": None, "b": None},
        "gates": {"a": [], "b": []},
        "status": "OK", "status_reason": "",
        "timeline": {},
        "excluded_from_comparison": False, "exclusion_reason": "",
        "integrity": {"mirror_verified": False, "legacy_import": True},
        "legacy": {"source": str(old.resolve()), "imported_at": time.time(), "notes": notes},
    }
    mp = cell / "manifest.json"
    if mp.exists():   # keep what reduce already wrote (band, integrity) on a re-import
        old_m = json.loads(mp.read_text())
        for k in ("band", "integrity", "timeline"):
            if old_m.get(k):
                manifest[k] = old_m[k] | ({"legacy_import": True} if k == "integrity" else {})
    mp.write_text(json.dumps(manifest, indent=2))
    return cell


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("old_cell")
    ap.add_argument("target_root")
    ap.add_argument("--label")
    a = ap.parse_args(argv)
    cell = import_cell(Path(a.old_cell), Path(a.target_root), a.label)
    print(cell)
    return 0


if __name__ == "__main__":
    sys.exit(main())
