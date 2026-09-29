#!/usr/bin/env python3
"""Extract candidate radio fields from 0xB97F ML1 records in a DLF.

  ml1-extract.py <dlf> [out.json]

No QCAT. Yields (unix_ts, {byte_offset: value/128.0}) for offsets that scanning
found to sit in plausible RSRP/SNR range. Byte 72 is the RSRP candidate: on Host B's
cell5m-a capture it reads median -90.94 dBm, within 1 dB of Host A's QMI figure for
the same cell. THE FIELD IDENTITY IS UNCONFIRMED -- matched by range and behaviour,
not by specification. QCAT would settle it.
"""
import struct, json
sys.path.insert(0,'/home/nsusser/diag-capture')
import dlf_records
import sys
P=sys.argv[1]
OUT=sys.argv[2] if len(sys.argv)>2 else "/tmp/ml1.json"
OFFSETS=[44,48,72,100,104,108,24]
rows=[]
f=open(P,"rb")
for lid,t,ln,off in dlf_records.iter_file(P, emit_offset=True):
    if lid!=0xB97F: continue
    f.seek(off); b=f.read(ln)
    if len(b)<200: continue
    vals={}
    for o in OFFSETS:
        vals[o]=struct.unpack_from("<i", b, o)[0]/128.0
    rows.append([t, vals])
json.dump([[r[0], {str(k):round(v,2) for k,v in r[1].items()}] for r in rows],
          open(OUT,"w"))
print(f"extracted {len(rows)} ML1 records")
