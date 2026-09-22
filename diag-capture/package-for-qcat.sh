#!/usr/bin/env bash
# After a cell: find the disturbed seconds, cut a DLF slice around each on BOTH hosts,
# and write a labelled package a QCAT/QXDM operator can open without asking us anything.
#
#   package-for-qcat.sh <celldir> <label> [out_dir]
#
# WHY THIS EXISTS. A 600 s cell produces ~6.6 GB of DIAG per host. Nobody opens a 6.6 GB
# DLF to look for a 600 ms event, and we cannot decode the records ourselves -- the RRC
# message contents need a licensed tool. So the value of a capture campaign is not the
# captures, it is the small, time-bounded, labelled slices that someone with QCAT will
# actually look at. Producing those by hand took most of an afternoon for one cell.
#
# THE MEDIA WINDOW IS TAKEN FROM capture_timestamp_us, NOT from the arming instant.
# probe_start is when a host armed; it differed from the media start by 53 s on one cell
# and 81 s on another, and every anomaly statistic computed over the wrong window is
# contaminated by lead-in seconds that carry no uplink load. This has produced two wrong
# answers already. capture_timestamp_us is stamped by the publisher at capture and is the
# only instant meaning the same thing on both hosts.
#
# CLOCKS. DIAG record timestamps are modem time (GPS-derived, ~UTC). Hosts run a PTP domain
# offset from UTC. modem = host - host_minus_utc_s, with the offset MEASURED per cell.
set -uo pipefail

CELLDIR=${1:?usage: package-for-qcat.sh <celldir> <label> [out_dir]}
LABEL=${2:?label required}
OUTDIR=${3:-$HOME/Downloads}
B=${B_HOST:-192.168.99.2}
DIAG=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIAG/.." && pwd)
PY="$DIAG/venv/bin/python3"
PAD=${PAD:-4}                 # seconds either side of a disturbed second
MAXEV=${MAXEV:-3}             # package at most this many events per cell
ssh_b() { timeout "${2:-60}" ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$B" "$1"; }
say() { printf '%s\n' "$*"; }

# THE GUARD THAT MATTERS. Selecting the right capture file is a heuristic and every
# heuristic we have tried has failed at least once -- ls -t picked a stale capture that
# was still writing, and an mtime gate accepted the very file it was written to reject.
# So do not rely on selection: CHECK THE PRODUCT. A slice that does not actually span the
# window we asked for is deleted and reported, whatever file it came from. This catches a
# stale capture, a non-overlapping window and a wrong clock offset with one test.
verify_slice() {
  local f=$1 want0=$2 want1=$3 who=$4
  if [ ! -s "$f" ]; then say "    $who slice MISSING or empty -- not packaged"; rm -f "$f"; return 1; fi
  python3 - "$f" "$want0" "$want1" "$who" <<'PYV'
import struct, sys, os
GPS=315964800; TICK=0.00125
f,w0,w1,who=sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
d=open(f,'rb').read(); i=0; ts=[]
while i < len(d)-12:
    ln,_=struct.unpack_from('<HH',d,i)
    if ln<12 or i+ln>len(d): break
    t,=struct.unpack_from('<Q',d,i+4)
    v=(t>>16)*TICK+(t&0xFFFF)/32768.0*TICK+GPS
    if 1.7e9<v<1.9e9: ts.append(v)
    i+=ln
if len(ts) < 100:
    print(f"    {who} slice has only {len(ts)} records -- REJECTED"); os.remove(f); sys.exit(1)
lo,hi=min(ts),max(ts)
# NO MIDPOINT CONDITION, DELIBERATELY -- it looks like an obviously sensible thing to add.
# A midpoint test only earns its place under SPAN coverage, where a window entirely
# outside the data can still score 100% and you need a second check that the window is
# really inside. Under per-second density that case scores zero on coverage alone, so the
# condition catches nothing coverage misses and adds an accept/reject cliff on top of a
# continuous measure -- the shape Host B removed after a reduction with 299 of 300 seconds
# present was discarded for having its one hole at the midpoint. Verified here: a window
# 100 s away from the data scores 0% and is rejected by coverage with no midpoint test.
# COVERAGE IS PER-SECOND PRESENCE, NOT THE SPAN FROM FIRST RECORD TO LAST.
# A span computation, (min(hi,w1)-max(lo,w0))/(w1-w0), is defeated by records clustered
# at the two edges with a hole between: it reports 100% for a slice that is empty across
# almost the whole window. Verified -- 300 records packed into half a second at each end
# of a 9 s window scored "100% of window" and PASSED the shipped version of this guard.
# Host B's equivalent check counts seconds present and was immune; this now matches it.
secs={int(t) for t in ts}
want=set(range(int(w0), int(w1)+1))
cov = len(secs & want) / max(1, len(want))
if cov < 0.5:
    print(f"    {who} slice covers only {cov*100:.0f}% of the requested window -- REJECTED")
    os.remove(f); sys.exit(1)
# BELOW 95% IS SHIPPED BUT MARKED, NOT SILENTLY ACCEPTED. Host B found a reduction at
# 53% passing a 50% floor and then being drawn as an ordinary page -- a gap wearing a
# guard's approval, which is worse than no guard, because an unguarded gap invites
# suspicion and a blessed one does not. Accept/reject is the wrong shape for a
# continuous quantity, so the figure travels with the slice.
flag = "" if cov >= 0.95 else f"  [INCOMPLETE: {cov*100:.0f}% of window]"
print(f"    {who} slice OK: {len(ts):,} records, {hi-lo:.1f}s, {cov*100:.0f}% of window, {os.path.getsize(f)/1e6:.0f} MB{flag}")
cov_log=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(f))),"coverage.txt")
with open(cov_log,"a") as cf:
    cf.write(f"{os.path.basename(f)}\t{len(ts)} records\t{cov*100:.0f}% of window\t"
             f"{'complete' if cov>=0.95 else 'INCOMPLETE'}\n")
