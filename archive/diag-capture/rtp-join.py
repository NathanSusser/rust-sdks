#!/usr/bin/env python3
"""Join Host A's and Host B's packet captures on RTP, and CONTROL the join against another cell.

WRITTEN BEFORE THE DATA EXISTS, deliberately. The recurring failure on 2026-09-17 was not bad
arithmetic -- it was correct arithmetic over a wrongly selected SET, five times in one day
between two hosts, and every instance produced a plausible number:

  Host B  averaged a 92 s burst over a 400 s window          -> "downlink idle at 0.053 Mbps"
  Host B  read a 40 s slice as the whole cell                -> "media arrived during received=0"
  Host B  filtered A to ONE 5-tuple on a cell that reconnected twice -> 84.3% instead of 100%
  Host A  selected B's OUTBOUND packets as "what B received" -> "100% loss"
  Host A  pgrep -f "[s]weep-driver" matched the shell carrying the pattern in its own argv
          -> "driver already running", refusing a sanctioned launch

No checker that validates VALUES catches any of those. What catches them is declaring the
selection before seeing the result, so this script PRINTS ITS SELECTION FIRST and cannot be
quietly retuned once a number is on screen.

WHAT THE JOIN RESTS ON, established 2026-09-17 from the first paired capture this project ever
took: the SFU re-originates RTP -- SSRC and payload type are rewritten (A sends PT 96, B receives
PT 109; SSRC sets disjoint) -- but SEQUENCE NUMBERS AND RTP TIMESTAMPS ARE PRESERVED end to end.
Two independently written implementations agreed to within one packet. So the join key is seq
(and rtp_ts as corroboration), NOT ssrc.

THREE SELECTION RULES, each one bought with a wrong answer:

 1. DIRECTION. Join A-OUTBOUND (dst = SFU) against B-INBOUND (dst = B). B's outbound is NACK/RTCP
    feedback and matching against it reports total loss on a perfectly good cell.

 2. DISTINCT SEQUENCE NUMBERS, not packets. A retransmits: the 2026-09-17 anchor put 13,116
    packets on the wire for 6,151 distinct seq -- 6,965 retransmissions. Any ratio over raw
    packet counts is inflated by that and means nothing.

 3. UNION A's FLOWS, WINDOW BY B's. A migrates to a new 5-tuple on every reconnect, so filtering
    to one of A's flows discards packets A genuinely sent. But A's SESSIONS are disjoint in time
    and a session that began after B stopped receiving was never offered to B -- so the window
    must be B's receive window, and loss is computed per A-session inside it.

THE CONTROL is the point of this script. A join that matches ~100% proves nothing on its own
unless the SAME method against a DIFFERENT cell matches ~0%. Sequence numbers are 16-bit, so a
busy cell occupies ~20% of the space and chance overlap is not negligible. Pass requires BOTH:
matched pair high AND cross-cell pair at chance or below.

Usage: rtp-join.py <cell-dir> [control-cell-dir]
"""
import collections, re, subprocess, sys

SFU_NET = '10.1.20.'


def rtp_packets(path):
    """Every RTP packet in a capture: (wire_time, src, dst, len, seq, rtp_ts, ssrc).

    RTCP (PT 200-210) and non-version-2 packets are excluded. Any line that is neither a
    well-formed header nor a hex continuation FLUSHES the buffer -- without that, a packet
    tcpdump renders unexpectedly makes the next packet's fields shift by a whole packet.
    """
    out = subprocess.run(['sudo', '-n', 'tcpdump', '-r', path, '-nn', '-tt', '-x', 'udp'],
                         capture_output=True, text=True).stdout
    hdr = re.compile(r'^(\d+\.\d+) IP (\d+\.\d+\.\d+\.\d+)\.(\d+) > '
                     r'(\d+\.\d+\.\d+\.\d+)\.(\d+): UDP, length (\d+)')
    pkts, h, cur = [], None, []

    def flush():
        nonlocal h, cur
        if h and cur:
            x = ''.join(cur)
            if len(x) >= 80:                      # 20B IP + 8B UDP + 12B RTP = 40B = 80 hex
                try:
                    b0, b1 = int(x[56:58], 16), int(x[58:60], 16)
                    if (b0 >> 6) == 2 and not (200 <= (b1 & 0x7f) <= 210):
                        pkts.append(dict(t=h[0], src=h[1], dst=h[3], ln=h[5],
                                         seq=int(x[60:64], 16), ts=int(x[64:72], 16),
                                         ssrc=int(x[72:80], 16)))
                except ValueError:
                    pass
        h, cur = None, []

    for line in out.splitlines():
        m = hdr.match(line)
        if m:
            flush()
            h = (float(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5), int(m.group(6)))
        elif re.match(r'^\s+0x', line):
            if h:
                cur.append(line.split(':', 1)[1].replace(' ', ''))
        else:
            flush()
    flush()
    return pkts


