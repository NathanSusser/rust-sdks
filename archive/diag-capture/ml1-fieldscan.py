#!/usr/bin/env python3
"""Find which 0xB97F fields changed across an event, without knowing the record layout.

  ml1-fieldscan.py <dlf> <pre_start> <pre_end> <post_start> <post_end> [ctrl_start ctrl_end]
      times as UTC HH:MM:SS (MODEM time -- 0xB97F timestamps are already true UTC)

PASS THE CONTROL WINDOW. A quiet stretch mid-cell, far from the event. Without it a field
that is merely INTERMITTENT looks like one that crossed: Host B reported an entry as
"dropped out at the event" that was already sentinel in 5 of 20 control samples, and dropped
the claim once a control was added. On Host A the same rule killed a field I had read as
"appearing" -- 41/75 sentinel before against 21/37 after is the same fraction, not a change.
Choose the POST window to END BEFORE the media stops, too: the capture keeps running after
the stream ends and the measurement set reconfigures again there, which has produced a
phantom event for us before.

AND CHECK THE BASE RATE, WHICH A CONTROL WINDOW DOES NOT GIVE YOU. A control window says the
field was quiet in one other place; it does not say how often the field crosses anyway. Host B
published a "both hosts reconfigured in the same second" finding built on a field that crosses
242 times in 385 s -- the event "appearance" was one of 31 valid runs over 3 s, and any pair of
windows would have found one. It passed a control window.

But the CAPTURE-WIDE count is the wrong denominator, and using it would throw away real
findings. On Host A, offsets 164 and 224 cross 43 and 110 times respectively -- and ZERO times
in the 120 s before the event, because their crossings are packed into the first 60 s and into
the aftermath of the event itself. Suppressing a verdict on the total would have killed both.
What matters is the rate in the quiet period LEADING UP TO the event, so that is what
`crossings_before` counts and what the verdict is gated on. Crossings after the event are not
background: they are plausibly consequences of it, or of the media stopping.

WHY. We have no spec for this record and QCAT is not on these hosts. What we do have is an
event with a known time, so the layout can be attacked differentially: a field that matters
is one that is stable before, stable after, and different across. This found, on cell5m-a:

  * the instantaneous counterpart of filtered byte 72, at offset 104 (corr 0.845 over 2.8
    min; @104 wobbles +/-2 dB sample to sample where @72 is smooth). Byte 72 is the L3
    FILTERED serving-beam BRSRP; @104 is the unfiltered one, and the filter's step response
    is ~160 ms to first motion and ~0.3 s to half -- NOT the ~1.8 s we first inferred.
  * a sentinel: -156.00 dB == raw int32 -19968, meaning "no measurement", not a level.
    Offset 164 held a real value for 3.4 minutes and went to the sentinel in the same 160 ms
    sample as the step, i.e. the measurement SET was reconfigured at the event.

THREE TRAPS, all of which cost us a wrong conclusion first:

  1. READ MEDIANS, NOT TRACES. Several of these fields swing multiple dB over seconds. Both
     hosts independently read one such excursion as a pre-event "progressive degradation"
     and built a causal story on it. Comparing window medians showed every field agreeing
     within 0.4 dB between a clean window and the supposed decline. There was no decline.
  2. AN INDEX-SHAPED FIELD IS NOT AN INDEX. Byte 127 is 4-valued, holds for ~26 s at a
     time, and changed at exactly the event -- but it changes 15 times across the capture
     and 13 of those move no signal field at all. Shape is not identification. Require that
     a candidate's OTHER transitions also do what the theory says they should.
  3. OFFSET ORDER IS NOT RANK ORDER. The ranked beam values are monotonic in offset on one
     host and not on the other, so "sorted by strength" read off one host is an artefact.

Fields whose low byte belongs to an int32 will also show up as "changed" u8s. Cross-check
any candidate against the int32 map before believing it is a field in its own right.
"""
import os, sys, struct, datetime
import statistics as st
from collections import Counter
# Resolve dlf_records.py next to THIS script, not at an absolute path in one operator's
# home directory. Set DIAG_CAPTURE_DIR to override.
sys.path.insert(0, os.environ.get('DIAG_CAPTURE_DIR',
                                  os.path.dirname(os.path.abspath(__file__))))
