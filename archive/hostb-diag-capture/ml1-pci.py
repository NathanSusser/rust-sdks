#!/usr/bin/env python3
"""Extract PCI + RSRP per 0xB97F ML1 record.

  ml1-pci.py <dlf> [out.csv]

CARRIER AGGREGATION: the record contains TWO carrier blocks at a 272-byte
stride -- PCell at +0 and SCell Idx1 at +272 (QCAT names them that way). Every
field below exists in both. An earlier version of this script read only the
first block and reported the rig as single-carrier n41; it is n41 + n25.

Byte 72 is confirmed by QCAT as "PCell Serving Cell Serving Beam Filtered Tx
BRSRP [dBm]" -- a per-BEAM figure, not cell RSRP. On Host A at 21:46:24.000Z
QCAT reads -89.54 and this field reads -89.41 at 21:46:24.047Z. Call it BRSRP.

PCI sits at byte 38 and byte 64 (uint16, little-endian) -- both read 85 on the
cell5m-a captures, matching what SCAT reported for this cell on 15 Sep. Two
copies of the same field is normal in ML1 (serving vs. measured-cell blocks),
so BOTH are emitted: if they ever disagree, that disagreement is the
handover/neighbour signal and is the whole point of the scan.

Byte 72 as <i /128.0 is the RSRP candidate (unconfirmed by spec; matched to
QMI by range, step size and correlation).

NR-ARFCN is a uint32 at byte 32 (reads 501390 on cell5m-a = 2506.95 MHz = band
n41). Band itself is NOT a field in this record -- in NR it is derived from the
ARFCN by which band's raster range contains it, which is what arfcn_to_band()
does below. Note 501390 is NOT the 521310 recorded on 15 Sep: same band, same
PCI, different carrier.

Timestamps are MODEM time, ~15 s ahead of the host clock. Subtract the
per-host offset before joining to anything else.
"""
import sys, struct
sys.path.insert(0, '/home/nsusser/diag-capture')
import dlf_records

# NR band from ARFCN. The band is NOT a field in the 0xB97F record -- in NR it is
# recovered by asking which band's DL raster range contains the value.
#
# Bands are written here as DL frequency edges (MHz) from TS 38.101-1 Table 5.2-1
# and converted with the TS 38.104 5.4.2.1 raster, rather than transcribed as
# ARFCN bounds. An earlier version of this table hardcoded the ARFCNs and got
# n71, n25 and n66 wrong by 65-460 MHz; deriving them makes that class of typo
# impossible, and each entry is checkable by eye against a spec sheet.
NR_DL_MHZ = {
    "n2": (1930, 1990), "n5": (869, 894),   "n12": (729, 746),
    "n25": (1930, 1995), "n26": (859, 894), "n29": (717, 728),
    "n30": (2350, 2360), "n38": (2570, 2620), "n40": (2300, 2400),
    "n41": (2496, 2690), "n48": (3550, 3700), "n53": (2483.5, 2495),
    "n66": (2110, 2200), "n70": (1995, 2020), "n71": (617, 652),
    "n77": (3300, 4200), "n78": (3300, 3800), "n79": (4400, 5000),
}

def mhz_to_arfcn(f):
    """TS 38.104 5.4.2.1 global frequency raster (FR1 only)."""
    if f < 3000:
        return int(round(f * 1000 / 5))              # 5 kHz step
    return int(round(600000 + (f - 3000) * 1000 / 15))  # 15 kHz step

NR_BANDS = [(n, mhz_to_arfcn(lo), mhz_to_arfcn(hi)) for n, (lo, hi) in NR_DL_MHZ.items()]

def arfcn_to_band(a):
    """May legitimately return more than one band.

    Several NR bands genuinely overlap -- n78 (3300-3800) is a subset of
    n77 (3300-4200), and n25 contains n2 -- so an ARFCN alone cannot always
    name one band; that needs the band indicator from RRC. Returning every
    match is the honest answer. Not an issue on this rig, which is n41.
    """
    hits = [n for n, lo, hi in NR_BANDS if lo <= a <= hi]
    return "/".join(sorted(hits)) if hits else "?"

P = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ml1-pci.csv"

n = 0
f = open(P, "rb")
with open(OUT, "w") as out:
    out.write("modem_ts,"
              "pcell_pci,pcell_arfcn,pcell_band,pcell_brsrp_dbm,"
              "scell_pci,scell_arfcn,scell_band,scell_brsrp_dbm\n")
    for lid, t, ln, off in dlf_records.iter_file(P, emit_offset=True):
        if lid != 0xB97F:
            continue
        f.seek(off); b = f.read(ln)
        if len(b) < 200:
            continue
        row = [f"{t:.6f}"]
        for base in (0, 272):          # PCell, then SCell Idx1
            # Records come in several lengths (396 / 456 / 516 on cell5m-a) and
            # the 272 stride does not hold for all of them, so VALIDATE the block
            # rather than trusting its length: a real one has a raster-plausible
            # ARFCN and a PCI in range. Blank beats a confident 62915051.
            if len(b) < base + 76:
                row += ["", "", "", ""]; continue
            arf = struct.unpack_from("<I", b, base + 32)[0]
            pci = struct.unpack_from("<H", b, base + 38)[0]
            if not (100000 <= arf <= 700000 and 0 <= pci <= 1007):
                row += ["", "", "", ""]; continue
            alt = struct.unpack_from("<H", b, base + 64)[0]
            br  = struct.unpack_from("<i", b, base + 72)[0] / 128.0
            if pci != alt:             # the two copies must agree; say so if not
                pci = f"{pci}/{alt}"
            row += [str(pci), str(arf), arfcn_to_band(arf), f"{br:.3f}"]
        out.write(",".join(row) + "\n")
        n += 1
print(f"{n} ML1 records -> {OUT}")
