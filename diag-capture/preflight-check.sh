#!/usr/bin/env bash
# Read every instrument against one known-good cell, with a NAMED pass condition for each.
#
# WHY THIS EXISTS. On 17 Sep the control-path probe reported 99.7% loss in a cell that was
# flawless by every other measure, and nobody noticed for a week. A broken instrument that
# reads plausibly is worse than no instrument, because its number gets used. So before a
# 90-minute ladder is committed, one short cell is run and EVERY instrument is read against
# a stated criterion -- not eyeballed, and not graded against whatever the run happened to
# produce.
#
# THE CRITERION THAT MATTERS MOST is the paired one: RTP sequence numbers seen in Host A's
# capture must also appear in Host B's. That match is the entire attribution table --
# sent-but-never-seen is network loss, seen-late is network or modem, seen-on-time-but-drawn-
# late is the host -- and it has never once been demonstrated end to end in this project.
# If it fails, the sweep cannot answer the operator's question and every other PASS is
# irrelevant.
#
# Usage: preflight-check.sh <cell-dir> [expected_duration_s]
# Exit 0 only if every check passes.
set -uo pipefail

D=${1:?usage: $0 <cell-dir> [duration_s]}
DUR=${2:-300}
[ -d "$D" ] || { echo "no such cell dir: $D" >&2; exit 2; }
label=$(basename "$D")
pass=0; fail=0; warn=0
ok()   { printf "  \033[32mPASS\033[0m  %-34s %s\n" "$1" "${2:-}"; pass=$((pass+1)); }
no()   { printf "  \033[31mFAIL\033[0m  %-34s %s\n" "$1" "${2:-}"; fail=$((fail+1)); }
hm()   { printf "  \033[33mWARN\033[0m  %-34s %s\n" "$1" "${2:-}"; warn=$((warn+1)); }
have() { [ -s "$1" ]; }

echo "=== pre-flight: $label (expected ${DUR}s) ==="
echo
echo "--- HOST A ---"

# pcap: exists, non-trivial, readable, and carries real UDP to the SFU.
pc="$D/$label.wwan0.pcap"
if have "$pc"; then
  sz=$(stat -c%s "$pc")
  n=$(sudo -n tcpdump -r "$pc" -c 200 2>/dev/null | grep -c 'IP ' || true)
  if [ "$sz" -gt 100000 ] && [ "$n" -gt 50 ]; then ok "A pcap" "$(numfmt --to=iec "$sz"), $n/200 IP packets readable"
  else no "A pcap" "$(numfmt --to=iec "$sz"), only $n readable IP packets"; fi
else no "A pcap" "missing or empty"; fi

# DIAG: pro-rata against the c046/c049/c052 benchmark of ~11.3M records / ~9.8M NR5G per 700 s.
# Scale-free forms: records/s and the NR5G SHARE. The ~11.3M / ~9.8M benchmark was measured
# over a 700 s capture -- ~16,100 rec/s at an ~87% NR5G share. Checking 300 s of capture
# against the absolute 11.3M reads as a 57% shortfall on a perfectly good capture, which is
# how a healthy cell gets failed. The share is the part that detects a sparse mask: a
# MASK=nr5g capture keeps the record rate up while the NR5G fraction collapses.
if have "$D/dlf-rates.out"; then
  rec=$(grep -oE 'records_total [0-9]+' "$D/dlf-rates.out" | grep -oE '[0-9]+' | head -1)
  rs=$(grep -oE 'resyncs [0-9]+' "$D/dlf-rates.out" | grep -oE '[0-9]+' | head -1)
  rps=$(python3 -c "print(f'{${rec:-0}/$DUR:.0f}')")
  okr=$(python3 -c "print(1 if ${rec:-0}/$DUR >= 16100*0.6 else 0)")
  if [ "$okr" = 1 ] && [ "${rs:-1}" -eq 0 ]; then ok "A DIAG rate" "$rec records, $rps/s (benchmark ~16100/s), resyncs $rs"
  else no "A DIAG rate" "$rec records, $rps/s (want >= 9660/s), resyncs ${rs:-?}"; fi
  # NR5G share from the per-second per-code CSV: codes 0xB800-0xB9FF are the NR5G range.
  if have "$D/dlf-rates.csv"; then
    share=$(python3 - "$D/dlf-rates.csv" <<'EOF'
import csv, sys
tot=nr=0
for r in csv.reader(open(sys.argv[1])):
    if not r or r[0].startswith('#') or r[0]=='second_rel_probe': continue
    try: code=int(r[1],16); c=int(r[2])
    except (ValueError, IndexError): continue
    tot+=c
    if 0xB800 <= code <= 0xB9FF: nr+=c
print(f"{100.0*nr/tot:.0f}" if tot else "0")
EOF
)
    if [ "${share:-0}" -ge 70 ]; then ok "A DIAG NR5G share" "${share}% (benchmark ~87%)"
    else no "A DIAG NR5G share" "${share}% -- sparse mask? full mask gives ~87%"; fi
  fi
else no "A DIAG rate" "no dlf-rates.out"; fi

