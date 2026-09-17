#!/usr/bin/env python3
"""S1: did the uplink probe cause the delay episode, and where does the queue sit?

Aligns every S1 signal on one clock (seconds from the cell epoch; both hosts PTP/SNTP
disciplined) and tests the predictions registered before the run:

  P-start   an episode begins 0-6 s after probe_start
  P-cut     the requested bitrate is cut within 3 s of episode start
  P-none    no episode outside [probe_start, probe_end + 10 s]
  P-where   the first hop whose RTT rises with the media delay locates the queue:
              A qdisc backlog/requeues          -> Host A kernel queue
              hop 3 (TTL=3 time-exceeded) rises  -> before hop 3: UE uplink buffer / RAN / transport
              hop 3 flat, A->SFU echo rises     -> between hop 3 and the SFU
              A->SFU flat, B->SFU flat           -> not a shared-path queue (SFU forwarding or B side)

An episode is media delay (capture on A -> receive on B, per frame) whose per-second
p95 exceeds max(3 x baseline p95, 150 ms); baseline = the seconds before probe_start.
Signals that were not recorded are reported as missing, never as flat.

CLOCKS. Host A is the PTP grandmaster on the cable with NTP off (free-running); Host B
slaves to it, so A/B timestamps (media delay, pings, probe log, pcap) share one clock.
Modem DLF timestamps come from the network (UTC), so they are shifted onto the host
clock with --host-minus-utc-s (B measured -4.5286 s by SNTP before S1). Without it the
DLF columns are left unaligned and flagged.

Usage: s1_queue_evidence.py --a-dir A_OUT --epoch E [--b-dir B_OUT] [--host-minus-utc-s X] [--out timeline.csv]
"""
import argparse, csv, glob, json, os, re, statistics as st
from collections import defaultdict

def pct(v, p):
    if not v: return None
    v = sorted(v); return v[min(len(v) - 1, int(p * len(v)))]

def read_ping(path):
    """ping -D: '[unix.us] 64 bytes from IP: icmp_seq=N ttl=T time=X ms' -> {sec: [rtt]}, sent seqs, answered seqs."""
    per = defaultdict(list); seqs = set(); first = None
    if not path or not os.path.exists(path): return None
    rx = re.compile(r'^\[(\d+\.\d+)\].*icmp_seq=(\d+).*time=([\d.]+) ms')
    for line in open(path, errors='replace'):
        m = rx.match(line)
        if m:
            t, seq, rtt = float(m[1]), int(m[2]), float(m[3])
            per[int(t)].append(rtt); seqs.add(seq)
    return dict(per=per, answered=len(seqs), maxseq=max(seqs) if seqs else 0)

