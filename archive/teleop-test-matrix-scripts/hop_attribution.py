#!/usr/bin/env python3
"""Place every uplink loss or rate collapse at the first hop where it appears.

Joins, per second on one UTC clock (both hosts are PTP-disciplined):

  A app      harness jsonl   requested (target) bitrate, bytes/packets sent
  A frames   pub.csv         frames captured/packetized by frame_id
  A queue    hops.csv        fq_codel drops / backlog / requeues on wwan0
  A modem    hops.csv        QMI TX dropped (host->modem handoff), 5G RSRP/SNR
  A wire     wwan0.pcap      UDP packets actually handed to the modem, per second
  A stack    .dlf            5G MAC-range record rate (scheduler activity)
  B frames   subscriber.csv  frames received by frame_id, lost packets, latency

Every input is optional; hops that were not recorded are reported as unknown,
never as clean. The verdict names the FIRST hop with evidence. It deliberately
stops at "after Host A's modem" when the evidence ends there: separating the
modem's own PDCP discard from the radio link and the network needs decoded
PDCP/MAC records (QCAT) or server-side stats, and this script does not guess.

Usage:
  hop_attribution.py --cap-kbps 2000 --jsonl A.jsonl [--pubcsv A.pub.csv]
      [--hops A.hops.csv] [--pcap A.wwan0.pcap] [--dlf A.dlf]
      [--b-subcsv B/subscriber.csv] [--out timeline.csv]
"""
import argparse, csv, json, math, struct, subprocess, sys, time
from collections import Counter, defaultdict

def utc(s): return time.strftime('%H:%M:%S', time.gmtime(s))

def load_jsonl(path):
    rows = []
    for line in open(path):
        try: d = json.loads(line)
        except Exception: continue
        v = d.get('video_out')
        if isinstance(v, dict) and v.get('frames_encoded'): rows.append((d['t_unix_us'] / 1e6, v))
    out = {}
    for (t0, a), (t1, b) in zip(rows, rows[1:]):
        dt = t1 - t0
        if dt <= 0: continue
        out[int(t1)] = dict(target_mbps=b.get('target_bitrate_bps', 0) / 1e6,
                            sent_mbps=(b['bytes_sent'] - a['bytes_sent']) * 8 / 1e6 / dt,
                            pkts_sent=(b['packets_sent'] - a['packets_sent']) / dt,
                            retx=b['retransmitted_packets_sent'] - a['retransmitted_packets_sent'],
                            nack=b['nack_count'] - a['nack_count'])
    return out

def load_pub(path):
    """Frames the publisher actually handed to WebRTC's packetizer, per capture second.

    A captured frame with no packetize timestamp never left Host A: the encoder or
    pacer dropped it. Counting it as sent would blame that drop on the network."""
    sent = Counter(); unsent = Counter(); ids = {}
    for r in csv.DictReader(open(path)):
        try:
            ts = int(r['capture_timestamp_us']); fid = int(r['frame_id'])
        except (KeyError, ValueError, TypeError): continue
        if ts <= 0: continue
        s = ts // 1_000_000
        try: pk = int(r.get('webrtc_packetize_timestamp_us') or 0)
        except ValueError: pk = 0
        if pk > 0: sent[s] += 1; ids[fid] = s
        else: unsent[s] += 1
    return sent, unsent, ids

