"""Raw captures -> reduced tables for one cell. Host A only (numpy allowed, tshark on PATH).

    reduce_cell(cell_dir) -> None

Reads manifest.json (label, epoch, duration, clock offsets), hosta/ and hostb/ as laid out
in CONTRACT.md, and writes <cell>/reduced/:

    dlf-rates-a.csv dlf-rates-b.csv band-a.csv band-b.csv   (only when that host's DLF exists)
    frames.csv seconds.csv spikes.csv episodes.csv inflight.csv reduce.json

then updates manifest["band"], manifest["integrity"]["reduce_resyncs"] and
manifest["integrity"]["reduce"] (see CONTRACT.md "added by reduce"). Nothing is written
outside the cell directory. A missing input (hostb/ absent, no DLF, no hops) is recorded in
reduce.json "missing" and the tables carry blanks where that input would have been.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from . import dlf_rates, frames as F, pcap_extract, segjoin

__all__ = ["reduce_cell"]


def _load_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _find(d: Path, exact: str, pattern: str) -> Path | None:
    """The contract name, else a single unambiguous match (never a guess between several)."""
    p = d / exact
    if p.exists():
        return p
    if not d.is_dir():
        return None
    m = sorted(d.glob(pattern))
    return m[0] if len(m) == 1 else None


def _offset(manifest: dict, host: str, cell: Path, notes: list) -> float | None:
    """host_minus_utc_s for a host: manifest clock, else host*/clock.json, else the PTP peer's value."""
    c = (manifest.get("clock") or {}).get(host) or _load_json(cell / f"host{host}" / "clock.json") or {}
    v = c.get("host_minus_utc_s")
    if v is not None:
        return float(v)
    other = "b" if host == "a" else "a"
    c2 = (manifest.get("clock") or {}).get(other) or _load_json(cell / f"host{other}" / "clock.json") or {}
    if c2.get("host_minus_utc_s") is not None:
        notes.append(f"clock offset for host {host} not recorded; used host {other}'s (hosts are PTP-locked)")
        return float(c2["host_minus_utc_s"])
    return None


def _duration(manifest: dict, pub: list[dict], epoch: float) -> float:
    d = (manifest.get("variables") or {}).get("duration_s")
    if d:
        return float(d)
    if pub:
        return math.ceil(pub[-1]["capture_us"] / 1e6 - epoch)
    return 300.0


def _dlf(host: str, path: Path | None, off: float | None, epoch: float, dur: float, out: Path, missing: list,
         notes: list) -> dict:
    res = {"present": False}
    if path is None:
        missing.append(f"{host}.dlf")
        return res
    if off is None:
        missing.append(f"{host}.clock_offset")
        notes.append(f"host {host} DLF present but no clock offset: modem time cannot be placed; skipped")
        return res
    s = dlf_rates.scan(path, off, epoch, dur)
    dlf_rates.write_csv(out / f"dlf-rates-{host}.csv", s["counts"], off, epoch, dur, s["records_total"], s["stats"])
    s["ml1"].write_csv(out / f"band-{host}.csv")
    band = s["ml1"].summary()
    per = dlf_rates.read_csv(out / f"dlf-rates-{host}.csv", epoch)
    act = dlf_rates.activity_index(per, dur)
    first = [v for k, v in act["index"].items() if 0 <= k < 15]
    x19 = {sec: n for sec, n in per.get(dlf_rates.X19EF, {}).items()} if per else {}
    res.update(present=True, records_total=s["records_total"], records_in_window=s["records_in_window"],
               records_outside_window=s["records_outside_window"], resyncs=s["stats"]["resyncs"],
               skipped_bytes=s["stats"]["skipped_bytes"], window=s["window"], band=band,
               steady_per_s=act["steady_per_s"], index=act["index"],
               first15_ratio=(sum(first) / len(first)) if first else None,
               x19ef=x19, x19ef_in_cell=dlf_rates.code_count(per, dlf_rates.X19EF, 0, int(dur)))
    if band.get("records_without_block"):
        notes.append(f"host {host}: {band['records_without_block']}/{band['records']} ML1 records yielded no carrier "
                     "block (not the same as 'no serving cell')")
    return res


