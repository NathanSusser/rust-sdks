"""Synthetic layout-v2 grids for the report tests (Host A: numpy). Nothing is committed: every
file is generated into a directory the caller owns (a temporary one in the tests).

    make_grid(root, ...) -> grid_dir       codec x fps x bpp x repeats, default 2 x 2 x 5 x 3
    make_repeat(grid_dir, rel, variables, ...) -> the repeat's directory

Each repeat gets what the pipeline would have left behind, below the raw captures: manifest.json;
the raw control-path logs hosta/control-pub.jsonl and hostb/control.csv, reduced by the real
reduce/control.py; reduced/frames.csv (with the per-frame qp column), seconds.csv, spikes.csv,
episodes.csv, probes.csv, dlf-rates-{a,b}.csv, band-{a,b}.csv and reduce.json; and metrics.json
from the real metrics.build(). Latencies follow the rig's shape: ~30 ms in flight, emission
growing with frame size, one or two ~400 ms path-side stalls per cell, ~50 ms receive-host
holds on B, QP falling with bpp (H.265 0-51, AV1 q-index 0-255).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from teleop.grid import metrics
from teleop.grid.reduce import control as CTL
from teleop.grid.reduce import frames as F

GRID_ID = "gsynth"
EPOCH = 1_790_384_410
W, H = 1600, 1300
CONTROL_HZ = 200


def combo_name(codec: str, fps: int, bpp: float) -> str:
    """The contract's <combo> for a codec x fps x bpp grid (the orchestrator's job in real runs)."""
    return f"{codec}-{fps}fps-b{int(round(bpp * 1000)):04d}"


def kbps_of(fps: int, bpp: float) -> int:
    return int(round(W * H * fps * bpp / 1000))


def _qp(rng, codec: str, bpp: float, key: np.ndarray) -> np.ndarray:
    f = math.log(bpp / 0.04) / math.log(3.5)
    if codec == "av1":
        q = 176 - 62 * f + rng.normal(0, 8, len(key)) - key * 18
        return np.clip(np.round(q), 20, 255)
    q = 37 - 10 * f + rng.normal(0, 1.7, len(key)) - key * 3
    return np.clip(np.round(q), 10, 51)


def _write_control_logs(cell: Path, rng, dur: float, stalls: list[tuple[float, float]], *, pub=True, recv=True):
    """hosta/control-pub.jsonl and hostb/control.csv as the harness and subscriber write them."""
    n = int((dur - 0.3) * CONTROL_HZ)
    ts = EPOCH + 0.2 + np.arange(n) / CONTROL_HZ + rng.normal(0, 0.0002, n)
    owd = 27 + rng.gamma(2.0, 1.4, n)
    lost = rng.random(n) < 0.0004
    for b0, amp in stalls:
        m = (ts - EPOCH >= b0) & (ts - EPOCH < b0 + 0.35)
        owd[m] += np.linspace(amp * 0.6, 5, m.sum())
        lost |= m & (rng.random(n) < 0.3)
    probe = (np.arange(n) % 100) == 0
    if pub:
        with open(cell / "hosta" / CTL.PUB_LOG, "w") as f:
            for i in range(n):
                f.write(json.dumps({"seq": i, "t_send_unix_us": int(ts[i] * 1e6), "t_send_monotonic_us": i * 5000,
                                    "probe": bool(probe[i])}) + "\n")
    if recv:
        tr = ts + owd / 1000
        order = np.argsort(tr)
        with open(cell / "hostb" / CTL.RECV_LOG, "w") as f:
            f.write("seq,t_send_unix_us,t_recv_unix_us,owd_us,probe_token,transport\n")
            for i in order:
                if lost[i]:
                    continue
                s_us, r_us = int(ts[i] * 1e6), int(tr[i] * 1e6)
                f.write(f"{i},{s_us},{r_us},{r_us - s_us},{1000 + i if probe[i] else 0},data_track_buf1\n")
    return owd


def make_repeat(grid_dir: Path, rel: str, variables: dict, *, index: int, repeat: int, seed: int,
                status: str = "OK", encoder: str | None = None, ptp_locked: bool = True,
                negotiated_codec: str | None = None, control: bool = True, qp_log: bool = True,
                kind: str = "cell", metrics_json: bool = True) -> Path:
    """One repeat directory at <grid>/<rel> (e.g. h265-30fps-b0040/r2 or controls/x00)."""
    rng = np.random.default_rng(seed)
    cell = Path(grid_dir) / rel
    for sub in ("hosta", "hostb", "reduced"):
        (cell / sub).mkdir(parents=True, exist_ok=True)
    codec, fps, bpp, kbps = variables["codec"], int(variables["fps"]), float(variables["bpp"]), int(variables["kbps"])
    dur = float(variables["duration_s"])
    impl = encoder or ("NVIDIA NVENC H.265" if codec == "h265" else "NVIDIA NVENC AV1" if codec == "av1"
                       else "NVIDIA NVENC H.264")
    label = (f"{GRID_ID}-{'x' if kind == 'control' else 'c'}{index:02d}-{codec}-{W}x{H}-{fps}fps-"
             f"b{int(round(bpp * 1000)):04d}-{kbps}k-v1-p1-r{repeat}")
    gates = [{"name": g, "pass": True, "detail": ""} for g in ("code_match", "binary", "ptp", "clock_offset", "modem",
                                                                  "sfu", "encoder", "link_mtu")]
    manifest = {
        "label": label, "grid_id": GRID_ID, "index": index, "kind": kind, "repeat": repeat,
        "variables": variables, "requested": {"width": W, "height": H},
        "negotiated": {"width": W, "height": H, "encoder_implementation": impl, "codec": negotiated_codec or codec},
        "epoch": EPOCH, "epoch_iso": "2026-09-30T01:00:10Z", "commit": "0123abcd",
        "clock": {"a": {"host_minus_utc_s": -15.8, "spread_ms": 4.1, "servers": 3},
                  "b": {"host_minus_utc_s": -15.8, "spread_ms": 3.9, "servers": 3}},
        "ptp": {"a": {"state": "MASTER"},
                "b": {"state": "SLAVE" if ptp_locked else "LISTENING", "servo_lines_30s": 30 if ptp_locked else 0,
                      "offset_ns": 1800}},
        "band": {"a": {"band": "n41", "arfcn": 520110, "pci": 123, "share": 1.0, "scell": None},
                 "b": {"band": "n41", "arfcn": 521310, "pci": 77, "share": 0.99, "scell": None}},
        "gates": {"a": gates, "b": gates[1:5]},
        "status": status, "status_reason": "" if status == "OK" else f"synthetic {status}",
        "timeline": {}, "excluded_from_comparison": False, "exclusion_reason": "",
        "integrity": {"captures_complete": status == "OK", "mirror_verified": True},
    }
    (cell / "manifest.json").write_text(json.dumps(manifest, indent=1))
    if not metrics_json:
        return cell

    # ---- frames ------------------------------------------------------------------------
    n = int(dur * fps)
    t = np.arange(n) / fps + rng.uniform(0, 0.002, n) + 0.05
    budget = kbps * 1000 / 8 / fps
    key = (np.arange(n) % (fps * 10)) == 0
    size = np.round(budget * rng.lognormal(-0.03, 0.25, n) * np.where(key, 3.2, 1.0))
    pk = np.ceil(size / 1150)
    up = rng.uniform(28, 45) * 1e6
    app = rng.gamma(2.0, 0.25, n)
    emis = size * 8 / up * 1e3 + rng.gamma(1.2, 0.3, n)
    infl = 24 + rng.gamma(2.5, 2.2, n) + np.maximum(0, size - 1.5 * budget) * 16 / up * 1e3
    stalls = [(float(rng.uniform(3, dur - 3)), float(rng.uniform(150, 380))) for _ in range(1 + int(rng.random() < 5 * bpp))]
    for b0, amp in stalls:
        m = (t >= b0) & (t < b0 + 0.35)
        infl[m] += np.linspace(amp, 15, m.sum())
    arr = rng.gamma(1.4, 0.5, n) + pk * 0.03
    w2a = rng.gamma(2.0, 0.6, n) + np.where(rng.random(n) < 0.006, rng.uniform(35, 55, n), 0.0)
    enc = (3.0 if codec == "h265" else 4.2) + size / 1e4 * 0.4 + rng.gamma(2, 0.25, n)
    c2p = enc + rng.gamma(2.0, 0.35, n)
    dec = (5.5 if codec == "h265" else 8.5) + size / 1e4 * 0.5 + rng.gamma(2.0, 0.6, n)
    ren = 3.5 + rng.gamma(2.0, 0.4, n)
    owd = app + emis + infl + arr + w2a
    received = rng.random(n) > 0.0015
    for b0, _ in stalls:
        m = (t >= b0) & (t < b0 + 0.35)
        received &= ~(m & (rng.random(n) < 0.05))
    rendered = received & (rng.random(n) > 0.001)
    lost_pk = np.cumsum(np.where(received, 0, pk))
    qp = _qp(rng, codec, bpp, key) if qp_log else None
    rows = []
    for i in range(n):
        cap = int((EPOCH + t[i]) * 1e6)
        pz = cap + int(c2p[i] * 1000)
        r = {"t_s": t[i], "frame_id": 1000 + i, "capture_us": cap, "rtp_ts": 90000 * i // fps, "bytes": int(size[i]),
             "packets_a": int(pk[i]), "encode_ms": enc[i], "capture_to_packetize_ms": c2p[i],
             "app_to_wire_a": app[i], "emission_a": emis[i], "received": int(received[i]), "rendered": int(rendered[i]),
             "packets_lost": int(lost_pk[i])}
        if received[i]:
            r.update(packets_b=int(pk[i]), in_flight=infl[i], arrival_b=arr[i], wire_to_app_b=w2a[i], owd=owd[i],
                     receive_us=pz + int(owd[i] * 1000), capture_to_receive=c2p[i] + owd[i],
                     e2e=c2p[i] + owd[i] + dec[i] + ren[i], decode=dec[i], render=ren[i])
        else:
            r.update(packets_b=None, in_flight=None, arrival_b=None, wire_to_app_b=None, owd=None, receive_us=None,
                     capture_to_receive=None, e2e=None, decode=None, render=None)
        if qp is not None:
            r["qp"] = int(qp[i])
        rows.append(r)
    F.finish_frames(rows)
    med = F.steady_medians(rows)
    spikes, episodes = F.spikes_and_episodes(rows, med, {}, {}, [])
    red = cell / "reduced"
    F.write_csv(red / "frames.csv", (*F.FRAME_COLS, "qp") if qp is not None else F.FRAME_COLS, rows)
    F.write_csv(red / "spikes.csv", F.SPIKE_COLS, spikes)
    F.write_csv(red / "episodes.csv", F.EPISODE_COLS, episodes)

    # ---- seconds ------------------------------------------------------------------------
    srows = []
    for s in range(-2, int(dur) + 2):
        m = (t >= s) & (t < s + 1)
        rec = m & received
        o = owd[rec]
        srows.append({"t": s, "a_bytes": int(size[m].sum() * 1.05), "a_packets": int(pk[m].sum()),
                      "b_bytes": int(size[rec].sum() * 1.05), "b_packets": int(pk[rec].sum()), "frames": int(rec.sum()),
                      "owd_p50": float(np.median(o)) if len(o) else None, "owd_max": float(o.max()) if len(o) else None,
                      "qp": float(qp[m].mean()) if (qp is not None and m.any()) else None,
                      "frames_encoded": int(m.sum()), "fps_encoded": float(m.sum()), "target_kbps": kbps,
                      "modem_a": float(rng.uniform(0.9, 1.1)), "modem_b": float(rng.uniform(0.9, 1.1)),
                      "rsrp_a": float(rng.normal(-86, 1.5)), "snr_a": float(rng.normal(15, 1.5)),
                      "rsrp_b": float(rng.normal(-89, 1.5)), "snr_b": float(rng.normal(13, 1.5)),
                      "lost_delta": 0})
    F.write_csv(red / "seconds.csv", F.SECONDS_COLS, srows)
    for h in ("a", "b"):
        lines = [f"# parser=synthetic; host_minus_utc_s=-15.800; probe_start={EPOCH:.3f} probe_end={EPOCH + dur:.3f}",
                 "second_rel_probe,code,count"]
        for sec in range(int(dur)):
            for code, base in (("0xB872", 400), ("0xB881", 238), ("0xB97F", 40)):
                lines.append(f"{sec},{code},{int(base * rng.uniform(0.9, 1.1))}")
        (red / f"dlf-rates-{h}.csv").write_text("\n".join(lines) + "\n")
        (red / f"band-{h}.csv").write_text("t_rel_epoch_s,carrier,band,arfcn,pci\n" + "".join(
            f"{sec},pcell,n41,{520110 if h == 'a' else 521310},{123 if h == 'a' else 77}\n" for sec in range(int(dur))))

    # ---- control path: raw logs -> the real reduce/control.py, probes ----------------------
    notes: list[str] = []
    if control:
        c_owd = _write_control_logs(cell, rng, dur, stalls)
        ctl = CTL.reduce_control(cell / "hosta", cell / "hostb", EPOCH, red, notes, variables.get("control_transport"))
        probes = [(float(k + 0.5), float(2 * np.percentile(c_owd, 50) + rng.gamma(2, 2.5))) for k in range(int(dur))
                  for _ in range(2)]
        CTL.write_probes(red / "probes.csv", probes)
        ctl.update(probe_section=True, probe_rtts=len(probes))
    else:
        ctl = CTL.reduce_control(cell / "hosta", cell / "hostb", EPOCH, red, notes, variables.get("control_transport"))
        ctl.update(probe_section=False, probe_rtts=0)

    nrec = int(received.sum())
    qps = [float(x) for x in qp] if qp is not None else []
    from teleop.grid.stats import summ  # noqa: PLC0415
    reduce_json = {
        "label": label, "epoch": EPOCH, "duration_s": dur, "missing": [], "notes": notes,
        "clock_offsets": {"a": -15.8, "b": -15.8},
        "encoder": {"implementation": impl, "frames_encoded": n, "frames_sent": n, "key_frames_encoded": int(key.sum()),
                    "quality_limitation_s": {"none": dur, "bandwidth": 0.0, "cpu": 0.0, "other": 0.0},
                    "codec": f"video/{(negotiated_codec or codec).upper()}"},
        "frames": {"published": n, "dropped_pre_encode": 1, "captured": n + 1, "received": nrec,
                   "dropped_post_encode": n - nrec, "rendered": int(rendered.sum())},
        "totals": {"packets_lost": int(lost_pk[-1]), "loss_events": int((~received).sum()), "nacks_sfu_to_a": None,
                   "nacks_b_to_sfu": None, "duplicates_a": 0, "duplicates_b": 0,
                   "a_video_bytes_in_cell": int(size.sum() * 1.05), "first_video_second_a": 0, "first_video_second_b": 0},
        "dlf": {h: {"present": True, "first15_ratio": 1.05, "steady_per_s": 700.0, "x19ef_in_cell": 0} for h in "ab"},
        "padding": {"share": None, "note": "filler NALs / AV1 padding are invisible through SRTP"},
        "qp_log": ({"rows": n, "with_qp": len(qps), "qp": summ(qps), "codec": codec, "implementation": "synthetic",
                    "joined": len(qps)} if qp is not None else None),
        "control": ctl,
    }
    (red / "reduce.json").write_text(json.dumps(reduce_json, indent=1))
    metrics.build(cell)
    return cell


def make_grid(root: Path, codecs=("h265", "av1"), fps=(30, 25), bpp=(0.04, 0.06, 0.08, 0.10, 0.14), repeats: int = 3,
              duration_s: int = 30, flags: bool = True, controls: bool = True, grid_id: str = GRID_ID) -> Path:
    """The tonight-shaped grid. With flags=True some repeats are made awkward on purpose:
    INCOMPLETE, not NVENC, PTP unlocked, codec fallback, SKIPPED (manifest only), not run yet
    (directory absent) and an older subscriber (no control logs)."""
    gd = Path(root) / grid_id
    gd.mkdir(parents=True, exist_ok=True)
    plan = []
    idx = 0
    special = {}
    if flags:
        special = {
            (combo_name(codecs[0], fps[0], bpp[-2]), 2): {"status": "INCOMPLETE"},
            (combo_name(codecs[-1], fps[-1], bpp[1]), 3): {"encoder": "libaom (software)"},
            (combo_name(codecs[-1], fps[0], bpp[-1]), 1): {"ptp_locked": False},
            (combo_name(codecs[0], fps[-1], bpp[0]), 1): {"negotiated_codec": "h264"},
            (combo_name(codecs[0], fps[-1], bpp[-1]), 3): "absent",
            (combo_name(codecs[-1], fps[-1], bpp[-2]), 2): {"status": "SKIPPED", "metrics_json": False},
            (combo_name(codecs[0], fps[0], bpp[1]), 3): {"control": False},
        }
    for r in range(1, repeats + 1):
        for c in codecs:
            for f in fps:
                for b in bpp:
                    combo = combo_name(c, f, b)
                    v = {"codec": c, "fps": f, "bpp": b, "kbps": kbps_of(f, b), "resolution": f"{W}x{H}", "width": W,
                         "height": H, "vbv_frames": 1, "padding": True, "target_quality": "off", "intra_refresh": 0,
                         "pin_bitrate": True, "duration_s": duration_s, "clip": "/media/clip-src-30s.mp4", "lead_s": 60,
                         "control_transport": "data_track_buf1"}
                    plan.append({"index": idx, "kind": "cell", "repeat": r, "variables": v})
                    sp = special.get((combo, r), {})
                    if sp != "absent":
                        make_repeat(gd, f"{combo}/r{r}", v, index=idx, repeat=r,
                                    seed=hash((combo, r)) % 2**32 if False else (idx * 7919 + r), **sp)
                    idx += 1
    if controls:
        v = {"codec": codecs[0], "fps": fps[0], "bpp": 0.05, "kbps": kbps_of(fps[0], 0.05), "resolution": f"{W}x{H}",
             "width": W, "height": H, "vbv_frames": 1, "padding": True, "target_quality": "off", "intra_refresh": 0,
             "pin_bitrate": True, "duration_s": duration_s, "clip": "/media/clip-src-30s.mp4", "lead_s": 60,
             "control_transport": "data_track_buf1"}
        make_repeat(gd, "controls/x00", v, index=0, repeat=1, seed=4242, kind="control")
        plan.append({"index": 0, "kind": "control", "repeat": 1, "variables": v})
    doc = {"id": grid_id, "description": "synthetic codec x fps x bpp", "order": "shuffle", "seed": 1,
           "defaults": {"repeats": repeats, "duration_s": duration_s, "resolution": f"{W}x{H}"},
           "definition": {"id": grid_id, "axes": {"codec": list(codecs), "fps": list(fps), "bpp": list(bpp)}},
           "cells": plan}
    (gd / "grid.yaml").write_text(json.dumps(doc, indent=1))   # JSON is valid YAML
    return gd