import dlf_records

def discover_blocks(b):
    """Carrier-block base offsets in one record, same three tests as ml1-ca.py."""
    out = []; base = 0
    while base <= len(b) - 76:
        arf = struct.unpack_from("<I", b, base + 32)[0]
        pci = struct.unpack_from("<H", b, base + 38)[0]
        alt = struct.unpack_from("<H", b, base + 64)[0]
        if 100000 <= arf <= 700000 and 0 <= pci <= 1007 and pci == alt:
            out.append(base); base += 76
        else:
            base += 4
    return tuple(out)


SENTINEL = -19968          # == -156.00 dB after /128; "not measured"

if len(sys.argv) < 6:
    print(__doc__.split("\n\n")[1].strip(), file=sys.stderr)
    sys.exit(2)
P = sys.argv[1]
def parse(hms, day):
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime.datetime(*day, h, m, s, tzinfo=datetime.timezone.utc).timestamp()

f = open(P, "rb")
# take the day from the first record so the caller only gives times
day = None
raw = []
for lid, t, ln, off in dlf_records.iter_file(P, emit_offset=True):
    if lid != 0xB97F:
        continue
    if day is None:
        d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
        day = (d.year, d.month, d.day)
    f.seek(off); raw.append((t, ln, f.read(ln)))
if not raw:
    print("no 0xB97F records", file=sys.stderr); sys.exit(1)
P0, P1, Q0, Q1 = (parse(a, day) for a in sys.argv[2:6])
CTRL = tuple(parse(a, day) for a in sys.argv[6:8]) if len(sys.argv) >= 8 else None

# SELECT BY LAYOUT SIGNATURE, NOT BY RECORD LENGTH. An earlier version restricted to the
# modal length and argued that this kept the fixed-offset verdicts comparable. It does not:
# on Host B, 1,195 records of 456 bytes carry the second carrier block at +272 while 43
# records of THE SAME 456 bytes carry it at +332. Length does not determine layout, so a
# length filter still mixes two field layouts -- and 43 records is exactly the population
# size that yields a finding nobody can reproduce. Group by the discovered block signature
# and analyse the modal signature only.
sigs = Counter(discover_blocks(b) for _, _, b in raw)
modal_sig = sigs.most_common(1)[0][0]
recs = [(t, b) for t, ln, b in raw if discover_blocks(b) == modal_sig]
joint = Counter((ln, discover_blocks(b)) for t, ln, b in raw)
# One layout can still span several lengths (Host A: 336 B and 396 B both at (0, 212)), so
# bound the scan to the SHORTEST analysed record. Reading past it would either throw or, with
# a wider selection, silently compare a field that exists in only some of the records.
SCAN_MAX = min(len(b) for _, b in recs) if recs else 0
pre = [b for t, b in recs if P0 <= t <= P1]
post = [b for t, b in recs if Q0 <= t <= Q1]
ctrl = [b for t, b in recs if CTRL and CTRL[0] <= t <= CTRL[1]]
print(f"{len(raw)} records; using the modal layout {modal_sig} ({len(recs)} records, "
      f"{len(raw)-len(recs)} excluded)")
print(f"scanning offsets 0..{min(len(b) for _, b in recs) if recs else 0} "
      f"(shortest analysed record; longer ones are truncated to it)")
print("length x layout joint distribution:")
for (ln, sg), n in sorted(joint.items(), key=lambda kv: -kv[1]):
    flag = "  <- analysed" if sg == modal_sig else ""
    print(f"  len {ln:>4}  blocks at {sg}  x{n}{flag}")
bylen = {}
for (ln, sg) in joint:
    bylen.setdefault(ln, set()).add(sg)
