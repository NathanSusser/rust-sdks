#!/usr/bin/env python3
"""Split a late-then-burst frame skip at Host B into "upstream" vs "B receive path".

A missing frame N at B has a signature: frame N-1 arrives ~50 ms late, N and N+1 land
right behind it, and the renderer shows only the newest. B decoded all of them. The
question is where the ~50 ms was added. B's wwan0 rx_packets counter, sampled at ~5 ms
on the same CLOCK_REALTIME as webrtc_receive_timestamp_us, answers the host half:

  counter quiet through the stall, then jumps at N-1 -> packets had not reached B's
                                                        driver: upstream (network, B's
                                                        modem DL, or USB batching)
  counter advancing normally through the stall       -> packets were on B already:
                                                        B's receive path

The sampler may write rows only when the counter changes (plus a 1 s heartbeat), so a
quiet stretch is a GAP between rows. Every verdict is printed next to a control: the same
measurement over ordinary on-time frames.

Usage: nicrx_stall_test.py <subscriber.csv> <nicrx.csv> [--late-ms 55] [--burst-ms 25]
                           [--interval-ms 5] [--all-late]
"""
import argparse, bisect, csv, statistics, sys


def load_nic(path):
    with open(path) as f:
        rows = [r for r in csv.reader(f) if r and not r[0].startswith('#')]
    hdr = [h.strip().lower() for h in rows[0]]
    ti = next(i for i, h in enumerate(hdr) if h.endswith('_us') or h.endswith('_ms') or h.startswith('unix') or h.startswith('t'))
    pi = hdr.index('rx_packets')
    scale = 1.0 if hdr[ti].endswith('_us') else 1000.0
    out = []
    for r in rows[1:]:
        try:
            t = float(r[ti]) * scale
            if t < 1e14:  # seconds, not ms/us
                t = float(r[ti]) * 1e6
            out.append((t, int(r[pi])))
        except (ValueError, IndexError):
            continue
    return out


def load_sub(path):
    frames = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                frames.append((int(r['frame_id']), float(r['webrtc_receive_timestamp_us']),
                               float(r['exposure_to_receive_ms'])))
            except (KeyError, ValueError, TypeError):
                continue
    return frames


def window_stats(nic, ts, w0, w1, interval_ms, rate=0.0):
    """(packets, longest quiet ms) over [w0, w1].

    A stop is not ended by a single stray packet. At 10 Mbps a real 80 ms downlink stop
    arrived as two 40 ms gaps split by one packet, which an exact-equality rule scored as
    two short quiets and called "unclear" (frame 1558, 2026-09-15). A gap counts as quiet
    when it carries less than 15% of the packets the link would deliver in that time, so
    the tolerance scales with bitrate and leaves low-rate cells judged as before.
    """
    j0, j1 = bisect.bisect_left(ts, w0), bisect.bisect_right(ts, w1)
    win = nic[max(j0 - 1, 0):min(j1 + 1, len(nic))]
    if len(win) < 2:
        return None
    got = win[-1][1] - win[0][1]
    quiet = run = 0.0
    for k in range(1, len(win)):
        gap = (win[k][0] - win[k - 1][0]) / 1000
        d = win[k][1] - win[k - 1][1]
        idle = d == 0 or (rate > 0 and d < 0.15 * rate * gap / 1000)
        if idle:
            run += gap; quiet = max(quiet, run)
        else:
            quiet = max(quiet, run + gap - interval_ms); run = 0.0
    return got, quiet


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else float('nan')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('subscriber')
    ap.add_argument('nicrx')
    ap.add_argument('--late-ms', type=float, default=55.0)
    ap.add_argument('--burst-ms', type=float, default=25.0)
    ap.add_argument('--interval-ms', type=float, default=5.0, help='sampler target period')
    ap.add_argument('--all-late', action='store_true', help='test every late-then-burst N-1, not only missing frames')
    a = ap.parse_args()

    nic = load_nic(a.nicrx)
    sub = load_sub(a.subscriber)
    if len(nic) < 100 or len(sub) < 100:
        sys.exit(f"too little data: nic={len(nic)} frames={len(sub)}")
    ts = [t for t, _ in nic]
    base = statistics.median(e for _, _, e in sub)
    span_s = (nic[-1][0] - nic[0][0]) / 1e6
    rate = (nic[-1][1] - nic[0][1]) / span_s if span_s > 0 else 0
    print(f"nic rows={len(nic)}  rx rate={rate:.0f} pkt/s  exposure_to_receive p50={base:.1f} ms")

    # Control: ordinary on-time frames, same measurement over a 50 ms window before receive.
    ctl_q, ctl_p = [], []
    for i in range(50, len(sub), 7):
        fid, rt, e = sub[i]
        if abs(e - base) <= 5:
            s = window_stats(nic, ts, rt - 50000, rt, a.interval_ms, rate)
            if s:
                ctl_p.append(s[0]); ctl_q.append(s[1])
    print(f"control (on-time frames, 50 ms before receive, n={len(ctl_q)}): packets p10/p50 "
          f"{pct(ctl_p, .1)}/{pct(ctl_p, .5)}  longest quiet p50/p90/p99 "
          f"{pct(ctl_q, .5):.0f}/{pct(ctl_q, .9):.0f}/{pct(ctl_q, .99):.0f} ms")
    q_hi = pct(ctl_q, .99)

    verdicts = {'upstream': 0, 'B-host': 0, 'unclear': 0}
    for i in range(1, len(sub)):
        (pid, prt, pe), (nid, nrt, _) = sub[i - 1], sub[i]
        if pid < 50:
            continue
        missing = nid == pid + 2
        burst = pe > a.late_ms and (nrt - prt) / 1000 < a.burst_ms
        if not (missing or (a.all_late and burst)):
            continue
        tag = f"miss {pid + 1}" if missing else f"late {pid}"
        if not burst:
            print(f"{tag}: not late-then-burst (N-1 exp2recv {pe:.0f} ms, next gap {(nrt - prt) / 1000:.0f} ms)")
            continue
        stall_ms = pe - base
        w0, w1 = prt - stall_ms * 1000, prt - 3000  # stop short of N-1's own packets
        j0, j1 = bisect.bisect_left(ts, w0), bisect.bisect_right(ts, w1)
        gaps = [(nic[k][0] - nic[k - 1][0]) / 1000 for k in range(max(j0, 1), min(j1 + 2, len(nic)))]
        s = window_stats(nic, ts, w0, w1, a.interval_ms, rate)
        if not s:
            verdicts['unclear'] += 1; print(f"{tag}: no samples in stall window"); continue
        if gaps and max(gaps) > 150:  # sampler itself paused: a gap that long says nothing
            verdicts['unclear'] += 1; print(f"{tag}: sampler gap {max(gaps):.0f} ms overlaps window"); continue
        got, quiet = s
        expect = rate * (w1 - w0) / 1e6
        if quiet >= 0.6 * (stall_ms - 3) and quiet > q_hi:
            v = 'upstream'
        elif got >= 0.5 * expect and quiet <= q_hi:
            v = 'B-host'
        else:
            v = 'unclear'
        verdicts[v] += 1
        print(f"{tag}: stall ~{stall_ms:.0f} ms  rx in window {got} (expected {expect:.0f})  "
              f"longest quiet {quiet:.0f} ms (control p99 {q_hi:.0f})  -> {v}")
    print("verdicts:", verdicts)


if __name__ == '__main__':
    main()