PYV
}

pub="$CELLDIR/$LABEL.pub.csv"
adlf="$CELLDIR/$LABEL.dlf"
[ -s "$pub" ]  || { say "no publisher CSV ($pub) -- cannot establish the media window; refusing"; exit 1; }
[ -s "$adlf" ] || { say "no Host A DLF ($adlf); refusing"; exit 1; }

# ---- media window, from the publisher's own frame timestamps
read -r M0 M1 < <(python3 - "$pub" <<'PY'
import csv, sys
r=list(csv.DictReader(open(sys.argv[1])))
a=int(r[0]['capture_timestamp_us'])/1e6; b=int(r[-1]['capture_timestamp_us'])/1e6
print(f"{a:.3f} {b:.3f}")
PY
)
say "media window (host clock): $M0 .. $M1  ($(python3 -c "print(f'{$M1-$M0:.0f}')") s)"

# ---- offsets, measured NOW on each host, never remembered
# CLOCK OFFSETS. Measure now for a cell that has just run; for an ARCHIVED capture the
# offset that matters is the one that was true when it was recorded. These hosts are PTP-
# synced to each other but not to UTC, and drift against UTC by seconds per day -- 8 s in
# three days, measured. Re-slicing a three-day-old DLF with today's offset moves the
# window by 8 s and lands on the wrong data while looking entirely reasonable.
# Pass A_OFFSET / B_OFFSET (from that cell's own dlf-rates header) when reprocessing.
if [ -n "${A_OFFSET:-}" ]; then AOFF="$A_OFFSET"; say "using supplied Host A offset $AOFF (archived capture)"
else AOFF=$(python3 "$DIAG/clock-offset.py" 2>/dev/null); fi
if [ -n "${B_OFFSET:-}" ]; then BOFF="$B_OFFSET"
else BOFF=$(ssh_b 'python3 ~/diag-capture/clock-offset.py' 40); fi
[ -n "$AOFF" ] || { say "could not measure Host A clock offset; refusing to guess"; exit 1; }
say "clock offsets: A ${AOFF}  B ${BOFF:-unavailable}"

# ---- reduce A over the media window if we have not already
ar="$CELLDIR/dlf-rates-media.csv"
[ -s "$ar" ] || timeout 2400 python3 "$DIAG/dlf-rates.py" "$adlf" "${M0%.*}" "${M1%.*}" "$AOFF" "$ar" 30 >/dev/null 2>&1
[ -s "$ar" ] || { say "reduction failed; refusing"; exit 1; }
hdr=$(head -1 "$ar" | grep -oE 'host_minus_utc_s=[-0-9.]+' | cut -d= -f2)
if [ -n "$hdr" ]; then
  d=$(python3 -c "print(abs($hdr - ($AOFF)) > 1.0)")
  [ "$d" = True ] && { say "REFUSING: reduction was anchored at offset $hdr but we are slicing with $AOFF."
                       say "For an archived capture pass A_OFFSET=$hdr (and B_OFFSET from B's header)."; exit 1; }
