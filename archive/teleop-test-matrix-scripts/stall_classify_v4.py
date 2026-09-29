#!/usr/bin/env python3
"""Classify late frames as held upstream of Host B, inside Host B, or unclear.

THE DEFINITION, agreed between Host A and Host B on 2026-09-16 after nine defects and
conventions were found by each host reading the other's numbers. Every clause is here
because getting it wrong changed the answer:

  1. window            [receive - 200 ms, receive + 30 ms]
  2. edge clipping     gaps clipped at BOTH window edges: start=max(row,lo), end=min(row,hi).
                       Unclipped, a gap starting before the window counts at full length and
                       inflates silence exactly where the verdicts are decided. The row
                       STRADDLING hi is read and truncated, not dropped: a truncated
                       observation is still an observation. Settled with Host B 2026-09-17
                       after the two implementations were diffed cell by cell; see
                       silence() for why the slice and the clip must change together.
  3. no subtraction    the gap is taken at face value; no sample-interval correction. It
                       cancels against the control, and subtracting implies precision the
                       5 ms sampling cannot deliver.
  4. tie-break         the LATEST qualifying gap wins, since the question is always
                       "did the silence end at this frame".
  5. sign              silence_end_rel = (silence_end - receive)/1000. NEGATIVE means the
                       silence ended BEFORE the frame arrived.
  6. control + bar     control = ON-TIME frames only, |e2r - median(e2r)| <= 5 ms, stride 5
                       or denser. bar = p99 of that control by nearest-rank. An unfiltered
                       control wanders 40-55 ms with sampling because it CONTAINS the stalls
                       the bar is meant to separate; filtered, it is stable to 0.1 ms from
                       n=451 to n=10,744. A k x median bar was proposed and rejected: it has
                       no stated false-positive rate, where p99 of a clean population is by
                       construction the silence exceeded by 1% of ordinary frames.
  7. verdicts          upstream:    silence >= bar AND -60 <= silence_end_rel <= +30
                       inside-host: silence < 0.6 x bar AND packets at the expected-arrival
                                    instant >= ordinary p10
                       else:        unclear. The catch-all MUST be unclear -- failing the
                       upstream test is not evidence of a host-side hold.
  8. guard             if the ordinary expected-arrival window carries >= 40 packets, the
                       inside-host arm ABSTAINS (-> unclear). At high bitrate neighbouring
                       frames fill the window and the test misreads; it called 5 of 10
                       "inside host" on a 10 Mbps cell where the downlink had demonstrably
                       stopped.

Usage: stall_classify_v4.py <label> <subscriber.csv> <nicrx.csv> [--csv out.csv]
"""
import argparse, bisect, csv, statistics, sys

WIN_LO_US, WIN_HI_US = -200_000, 30_000
EXP_LO_US, EXP_HI_US = -10_000, 15_000
BAR_STRIDE = 5
INSIDE_FRAC = 0.6
GUARD_PACKETS = 40


def load_nic(path):
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                out.append((float(r['unix_us']), int(r['rx_packets'])))
            except (KeyError, ValueError, TypeError):
                continue
    out.sort()
    return out


def load_sub(path):
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                out.append((int(r['frame_id']), float(r['webrtc_receive_timestamp_us']),
                            float(r['exposure_to_receive_ms']), float(r['capture_timestamp_us'])))
            except (KeyError, ValueError, TypeError):
                continue
    return out


def silence(nic, ts, rt):
    """Clause 1-5: longest clipped gap in the window, latest on ties.

    THE SLICE AND THE CLIP ARE A PAIR -- do not change one without the other. `j + 1`
    deliberately reaches ONE ROW PAST hi so that a gap straddling the aperture edge is
    seen at all, and min(w[k][0], hi) then truncates it to the part actually observed.
    Keep the reach and drop the clip and you measure silence that extends past the window
    -- a third rule that is neither this one nor Host B's, and it inflates the CONTROL as
    well as the stalls, so it moves the bar itself (predicted 41.10 -> 49.99 ms on c053).
    A reads the straddling row and truncates; B originally bounded its loop at j and never
    saw the row, which is the more conservative rule but discards a real observation right
    at the edge where the silence we care about is most likely to be cut off. Measured
    2026-09-17 across c049/c050/c052/c053/depal-8mbps/depal-2mbps: the two differed on 1
    of 264 stalled frames; B adopted this convention and they now agree on all six.
    """
    lo, hi = rt + WIN_LO_US, rt + WIN_HI_US
    i, j = bisect.bisect_left(ts, lo), bisect.bisect_right(ts, hi)
    w = nic[max(i - 1, 0):j + 1]
    best, end = 0.0, None
    for k in range(1, len(w)):
        s, e = max(w[k - 1][0], lo), min(w[k][0], hi)
        if e <= s:
            continue
        gap = (e - s) / 1000
        if gap >= best:                      # >= keeps the LATEST on ties
            best, end = gap, e
    return best, ((end - rt) / 1000 if end is not None else float('nan'))


