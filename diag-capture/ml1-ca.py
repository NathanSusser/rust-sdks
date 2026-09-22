#!/usr/bin/env python3
"""ML1 0xB97F -> per-carrier PCI/ARFCN/BRSRP, DISCOVERING each carrier block.

Why not a fixed stride: B found PCell at +0 and SCell at +272 on its captures and
noted the stride fails on 516-byte records. On Host A's 396/336-byte records the
SCell sits at +212. The stride is not a constant, so do not assume one -- walk the
record on a 4-byte grid and accept a block only where ARFCN is raster-plausible,
PCI is in range, AND the two PCI copies (+38, +64) agree. That last test is what
makes the scan safe: it is a 16-bit self-consistency check, so a chance hit needs
two independent fields to line up.

Blocks are emitted in offset order: lowest offset is the PCell (QCAT's naming).
"""
import os, sys, struct
# Resolve dlf_records.py next to THIS script, not at an absolute path in one operator's
# home directory. Set DIAG_CAPTURE_DIR to override.
sys.path.insert(0, os.environ.get('DIAG_CAPTURE_DIR',
                                  os.path.dirname(os.path.abspath(__file__))))
import dlf_records

NR_DL_MHZ = {"n2":(1930,1990),"n5":(869,894),"n12":(729,746),"n25":(1930,1995),
 "n26":(859,894),"n29":(717,728),"n30":(2350,2360),"n38":(2570,2620),
 "n40":(2300,2400),"n41":(2496,2690),"n48":(3550,3700),"n53":(2483.5,2495),
 "n66":(2110,2200),"n70":(1995,2020),"n71":(617,652),"n77":(3300,4200),
 "n78":(3300,3800),"n79":(4400,5000)}
def m2a(f):
    return int(round(f*1000/5)) if f < 3000 else int(round(600000+(f-3000)*1000/15))
BANDS=[(n,m2a(lo),m2a(hi)) for n,(lo,hi) in NR_DL_MHZ.items()]
def band(a):
    h=[n for n,lo,hi in BANDS if lo<=a<=hi]
    return "/".join(sorted(h)) if h else "?"

P=sys.argv[1]; OUT=sys.argv[2] if len(sys.argv)>2 else "/tmp/ml1-ca.csv"
f=open(P,"rb"); n=0
# A BLANK MUST NOT BE QUIET. The fixed-stride version of this script blanked the second
# carrier on every record of a host whose stride it did not know, and the blank read as
# "single carrier" all the way into a published report. So account for every record and
# say out loud, on stderr, how many carriers were found and at which offsets. A stride
# this script cannot find is now a visible anomaly instead of an empty column.
from collections import Counter
ncar = Counter(); offs = Counter(); lens = Counter()
with open(OUT,"w") as out:
    out.write("modem_ts,n_carriers,"
              "pcell_off,pcell_pci,pcell_arfcn,pcell_band,pcell_brsrp_dbm,"
              "scell_off,scell_pci,scell_arfcn,scell_band,scell_brsrp_dbm\n")
    for lid,t,ln,off in dlf_records.iter_file(P, emit_offset=True):
        if lid!=0xB97F: continue
        f.seek(off); b=f.read(ln)
        blocks=[]
        base=0
        while base <= len(b)-76:
            arf=struct.unpack_from("<I",b,base+32)[0]
            pci=struct.unpack_from("<H",b,base+38)[0]
            alt=struct.unpack_from("<H",b,base+64)[0]
            if 100000<=arf<=700000 and 0<=pci<=1007 and pci==alt:
                br=struct.unpack_from("<i",b,base+72)[0]/128.0
                blocks.append((base,pci,arf,band(arf),br))
                base+=76          # a block is at least this long; don't re-hit it
            else:
                base+=4
        row=[f"{t:.6f}",str(len(blocks))]
        for i in range(2):
            if i<len(blocks):
                o,pci,arf,bd,br=blocks[i]
                row+=[str(o),str(pci),str(arf),bd,f"{br:.3f}"]
            else: row+=["","","","",""]
        out.write(",".join(row)+"\n"); n+=1
        ncar[len(blocks)] += 1; lens[ln] += 1
        offs[tuple(b_[0] for b_ in blocks)] += 1
print(f"{n} ML1 records -> {OUT}")
if n:
    e = lambda m: print(m, file=sys.stderr)
    e(f"  record lengths : {dict(lens.most_common())}")
    e(f"  carriers/record: {dict(ncar.most_common())}")
    e(f"  block offsets  : {dict(offs.most_common())}")
    modal = ncar.most_common(1)[0][0]
    odd = sum(v for k, v in ncar.items() if k != modal)
    if odd:
        e(f"  WARNING: {odd}/{n} records ({100*odd/n:.1f}%) did not yield {modal} carriers.")
        e(f"  Those rows have BLANK carrier columns. A blank here means this scan found no")
        e(f"  valid block, NOT that the carrier is absent -- check before reading it as one.")
    if ncar.get(0):
        e(f"  WARNING: {ncar[0]} records yielded NO carrier block at all. Either the field")
        e(f"  layout differs on this capture or the validation is too strict; do not treat")
        e(f"  these as records without a serving cell.")
