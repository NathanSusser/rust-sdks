#!/usr/bin/env python3
"""S2 readout: which traffic class escaped the uplink queue during the probe?

Paired 5 Hz probes from Host A to the SFU media node, compared between a baseline window
(probe_start-60 .. probe_start-5) and the probe window (probe_start .. probe_end):
  ICMP echo DSCP 0 | ICMP echo EF | UDP TTL=3 DSCP 0 | UDP TTL=3 EF | TCP SYN->RST DSCP 0
plus 10 Hz qdisc requeues/backlog, publisher target (pinned: must not be cut) and, with
--b-dir, B's per-frame media delay and packet loss.

Registered readout (rust-sdks-29, written before the data):
  R1 EF-marked ICMP and UDP queue like DSCP 0     -> no DSCP-aware classifier acted on our mark
  R2 TCP RST RTT queues like UDP                   -> classifier separates ICMP from everything else
  R3 TCP stays flat like ICMP                      -> classifier separates UDP specifically
  R4 pinned: media delay grows for the whole probe, no target cut; loss only on overflow
  R5 10 Hz requeues burst during the probe
"Queued" = probe-window p90 RTT exceeds baseline p90 by >= max(30 ms, 50%).
DSCP caveat: a mark can be bleached upstream; "EF did not escape" means no classifier
acted on our mark, not that none exists.

Usage: s2_class_readout.py --a-dir A_OUT [--b-dir B_OUT] [--probe-csv probe.csv]
"""
import argparse, csv, glob, json, os, re
from collections import defaultdict

def pct(v, p):
    if not v: return None
    v = sorted(v); return v[min(len(v) - 1, int(p * len(v)))]

def ping_samples(path):
    """ping -D: list of (reply_unix_s, rtt_ms) and max seq sent (from seq numbering)."""
    if not path or not os.path.exists(path): return None
    rx = re.compile(r'^\[(\d+\.\d+)\].*icmp_seq=(\d+).*time=([\d.]+) ms'); out = []; seqs = set()
    for line in open(path, errors='replace'):
        m = rx.match(line)
        if m: out.append((float(m[1]), float(m[3]))); seqs.add(int(m[2]))
    return dict(samples=out, answered=len(seqs), maxseq=max(seqs) if seqs else 0)

def ttl_samples(path, ttl=3):
    if not path or not os.path.exists(path): return None
    out = []; sent = 0
    for r in csv.DictReader(open(path)):
        if int(r['ttl']) != ttl: continue
        sent += 1
        if r['rtt_ms']: out.append((float(r['send_unix_ms']) / 1000, float(r['rtt_ms'])))
    return dict(samples=out, answered=len(out), sent=sent)

def tcp_samples(path):
    if not path or not os.path.exists(path): return None
    out = []; sent = 0; res = defaultdict(int)
    for r in csv.DictReader(open(path)):
        sent += 1; res[r['result']] += 1
        if r['rtt_ms']: out.append((float(r['send_unix_ms']) / 1000, float(r['rtt_ms'])))
    return dict(samples=out, answered=len(out), sent=sent, results=dict(res))

