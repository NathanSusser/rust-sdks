"""Resyncing DLF record iterator used by dlf-rates.py (iter_file) and inspect_dlf.py
(records). NOT used by dlf-check, which has its own standalone mmap/struct walk with its
own acceptance rule -- the two disagree, so do not describe either as "the" reader.
Measured 2026-09-17 on the 107 MB s1-probe capture: dlf-check 390,687 records / 2 resyncs
/ 130 bytes skipped, this module 390,037 / 5 / 1,988. dlf-check accepts 650 records this
module rejects. On a clean capture (the 36 MB s1 capture, 0 resyncs) all three agree
exactly, so the split appears only where malformed records exist.

The two entry points here DO agree with each other: verified on both s1 captures at chunk
sizes 16, 32 and 64 MB, identical record counts, resyncs and skipped bytes. Anything
shared across hosts comes from iter_file, so that equivalence is the one that matters.

NEVER COMPUTE A CAPTURE SPAN FROM RECORD TIMESTAMPS. Clip to the cell window first. A
capture carries a few records whose timestamp field is garbage, at BOTH ends: on the 107 MB
s1 capture A's maximum decodes to year 13086 and B's independent parser decodes the minimum
to 1980, so a naive max-minus-min reads as tens of thousands of days on a 6-minute capture.
It is a handful of records in ~390,000 -- harmless to counts, fatal to any span, axis or
header derived from them. Reader-independent: two parsers see the same strays.

QCSuper's dlf_dump can leave malformed records in a DLF ("Dismissing log type ...,
indicating size N instead of M"). A reader that walks records sequentially and stops at
the first bad length silently truncates the capture -- on S1 (2026-09-15) that made a
16-minute capture look as if it had ended before the cell began. On a bad length this
advances byte by byte until a record parses AND the record after it parses too.
"""
import struct

GPS_EPOCH = 315964800

def _plausible(data, off, lo, hi):
    """Accept a record by structure: the length fits and the
    log code is in a DIAG log range. The timestamp is NOT used to accept: valid records
    carry timestamps that do not decode to GPS time (checking it caused 909 false
    resyncs on a clean fast-build DLF). lo/hi are kept for API compatibility."""
    if off + 12 > len(data): return None
    ln, lid = struct.unpack_from('<HH', data, off)
    if ln < 12 or off + ln > len(data): return None
    if not (0x1000 <= lid <= 0x1FFF or 0xB000 <= lid <= 0xB9FF): return None
    ts, = struct.unpack_from('<Q', data, off + 4)
    t = GPS_EPOCH + ((ts >> 16) + (ts & 0xFFFF) / 65536) * 1.25e-3
    return ln, lid, t

def records(data, lo=1.0e9, hi=4.0e9, stats=None, offsets=None, emit_offset=False):
    """Yield (log_id, unix_ts, length). `stats` (dict) receives resyncs and skipped bytes.

    `offsets`, if a list, receives (start_byte, end_byte) for each resynced region.
    `emit_offset` changes the yield to (log_id, unix_ts, length, byte_offset).
    Both are additive: the acceptance rule and the default 3-tuple yield are unchanged, so
    counts match every earlier run. Offsets exist because post-resync TIMESTAMPS cannot be
    trusted -- a resync means the walk just crossed garbage, so the first record after it is
    the least reliable in the file (B saw one decode to the bare GPS epoch, i.e. a zeroed
    timestamp field). Byte position is reliable where the timestamp is not, so "are the
    dropped bytes clustered at the events?" must be asked in offsets.
    """
    off = 0; resyncs = skipped = 0
    while off + 12 <= len(data):
        r = _plausible(data, off, lo, hi)
        if r is None:
            start = off; off += 1
            while off + 12 <= len(data):
                r1 = _plausible(data, off, lo, hi)
                if r1 and (off + r1[0] >= len(data) or _plausible(data, off + r1[0], lo, hi)): break
                off += 1
            resyncs += 1; skipped += off - start
            if offsets is not None: offsets.append((start, off))
            continue
        ln, lid, t = r
        yield (lid, t, ln, off) if emit_offset else (lid, t, ln)
        off += ln
    if stats is not None: stats.update(resyncs=resyncs, skipped_bytes=skipped)


def iter_file(path, chunk=64 * 1024 * 1024, stats=None, offsets=None, emit_offset=False):
    """Stream (log_id, unix_ts, length) from a DLF of any size in fixed-size chunks.

    A multi-GB DLF must not be read whole (A's S2 capture is ~9 GB). Records are parsed
    from a sliding buffer; a record split across a chunk boundary is carried into the next
    read. Resync logic and counts match records() exactly.

    `offsets` and `emit_offset` behave as in records(), and report FILE-ABSOLUTE positions:
    `off` here indexes a buffer that is re-based on every chunk, so `base` accumulates the
    bytes already consumed. Without it offsets would be right only inside the first chunk
    and silently wrong after 64 MB -- on a 5.9 GB capture, wrong for 99% of the file."""
    resyncs = skipped = 0
    buf = b''; eof = False; base = 0
    with open(path, 'rb') as f:
        while True:
            if not eof:
                more = f.read(chunk)
                if not more: eof = True
                buf += more
            off = 0
            # keep a tail large enough for one maximal record + the next header when not at EOF
            limit = len(buf) if eof else max(0, len(buf) - (65536 + 12))
            while off + 12 <= len(buf) and off < limit:
                r = _plausible(buf, off, 0, 0)
                if r is None or (not eof and off + r[0] + 12 > len(buf)):
                    if r is not None: break              # record spans the boundary: read more
                    start = off; off += 1
                    while off + 12 <= len(buf) and (eof or off < limit):
                        r1 = _plausible(buf, off, 0, 0)
                        if r1 and (off + r1[0] >= len(buf) or _plausible(buf, off + r1[0], 0, 0)): break
                        off += 1
                    resyncs += 1; skipped += off - start
                    if offsets is not None: offsets.append((base + start, base + off))
                    continue
                ln, lid, t = r
                yield (lid, t, ln, base + off) if emit_offset else (lid, t, ln)
                off += ln
            buf = buf[off:]
            base += off
            if eof and len(buf) < 12: break
            if eof and off == 0: break
    if stats is not None: stats.update(resyncs=resyncs, skipped_bytes=skipped)
