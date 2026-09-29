#!/usr/bin/env python3
"""Score candidate NTP servers and estimators for clock-offset.py. Run before changing SERVERS.

  ntp-server-check.py [runs]          default 30

WHY THIS EXISTS. The server list in clock-offset.py was chosen by measurement, not by
reputation, and it must be rechosen the same way. Two findings from 2026-09-22 that only
measurement would have produced:

  * pool.ntp.org is by far the worst server on BOTH hosts (A: 23 ms range; B: 16.9 ms stdev,
    3-4x the next worst). It resolves to a different server on every lookup, so it
    contributes a fresh network path each time.
  * RTT DOES NOT PREDICT STABILITY. time.apple.com is the slowest server on both hosts
    (~90 ms) and the steadiest on both. time.nist.gov is slow AND worst-but-one. So do not
    select a server, or an estimator, on round trip -- score on repeatability, which is the
    thing we actually care about.

A least-RTT estimator looked 3x better than the median on Host A and only ~10% better on
Host B. The cause was not the estimator: on A one server won the RTT race 28 times in 30,
so least-RTT was really "always ask the same server", and that consistency -- not the low
RTT -- removed the between-server bias. On B no server dominates, so the gain vanished, and
pool.ntp.org won the race 3 times in 12, meaning least-RTT actively selected the server we
were trying to drop. MEASURE ON BOTH HOSTS BEFORE CHANGING THE SET; a result from one path
does not transfer.

READ THE us/s COLUMN AS NOTHING. It is the slope of the common-mode fit, printed only so the
detrending is auditable. Over a ~60 s window it came out at 59.6, 9.0 and -6.2 us/s for
different estimators on one host against a known drift of ~32 us/s -- inconsistent in SIGN,
so the window carries no drift information whatever. The fit is legitimate as common-mode
removal because it is the same subtraction for every candidate; it is not a drift
measurement and must never be quoted as one. To measure drift, compare offsets hours apart.

COMPARE ON STDEV, NEVER ON RANGE. Range can only grow as samples are added, so ranges from
different n are not comparable at all.
"""
import socket, struct, sys, time
import statistics as st

CANDIDATES = ("time.google.com", "time.cloudflare.com", "time.apple.com",
              "time.nist.gov", "pool.ntp.org")
SETS = {
    "median of 3 (shipped)": ("time.google.com", "time.cloudflare.com", "time.apple.com"),
    "median of 3 (+nist)":   ("time.google.com", "time.cloudflare.com", "time.nist.gov"),
    "median of 2 (no 3rd)":  ("time.google.com", "time.cloudflare.com"),
    "median of 3 (w/ pool)": ("time.google.com", "time.cloudflare.com", "pool.ntp.org"),
}

def query(host):
    """(host_minus_utc_ms, rtt_ms) or None."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        t0 = time.time()
        s.sendto(b"\x1b" + 47 * b"\0", (host, 123))
        data, _ = s.recvfrom(48)
        t3 = time.time()
        s.close()
        t1 = struct.unpack("!I", data[32:36])[0] + struct.unpack("!I", data[36:40])[0] / 2**32 - 2208988800
        t2 = struct.unpack("!I", data[40:44])[0] + struct.unpack("!I", data[44:48])[0] / 2**32 - 2208988800
        return -((t1 - t0) + (t2 - t3)) / 2 * 1000, (t3 - t0) * 1000
    except Exception:
        return None

N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
runs = []
t_start = time.time()
for _ in range(N):
    r = {h: v for h in CANDIDATES if (v := query(h))}
    if r:
        runs.append((time.time() - t_start, r))
    time.sleep(1.5)
if len(runs) < 5:
    print("too few successful runs to score anything", file=sys.stderr)
    sys.exit(1)

print(f"n={len(runs)} runs over {runs[-1][0]:.0f}s\n")
print(f"{'server':<22}{'answered':>9}{'stdev_ms':>10}{'medRTT_ms':>11}")
for h in CANDIDATES:
    v = [r[h][0] for _, r in runs if h in r]
    rt = [r[h][1] for _, r in runs if h in r]
    if len(v) > 1:
        print(f"{h:<22}{len(v):>4}/{len(runs):<4}{st.stdev(v):>10.2f}{st.median(rt):>11.1f}")

print(f"\n{'estimator':<24}{'stdev_ms':>10}{'detrended':>11}{'(us/s: ignore)':>16}")
for name, srvs in SETS.items():
    xs, ys = [], []
    for t, r in runs:
        av = [r[h][0] for h in srvs if h in r]
        if av:
            xs.append(t); ys.append(st.median(av))
    if len(ys) < 3:
        continue
    mx, my = st.mean(xs), st.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0
    res = [y - (my + b * (x - mx)) for x, y in zip(xs, ys)]
    print(f"{name:<24}{st.stdev(ys):>10.2f}{st.stdev(res):>11.2f}{b*1e3:>16.1f}")
print("\nthe us/s column is the common-mode slope, printed for audit only -- it is NOT a")
print("drift measurement and is not consistent even in sign over a window this short.")