def window(samples, lo, hi): return [v for t, v in samples if lo <= t < hi]

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--a-dir', required=True); ap.add_argument('--b-dir'); ap.add_argument('--probe-csv')
    a = ap.parse_args(); A = a.a_dir; B = a.b_dir
    g = lambda d, pat: (sorted(glob.glob(os.path.join(d, pat))) or [None])[0] if d else None
    pc = a.probe_csv or g(A, 'probe.csv')
    ev = {}
    for r in csv.DictReader(open(pc)): ev.setdefault(r['event'], []).append((int(r['unix_ms']) / 1000, r['detail']))
    ps, pe = ev['probe_start'][0][0], ev['probe_end'][0][0]
    par = [d for k, v in ev.items() if k.startswith('parallel_end') for _, d in v]
    print(f"probe: start {ps:.3f}  duration {pe - ps:.2f} s  parallel ends {len(par)}  "
          f"aggregate {sum(float(x.split()[0]) for x in par if x):.1f} Mbps  single {ev.get('single_end', [(0, '?')])[0][1]}")
    base = (ps - 60, ps - 5); prob = (ps, pe)

    classes = [('ICMP DSCP0', ping_samples(g(A, 'ping-icmp-dscp0.txt'))), ('ICMP EF', ping_samples(g(A, 'ping-icmp-ef.txt'))),
               ('UDP TTL3 DSCP0', ttl_samples(g(A, 'udp-ttl3-dscp0.csv'))), ('UDP TTL3 EF', ttl_samples(g(A, 'udp-ttl3-ef.csv'))),
               ('TCP RST DSCP0', tcp_samples(g(A, 'tcp-rst-dscp0.csv')))]
    queued = {}
    print(f"\n{'class':16s} {'base n':>6} {'base p50/p90':>14} {'probe n':>7} {'probe p50/p90/max':>20}  verdict")
    for name, d in classes:
        if d is None: print(f"{name:16s} NOT RECORDED"); queued[name] = None; continue
        b = window(d['samples'], *base); p = window(d['samples'], *prob)
        b90, p90 = pct(b, .9), pct(p, .9)
        if b90 is None or p90 is None: verdict = 'insufficient samples'; q = None
        else:
            q = (p90 - b90) >= max(30.0, 0.5 * b90); verdict = 'QUEUED' if q else 'flat'
        queued[name] = q
        fmt = lambda x: '-' if x is None else f"{x:.0f}"
        print(f"{name:16s} {len(b):6d} {fmt(pct(b,.5)):>6}/{fmt(b90):<7} {len(p):7d} {fmt(pct(p,.5)):>6}/{fmt(p90)}/{fmt(max(p) if p else None):<6}  {verdict}")

    # 10 Hz qdisc
    qd = g(A, 'qdisc-10hz.csv')
    if qd:
        rows = list(csv.DictReader(open(qd))); rq = []
        for x, y in zip(rows, rows[1:]):
            try:
                dt = (int(y['unix_ms']) - int(x['unix_ms'])) / 1000
                if dt > 0: rq.append((int(y['unix_ms']) / 1000, (int(y['requeues']) - int(x['requeues'])) / dt, int(y['backlog_pkts'] or 0)))
            except ValueError: pass
        bq = [r for t, r, _ in rq if base[0] <= t < base[1]]; pq = [r for t, r, _ in rq if prob[0] <= t < prob[1]]
        bl = [b for t, _, b in rq if prob[0] <= t < prob[1]]
        print(f"\nqdisc 10 Hz: requeues/s baseline p50 {pct(bq,.5) or 0:.0f}  probe p50 {pct(pq,.5) or 0:.0f} max {max(pq) if pq else 0:.0f}; backlog max during probe {max(bl) if bl else 0} pkts")
        r5 = bool(pq and bq and pct(pq, .5) > 3 * max(1.0, pct(bq, .5)))
    else:
        print("\nqdisc 10 Hz: NOT RECORDED"); r5 = None

    # publisher target (pinned)
    jl = g(A, '*.jsonl'); tgt = []
    if jl:
        for line in open(jl):
            try: d = json.loads(line)
            except Exception: continue
            v = d.get('video_out')
            if isinstance(v, dict) and v.get('target_bitrate_bps'): tgt.append((d['t_unix_us'] / 1e6, v['target_bitrate_bps'] / 1e6))
    tp = window(tgt, prob[0], prob[1] + 10); tb = window(tgt, *base)
    cut = bool(tp and tb and min(tp) < 0.7 * pct(tb, .5))
    print(f"publisher target: baseline p50 {pct(tb,.5) or 0:.2f} Mbps, min during probe..+10s {min(tp) if tp else 0:.2f} -> {'CUT (pin NOT holding)' if cut else 'held'}")

    # B media delay / loss
    r4 = None
    if B:
        sub = g(B, 'subscriber.csv'); dl = []; lost = []
        if sub:
            prev = None
            for r in csv.DictReader(open(sub)):
                try: cap = int(r['capture_timestamp_us']); rx = int(r['webrtc_receive_timestamp_us'])
                except (KeyError, ValueError): continue
                if cap > 0 and rx > cap: dl.append((cap / 1e6, (rx - cap) / 1000))
                try:
                    pl = int(float(r['packets_lost']))
                    if prev is not None and pl > prev: lost.append((rx / 1e6, pl - prev))
                    prev = pl
                except (KeyError, ValueError): pass
            bd = window(dl, *base); pd = window(dl, *prob)
            ls = sum(n for t, n in lost if prob[0] <= t < prob[1] + 10)
            by_sec = defaultdict(list)
            for t, v in dl:
                if prob[0] - 5 <= t < prob[1] + 15: by_sec[int(t - ps)].append(v)
            print(f"B media delay: baseline p50 {pct(bd,.5) or 0:.0f} ms p95 {pct(bd,.95) or 0:.0f}; probe p50 {pct(pd,.5) or 0:.0f} p95 {pct(pd,.95) or 0:.0f} max {max(pd) if pd else 0:.0f}; packets lost probe..+10s: {ls}")
            print("   per-second p95 (s rel probe_start): " + " ".join(f"{s:+d}:{pct(v,.95):.0f}" for s, v in sorted(by_sec.items())))
            hi = [s for s, v in by_sec.items() if 0 <= s <= (pe - ps) and pct(v, .95) and bd and pct(v, .95) > 2 * pct(bd, .95)]
            r4 = (len(hi) >= 0.6 * max(1, int(pe - ps))) and not cut
        else:
            print("B media delay: NOT RECORDED")

    def say(label, cond, text):
        print(f"  {label}: {'n/a' if cond is None else ('SUPPORTED' if cond else 'not supported')}  -- {text}")
    print("\nregistered readout:")
    ic0, ief, ud0, uef, tcp = (queued.get(k) for k in ('ICMP DSCP0', 'ICMP EF', 'UDP TTL3 DSCP0', 'UDP TTL3 EF', 'TCP RST DSCP0'))
    say('R1', None if None in (ief, uef, ic0, ud0) else (ief == ic0 and uef == ud0), 'EF queues exactly like DSCP 0 (no DSCP-aware classifier acted on our mark)')
    say('R2', None if None in (tcp, ud0, ic0) else (tcp and ud0 and not ic0), 'TCP queues like UDP; ICMP alone escapes')
    say('R3', None if None in (tcp, ud0, ic0) else ((not tcp) and ud0 and not ic0), 'TCP flat like ICMP; UDP alone queues')
    say('R4', r4, 'pinned: B delay elevated for most of the probe, target not cut')
    say('R5', r5, '10 Hz requeues burst during the probe')

if __name__ == '__main__':
    main()
