#!/usr/bin/env python3
"""S3 readout: is the A<->B coupling a shared-cell (PCI 85) scheduler effect?

Per arm, everything is aligned to the UPLOAD window [upload_start, upload_end] from that
arm's probe.csv (A's for arm a, B's for arms b and c), baseline = upload_start-50..-5 s.

  a  A uploads, no video on either host  -> do B's watched codes step with A's upload?
  b  A streams pinned to B, B uploads     -> do A's probe RTTs / qdisc backlog / BSR rate or
                                              B's media delay rise while B uploads?
  c  B uploads, no video                 -> do A's watched codes step with B's upload?

DLF rates: A's dlf-rates.csv is written by dlf-rates.py with probe_start = upload_start
(column second_rel_probe). B's dlf-rates-hostb.csv counts seconds relative to the ARM EPOCH,
so it is shifted by (upload_start - epoch). Codes are unnamed except where MobileInsight names
them; rates only (v3 payloads are not publicly decodable). DLF timing trust is +-2 s.

Usage: s3_readout.py --arm a|b|c --epoch E --upload-csv probe.csv --a-dir A_DIR
                     [--a-rates A/dlf-rates.csv] [--b-dir B_DIR] [--b-rates B/dlf-rates-hostb.csv]
"""
import argparse, csv, glob, json, os, re
from collections import defaultdict

WATCH = {0xB882: '-', 0xB8A6: '-', 0xB958: '-', 0xB8A8: '-', 0xB8A7: '-', 0xB885: '-',
         0xB870: '-', 0xB872: 'NR_L2_UL_TB', 0xB873: 'NR_L2_UL_BSR', 0xB87C: '-', 0x158C: '-',
         0xB883: 'NR_MAC_UL_PhysCh_Sched', 0xB881: 'NR_MAC_UL_TB_Stats', 0xB983: '-'}

def pct(v, p):
    if not v: return None
    v = sorted(v); return v[min(len(v) - 1, int(p * len(v)))]

def fmt(x, nd=0):
    return '-' if x is None else (f"{x:.{nd}f}")

def load_upload(path):
    ev = {}
    for r in csv.DictReader(open(path)): ev.setdefault(r['event'], []).append((int(r['unix_ms']) / 1000, r['detail']))
    ps, pe = ev['probe_start'][0][0], ev['probe_end'][0][0]
    par = sum(float(d.split()[0]) for k, v in ev.items() if k.startswith('parallel_end') for _, d in v if d)
    return ps, pe, par

def rates(path, shift=0.0):
    """dlf-rates csv -> {code: {sec_rel_upload: count}}; shift converts the file's zero to upload_start."""
    per = defaultdict(dict)
    if not path or not os.path.exists(path): return None
    for line in open(path):
        if line.startswith('#') or line.startswith('second'): continue
        s, code, c = line.strip().split(',')
        per[int(code, 16)][int(s) - int(round(shift))] = int(c)
    return per