def packets_at_expected(nic, ts, cap_us, baseline_us):
    """Clause 7 inside-host arm: packets delivered around the expected arrival instant."""
    exp = cap_us + baseline_us
    i = bisect.bisect_left(ts, exp + EXP_LO_US)
    j = bisect.bisect_right(ts, exp + EXP_HI_US)
    w = nic[max(i - 1, 0):j + 1]
    return (w[-1][1] - w[0][1]) if len(w) >= 2 else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('label')
    ap.add_argument('subscriber')
    ap.add_argument('nicrx')
    ap.add_argument('--csv')
    a = ap.parse_args()

    nic = load_nic(a.nicrx)
    sub = load_sub(a.subscriber)
    if len(nic) < 100 or len(sub) < 100:
        sys.exit(f"{a.label}: too little data (nic={len(nic)} sub={len(sub)})")
    ts = [t for t, _ in nic]
    base_ms = statistics.median(e for _, _, e, _ in sub)
    baseline_us = base_ms * 1000

    # Clause 6: control = on-time frames at stride 5+, bar = p99 nearest-rank.
    ctl = sorted(silence(nic, ts, rt)[0] for _, rt, e, _ in sub[::BAR_STRIDE] if abs(e - base_ms) <= 5)
    ctl_median = statistics.median(ctl)
    bar = ctl[-(-int(len(ctl) * 99) // 100) - 1]

    # Clause 7/8 reference: ordinary frames' packet count at their own expected instant.
    ordinary = sorted(packets_at_expected(nic, ts, c, baseline_us)
                      for _, _, e, c in sub if abs(e - base_ms) <= 5)
    ord_p10 = ordinary[len(ordinary) // 10] if ordinary else 0
    ord_med = ordinary[len(ordinary) // 2] if ordinary else 0
    guard_tripped = ord_med >= GUARD_PACKETS

    stalled = [(f, rt, e, c) for (f, rt, e, c), (_, nrt, _, _) in zip(sub, sub[1:])
               if e >= 2.5 * base_ms and (nrt - rt) / 1000 <= 25]

    rows, n = [], {'upstream': 0, 'inside-host': 0, 'unclear': 0}
    for f, rt, e, c in stalled:
        sil, rel = silence(nic, ts, rt)
        pk = packets_at_expected(nic, ts, c, baseline_us)
        if sil >= bar and -60 <= rel <= 30:
            v = 'upstream'
        elif not guard_tripped and sil < INSIDE_FRAC * bar and pk >= ord_p10:
            v = 'inside-host'
        else:
            v = 'unclear'
        n[v] += 1
        rows.append([f, f"{e:.1f}", f"{sil:.1f}", f"{rel:+.1f}", pk, v])

    sils = sorted(float(r[2]) for r in rows)   # r[2] is a formatted string: float() or this sorts lexicographically
    smed = sils[len(sils) // 2] if sils else float('nan')
    overlap = smed < 1.5 * bar
    print(f"{a.label}")
    print(f"  control n={len(ctl)} median {ctl_median:.2f} ms  ->  bar = p99 nearest-rank = {bar:.2f} ms")
    print(f"  ordinary expected-window packets: p10 {ord_p10} median {ord_med}"
          f"  -> inside-host arm {'ABSTAINS (clause 8 guard)' if guard_tripped else 'usable'}")
    print(f"  stalled frames {len(stalled)}, stall silence median {smed:.1f} ms")
    print(f"  verdicts: upstream {n['upstream']}  inside-host {n['inside-host']}  unclear {n['unclear']}")
    print(f"  publishable split? {'NO - stall median within 1.5x bar, distributions overlap' if overlap else 'yes'}")
    if a.csv:
        with open(a.csv, 'w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(['frame_id', 'e2r_ms', 'longest_strict_silence_ms',
                        'silence_end_rel_receive_ms', 'packets_at_expected', 'verdict'])
            w.writerows(rows)


if __name__ == '__main__':
    main()