def _dropped_post(rows: list[dict]) -> int | None:
    """Published frames that B never received, counted only inside B's receive span (first..last
    received frame): frames before the subscriber joined or after it left are not drops."""
    idx = [i for i, r in enumerate(rows) if r["received"]]
    if not idx:
        return None
    return sum(1 for r in rows[idx[0]:idx[-1] + 1] if not r["received"])


def _first_second(pk: list) -> int | None:
    return int(math.floor(min(t for t, _ in pk))) if pk else None


def reduce_cell(cell_dir) -> None:
    cell = Path(cell_dir)
    mpath = cell / "manifest.json"
    manifest = json.loads(mpath.read_text())
    label = manifest.get("label") or cell.name
    epoch = float(manifest["epoch"])
    ha, hb = cell / "hosta", cell / "hostb"
    out = cell / "reduced"
    out.mkdir(exist_ok=True)
    missing: list[str] = []
    notes: list[str] = []
    if not hb.is_dir():
        missing.append("hostb/")
        notes.append("hostb/ missing: A-only cell; B-side segments, owd, e2e, loss and B modem are null")

    # ---- app-side logs
    p_pub = _find(ha, f"{label}.pub.csv", "*.pub.csv")
    pub = segjoin.load_pub(p_pub) if p_pub else []
    if not p_pub:
        missing.append("a.pub.csv")
    p_sub = _find(hb, "subscriber.csv", "subscriber*.csv")
    sub = segjoin.load_sub(p_sub) if p_sub else None
    if hb.is_dir() and not p_sub:
        missing.append("b.subscriber.csv")
    dur = _duration(manifest, pub, epoch)
    run_json = _load_json(ha / "run.json") or {}

    # ---- modem
    off_a, off_b = _offset(manifest, "a", cell, notes), _offset(manifest, "b", cell, notes)
    dlf_a = _dlf("a", _find(ha, f"{label}.dlf", "*.dlf"), off_a, epoch, dur, out, missing, notes)
    dlf_b = _dlf("b", _find(hb, f"{label}.dlf", "*.dlf") if hb.is_dir() else None, off_b, epoch, dur, out,
                 [] if not hb.is_dir() else missing, notes)

    # ---- wire
    wires, flows = {}, {}
    for host, d in (("a", ha), ("b", hb)):
        p = _find(d, f"{label}.wwan0.pcap", "*.pcap") if d.is_dir() else None
        if p is None:
            if d.is_dir():
                missing.append(f"{host}.pcap")
            wires[host] = None; flows[host] = pcap_extract.Flow()
            continue
        pk = pcap_extract.extract(p)
        flows[host] = pcap_extract.find_flow(pk, host)
        wires[host] = segjoin.build_wire(pk, flows[host], host) if flows[host].video_pt is not None else None
        if wires[host] is None:
            notes.append(f"host {host}: no RTP video flow found in {p.name}")
    wa, wb = wires["a"], wires["b"]
    if flows["a"].sfu and flows["b"].sfu and flows["a"].sfu != flows["b"].sfu:
        notes.append("A and B see different SFU media addresses")

    # ---- join
    shift = shift_med = agree = None
    wire_ts, join_resyncs = None, 0
    if wa is not None and pub:
        shift, shift_med, steady_from = segjoin.choose_shift(pub, wa, epoch)
        if shift is None:
            notes.append("no pub<->A-wire row shift in -3..+3 gives app_to_wire_a >= -0.5 ms; wire segments left null")
        else:
            agree = segjoin.order_agreement(pub, wa, shift)
            wire_ts, join_resyncs = segjoin.resolve(pub, wa, shift, epoch, steady_from)
            if join_resyncs:
                notes.append(f"pub<->A-wire order join left the capture clock {join_resyncs} time(s); those rows re-joined by timestamp")
    rows = segjoin.join(pub, sub, wa, wb, epoch, wire_ts)
    med_bytes = F.finish_frames(rows)
    med = F.steady_medians(rows)

    # ---- side inputs
    hops_a = F.read_hops(_find(ha, f"{label}.hops.csv", "*.hops*.csv"), epoch)
    hops_b = F.read_hops(_find(hb, f"{label}.hops.csv", "*.hops*.csv"), epoch) if hb.is_dir() else []
    if not hops_a:
        missing.append("a.hops.csv")
    if hb.is_dir() and not hops_b:
        missing.append("b.hops.csv")
    stats = F.read_webrtc_stats(_find(ha, f"{label}.jsonl", "*.jsonl"), epoch)
    if not stats["polls"]:
        missing.append("a.jsonl")
    enc = F.encoder_per_second(stats["polls"])

    # ---- loss, nacks, in flight (all times relative to epoch)
    loss_total, loss_ev = (None, None)
    if sub is not None:
        loss_total, loss_ev = F.loss_timeline([(c / 1e6 - epoch, s["packets_lost"]) for c, s in sub.items()])
    rel = lambda ts: [t - epoch for t in ts]
    a_pk = [(t - epoch, n) for t, n, _ in wa.pkt_times] if wa else []
    b_pk = [(t - epoch, n) for t, n, _ in wb.pkt_times] if wb else []
    inflight = []
    if wa and wb:
        # only frames seen on BOTH wires: frames sent before B subscribed would otherwise sit in
        # the A-minus-B count forever
        common = set(wa.frames) & set(wb.frames)
        inflight = F.inflight_samples([t - epoch for t, _, ts in wa.pkt_times if ts in common],
                                      [t - epoch for t, _, ts in wb.pkt_times if ts in common], dur)
    nacks_a = rel(wa.nack_rx) if wa else []
    nacks_b = rel(wb.nack_tx) if wb else []
    nacks_a_visible = bool(wa and wa.rtcp_rx)
    nacks_b_visible = bool(wb and wb.rtcp_tx)
    if wa and not nacks_a_visible:
        notes.append("no SFU->A RTCP could be dissected (compound SRTCP); nacks_sfu_to_a not measurable")
    if wb and not nacks_b_visible:
        notes.append("B->SFU RTCP is compound SRTCP (first header RR/SR, rest encrypted): NACKs from B are not "
                     "visible in B's pcap; nacks_b_to_sfu is null")

    kq_a_by_s = {}
    for h in hops_a:
        if h["kq_bytes"] is not None:
            s = int(math.floor(h["t"])); kq_a_by_s[s] = max(kq_a_by_s.get(s, 0), h["kq_bytes"])
    spikes, episodes = F.spikes_and_episodes(rows, med, dlf_a.get("index") or {}, kq_a_by_s, nacks_b)

    seconds = range(-5, int(math.ceil(dur)) + 5)
    sec_rows = F.per_second(rows, a_pk, b_pk, nacks_a_visible, nacks_b_visible, rel(wa.dup_times) if wa else [], rel(wb.dup_times) if wb else [],
                            nacks_a, nacks_b, loss_ev, inflight, hops_a, hops_b,
                            dlf_a.get("index") if dlf_a["present"] else None,
                            dlf_b.get("index") if dlf_b["present"] else None,
                            dlf_a.get("x19ef") if dlf_a["present"] else None,
                            dlf_b.get("x19ef") if dlf_b["present"] else None, enc, seconds)

    F.write_csv(out / "frames.csv", F.FRAME_COLS, rows)
    F.write_csv(out / "seconds.csv", F.SECONDS_COLS, sec_rows)
    F.write_csv(out / "spikes.csv", F.SPIKE_COLS, spikes)
    F.write_csv(out / "episodes.csv", F.EPISODE_COLS, episodes)
    F.write_csv(out / "inflight.csv", ("t", "in_flight"), [{"t": t, "in_flight": v} for t, v in inflight])

    # ---- scalars
    in_cell = lambda ts: sum(1 for t in ts if 0 <= t < dur + 5)
    last = stats["polls"][-1] if stats["polls"] else {}
    meta = stats["meta"] or {}
    gaps = [r["frame_id_gap"] for r in pub if r["frame_id_gap"] is not None]
    dropped_pre = int(sum(g - 1 for g in gaps if g > 1)) if pub else None
    padding = (manifest.get("variables") or {}).get("padding")
    pad_off = padding in (False, "off", 0, "0")
    red = {
        "label": label, "epoch": epoch, "duration_s": dur, "generated_at": time.time(),
        "missing": missing, "notes": notes,
        "clock_offsets": {"a": off_a, "b": off_b},
        "flow": {h: {"local": f.local, "sfu": f.sfu, "video_pt": f.video_pt, "pt_counts": f.pt_counts}
                 for h, f in flows.items()},
        "join": {"shift": shift, "steady_app_to_wire_a_median_ms": shift_med, "order_agreement": agree,
                 "join_resyncs": join_resyncs, "pub_rows": len(pub), "sub_rows": len(sub) if sub is not None else None,
                 "joined_owd": sum(1 for r in rows if r["owd"] is not None),
                 "joined_wire_a": sum(1 for r in rows if r["app_to_wire_a"] is not None),
                 "joined_all": sum(1 for r in rows if r["wire_to_app_b"] is not None),
                 "a_wire_frames": len(wa.frames) if wa else None, "b_wire_frames": len(wb.frames) if wb else None},
        "steady_median_ms": med, "median_frame_bytes": med_bytes,
        "dlf": {h: {k: v for k, v in d.items() if k not in ("index", "x19ef")} for h, d in (("a", dlf_a), ("b", dlf_b))},
        "totals": {
            "packets_lost": loss_total, "loss_events": len(loss_ev) if loss_ev is not None else None,
            "nacks_sfu_to_a": in_cell(nacks_a) if nacks_a_visible else None,
            "nacks_b_to_sfu": in_cell(nacks_b) if nacks_b_visible else None,
            "rtcp_dissected": {"sfu_to_a": wa.rtcp_rx if wa else None, "b_to_sfu": wb.rtcp_tx if wb else None},
            "duplicates_a": in_cell(rel(wa.dup_times)) if wa else None,
            "duplicates_b": in_cell(rel(wb.dup_times)) if wb else None,
            "a_video_bytes_in_cell": sum(n for t, n in a_pk if 0 <= t < dur) if wa else None,
            "first_video_second_a": _first_second([x for x in a_pk if x[0] >= -5]),
            "first_video_second_b": _first_second([x for x in b_pk if x[0] >= -5]),
        },
        "frames": {
            "published": len(pub), "dropped_pre_encode": dropped_pre,
            "captured": len(pub) + dropped_pre if dropped_pre is not None else None,
            "received": len(sub) if sub is not None else None,
            "dropped_post_encode": _dropped_post(rows),
            "rendered": sum(1 for s in sub.values() if (s["frame_gpu_complete_timestamp_us"] or 0) > 0) if sub else None,
        },
        "encoder": {
            "implementation": last.get("encoder_implementation") or meta.get("encoder_implementation")
                              or run_json.get("encoder_implementation"),
            "frames_encoded": last.get("frames_encoded"), "frames_sent": last.get("frames_sent"),
            "key_frames_encoded": last.get("key_frames_encoded"),
            "frame_width": last.get("frame_width"), "frame_height": last.get("frame_height"),
            "quality_limitation_s": {k: last.get(f"quality_limitation_{k}_s") for k in ("none", "bandwidth", "cpu", "other")}
                                    if last else {k: None for k in ("none", "bandwidth", "cpu", "other")},
            "codec": meta.get("negotiated_codec") or last.get("codec_mime_type"),
        },
        "padding": {"share": 0.0 if pad_off else None,
                    "note": "padding off for this cell" if pad_off else
                    "filler NALs (H.264 type 12) / AV1 padding are invisible through SRTP; share not measurable from the wire"},
        "spikes": {"n": len(spikes), "episodes": len(episodes)},
    }
    (out / "reduce.json").write_text(json.dumps(red, indent=1, default=str))

    # ---- manifest
    manifest["band"] = {h: ({k: d["band"][k] for k in ("band", "arfcn", "pci", "share")} | {"scell": d["band"].get("scell")})
                        if d["present"] else None for h, d in (("a", dlf_a), ("b", dlf_b))}
    integ = manifest.setdefault("integrity", {})
    dlf_rs = {h: (d.get("resyncs") if d["present"] else None) for h, d in (("a", dlf_a), ("b", dlf_b))}
    integ["reduce_resyncs"] = join_resyncs + sum(v for v in dlf_rs.values() if v)
    integ["reduce"] = {"missing": missing, "notes": notes, "segjoin_shift": shift, "order_agreement": agree,
                       "join_resyncs": join_resyncs, "dlf_resyncs": dlf_rs}
    manifest.setdefault("timeline", {})["reduced"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = mpath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    os.replace(tmp, mpath)