def ping_by_sec(path, zero):
    rx = re.compile(r'^\[(\d+\.\d+)\].*icmp_seq=\d+.*time=([\d.]+) ms'); d = defaultdict(list)
    if not path or not os.path.exists(path): return None
    for l in open(path, errors='replace'):
        m = rx.match(l)
        if m: d[int((float(m[1]) - zero) // 1)].append(float(m[2]))
    return d

def csv_rtt_by_sec(path, zero, tcol='send_unix_ms', vcol='rtt_ms', filt=None):
    d = defaultdict(list)
    if not path or not os.path.exists(path): return None
    for r in csv.DictReader(open(path)):
        if filt and not filt(r): continue
        if r[vcol]: d[int((float(r[tcol]) / 1000 - zero) // 1)].append(float(r[vcol]))
    return d

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--arm', required=True, choices='abc'); ap.add_argument('--epoch', type=int, required=True)
    ap.add_argument('--upload-csv', required=True); ap.add_argument('--a-dir', required=True)
    ap.add_argument('--a-rates'); ap.add_argument('--a-rates-epoch-relative', action='store_true',
                    help='A rates file counts seconds from the ARM EPOCH (like B\'s), not from upload_start')
    ap.add_argument('--b-dir'); ap.add_argument('--b-rates')
    a = ap.parse_args()
    ps, pe, par = load_upload(a.upload_csv); dur = pe - ps
    print(f"S3 arm {a.arm}: upload {'A' if a.arm == 'a' else 'B'} start epoch{ps - a.epoch:+.2f}s, {dur:.1f} s, parallel {par:.1f} Mbps")
    A, B = a.a_dir, a.b_dir
    g = lambda d, pat: (sorted(glob.glob(os.path.join(d, pat))) or [None])[0] if d else None
    base = range(-50, -5); win = range(0, int(dur) + 1)

    # --- A-side path probes ---
    icmp = ping_by_sec(g(A, 'ping-icmp-dscp0.txt'), ps)
    udp = csv_rtt_by_sec(g(A, 'udp-ttl3-dscp0.csv'), ps, filt=lambda r: r['ttl'] == '3')
    tcp = csv_rtt_by_sec(g(A, 'tcp-rst-dscp0.csv'), ps)
    def agg(d, rng):
        return [x for s in rng for x in d.get(s, [])] if d is not None else None
    print("\nA path probes to 10.1.20.16 (baseline p50/p90 -> upload p50/p90/max, n):")
    for name, d in (('ICMP', icmp), ('UDP TTL=3', udp), ('TCP SYN->RST', tcp)):
        if d is None: print(f"  {name:13s} NOT RECORDED"); continue
        b, w = agg(d, base), agg(d, win)
        rise = None if pct(b, .9) is None or pct(w, .9) is None else pct(w, .9) - pct(b, .9)
        verdict = '-' if rise is None else ('QUEUED' if rise >= max(30, 0.5 * pct(b, .9)) else 'flat')
        print(f"  {name:13s} {fmt(pct(b,.5))}/{fmt(pct(b,.9))} (n{len(b)}) -> {fmt(pct(w,.5))}/{fmt(pct(w,.9))}/{fmt(max(w) if w else None)} (n{len(w)})  {verdict}")

    # --- A qdisc 10 Hz ---
    qd = g(A, 'qdisc-10hz.csv')
    if qd:
        rows = list(csv.DictReader(open(qd))); bl_b, bl_w, rq_w = [], [], []
        for x, y in zip(rows, rows[1:]):
            try:
                t = int(y['unix_ms']) / 1000 - ps; blog = int(y['backlog_pkts'] or 0)
                dt = (int(y['unix_ms']) - int(x['unix_ms'])) / 1000; rq = (int(y['requeues']) - int(x['requeues'])) / dt if dt > 0 else 0
            except ValueError: continue
            if -50 <= t < -5: bl_b.append(blog)
            if 0 <= t <= dur: bl_w.append(blog); rq_w.append(rq)
        print(f"\nA qdisc: backlog max baseline {max(bl_b) if bl_b else '-'} -> upload {max(bl_w) if bl_w else '-'} pkts "
              f"(seconds >10 pkts: {len({int(i/10) for i, v in enumerate(bl_w) if v > 10})}); requeue max {max(rq_w) if rq_w else 0:.0f}/s")

    # --- arm b: publisher target and B media delay ---
    if a.arm == 'b':
        jl = g(A, '*.jsonl'); tg = defaultdict(list)
        if jl:
            for line in open(jl):
                try: d = json.loads(line)
                except Exception: continue
                v = d.get('video_out')
                if isinstance(v, dict) and v.get('target_bitrate_bps'): tg[int((d['t_unix_us'] / 1e6 - ps) // 1)].append(v['target_bitrate_bps'] / 1e6)
        tb, tw = agg(tg, base), agg(tg, win)
        print(f"A publisher target: baseline p50 {fmt(pct(tb,.5),2)} -> upload min {fmt(min(tw) if tw else None,2)} Mbps")
        sub = g(B, 'subscriber.csv') if B else None
        if sub:
            md = defaultdict(list); lost = 0; prev = None
            for r in csv.DictReader(open(sub)):
                try: cap = int(r['capture_timestamp_us']); rx = int(r['webrtc_receive_timestamp_us'])
                except (KeyError, ValueError): continue
                if cap > 0 and rx > cap: md[int((cap / 1e6 - ps) // 1)].append((rx - cap) / 1000)
                try:
                    pl = int(float(r['packets_lost']))
                    if prev is not None and pl > prev and 0 <= rx / 1e6 - ps <= dur + 10: lost += pl - prev
                    prev = pl
                except (KeyError, ValueError): pass
            mb, mw = agg(md, base), agg(md, win)
            print(f"B media delay: baseline p50/p95 {fmt(pct(mb,.5))}/{fmt(pct(mb,.95))} -> upload p50/p95/max {fmt(pct(mw,.5))}/{fmt(pct(mw,.95))}/{fmt(max(mw) if mw else None)} ms; lost upload..+10 s: {lost}")
            print("   per-second p95: " + " ".join(f"{s:+d}:{fmt(pct(md[s],.95))}" for s in range(-3, int(dur) + 6) if md.get(s)))

    # --- watched DLF codes, both hosts ---
    ar = rates(a.a_rates or g(A, 'dlf-rates.csv'), shift=(ps - a.epoch) if a.a_rates_epoch_relative else 0.0)
    br = rates(a.b_rates or g(B, 'dlf-rates-hostb.csv'), shift=(ps - a.epoch)) if B or a.b_rates else None
    med = lambda d, rng: pct([d.get(s, 0) for s in rng], .5)
    print("\nwatched DLF codes, median per second  baseline(-50..-5) -> upload(0..dur)   [ratio]")
    print(f"  {'code':7s} {'name':24s} {'A':>22s}   {'B':>22s}")
    for c, name in WATCH.items():
        def cell(r):
            if r is None: return 'not recorded'
            d = r.get(c, {}); b, w = med(d, base), med(d, win)
            ratio = f"{w / b:.2f}" if b else ('new' if w else '-')
            return f"{b} -> {w} [{ratio}]"
        print(f"  0x{c:04X}  {name:24s} {cell(ar):>22s}   {cell(br):>22s}")

    print("\nquestion for this arm:")
    q = {'a': "do B's watched codes step with A's upload while B carries NO media? (column B)",
         'b': "do A's probe RTTs, qdisc backlog, BSR rate, or B's media delay rise while B uploads? (above)",
         'c': "do A's watched codes step with B's upload while A carries NO media? (column A)"}[a.arm]
    print("  " + q)

if __name__ == '__main__':
    main()