mixed = [ln for ln, v in bylen.items() if len(v) > 1]
if mixed:
    print(f"  NOTE: length(s) {mixed} carry MORE THAN ONE layout, so a length filter would")
    print("  have mixed field layouts here. This scan is keyed to the layout, not the length.")
print(f"pre={len(pre)}  post={len(post)}  ctrl={len(ctrl) if ctrl else 'NONE GIVEN'}")
if not ctrl:
    print("  no control window: sentinel crossings below are UNVERIFIED and an already-")
    print("  intermittent field will look like one that crossed. Pass ctrl_start ctrl_end.")
if len(pre) < 5 or len(post) < 5:
    print("windows too thin to compare", file=sys.stderr); sys.exit(1)

G = lambda b, o: struct.unpack_from("<i", b, o)[0]

LOOKBACK = 120.0          # seconds of quiet background required before the event

def check_layout_constant(rs):
    """Every verdict below is keyed to a FIXED OFFSET, which silently assumes the record
    layout never moves. It does move: Host B's carrier block sits at +272 in most records
    and +332 in others, so a fixed offset read two DIFFERENT fields and the switch between
    them looked like a field dropping out. It passed a control window AND a base-rate gate,
    because both answer "does this field cross often?" and neither answers "is this one
    field?". So test it directly rather than warning about it in prose."""
    sig = Counter(discover_blocks(b) for _, b in rs)
    print(f"\nrecord layout: {len(sig)} distinct carrier-block signature(s)")
    for k, n in sig.most_common():
        print(f"  blocks at {k}  x{n}")
    if len(sig) > 1:
        floor = min(min(k[1:]) for k in sig if len(k) > 1) if any(len(k) > 1 for k in sig) else 0
        print(f"  *** LAYOUT IS NOT CONSTANT. Every fixed-offset verdict below is unsafe at or")
        print(f"  *** past offset {floor}: two record variants put different fields there, and a")
        print(f"  *** switch between them mimics a dropout. Read block-relative instead -- take")
        print(f"  *** the base from discover_blocks() per record, then the field relative to it.")
        return False
    # "Constant" is a property of THIS capture, not of the host or the firmware. 0xB97F is a
    # variable-length record carrying a variable number of 60-byte per-beam entries: every
    # record length we have seen on either host is 36 + k*60 and every second-carrier-block
    # offset is 32 + k*60. Host A and Host B run byte-identical firmware (RM520NGLAAR03A04M4G),
    # carrier config (Commercial-TMO 0A01050F) and h/w revision, so the layout difference is
    # purely the entry COUNT -- a function of how many beams the modem is tracking. The count
    # varies WITHIN a single capture on both hosts, so a fixed offset is unsafe even against
    # the same host an hour earlier. This check passing means the block base did not move in
    # this capture; it is not a licence to reuse the offset anywhere else.
    print("  the analysed set is one layout, so the verdicts below compare one field.")
    print("  This is a property of THIS CAPTURE. Do not carry these offsets anywhere else,")
    print("  including to another capture on this same host with this same firmware.")
    return True

def base_rate(o):
    """(crossings in the LOOKBACK before the pre-window ends, crossings in the whole capture).

    The first number is the one that matters. The capture-wide total is reported only so a
    clustered field is visible as clustered -- a field with a high total and zero crossings
    before the event is quiet WHERE THE EVENT IS, which is the only place it has to be quiet.
    """
    flags = [(t, G(b, o) == SENTINEL) for t, b in recs]
    xs = [b[0] for a, b in zip(flags, flags[1:]) if a[1] != b[1]]
    return sum(1 for t in xs if P1 - LOOKBACK <= t < P1), len(xs)
check_layout_constant(recs)

