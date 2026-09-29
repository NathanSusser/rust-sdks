#!/usr/bin/env python3
"""Extract every NR carrier from 0xB97F ML1 records, without assuming a stride.

  ml1-ca.py <dlf> [out.csv]

WHY NOT A FIXED STRIDE. The second carrier block sits at +272 on Host B and at
+212 on Host A, and neither is predictable from record length (B has 396/456/516
byte records, A has 396/336). A hardcoded stride returns a BLANK SCell on the
other host, which reads as "single carrier" -- a wrong answer arrived at safely.

SO: walk the record on a 4-byte grid and accept a block only where all three
hold -- ARFCN raster-plausible, PCI in range, AND the two PCI copies at +38 and
+64 agree. The last test is what makes the scan safe rather than reckless: a
chance hit has to line up two independent 16-bit fields.

Fields within a block: +32 ARFCN <I, +38 PCI <H, +64 PCI <H (copy),
+72 Serving Beam Filtered Tx BRSRP <i /128.0 (QCAT-confirmed, per-BEAM).
Timestamps are the modem's, which is TRUE UTC; the host clocks are the wrong ones.
"""
import sys, struct
sys.path.insert(0, '/home/nsusser/diag-capture')
import dlf_records

NR_DL_MHZ = {"n2":(1930,1990),"n5":(869,894),"n12":(729,746),"n25":(1930,1995),
             "n26":(859,894),"n29":(717,728),"n30":(2350,2360),"n38":(2570,2620),
             "n40":(2300,2400),"n41":(2496,2690),"n48":(3550,3700),"n53":(2483.5,2495),
             "n66":(2110,2200),"n70":(1995,2020),"n71":(617,652),"n77":(3300,4200),
             "n78":(3300,3800),"n79":(4400,5000)}
def mhz(a):  return a*5/1000.0 if a < 600000 else 3000 + (a-600000)*15/1000.0
def to_arfcn(f): return int(round(f*1000/5)) if f < 3000 else int(round(600000+(f-3000)*1000/15))
BANDS = [(n, to_arfcn(lo), to_arfcn(hi)) for n,(lo,hi) in NR_DL_MHZ.items()]
def band(a):
    h = [n for n,lo,hi in BANDS if lo <= a <= hi]
    return "/".join(sorted(h)) if h else "?"

def carriers(b):
    """Every validated carrier block, in record order."""
    out = []
    for base in range(0, len(b)-75, 4):
        arf = struct.unpack_from("<I", b, base+32)[0]
        if not 100000 <= arf <= 700000:            # raster-plausible
            continue
        pci = struct.unpack_from("<H", b, base+38)[0]
        if pci > 1007 or pci != struct.unpack_from("<H", b, base+64)[0]:
            continue                                # both PCI copies must agree
        out.append((base, arf, pci, struct.unpack_from("<i", b, base+72)[0]/128.0))
    return out

def main():
    P = sys.argv[1]; OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ml1-ca.csv"
    import collections
    f = open(P, "rb"); n = 0; skipped = 0
    strides = collections.Counter(); ncar = collections.Counter(); lens = collections.Counter()
    with open(OUT, "w") as out:
        out.write("modem_ts,cc,offset,pci,arfcn,mhz,band,brsrp_dbm\n")
        for lid, t, ln, off in dlf_records.iter_file(P, emit_offset=True):
            if lid != 0xB97F: continue
            f.seek(off); b = f.read(ln)
            cs = carriers(b)
            lens[len(b)] += 1
            if not cs:
                skipped += 1
                continue
            n += 1
            ncar[len(cs)] += 1
            strides[tuple(c[0] for c in cs)] += 1
            for i, (base, arf, pci, br) in enumerate(cs):
                out.write(f"{t:.6f},{i},{base},{pci},{arf},{mhz(arf):.2f},{band(arf)},{br:.3f}\n")
    # Account for EVERY record on stderr. A blank or missing carrier here means
    # THIS SCAN FOUND NO VALID BLOCK -- never that the carrier is absent. The
    # failure mode this guards is a silent one: a stride that does not match
    # produces an empty column that reads exactly like "single carrier", which
    # is how a fixed +272 reader reported Host A as single-carrier and dropped
    # 55 of Host B's own records.
    print(f"{n} records -> {OUT}", file=sys.stderr)
    print(f"  record lengths : {dict(lens)}", file=sys.stderr)
    print(f"  carriers/record: {dict(ncar)}", file=sys.stderr)
    print(f"  block offsets  : {dict(strides)}", file=sys.stderr)
    if skipped:
        print(f"  !! {skipped} records yielded NO valid block -- not counted above", file=sys.stderr)
    if ncar:
        modal = ncar.most_common(1)[0][0]
        odd = sum(c for k, c in ncar.items() if k != modal)
        if odd:
            print(f"  !! WARNING: {odd} of {n} records deviate from the modal "
                  f"{modal} carriers/record. A short count is a SCAN result, "
                  f"not evidence the carrier was absent -- check the stride.",
                  file=sys.stderr)
    print(f"{n} records -> {OUT}")

main()