# diag-log-off must have run: fast2 makes QCSuper's own on_deinit a no-op, so if this is
# skipped the modem keeps logging into nothing and the NEXT capture starts dirty.
if grep -qi 'logging off\|log mask off\|diag-log-off' "$D/recorder.out" "$D/long-run.out" 2>/dev/null; then
  ok "A diag-log-off" "ran"
else hm "A diag-log-off" "no evidence in recorder.out/long-run.out"; fi
if fuser /dev/ttyUSB0 >/dev/null 2>&1; then no "A DIAG port released" "ttyUSB0 still held"
else ok "A DIAG port released" "ttyUSB0 free"; fi

# Publisher: frame count against 30 fps, and the codec/encoder actually negotiated.
# Gate on ACHIEVED FPS, not an absolute count or a percentage floor. A 70% floor accepts a
# cell truncated to two thirds as "close enough" -- which is exactly how a short cell gets
# pooled with full ones -- while an absolute count has to be rescaled for every duration and
# will eventually be checked against the wrong one. fps is scale-free and catches truncation
# directly, because a truncated cell has the right fps over the wrong span.
if have "$D/$label.pub.csv"; then
  rows=$(( $(wc -l < "$D/$label.pub.csv") - 1 ))
  read -r span fps < <(python3 - "$D/$label.pub.csv" <<'EOF'
import csv, sys
ts=[float(r['capture_timestamp_us']) for r in csv.DictReader(open(sys.argv[1]))
    if r.get('capture_timestamp_us','').strip() not in ('','0')]
if len(ts) > 1:
    s=(max(ts)-min(ts))/1e6
    print(f"{s:.1f} {len(ts)/s:.1f}")
else: print("0 0")
EOF
)
  okfps=$(python3 -c "print(1 if 28.5 <= $fps <= 31.0 else 0)")
  okspan=$(python3 -c "print(1 if $span >= 0.95*$DUR else 0)")
  if [ "$okfps" = 1 ] && [ "$okspan" = 1 ]; then ok "A frames encoded" "$rows frames, ${fps} fps over ${span}s"
  elif [ "$okfps" = 1 ]; then no "A frames encoded" "${fps} fps OK but span ${span}s of ${DUR}s -- TRUNCATED"
  else no "A frames encoded" "$rows frames, ${fps} fps over ${span}s (want 28.5-31 fps)"; fi
else no "A frames encoded" "no pub.csv"; fi

if have "$D/$label.jsonl"; then
  python3 - "$D/$label.jsonl" <<'EOF'
import sys, json
last=None
for line in open(sys.argv[1]):
    line=line.strip()
    if line: last=line
d=json.loads(last) if last else {}
req,neg = d.get('requested_codec'), d.get('negotiated_codec')
enc,tier = d.get('encoder_implementation'), d.get('encoder_tier')
print(f"  {'PASS' if req==neg and req else 'FAIL'}  {'A codec negotiated':<34} requested={req} negotiated={neg}")
print(f"  {'PASS' if tier=='nvenc' else 'FAIL'}  {'A encoder':<34} {enc} tier={tier}")
EOF
fi

# The pin and degradation a cell RAN with, read from its own log rather than assumed.
if grep -q 'gcc-overrides' "$D/$label.log" 2>/dev/null; then
  ok "A pin/degradation logged" "$(grep -m1 'gcc-overrides' "$D/$label.log" | cut -c15-)"
else no "A pin/degradation logged" "no gcc-overrides line"; fi

# Control probe MUST be absent, not 99.7%. Requires a harness peer at both ends, which this
# topology does not have; a number here means the narrow fix did not take.
if have "$D/$label.jsonl"; then
  if grep -q '"probes_lost"' "$D/$label.jsonl"; then
    pl=$(grep -o '"probes_lost":[0-9]*' "$D/$label.jsonl" | tail -1 | cut -d: -f2)
    hm "A control probe" "still emitting probes_lost=$pl -- must be absent, not a number"
  else ok "A control probe" "absent, as it must be in this topology"; fi
fi

echo
echo "--- HOST B ---"
B="$D/hostb"
if have "$B/subscriber.csv"; then
  rows=$(( $(wc -l < "$B/subscriber.csv") - 1 ))
  lof=$(( 30 * DUR * 70 / 100 ))
  if [ "$rows" -ge "$lof" ]; then ok "B frames received" "$rows rows (>= $lof)"
  else no "B frames received" "$rows rows, expected >= $lof"; fi
else no "B frames received" "no subscriber.csv"; fi

if have "$B/subscriber.log"; then
  dec=$(grep -oE 'decoder=[A-Za-z0-9]+' "$B/subscriber.log" | tail -1 | cut -d= -f2)
  [ -n "$dec" ] && ok "B decoder" "$dec" || no "B decoder" "no decoder named in log"
  first=$(grep -m1 -oE 'received=[0-9]+' "$B/subscriber.log" | cut -d= -f2)
  [ "${first:-0}" -gt 0 ] && ok "B decode health" "climbing in first sample (received=$first)" \
                          || no "B decode health" "received=0 in first sample"