def load_b(path, a_ids):
    got = Counter(); lat = defaultdict(list); lost = {}; prev = None
    for r in csv.DictReader(open(path)):
        try:
            fid = int(r['frame_id']); cap = int(r['capture_timestamp_us']); rx = int(r['webrtc_receive_timestamp_us'])
        except (KeyError, ValueError, TypeError): continue
        s = a_ids.get(fid, cap // 1_000_000)
        got[s] += 1
        if cap > 0 and rx > cap: lat[s].append((rx - cap) / 1000)
        try:
            pl = int(float(r['packets_lost']))
            rs = rx // 1_000_000
            if prev is not None and pl > prev: lost[rs] = lost.get(rs, 0) + pl - prev
            prev = pl
        except (KeyError, ValueError, TypeError): pass
    return got, lat, lost

def load_hops(path):
    rows = list(csv.DictReader(open(path))); out = {}
    for a, b in zip(rows, rows[1:]):
        def d(k):
            try: return int(b[k]) - int(a[k])
            except (ValueError, KeyError): return None
        def f(k):
            try: return float(b[k])
            except (ValueError, KeyError): return None
        out[int(b['unix_ms']) // 1000] = dict(q_drop=d('qdisc_dropped'), q_requeue=d('qdisc_requeues'),
            q_backlog=f('qdisc_backlog_pkts'), sys_drop=d('sys_tx_dropped'), qmi_tx_ok=d('qmi_tx_ok'),
            qmi_tx_drop=d('qmi_tx_dropped'), rsrp=f('nr_rsrp_dbm'), snr=f('nr_snr_db'))
    return out

def load_pcap(path):
    p = subprocess.run(['tshark', '-r', path, '-T', 'fields', '-e', 'frame.time_epoch', '-e', 'ip.dst', '-Y', 'udp'],
                       capture_output=True, text=True)
    by_dst = Counter(); rows = []
    for line in p.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) < 2 or not parts[1]: continue
        t, dst = float(parts[0]), parts[1]; by_dst[dst] += 1; rows.append((int(t), dst))
    if not rows: return {}, None
    top = by_dst.most_common(1)[0][0]
    per = Counter(s for s, d in rows if d == top)
    return per, top

def load_dlf(path):
    import sys as _s, os as _o
    _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), '../../diag-capture'))
    from dlf_records import iter_file
    mac = Counter()
    for lid, t, _ln in iter_file(path):
        if 0xB880 <= lid <= 0xB8BF: mac[int(t)] += 1
    return mac