fi

# ---- find disturbed seconds, normalised against the log's own thinning
MEDIA_SPAN=$(python3 -c "print(int($M1-$M0))")
mapfile -t EVENTS < <(python3 - "$ar" "$MAXEV" "$MEDIA_SPAN" <<'PY'
import csv, statistics, sys
from collections import defaultdict
per=defaultdict(dict); allsec=defaultdict(int)
for r in csv.reader(open(sys.argv[1])):
    if not r or r[0].startswith('#') or r[0]=='second_rel_probe': continue
    try: s=int(r[0]); c=int(r[1],16); n=int(r[2])
    except (ValueError,IndexError): continue
    per[c][s]=per[c].get(s,0)+n; allsec[s]+=n
# MEDIA SECONDS ONLY, and the bound comes from the PUBLISHER, not from the file.
# The reduction carries a margin either side, so max(second) runs past the end of the
# media. Searching that range flags capture-edge seconds -- where the uplink is idle and
# the code mix is completely different -- as though they were events. This has now
# produced a wrong answer three separate times; the media span is passed in for exactly
# that reason and must never be re-derived from the reduction.
span=int(sys.argv[3])
# TWO INDEPENDENT BOUNDS ON THE MEDIA END, and we take the earlier.
#
#   (a) the publisher's own last frame timestamp, passed in as span;
#   (b) a data-driven detection: the first second past mid-cell where the record rate
#       holds below 92% of steady state for five consecutive seconds.
#
# (b) exists because Host B found that when media stops the DIAG record rate does not
# dip, it STEPS DOWN about 12% and STAYS there. Normalising across both regimes makes
# the codes that fall hardest at the boundary look like outliers, and on B's first pass
# ALL NINETEEN overnight cycles reported their worst second at the boundary. (a) alone
# was sufficient on every cell we have checked -- A's rate is flat to 296 and steps at
# 298 -- but (a) trusts that the publisher stopped stamping frames exactly when the
# modem's load fell, and nothing guarantees that. Two bounds cost nothing.
steady=statistics.median([allsec[x] for x in range(3, min(span-10, 290)) if x in allsec])
run=0; detected=None
for x in sorted(allsec):
    if x < span//2: continue
    if allsec.get(x,0) < 0.92*steady:
        run += 1
        if run >= 5 and detected is None: detected = x-4
    else: run = 0
if detected is not None: span=min(span, detected)
# Trim the first and last few media seconds. The stream is starting up and shutting down
# there -- the encoder is ramping, the last frames are draining -- so the code mix differs
# from steady state and those seconds flag as events without being any. The hand analysis
# of cell5m-a excluded them for this reason and found exactly one event; not excluding
# them here surfaced the final second as a second false positive.
EDGE=3
MID=[s for s in range(EDGE, span-EDGE+1) if s in allsec]
if len(MID)<60: sys.exit(0)
med={c:statistics.median([d.get(s,0) for s in MID]) for c,d in per.items()}
codes={c:m for c,m in med.items() if m>=5}
logmed=statistics.median([allsec[s] for s in MID])
scored=[]
for s in MID:
    ld=100.0*(allsec[s]-logmed)/logmed
    n=sum(1 for c,m in codes.items() if 100.0*(per[c].get(s,0)-m)/m-ld < -30)
    scored.append((n,s))
base=statistics.median([n for n,_ in scored])
# a real event is far above this cell's OWN baseline, not above a fixed number
hits=sorted((n,s) for n,s in scored if n >= max(15, base+12))
for n,s in sorted(hits, reverse=True)[:int(sys.argv[2])]:
    print(f"{s} {n}")
PY
)