def a_pcap(cell):
    import glob
    g = [p for p in glob.glob(f'{cell}/*.wwan0.pcap')]
    return g[0] if g else None


def b_pcap(cell):
    import glob
    g = glob.glob(f'{cell}/hostb/*.pcap')
    return g[0] if g else None


def sessions(a_out):
    """A's successive publish sessions, one per SSRC, with their time ranges."""
    by = collections.defaultdict(list)
    for p in a_out:
        by[p['ssrc']].append(p)
    return sorted(((s, g) for s, g in by.items() if len(g) >= 100),
                  key=lambda kv: min(p['t'] for p in kv[1]))


def report(cell, control=None):
    ap, bp = a_pcap(cell), b_pcap(cell)
    if not ap or not bp:
        sys.exit(f"missing capture: A={ap} B={bp}")
    a = [p for p in rtp_packets(ap) if p['dst'].startswith(SFU_NET)]   # rule 1: A outbound
    b = [p for p in rtp_packets(bp) if not p['dst'].startswith(SFU_NET)]  # rule 1: B inbound
    b = [p for p in b if p['src'].startswith(SFU_NET)]

    print("SELECTION (declared before any number below):")
    print(f"  A side     : outbound only, dst in {SFU_NET}0/24, ALL flows unioned   ({len(a)} pkts)")
    print(f"  B side     : inbound only, src in {SFU_NET}0/24                       ({len(b)} pkts)")
    if not a or not b:
        sys.exit("  one side has no RTP to the SFU -- cell is single-ended, nothing to join")
    # WINDOW. Anchoring to B's inbound RTP is what Host B's draw_modem_page did with the
    # subscriber's frame span, and it fails the same way: on the cells that matter most --
    # the ones where B received almost nothing -- the window collapses toward zero, the
    # denominator with it, and the loss ratio becomes meaningless. With B receiving nothing
    # at all, min() raises before any of that. So the window comes from the CELL definition
    # (probe_start/probe_end in A's dlf-rates header) whenever that is available, and B's
    # inbound span is only a fallback. Which one was used is printed, never assumed.
    lo = hi = None
    src = "B's inbound RTP span (fallback)"
    import glob as _glob
    for rates in _glob.glob(f'{cell}/dlf-rates.csv'):
        head = open(rates).readline()
        ps = re.search(r'probe_start=([0-9.]+)', head)
        pe = re.search(r'probe_end=([0-9.]+)', head)
        if ps and pe:
            lo, hi = float(ps.group(1)), float(pe.group(1))
            src = "cell window from dlf-rates header"
    if lo is None:
        if not b:
            sys.exit("  B received no RTP and no cell window available -- cannot define a window")
        lo, hi = min(p['t'] for p in b), max(p['t'] for p in b)
    b_lo, b_hi = (min(p['t'] for p in b), max(p['t'] for p in b)) if b else (0, 0)
    print(f"  window     : {src}, {hi - lo:.1f}s")
    print(f"               (B's inbound RTP actually spanned {b_hi - b_lo:.1f}s of it)")
    print(f"  key        : distinct RTP sequence number (ssrc is rewritten by the SFU)")
    print(f"  denominator: DISTINCT seq A sent in that window, per A-session\n")

    bseq = {p['seq'] for p in b}
    print(f"B received {len(b)} packets carrying {len(bseq)} distinct seq\n")
    print("A sessions (disjoint in time = successive publishes across reconnects):")
    for s, g in sessions(a):
        t0, t1 = min(p['t'] for p in g), max(p['t'] for p in g)
        inwin = [p for p in g if lo <= p['t'] <= hi]
        print(f"  ssrc {hex(s):>12}  {len(g):6d} pkts  {t1 - t0:6.1f}s span   {len(inwin):6d} inside B's window")
        if not inwin:
            print(f"                 never offered to B -- began after B stopped receiving")
            continue
        dsent = {p['seq'] for p in inwin}
        got = dsent & bseq
        print(f"                 {len(inwin)} pkts / {len(dsent)} distinct seq -> {len(got)} reached B"
              f"  = LOSS {100.0 * (1 - len(got) / len(dsent)):.1f}%")
        print(f"                 ({len(inwin) - len(dsent)} retransmissions excluded from the denominator)")

    aseq = {p['seq'] for p in a}
    matched = 100.0 * len(aseq & bseq) / len(bseq)
    space = 100.0 * len(aseq) / 65536
    print(f"\nJOIN  matched pair : {len(aseq & bseq)}/{len(bseq)} = {matched:.1f}% of B's seq found in A")
    print(f"      chance floor : {space:.1f}%  (A occupies {len(aseq)} of 65536 seq values)")

    if control:
        cb = b_pcap(control)
        if not cb:
            print(f"\nCONTROL: {control} has no B capture -- cannot run")
            return
        cbp = [p for p in rtp_packets(cb) if p['src'].startswith(SFU_NET)]
        cbseq = {p['seq'] for p in cbp}
        if not cbseq:
            print("\nCONTROL: control cell has no B inbound RTP")
            return
        x = 100.0 * len(aseq & cbseq) / len(cbseq)
        print(f"\nCONTROL  this cell's A seq vs {control.split('/')[-1]}'s B inbound:")
        print(f"      cross pair   : {len(aseq & cbseq)}/{len(cbseq)} = {x:.1f}%")
        # VERDICT. The old rule was `matched > 90 and x < max(2 * space, 30)`, and it
        # could not fail. `space` is len(aseq)/65536, so once A emits more than 65536
        # packets the distinct-sequence set SATURATES at 100% -- and the threshold
        # becomes max(200, 30) = 200, which no percentage can exceed. It printed
        # "JOIN PROVEN" on a cell whose control overlap was 100%, i.e. on a control
        # that had no power to discriminate anything at all.
        #
        # Three conditions now, and all three must hold:
        #   1. the matched pair is high                      -- B's packets are in A's set
        #   2. the sequence space is NOT saturated           -- the control CAN discriminate
        #   3. the cross pair actually falls near chance     -- and is far below matched
        # Condition 2 is the one that was missing. A saturated space is not a failed
        # join, it is an UNUSABLE TEST: with 65536 of 65536 values occupied, an
        # unrelated cell matches by construction. Say that, rather than claiming proof.
        SATURATION_LIMIT = 50.0     # above this, the control cannot separate signal from chance
        NEAR_CHANCE_SLACK = 10.0    # cross must sit within this many points of the floor
        MIN_SEPARATION = 50.0       # and this far below the matched pair
        saturated = space >= SATURATION_LIMIT
        near_chance = x <= space + NEAR_CHANCE_SLACK
        separated = (matched - x) >= MIN_SEPARATION
        ok = matched > 90 and not saturated and near_chance and separated

        if ok:
            verdict = 'JOIN PROVEN'
        elif saturated:
            verdict = 'TEST UNUSABLE (sequence space saturated)'
        else:
            verdict = 'INCONCLUSIVE'
        print(f"\n  VERDICT: {verdict} -- matched {matched:.1f}%, cross {x:.1f}%, chance {space:.1f}%")
        for label, passed, detail in (
                ("matched pair high", matched > 90, f"{matched:.1f}% > 90%"),
                ("space discriminating", not saturated, f"chance floor {space:.1f}% < {SATURATION_LIMIT:.0f}%"),
                ("cross at chance", near_chance, f"{x:.1f}% <= {space:.1f}% + {NEAR_CHANCE_SLACK:.0f}"),
                ("matched vs cross", separated, f"{matched - x:.1f} pts >= {MIN_SEPARATION:.0f}")):
            print(f"    [{'PASS' if passed else 'FAIL'}] {label:22s} {detail}")
        if saturated:
            print(f"  A occupies {len(aseq)} of 65536 sequence values. A 16-bit sequence number")
            print("  wraps every 65536 packets, so on a long cell an UNRELATED capture overlaps")
            print("  almost perfectly. Re-run the join on unwrapped sequence numbers, or on a")
            print("  shorter window, before drawing any conclusion from this pair.")
        print("  A high matched rate alone proves nothing; the cross pair must fall to chance.")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    report(sys.argv[1].rstrip('/'), sys.argv[2].rstrip('/') if len(sys.argv) > 2 else None)