def read_hop_ttl(path):
    out = {}
    if not path or not os.path.exists(path): return None
    for r in csv.DictReader(open(path)):
        ttl = int(r['ttl']); d = out.setdefault(ttl, dict(per=defaultdict(list), sent=defaultdict(int), hop=set()))
        s = int(float(r['send_unix_ms']) // 1000); d['sent'][s] += 1
        if r['rtt_ms']:
            d['per'][s].append(float(r['rtt_ms'])); d['hop'].add(r['hop_ip'])
    return out

def read_probe(path):
    if not path or not os.path.exists(path): return None
    ev = {}
    for r in csv.DictReader(open(path)): ev.setdefault(r['event'], []).append((int(r['unix_ms']) / 1000, r['detail']))
    return ev

def read_jsonl(path):
    rows = []
    if not path or not os.path.exists(path): return {}
    for l in open(path):
        try: d = json.loads(l)
        except Exception: continue
        v = d.get('video_out')
        if isinstance(v, dict) and v.get('frames_encoded'): rows.append((d['t_unix_us'] / 1e6, v))
    out = {}
    for (t0, a), (t1, b) in zip(rows, rows[1:]):
        dt = t1 - t0
        if dt > 0:
            out[int(t1)] = dict(target=b.get('target_bitrate_bps', 0) / 1e6, sent=(b['bytes_sent'] - a['bytes_sent']) * 8 / 1e6 / dt,
                                retx=b['retransmitted_packets_sent'] - a['retransmitted_packets_sent'])
    return out

def read_hops(path):
    if not path or not os.path.exists(path): return {}
    rows = list(csv.DictReader(open(path))); out = {}
    for a, b in zip(rows, rows[1:]):
        def d(k):
            try: return int(b[k]) - int(a[k])
            except (ValueError, KeyError): return None
        def f(k):
            try: return float(b[k])
            except (ValueError, KeyError): return None
        out[int(b['unix_ms']) // 1000] = dict(backlog=f('qdisc_backlog_pkts'), requeue=d('qdisc_requeues'), qdrop=d('qdisc_dropped'),
                                              qmidrop=d('qmi_tx_dropped'), snr=f('nr_snr_db'), rsrp=f('nr_rsrp_dbm'))
    return out

def read_dlf_rates(path, host_minus_utc):
    """Per host-clock second: A's modem DLF record counts for NR5G MAC range, all NR5G, and ML1 range."""
    import sys as _s
    _s.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../diag-capture'))
    from dlf_records import iter_file
    if not path or not os.path.exists(path): return None
    mac = defaultdict(int); nr = defaultdict(int); ml1 = defaultdict(int)
    for lid, t, _ln in iter_file(path):
        if 0xB800 <= lid <= 0xB9FF:
            sec = int(t + (host_minus_utc or 0.0)); nr[sec] += 1
            if 0xB880 <= lid <= 0xB8BF: mac[sec] += 1
            if 0xB970 <= lid <= 0xB9FF: ml1[sec] += 1
    return dict(mac=mac, nr=nr, ml1=ml1)

def read_b_delay(path):
    per = defaultdict(list)
    if not path or not os.path.exists(path): return None
    for r in csv.DictReader(open(path)):
        try: cap = int(r['capture_timestamp_us']); rx = int(r['webrtc_receive_timestamp_us'])
        except (KeyError, ValueError): continue
        if cap > 0 and rx > cap: per[cap // 1_000_000].append((rx - cap) / 1000)
    return per

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--a-dir', required=True); ap.add_argument('--b-dir'); ap.add_argument('--epoch', type=int, required=True)
    ap.add_argument('--host-minus-utc-s', type=float, default=None)
    ap.add_argument('--out'); a = ap.parse_args()
    A = a.a_dir; B = a.b_dir; E = a.epoch
    g = lambda d, pat: (sorted(glob.glob(os.path.join(d, pat))) or [None])[0] if d else None

    probe = read_probe(g(A, 'probe.csv'))
    jl = read_jsonl(g(A, '*.jsonl')); hops = read_hops(g(A, '*.hops.csv'))
    a16 = read_ping(g(A, 'ping-10.1.20.16.txt')); a21 = read_ping(g(A, 'ping-10.1.20.21.txt'))
    ttl = read_hop_ttl(g(A, 'hop-ttl.csv')) or {}
    bdel = read_b_delay(g(B, 'subscriber.csv')) if B else None
    dlf = read_dlf_rates(g(A, '*.dlf'), a.host_minus_utc_s)
    b16 = read_ping(g(B, 'ping-10.1.20.16.txt')) if B else None
    b21 = read_ping(g(B, 'ping-10.1.20.21.txt')) if B else None

    ps = probe['probe_start'][0][0] if probe and 'probe_start' in probe else None
    pe = probe['probe_end'][0][0] if probe and 'probe_end' in probe else None
    secs = range(E - 60, E + 360)
    def s_p(d, s, p):
        return pct(d['per'].get(s, []), p) if d else None

    rows = []
    for s in secs:
        dl = bdel.get(s, []) if bdel else []
        j = jl.get(s, {}); h = hops.get(s, {})
        t3 = ttl.get(3); t2 = ttl.get(2)
        phase = ''
        if ps and pe and ps <= s + 0.999 and s <= pe: phase = 'probe'
        rows.append(dict(t_rel=s - E, unix_s=s,
            media_p50=pct(dl, .5), media_p95=pct(dl, .95), media_frames=len(dl) if bdel else None,
            target=j.get('target'), sent=j.get('sent'), retx=j.get('retx'),
            a_sfu16_p50=s_p(a16, s, .5), a_sfu16_max=s_p(a16, s, 1.0), a_sfu21_p50=s_p(a21, s, .5),
            hop3_p50=pct(t3['per'].get(s, []), .5) if t3 else None, hop3_ans=len(t3['per'].get(s, [])) if t3 else None, hop3_sent=t3['sent'].get(s, 0) if t3 else None,
            hop2_ans=len(t2['per'].get(s, [])) if t2 else None,
            b_sfu16_p50=s_p(b16, s, .5), b_sfu21_p50=s_p(b21, s, .5),
            q_backlog=h.get('backlog'), q_requeue=h.get('requeue'), q_drop=h.get('qdrop'), qmi_drop=h.get('qmidrop'), snr=h.get('snr'),
            dlf_mac=dlf['mac'].get(s, 0) if dlf else None, dlf_nr=dlf['nr'].get(s, 0) if dlf else None,
            dlf_ml1=dlf['ml1'].get(s, 0) if dlf else None,
            probe=phase))
    if a.out:
        with open(a.out, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

    # ---- report ----
    def cover(d, name):
        return f"{name}: {'NOT RECORDED' if d is None else str(d['answered']) + ' replies'}"
    print(f"S1 epoch {E}  probe_start {ps - E if ps else 'n/a':+.1f}s  probe_end {pe - E if pe else 'n/a':+.1f}s" if ps and pe else f"S1 epoch {E}  probe events: {'missing' if not probe else sorted(probe)}")
    if probe:
        for k in sorted(probe):
            for t, dtl in probe[k]: print(f"   {k:16s} {t - E:+7.2f}s  {dtl}")
    if dlf is None: print("modem DLF: NOT RECORDED")
    elif a.host_minus_utc_s is None: print("modem DLF: present but NOT clock-aligned (pass --host-minus-utc-s); dlf_* columns are UTC-second buckets")
    else: print(f"modem DLF: aligned to host clock with host-UTC {a.host_minus_utc_s:+.3f} s")
    print("coverage:", "; ".join([cover(a16, 'A->10.1.20.16'), cover(a21, 'A->10.1.20.21'), cover(b16, 'B->10.1.20.16'), cover(b21, 'B->10.1.20.21')]))
    for t in (2, 3):
        d = ttl.get(t)
        if d: print(f"   hop TTL={t}: {sum(len(v) for v in d['per'].values())}/{sum(d['sent'].values())} answered by {','.join(d['hop']) or '-'}")
        else: print(f"   hop TTL={t}: NOT RECORDED")
    if not bdel:
        print("media delay: NOT RECORDED (need B subscriber.csv) -- episode tests cannot run"); return
    base = [r['media_p95'] for r in rows if r['media_p95'] is not None and ps and 10 <= r['t_rel'] and r['unix_s'] < ps - 5]
    b95 = st.median(base) if base else None
    thr = max(3 * b95, 150) if b95 else 150
    ep = [r for r in rows if r['media_p95'] is not None and r['media_p95'] > thr]
    print(f"media delay baseline p95 {b95:.1f} ms -> episode threshold {thr:.0f} ms; episode seconds: {len(ep)}" if b95 else f"episode threshold {thr} ms; episode seconds: {len(ep)}")
    if not ep: print("verdict: NO EPISODE"); return
    # contiguous episodes
    groups = []; cur = [ep[0]]
    for r in ep[1:]:
        if r['unix_s'] - cur[-1]['unix_s'] <= 2: cur.append(r)
        else: groups.append(cur); cur = [r]
    groups.append(cur)
    pre = [r for r in rows if ps and r['unix_s'] < ps - 5 and r['t_rel'] >= 10]
    def med(k, rs):
        v = [r[k] for r in rs if r[k] is not None]; return st.median(v) if v else None
    tgt0 = med('target', pre)
    for gi, grp in enumerate(groups, 1):
        s0, s1 = grp[0]['unix_s'], grp[-1]['unix_s']
        print(f"\nepisode {gi}: t{s0 - E:+d}..{s1 - E:+d}s  peak media p95 {max(r['media_p95'] for r in grp):.0f} ms")
        if ps and pe:
            print(f"   P-start (0-6 s after probe_start): onset {s0 - ps:+.1f}s -> {'PASS' if 0 <= s0 - ps <= 6 else 'FAIL'}")
            print(f"   P-none  (inside [start, end+10]):  {'PASS' if ps - 1 <= s0 and s1 <= pe + 10 else 'FAIL'}")
        cut = next((r for r in rows if s0 <= r['unix_s'] <= s0 + 3 and r['target'] is not None and tgt0 and r['target'] < 0.7 * tgt0), None)
        print(f"   P-cut   (target < 70% of {tgt0:.2f} Mbps within 3 s): {'PASS at t' + format(cut['t_rel'], '+d') + 's' if cut else 'FAIL'}" if tgt0 else "   P-cut: no pre-probe target")
        win = [r for r in rows if s0 - 1 <= r['unix_s'] <= min(s1, s0 + 15)]
        def rise(k):
            b, e = med(k, pre), (max((r[k] for r in win if r[k] is not None), default=None))
            return (b, e, (e - b) if (b is not None and e is not None) else None)
        print("   where (pre-probe median -> episode max):")
        for k, label in (('dlf_mac', 'A modem NR MAC-range recs/s'), ('q_backlog', 'A qdisc backlog pkts'), ('hop3_p50', 'A hop 3 RTT ms'), ('a_sfu16_p50', 'A->SFU media node RTT ms'),
                         ('a_sfu21_p50', 'A->SFU ingress RTT ms'), ('b_sfu16_p50', 'B->SFU media node RTT ms'), ('media_p95', 'media delay p95 ms')):
            b, e, dlt = rise(k)
            print(f"      {label:28s} {('%.1f' % b) if b is not None else 'n/a':>8} -> {('%.1f' % e) if e is not None else 'n/a':>8}   rise {('%+.1f' % dlt) if dlt is not None else 'n/a'}")
        hb, he, hd = rise('hop3_p50'); ab, ae, ad = rise('a_sfu16_p50'); bb, be, bd = rise('b_sfu16_p50'); qb, qe, qd = rise('q_backlog')
        big = lambda base, d: d is not None and base is not None and (d >= 30 or d >= 0.5 * base)
        if qd is not None and qe and qe > 50: where = 'Host A kernel queue (wwan0 qdisc backlog)'
        elif big(hb, hd): where = 'before hop 3: UE uplink buffer / RAN / transport (hop-3 RTT rose with the media delay)'
        elif hd is not None and big(ab, ad): where = 'between hop 3 and the SFU (hop 3 flat, A->SFU echo rose)'
        elif hd is None and big(ab, ad): where = 'on A\'s path to the SFU (A->SFU echo rose; hop 3 had no replies in the window, so before/after hop 3 is unresolved)'
        elif not big(ab, ad) and not big(bb, bd): where = 'not a shared-path queue: A->SFU and B->SFU echo stayed flat'
        else: where = 'unresolved'
        print(f"   P-where: {where}")

if __name__ == '__main__':
    main()