def pct(v, p):
    if not v: return None
    v = sorted(v); return v[min(len(v) - 1, int(p * len(v)))]

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cap-kbps', type=float, required=True)
    ap.add_argument('--jsonl', required=True); ap.add_argument('--pubcsv'); ap.add_argument('--hops')
    ap.add_argument('--pcap'); ap.add_argument('--dlf'); ap.add_argument('--b-subcsv'); ap.add_argument('--out')
    a = ap.parse_args(); cap = a.cap_kbps / 1000

    app = load_jsonl(a.jsonl)
    sent, unsent, ids = load_pub(a.pubcsv) if a.pubcsv else ({}, {}, {})
    got, lat, blost = load_b(a.b_subcsv, ids) if a.b_subcsv else ({}, {}, {})
    hops = load_hops(a.hops) if a.hops else {}
    wire, wire_dst = load_pcap(a.pcap) if a.pcap else ({}, None)
    mac = load_dlf(a.dlf) if a.dlf else {}

    secs = sorted(app)
    if not secs: sys.exit('no publisher samples in jsonl')
    # Ignore the first 10 s (encoder ramps from its start bitrate) and the last 3 s
    # (frames still in flight when the stream stops look like loss at the receiver).
    steady = [s for s in secs if secs[0] + 10 <= s <= secs[-1] - 3]
    base_wire = pct([wire.get(s, 0) for s in steady], 0.5) or 0
    base_mac = pct([mac.get(s, 0) for s in steady], 0.5) or 0
    base_snr = pct([hops[s]['snr'] for s in steady if s in hops and hops[s]['snr'] is not None], 0.5)

    cols = ['utc', 'unix_s', 'target_mbps', 'sent_mbps', 'a_frames', 'a_unsent', 'b_frames', 'frames_missing', 'b_lost_pkts',
            'lat_p50_ms', 'lat_p95_ms', 'wire_pkts', 'q_drop', 'q_requeue', 'q_backlog', 'qmi_tx_drop', 'rsrp', 'snr',
            'mac_recs', 'retx', 'nack', 'first_hop']
    rows = []
    for s in steady:
        ap_ = app[s]; h = hops.get(s, {})
        missing = (sent.get(s, 0) - got.get(s, 0)) if (sent and got) else None
        r = dict(utc=utc(s), unix_s=s, target_mbps=round(ap_['target_mbps'], 3), sent_mbps=round(ap_['sent_mbps'], 3),
                 a_frames=sent.get(s) if sent else None, a_unsent=unsent.get(s, 0) if a.pubcsv else None,
                 b_frames=got.get(s) if got else None,
                 frames_missing=missing, b_lost_pkts=blost.get(s, 0) if a.b_subcsv else None,
                 lat_p50_ms=pct(lat.get(s), .5), lat_p95_ms=pct(lat.get(s), .95),
                 wire_pkts=wire.get(s, 0) if a.pcap else None, q_drop=h.get('q_drop'), q_requeue=h.get('q_requeue'),
                 q_backlog=h.get('q_backlog'), qmi_tx_drop=h.get('qmi_tx_drop'), rsrp=h.get('rsrp'), snr=h.get('snr'),
                 mac_recs=mac.get(s, 0) if a.dlf else None, retx=ap_['retx'], nack=ap_['nack'])
        collapse = ap_['target_mbps'] < 0.5 * cap
        loss = (missing or 0) > 1 or (r['b_lost_pkts'] or 0) > 0
        a_dropped = a.pubcsv and unsent.get(s, 0) > 1
        local = '' if a.hops else ' (A queue/modem counters not recorded)'
        snr_note = (' -- 5G SNR dropped' if base_snr is not None and h.get('snr') is not None and h['snr'] < base_snr - 6 else '')
        hop = ''
        if collapse or loss:
            if a.hops and ((h.get('q_drop') or 0) > 0 or (h.get('q_backlog') or 0) > 50):
                hop = 'A kernel queue (wwan0 fq_codel drops/backlog)'
            elif a.hops and (h.get('qmi_tx_drop') or 0) > 0:
                hop = 'A modem handoff (QMI TX dropped)'
            elif collapse and not loss:
                hop = ('A sender lowered its rate' + (' with no local drops' if a.hops else local)
                       + (' -- scheduler activity also fell' if a.dlf and base_mac and mac.get(s, 0) < 0.6 * base_mac else '')
                       + snr_note)
            elif a.pcap and base_wire and wire.get(s, 0) < 0.6 * base_wire and not collapse:
                hop = 'A app->wire (packets sent by WebRTC did not reach wwan0)'
            elif loss and (r['b_lost_pkts'] or 0) == 0 and a_dropped:
                hop = 'A sender dropped frames before sending (encoder/pacer; no packets lost in transit)'
            elif loss:
                hop = ('after A modem: modem PDCP discard, radio, network or SFU->B'
                       + ('' if a.pcap else ' (no A pcap: cannot rule out A host)') + snr_note)
        elif a_dropped:
            hop = 'A sender dropped frames before sending (encoder/pacer)' 
        r['first_hop'] = hop
        rows.append(r)

    if a.out:
        with open(a.out, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)

    recorded = [n for n, v in (('A app', True), ('A frames', a.pubcsv), ('A queue/modem', a.hops), ('A wire', a.pcap),
                               ('A modem log', a.dlf), ('B frames', a.b_subcsv)) if v]
    print(f"window {utc(steady[0])}-{utc(steady[-1])} UTC ({len(steady)} s steady)  cap {cap:g} Mbps")
    print("recorded hops:", ", ".join(recorded))
    print("NOT recorded :", ", ".join(n for n, v in (('A queue/modem', a.hops), ('A wire', a.pcap), ('A modem log', a.dlf),
                                                   ('B frames', a.b_subcsv)) if not v) or "none")
    if wire_dst: print(f"wire: UDP to {wire_dst}, median {base_wire} pkt/s")
    ev = []; cur = None
    for r in rows:
        if r['first_hop']:
            if cur and cur['hop'] == r['first_hop'] and r['unix_s'] == cur['end'] + 1: cur['end'] = r['unix_s']; cur['n'] += 1
            else: cur = dict(hop=r['first_hop'], start=r['unix_s'], end=r['unix_s'], n=1); ev.append(cur)
    tot_missing = sum(r['frames_missing'] or 0 for r in rows if (r['frames_missing'] or 0) > 0)
    tot_lost = sum(r['b_lost_pkts'] or 0 for r in rows)
    print(f"collapse seconds (target < {0.5*cap:g} Mbps): {sum(1 for r in rows if r['target_mbps'] < 0.5*cap)}")
    if a.pubcsv: print(f"frames captured on A but never packetized: {sum(unsent.get(s, 0) for s in steady)}")
    if a.b_subcsv: print(f"frames missing at B: {tot_missing}   packets lost at B: {tot_lost}")
    if not ev: print("verdict: no collapse and no loss in the steady window")
    for e in ev: print(f"  {utc(e['start'])}-{utc(e['end'])} ({e['n']} s): {e['hop']}")

if __name__ == '__main__':
    main()
