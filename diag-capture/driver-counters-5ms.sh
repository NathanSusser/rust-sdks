#!/usr/bin/env bash
# Sample Host A's uplink driver counters finely enough to see the requeue spikes.
#
# WHY. On 2026-09-16 A's modem refused packets at up to 1935/s while the kernel queue
# stayed empty, at 2 Mbps, with no upload running -- 7 of 15 such spikes within 300 ms of a
# delayed frame, in windows covering 8% of the run. The existing qdisc sampler runs at
# ~8.5 Hz, which is far too coarse for a sub-second event, and the modem's own DIAG logs
# cannot answer it: two raw-record scans (c049 host-side stalls, c052 path-side) found no
# change in scheduling-report ARRIVALS in the 50 ms before a late frame, and a grant that
# shrinks rather than stops would look identical in arrival counts anyway. That needs the
# record payload and therefore QCAT, which we do not have. This instrument is the cheap
# alternative: no capture, no log mask, no disk, and it tests the modem INTERFACE directly.
#
# COSTS, measured on A 2026-09-17 rather than assumed. There is no single number: this
# host runs the powersave governor, and a short subprocess burst on an OTHERWISE IDLE box
# never ramps the cores off 800 MHz, so the same call costs 4.75x what it costs during a
# run. Both figures below are this host, this binary, minutes apart:
#                                    idle, ~800 MHz      loaded, ~5.5 GHz
#   /sys statistics read, in-process     0.016 ms            0.019 ms
#   /proc/net/dev read, in-process       0.062 ms                -
#   /bin/true (fork+exec floor)          0.83  ms            0.41  ms
#   tc -s qdisc show, subprocess         5.32  ms            1.12  ms
#   tc -V (starts up, no netlink)        4.83  ms                -
# Read the last two together: ~90% of tc's cost is process startup and only ~0.5 ms is the
# qdisc read, which is why the in-process reads barely move between the two states while
# everything that forks moves by 4-5x. An earlier note in this file gave tc as 1.41 ms flat;
# that was the loaded state measured without knowing it was a state. (The older
# qdisc-10hz.sh claim that "tc itself costs ~40 ms" is wrong in the other direction: 4.3 ms
# from a bash loop was that loop's fork cost, not tc's.)
#
# So: tx counters every 5 ms from /proc/net/dev with no fork, and requeues every 20 ms via
# tc. The 20 ms interval is set to survive the WORST state, not the state a benchmark
# happens to catch. Verified idle and unboosted, 2026-09-17, 10 s:
#   tc every 4 samples:  1888 samples, period p50 5.02 p95 6.59 p99 7.33 max 7.62 ms, 0 >10 ms
#   tc disabled:         2000 samples, period p50 5.00 p95 5.01 p99 5.09 max 5.20 ms, 0 >10 ms
# The cost of tc is 5.6% of sample slots lost to catch-up, with the fast counters holding
# their 5 ms cadence and no sample over 10 ms. During a cell the cores are boosted and the
# tc calls cost ~1.1 ms, so this is the pessimistic bound.
#
# DO NOT port the 20 ms to another host as a constant. Re-measure there in BOTH states
# (idle, and with cores ramped), or better, read the achieved period this script writes to
# .period.txt, which measures the real thing instead of predicting it.
#
# Rows are written only when a counter CHANGES, plus a 1 Hz heartbeat -- the same shape as
# B's nicrx.csv, so the same readers work and a quiet stretch is a gap between rows.
#
# Usage: driver-counters-5ms.sh <duration_s> <out.csv> [iface=wwan0] [tc_every_n=4]
set -u
dur=${1:?usage: $0 <duration_s> <out.csv> [iface] [tc_every_n]}
out=${2:?usage: $0 <duration_s> <out.csv> [iface] [tc_every_n]}
IF=${3:-wwan0}
TC_EVERY=${4:-4}

python3 - "$dur" "$out" "$IF" "$TC_EVERY" <<'PY'
import sys, time, subprocess, re
dur, out, iface, tc_every = float(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
INTERVAL = 0.005

req_re = re.compile(r'requeues (\d+)')
back_re = re.compile(r'backlog (\d+)b (\d+)p')

def tc_stats():
    try:
        s = subprocess.run(['tc', '-s', 'qdisc', 'show', 'dev', iface],
                           capture_output=True, text=True, timeout=1).stdout
        r = req_re.search(s); b = back_re.search(s)
        return (int(r.group(1)) if r else -1,
                int(b.group(2)) if b else -1,
                int(b.group(1)) if b else -1)
    except Exception:
        return (-1, -1, -1)

def dev_line():
    with open('/proc/net/dev') as f:
        for line in f:
            if line.lstrip().startswith(iface + ':'):
                p = line.replace(':', ' ').split()
                return int(p[9]), int(p[10]), int(p[12])   # tx_bytes, tx_packets, tx_drop
    return 0, 0, 0

end = time.time() + dur
last = None
last_beat = 0.0
n = 0
periods = []
prev_t = None
req = bpkts = bbytes = -1
i = 0
with open(out, 'w') as w:
    w.write('unix_us,tx_bytes,tx_packets,tx_drop,requeues,backlog_pkts,backlog_bytes\n')
    nxt = time.monotonic()
    while time.time() < end:
        t = time.time()
        txb, txp, txd = dev_line()
        if i % tc_every == 0:
            req, bpkts, bbytes = tc_stats()
        i += 1
        if prev_t is not None:
            periods.append((t - prev_t) * 1e6)
        prev_t = t
        cur = (txb, txp, txd, req, bpkts, bbytes)
        if cur != last or t - last_beat >= 1.0:
            w.write(f'{int(t*1e6)},{txb},{txp},{txd},{req},{bpkts},{bbytes}\n')
            n += 1
            last = cur
            if t - last_beat >= 1.0:
                last_beat = t
        nxt += INTERVAL
        d = nxt - time.monotonic()
        if d > 0:
            time.sleep(d)
        else:
            nxt = time.monotonic()
periods.sort()
q = lambda f: periods[min(len(periods) - 1, int(len(periods) * f))] / 1000 if periods else float('nan')
summary = (f'samples {len(periods)+1}; achieved period ms p50 {q(.5):.2f} p95 {q(.95):.2f} '
           f'p99 {q(.99):.2f} max {q(1.0):.2f}; over 10 ms: {sum(1 for x in periods if x > 10000)}; '
           f'target {INTERVAL*1000:.1f} ms; tc every {tc_every} samples; clock CLOCK_REALTIME')
open(out + '.period.txt', 'w').write(summary + '\n')
print(f'{out}: {n} rows over {dur:.0f} s; {summary}')
PY
