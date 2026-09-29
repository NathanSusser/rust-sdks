"""ML1 0xB97F -> per-carrier PCI / ARFCN / band / BRSRP (ported from archive/diag-capture/ml1-ca.py).

Carrier blocks are DISCOVERED per record, never located by a fixed stride. The stride
differs by host and record length (+272 on one host's 516-byte records, +212 on the
other host's 396/336-byte records), so the record is walked on a 4-byte grid and a
block is accepted only where the ARFCN is raster-plausible, the PCI is in range AND the
two PCI copies (+38, +64) agree -- a 16-bit self-consistency check, so a chance hit
needs two independent fields to line up. Blocks are emitted in offset order: the lowest
offset is the PCell (QCAT's naming).

A blank must not be quiet: records that yield no block are counted and reported in the
summary, never read as "no serving cell".

Band from ARFCN. The rig sees n71 (617-652 MHz DL), n41 (2496-2690) and n25 (1930-1995).
Raster note: n2 (1930-1990 MHz) lies entirely inside n25, and n38 inside n41, so an ARFCN
in the overlap is ambiguous from the raster alone. The carrier here deploys n25 and n41,
so the overlap resolves to the superset band (n25, n41); the raw overlap is kept in
`band_raw` for anyone who needs it.
"""
from __future__ import annotations

import csv
import struct
from collections import Counter

ML1_CODE = 0xB97F
BLOCK_MIN = 76

NR_DL_MHZ = {
    "n2": (1930, 1990), "n5": (869, 894), "n12": (729, 746), "n25": (1930, 1995),
    "n26": (859, 894), "n29": (717, 728), "n30": (2350, 2360), "n38": (2570, 2620),
    "n40": (2300, 2400), "n41": (2496, 2690), "n48": (3550, 3700), "n53": (2483.5, 2495),
    "n66": (2110, 2200), "n70": (1995, 2020), "n71": (617, 652), "n77": (3300, 4200),
    "n78": (3300, 3800), "n79": (4400, 5000),
}
# Overlap resolution (see module docstring). Anything not listed stays as the raw "a/b".
PREFER = {"n2/n25": "n25", "n38/n41": "n41", "n77/n78": "n77", "n26/n5": "n26"}


def mhz_to_arfcn(f: float) -> int:
    return int(round(f * 1000 / 5)) if f < 3000 else int(round(600000 + (f - 3000) * 1000 / 15))


def arfcn_to_mhz(a: int) -> float:
    return a * 0.005 if a < 600000 else 3000 + (a - 600000) * 0.015


BANDS = [(n, mhz_to_arfcn(lo), mhz_to_arfcn(hi)) for n, (lo, hi) in NR_DL_MHZ.items()]


def band_raw(arfcn: int) -> str:
    hits = [n for n, lo, hi in BANDS if lo <= arfcn <= hi]
    return "/".join(sorted(hits)) if hits else "?"


def band(arfcn: int) -> str:
    r = band_raw(arfcn)
    return PREFER.get(r, r)


def parse_record(b: bytes) -> list[tuple[int, int, int, float]]:
    """Carrier blocks in one 0xB97F record: [(offset, pci, arfcn, brsrp_dbm)], PCell first."""
    blocks = []
    base = 0
    while base <= len(b) - BLOCK_MIN:
        arf = struct.unpack_from("<I", b, base + 32)[0]
        pci = struct.unpack_from("<H", b, base + 38)[0]
        alt = struct.unpack_from("<H", b, base + 64)[0]
        if 100000 <= arf <= 700000 and 0 <= pci <= 1007 and pci == alt:
            br = struct.unpack_from("<i", b, base + 72)[0] / 128.0
            blocks.append((base, pci, arf, br))
            base += BLOCK_MIN          # a block is at least this long; don't re-hit it
        else:
            base += 4
    return blocks


class Collector:
    """Accumulates ML1 rows during the single DLF pass in dlf_rates.scan()."""

    def __init__(self):
        self.rows: list[tuple[float, float, int, int, int, int, float]] = []  # modem_ts, t_rel, idx, off, pci, arfcn, brsrp
        self.ncar = Counter()
        self.lens = Counter()
        self.offs = Counter()
        self.records = 0

    def add(self, modem_ts: float, t_rel: float, rec: bytes) -> None:
        blocks = parse_record(rec)
        self.records += 1
        self.ncar[len(blocks)] += 1
        self.lens[len(rec)] += 1
        self.offs[tuple(b[0] for b in blocks)] += 1
        for i, (off, pci, arf, br) in enumerate(blocks):
            self.rows.append((modem_ts, t_rel, i, off, pci, arf, br))

    def write_csv(self, path) -> None:
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["modem_ts", "t_rel_epoch_s", "carrier", "block_off", "pci", "arfcn", "band", "band_raw", "brsrp_dbm"])
            for ts, tr, i, off, pci, arf, br in self.rows:
                w.writerow([f"{ts:.6f}", f"{tr:.3f}", "pcell" if i == 0 else f"scell{i}", off, pci, arf,
                            band(arf), band_raw(arf), f"{br:.3f}"])

    def summary(self) -> dict:
        """Majority PCell (band, arfcn, pci, share) + the same for the first SCell, and the diagnostics."""
        out = {"band": None, "arfcn": None, "pci": None, "share": None, "records": self.records,
               "carriers_per_record": {str(k): v for k, v in self.ncar.most_common()},
               "block_offsets": {",".join(map(str, k)) or "none": v for k, v in self.offs.most_common(6)},
               "record_lengths": {str(k): v for k, v in self.lens.most_common(6)},
               "records_without_block": self.ncar.get(0, 0), "scell": None}
        if not self.records:
            return out
        for idx, key in ((0, None), (1, "scell")):
            c = Counter((band(a), a, p) for _, _, i, _, p, a, _ in self.rows if i == idx)
            if not c:
                continue
            (bd, arf, pci), n = c.most_common(1)[0]
            rs = [br for _, _, i, _, p, a, br in self.rows if i == idx and a == arf and p == pci]
            d = {"band": bd, "arfcn": arf, "pci": pci, "share": round(n / self.records, 4),
                 "brsrp_dbm_mean": round(sum(rs) / len(rs), 2) if rs else None}
            if key is None:
                out.update(d)
            else:
                out["scell"] = d
        return out
