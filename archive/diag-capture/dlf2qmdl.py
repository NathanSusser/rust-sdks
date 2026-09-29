#!/usr/bin/env python3
"""Re-wrap a QCSuper DLF as a QMDL (raw HDLC diag stream) so SCAT can read it.

A DLF record is exactly a diag log header plus payload: uint16 len | uint16 code |
uint64 ts | payload. On the wire the modem sends that inside a DIAG_LOG_F (0x10)
response, HDLC-framed with a CRC-16/X-25 and 0x7D/0x7E escaping. Rebuild that.
"""
import struct, sys
import crcmod

crc = crcmod.predefined.mkCrcFun('x-25')

def frame(payload: bytes) -> bytes:
    c = crc(payload)
    raw = payload + bytes([c & 0xFF, (c >> 8) & 0xFF])
    out = bytearray()
    for b in raw:
        out.extend([0x7D, b ^ 0x20] if b in (0x7D, 0x7E) else [b])
    out.append(0x7E)
    return bytes(out)

src, dst = sys.argv[1], sys.argv[2]
data = open(src, 'rb').read(); off = n = 0
with open(dst, 'wb') as f:
    while off + 12 <= len(data):
        ln, = struct.unpack_from('<H', data, off)
        if ln < 12 or off + ln > len(data):
            break
        rec = data[off:off + ln]
        f.write(frame(b'\x10\x00' + struct.pack('<H', ln) + rec))
        off += ln; n += 1
print(f"{n} records -> {dst}")