print(f"\nint32/128 fields in RSRP range, or crossing the sentinel:")
print(f"  {'off':>4}{'pre':>9}{'post':>9}{'delta':>8}{'pre_sd':>8}  note")
for o in range(SCAN_MAX - 3):
    a = [G(b, o) for b in pre]; c = [G(b, o) for b in post]
    sa = sum(1 for v in a if v == SENTINEL); sc = sum(1 for v in c if v == SENTINEL)
    av = [v / 128.0 for v in a if v != SENTINEL]; cv = [v / 128.0 for v in c if v != SENTINEL]
    if not av or not cv:
        # No valid samples one side: still a crossing, and usually the strongest one, so
        # give it the same control test rather than reporting a bare count.
        if sa or sc:
            cf = None
            if ctrl:
                cv2 = [G(b, o) for b in ctrl]
                cf = sum(1 for v in cv2 if v == SENTINEL) / len(cv2)
            nb, nt = base_rate(o)
            if cf is not None and cf > 0.1:
                v = f"INTERMITTENT IN CONTROL ({cf:.0%}) -- crossing NOT claimable"
            elif nb > 0:
                v = f"CROSSES ANYWAY ({nb} times in the {LOOKBACK:.0f}s before) -- NOT claimable"
            elif not cv:
                v = "DROPPED OUT at event (no valid samples after)"
            else:
                v = "APPEARED at event (no valid samples before)"
            print(f"  {o:>4}{'':>9}{'':>9}{'':>8}{'':>8}  sentinel {sa}/{len(a)} -> {sc}/{len(c)}; "
                  f"crossings_before={nb} total={nt}; {v}")
        continue
    ma, mc = st.median(av), st.median(cv)
    if not (-140 <= ma <= -40):
        continue
    sd = st.stdev(av) if len(av) > 1 else 0.0
    note = []
    # The verdict is COMPUTED from the fractions, never written into the print. A verdict
    # baked into a print statement fires whatever the number says -- Host B shipped one
    # reading "structure corroborated" that printed directly above a correlation of 0.144.
    pf, qf = sa / len(a), sc / len(c)
    cf = None
    if ctrl:
        cv = [G(b, o) for b in ctrl]
        cf = sum(1 for v in cv if v == SENTINEL) / len(cv)
    nb, nt = base_rate(o)
    if sa != sc: note.append(f"SENTINEL {sa}/{len(a)} -> {sc}/{len(c)}")
    if sa != sc: note.append(f"crossings_before={nb} total={nt}")
    if cf is not None and cf > 0.1 and sa != sc:
        note.append(f"INTERMITTENT IN CONTROL ({cf:.0%}) -- crossing NOT claimable")
    elif nb > 0 and sa != sc:
        note.append(f"CROSSES ANYWAY ({nb} times in the {LOOKBACK:.0f}s before) -- NOT claimable")
    elif qf > 0.5 and pf < 0.1: note.append("DROPPED OUT at event")
    elif pf > 0.5 and qf < 0.1: note.append("APPEARED at event")
    if sd < 0.01: note.append("frozen in pre-window")
    if abs(mc - ma) > 3 and max(pf, qf) < 0.5: note.append("STEP")
    print(f"  {o:>4}{ma:>9.2f}{mc:>9.2f}{mc-ma:>8.2f}{sd:>8.2f}  {'; '.join(note)}")

print(f"\nindex-like u8 fields (all values <64, <=12 distinct) that DIFFER across the event:")
print("  (shape is not identification -- check each candidate's OTHER transitions)")
allb = [b for _, b in recs]
found = 0
for o in range(SCAN_MAX):
    v = [b[o] for b in allb]
    s = set(v)
    if max(s) >= 64 or len(s) > 12:
        continue
    cp, cq = Counter(b[o] for b in pre), Counter(b[o] for b in post)
    if set(cp) != set(cq):
        runs, cur = [], 1
        for x, y in zip(v, v[1:]):
            if x == y: cur += 1
            else: runs.append(cur); cur = 1
        runs.append(cur)
        print(f"  u8 @{o:>4}  pre {dict(cp)} -> post {dict(cq)}   "
              f"alphabet {sorted(s)}  {len(runs)} runs, median {st.median(runs):.0f} records")
        found += 1
if not found:
    print("  none")