if [ ${#EVENTS[@]} -eq 0 ]; then
  say "no disturbed second found in this cell -- nothing to package (this is a normal result)"
  exit 0
fi
say "disturbed seconds: ${#EVENTS[@]}"

PKG="$OUTDIR/${LABEL}-qcat"
rm -rf "$PKG"; mkdir -p "$PKG/hostA" "$PKG/hostB"
SUMMARY=""
for e in "${EVENTS[@]}"; do
  read -r sec score <<<"$e"
  ev_host=$(python3 -c "print(f'{$M0 + $sec:.3f}')")
  t0=$(python3 -c "print(f'{$M0 + $sec - $PAD - $AOFF:.2f}')")
  t1=$(python3 -c "print(f'{$M0 + $sec + 1 + $PAD - $AOFF:.2f}')")
  # LABEL IN TRUE UTC, NOT THE HOST CLOCK. These hosts are PTP-locked to each other and to
  # NOTHING ELSE -- no NTP, no chrony, a free-running grandmaster drifting ~2.8 s/day, 25 s
  # behind UTC as of 2026-09-22. The DLF records themselves are network-disciplined and ARE
  # true UTC, so a filename or README stamped with the host clock and a "Z" tells an external
  # reader a time 25 s away from what they will see inside the file. Subtracting the measured
  # offset converts host -> UTC, which is the same arithmetic the slice window already uses.
  ev_utc=$(python3 -c "print(f'{$M0 + $sec - $AOFF:.3f}')")
  z=$(date -u -d "@${ev_utc%.*}" +%H-%M-%SZ)
  say "  event at media t=${sec}s ($z, $score codes) -> slicing modem $t0 .. $t1"
  timeout 1800 "$PY" "$DIAG/dlf-slice.staged" "$adlf" \
    "$PKG/hostA/${LABEL}_hostA_event_t${sec}s_${z}.dlf" "$t0" "$t1" >/dev/null 2>&1 || true
  verify_slice "$PKG/hostA/${LABEL}_hostA_event_t${sec}s_${z}.dlf" "$t0" "$t1" A
  if [ -n "${BOFF:-}" ]; then
    bt0=$(python3 -c "print(f'{$M0 + $sec - $PAD - $BOFF:.2f}')")
    bt1=$(python3 -c "print(f'{$M0 + $sec + 1 + $PAD - $BOFF:.2f}')")
    # PICK B'S CAPTURE BY ITS FILENAME TIMESTAMP, NOT BY mtime OR ls -t.
    # The capture scripts stamp creation time into the name with the same clock
    # date(1) reads, so the name is the authority. A previous run's capture that is
    # STILL WRITING carries an mtime of "just now" and wins an `ls -t`, which is
    # exactly how a stale capture got reduced on Host B for cell5m-a2. We want the
    # newest capture that STARTED AT OR BEFORE the media began.
    mstart=$(python3 -c "print(int($M0))")
    bdlf=$(ssh_b "for f in ~/diag-logs/${LABEL}-*.dlf; do
        [ -e \"\$f\" ] || continue
        n=\$(basename \"\$f\"); st=\${n##*-}; st=\${st%%.dlf}
        e=\$(date -u -d \"\${st:0:8} \${st:9:2}:\${st:11:2}:\${st:13:2}\" +%s 2>/dev/null) || continue
        [ \"\$e\" -le $(( mstart + 5 )) ] && echo \"\$e \$f\"
      done | sort -n | tail -1 | cut -d' ' -f2-" 60)
    if [ -z "$bdlf" ]; then
      say "    B: no capture whose filename time precedes the media start -- NOT slicing"
    else
      say "    B capture: $(basename "$bdlf")"
      ssh_b "timeout 1800 ~/diag-capture/dlf-slice \"$bdlf\" ~/${LABEL}_hostB_event_t${sec}s.dlf $bt0 $bt1 >/dev/null 2>&1" 1900
      rsync -a --timeout=600 "$B:~/${LABEL}_hostB_event_t${sec}s.dlf" \
        "$PKG/hostB/${LABEL}_hostB_event_t${sec}s_${z}.dlf" 2>/dev/null || true
      verify_slice "$PKG/hostB/${LABEL}_hostB_event_t${sec}s_${z}.dlf" "$bt0" "$bt1" B
    fi
  fi
  SUMMARY="${SUMMARY}  media t=${sec}s  ${z}  ${score} log codes disturbed"$'\n'
done

# ---- RRC messages inside each slice, so the operator knows what to open
RRC=$(python3 - "$PKG" "$AOFF" <<'PY'
import struct, glob, sys, datetime
GPS=315964800; TICK=0.00125
off=float(sys.argv[2])
out=[]
for p in sorted(glob.glob(sys.argv[1]+'/host*/*.dlf')):
    d=open(p,'rb').read(); i=0; hits=[]
    while i < len(d)-12:
        ln,code=struct.unpack_from('<HH',d,i)
        if ln<12 or i+ln>len(d): break
        if code==0xB821:
            ts,=struct.unpack_from('<Q',d,i+4)
            t=(ts>>16)*TICK+(ts&0xFFFF)/32768.0*TICK+GPS
            if 1.7e9<t<1.9e9: hits.append(t+off)
        i+=ln
    f=lambda x: datetime.datetime.fromtimestamp(x, datetime.UTC).strftime('%H:%M:%S.%f')[:-3]
    out.append(f"  {p.split('/')[-1]}")
    out.append(f"      {len(hits)} x 0xB821 NR RRC OTA" + (": " + ", ".join(f(t)+'Z' for t in hits) if hits else ""))
print("\n".join(out))
PY
)

cat > "$PKG/README.txt" <<EOF
================================================================================
$LABEL  --  Qualcomm DIAG slices for QCAT/QXDM decode
================================================================================
Cell      : $LABEL
Media     : $(date -u -d "@$(python3 -c "print(int($M0 - $AOFF))")" +%FT%H:%M:%SZ) .. $(date -u -d "@$(python3 -c "print(int($M1 - $AOFF))")" +%H:%M:%SZ)  (TRUE UTC, matching the DLF records)
Hosts     : two Quectel RM520N-GL on the same 5G network, capturing concurrently
Clocks    : modem_time = host_time - (host_minus_utc_s)
              Host A  host_minus_utc_s = $AOFF   -> modem = host + $(python3 -c "print(f'{-1*$AOFF:.3f}')")
              Host B  host_minus_utc_s = ${BOFF:-n/a}
            ALL TIMES IN THIS README AND IN THE FILENAMES ARE TRUE UTC, the same clock
            the DLF records carry. Our capture hosts run $AOFF s from UTC (free-running,
            PTP-locked to each other only, drifting ~2.8 s/day) and that offset has already
            been applied. Do not re-apply it.

            PRECISION: do not read these absolute labels to better than ~30 ms. The offset
            is measured once per capture against public NTP (5.8 ms stdev, 15.8 ms run-to-
            run spread) and then DRIFTS DURING the capture -- at 32 us/s that is 29 ms
            across a 900 s cell, which dominates the measurement noise. Two packages from
            our two hosts may label the same instant ~30 ms apart; neither is wrong, they
            bracket it. Run-relative times and Host-A-versus-Host-B comparisons are
            unaffected and remain good to microseconds, because the hosts are PTP-locked
            to each other even though neither is locked to UTC.

DISTURBED SECONDS PACKAGED HERE (relative to media start)
$SUMMARY
Each slice is +/- ${PAD}s around the second, cut on record boundaries only.

THE ASK
  1. Decode the 0xB821 (NR RRC OTA) records. What message types are they?
  2. Around the disturbed second, do 0xB883 / 0xB872 / 0xB873 show a grant
     withdrawal, a beam or bandwidth-part switch, or a handover interruption?
  3. Any PDCP/RLC discard indication, and what triggered it (buffer full vs
     discard timer expiry)?

SLICE COVERAGE -- how much of each requested window the slice actually contains.
Anything marked INCOMPLETE has a gap; the figure is per-second presence, not the span
from first record to last, because a slice with records only at the two edges reaches
across the whole window while containing nothing.
$(cat "$PKG/coverage.txt" 2>/dev/null | sed 's/^/  /' || echo "  (none recorded)")

RRC MESSAGES PRESENT
$RRC

FORMAT
  Raw DLF (QCSuper --dlf-dump): inner payloads of DIAG_LOG_F records, which
  QCSuper documents as openable with QCSuper or QXDM. QCAT reads DLF directly.
  Recent QXDM defaults to ISF and may want an import step. These can be
  re-wrapped as QMDL on request.

HOW THE DISTURBED SECONDS WERE FOUND
  Per-second record counts per log code, each normalised against the whole
  log's own thinning in that second, so generalised logging loss cancels. A
  second is flagged when many codes depart sharply from the log's behaviour.
  This is a screening statistic for choosing what to slice -- not a claim
  about cause.
================================================================================
EOF

sz=$(du -sh "$PKG" | cut -f1)
say "package: $PKG  ($sz)"
( cd "$OUTDIR" && zip -rq "${LABEL}-qcat.zip" "$(basename "$PKG")" ) \
  && say "zip: $OUTDIR/${LABEL}-qcat.zip ($(du -h "$OUTDIR/${LABEL}-qcat.zip" | cut -f1))"