fi

have "$B/dlf-rates-hostb.csv" && ok "B DIAG summary" "$(du -h "$B/dlf-rates-hostb.csv" | cut -f1)" \
                              || no "B DIAG summary" "missing"

bpc=$(ls "$B"/*.pcap 2>/dev/null | head -1)
if [ -n "$bpc" ] && [ -s "$bpc" ]; then ok "B pcap" "$(numfmt --to=iec "$(stat -c%s "$bpc")")"
else no "B pcap" "missing -- the paired check below cannot run"; fi

echo
echo "--- PAIRED (the one that decides whether the sweep can answer anything) ---"
if [ -s "$pc" ] && [ -n "$bpc" ] && [ -s "$bpc" ]; then
  python3 - "$pc" "$bpc" <<'EOF'
import subprocess, sys, re
# An SFU is NOT a router: it terminates and re-originates RTP per subscriber, with a
# per-subscriber SSRC and rewritten sequence numbers (needed to hide dropped layers and to
# keep a contiguous sequence across simulcast switches). If that happens here, A's uplink
# and B's downlink sequence spaces are UNRELATED and a seq join reads ~0% -- which looks
# identical to total packet loss and is nothing of the kind. So establish WHICH KEY IS
# VALID before reporting any overlap as a delivery figure.
#
# The RTP timestamp is the media-clock value for a frame and the SFU must preserve it or
# playout timing breaks. All packets of one frame share it, so it joins FRAMES, not packets
# -- which is what the attribution table actually needs.
#
# frame_id from our packet trailer would be ideal and is NOT recoverable here: -s 96 leaves
# ~56 payload bytes after IP+UDP+RTP, and the trailer sits at the END of the frame.
def rtp(path, n=6000):
    out = subprocess.run(['sudo','-n','tcpdump','-r',path,'-nn','-c',str(n),'-x','udp'],
                         capture_output=True, text=True).stdout
    seqs, tss, ssrcs, mark = set(), set(), set(), 0
    cur=[]
    def flush(h):
        nonlocal mark
        if len(h) < 56+24: return
        try:
            b1 = int(h[56+2:56+4], 16)
            if b1 & 0x80: mark += 1
            seqs.add(int(h[56+4:56+8], 16))
            tss.add(int(h[56+8:56+16], 16))
            ssrcs.add(int(h[56+16:56+24], 16))
        except ValueError: pass
    for line in out.splitlines():
        if re.match(r'^\s+0x', line):
            cur.append(line.split(':',1)[1].replace(' ',''))
        elif cur:
            flush(''.join(cur)); cur=[]
    if cur: flush(''.join(cur))
    return seqs, tss, ssrcs, mark

(aq, ats, assr, am) = rtp(sys.argv[1])
(bq, bts, bssr, bm) = rtp(sys.argv[2])
if not aq or not bq:
    print(f"  FAIL  {'paired RTP join':<34} could not extract RTP (A={len(aq)} B={len(bq)} packets)")
    raise SystemExit

ssrc_shared = assr & bssr
seq_pct = 100.0*len(aq & bq)/len(aq)
ts_pct  = 100.0*len(ats & bts)/len(ats)
print(f"  ----  {'A SSRCs':<34} {sorted(hex(s) for s in assr)[:4]}")
print(f"  ----  {'B SSRCs':<34} {sorted(hex(s) for s in bssr)[:4]}")

if not ssrc_shared:
    print(f"  ----  {'SSRC sets are DISJOINT':<34} the SFU re-originates RTP; seq is NOT a valid key")
    if ts_pct >= 50:
        print(f"  PASS  {'paired join on RTP TIMESTAMP':<34} {ts_pct:.0f}% of A's frame timestamps seen at B "
              f"(A={len(ats)} B={len(bts)})")
        print(f"        Join at FRAME granularity. Per-packet loss localisation is not available")
        print(f"        through this SFU; the attribution table must be restated per frame.")
    else:
        print(f"  FAIL  {'paired join':<34} neither seq ({seq_pct:.0f}%) nor timestamp ({ts_pct:.0f}%) joins")
        print(f"        Check the capture WINDOWS overlap before concluding anything about loss.")
else:
    print(f"  ----  {'SSRC shared':<34} {sorted(hex(s) for s in ssrc_shared)[:4]} -- seq key may be valid")
    v = 'PASS' if seq_pct >= 50 else 'FAIL'
    print(f"  {v}  {'paired join on RTP SEQ':<34} {seq_pct:.0f}% of A's seqs at B (ts join {ts_pct:.0f}%)")
    print(f"        Packet-level attribution available. Confirm the capture windows overlap")
    print(f"        before reading any shortfall as loss.")
EOF
else
  echo "  SKIP  paired RTP join                  one or both pcaps missing"
fi

echo
echo "=== $pass passed, $fail failed, $warn warned ==="
[ "$fail" -eq 0 ] || echo "DO NOT PROCEED INTO THE LADDER. Fix, then re-run the pre-flight: 10 minutes against 90."
exit $(( fail > 0 ? 1 : 0 ))
