#!/usr/bin/env bash
# Repeated paired cells overnight, hunting the receive-side reconfiguration signature.
#
# WHY A NEW LOOP AND NOT overnight-driver2.sh: that driver has its own publish path and its
# own B arming, neither of which carries the 2026-09-23 fixes (LK_URL/CLIP passthrough, the
# pre-publish lead check, the SFU/clip product assertion). This wraps paired-cell.sh, which
# has them. Duplicating a loop is cheaper than auditing a second publish path.
#
# NOTHING IS EVER DELETED. Operator instruction, stated twice. The loop STOPS when disk runs
# low; it does not reclaim. B ships its DLF here after each cell because B has ~97 GB against
# this host's ~569 GB, and 30 GB/hour per host means B alone holds ~3 hours.
#
# WHAT THE RADIO SCAN IS AND IS NOT. find-beam-events.py is a CANDIDATE FINDER, not an event
# detector, and the index says "candidate" for that reason. Measured 2026-09-23 on cell5m-a,
# the capture whose true event count is 1: it fires SIX times (+8.65, -9.23, +6.46, -9.39,
# +8.85, +5.70 dB). Only the 21:46:26 +8.85 is the real event. A 5-in-6 false-positive rate
# over ~100 cells is ~600 phantom rows, which would bury the real ones.
#
# What actually distinguished the real event was NOT available in A's DLF bytes: the per-Rx-path
# differential (Rx1 +11.8 while Rx3 -2.0) and the PUSCH collapse both came from QCAT panels, and
# the four Rx paths have never been located in these records. The sentinel test was tried as a
# substitute and does not separate them either -- 21:40:43 scores 88%/75% against the real
# event's 100%/62%.
#
# SO THE TRUSTWORTHY TRIGGER IS THE VIDEO SIDE, which is why every row also carries B's frame
# count and A's packet counters. In cell5m-a the video signature was unambiguous -- 361 packets
# lost with 88% inside one 900 ms window -- and it is what the operator actually cares about.
# Radio candidates are for looking at the windows the video flags, not the other way round.
#
# THE CAMPAIGN IS VOID WITHOUT A WORKING SCAN, so prove the scan still fires.
# At a 1-in-19 base rate most cells are clean, and 80 clean cells look identical whether the
# detector works or is broken. Every CONTROL_EVERY cells we re-scan a slice containing a KNOWN
# event; if that stops firing, everything after it is unusable and the log says so. This is the
# one guard in the campaign that can fail, which is why it is here.
#
# Usage: beam-hunt.sh <end_unix_s> [cell_s=900] [cap_kbps=5000] [codec=h264]
#        e.g.  beam-hunt.sh $(date -u -d '08:00 tomorrow' +%s)
set -uo pipefail

END=${1:?usage: beam-hunt.sh <end_unix_s> [cell_s] [cap_kbps] [codec]}
CELL=${2:-900}
CAP=${3:-5000}
CODEC=${4:-h264}
CONTROL_EVERY=${CONTROL_EVERY:-10}
MIN_FREE_GB_A=${MIN_FREE_GB_A:-60}
MIN_FREE_GB_B=${MIN_FREE_GB_B:-20}
B=${B_HOST:-192.168.99.2}

DIAG=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIAG/.." && pwd)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
CAMP="$REPO/results/beam-hunt-$STAMP"
# LABELS MUST BE UNIQUE ACROSS CAMPAIGNS. The first restart on 2026-09-23 began again at
# bh-001 while results/bh-001 still held the previous campaign's good 4.17 GB cell, and
# hop-recorder.sh opens "$out/$label.dlf" for writing -- it would have truncated it. Caught
# during the 180 s arming lead with minutes to spare. The operator's instruction is that
# nothing is deleted, and silently overwriting is deleting.
TAG="bh$(date -u -d "${STAMP:0:4}-${STAMP:4:2}-${STAMP:6:2} ${STAMP:9:2}:${STAMP:11:2}" +%m%d%H%M 2>/dev/null || date -u +%m%d%H%M)"
mkdir -p "$CAMP"
INDEX="$CAMP/event-index.csv"
LOG="$CAMP/campaign.log"
CONTROL_SLICE="$REPO/results/cell5m-a/cell5m-a-EVENT-for-qcat.dlf"

log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*" | tee -a "$LOG"; }
ssh_b() { timeout "${2:-30}" ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$B" "$1"; }
free_gb()   { df --output=avail -BG /home | tail -1 | tr -dc '0-9'; }
free_gb_b() { ssh_b 'df --output=avail -BG ~ | tail -1 | tr -dc "0-9"' 20; }

[ -f "$CONTROL_SLICE" ] || { log "REFUSING: control slice missing: $CONTROL_SLICE"; exit 1; }
# Prove the control fires BEFORE the campaign, not only during it. A control that never
# worked would silently bless every clean cell.
if ! python3 "$DIAG/find-beam-events.py" "$CONTROL_SLICE" 2>&1 | grep -q "SIGNATURE MATCH"; then
  log "REFUSING: the positive control does not fire on a slice with a KNOWN event."
  log "          The detector is broken; a campaign now would produce 80 meaningless clean cells."
  exit 1
fi
log "positive control OK before start (detector fires on the known event)"

[ -f "$INDEX" ] || echo "cell,label,epoch_utc,kind,rc,dlf_gb,dlf_resyncs,partial,b_frames,b_lost_max,b_dlf_shipped,verdict,event_utc,step_db,notes" > "$INDEX"
log "=== campaign $CAMP ==="
log "cells of ${CELL}s at ${CAP}k $CODEC until $(date -u -d @"$END" +%FT%TZ); control every $CONTROL_EVERY"

# Never start on top of a live run: paired-cell.sh holds the DIAG port and the room.
if pgrep -x paired-cell.sh >/dev/null 2>&1 || pgrep -f "[p]aired-cell.sh " >/dev/null 2>&1; then
  log "REFUSING: a paired-cell.sh is already running. Wait for it to finish."
  exit 1
fi

k=0; fails=0
while [ "$(date -u +%s)" -lt "$END" ]; do
  # ---- disk gates, both hosts, BEFORE arming anything
  fa=$(free_gb); fb=$(free_gb_b)
  [ -n "$fb" ] || { log "cannot read B's free space; stopping rather than guessing"; break; }
  if [ "$fa" -lt "$MIN_FREE_GB_A" ]; then log "STOP: A has ${fa}GB free (< ${MIN_FREE_GB_A}GB). Nothing deleted."; break; fi
  if [ "$fb" -lt "$MIN_FREE_GB_B" ]; then log "STOP: B has ${fb}GB free (< ${MIN_FREE_GB_B}GB). Nothing deleted."; break; fi
  # A whole cell must fit in the remaining time, or we cut one short for nothing.
  left=$(( END - $(date -u +%s) ))
  if [ "$left" -lt $(( CELL + 420 )) ]; then log "STOP: ${left}s left, less than a cell plus overhead."; break; fi

  k=$((k+1))
  label="$TAG-$(printf '%03d' "$k")"
  log "--- cell $k: $label  (A ${fa}GB free, B ${fb}GB free, ${left}s to end) ---"

  # CLEAR A PROVABLY-FINISHED SUBSCRIBER ON B BEFORE ARMING.
  #
  # Measured 2026-09-23: EVERY completed cell leaves B's subscriber hung. It logs "Track
  # unpublished ... ending the run", writes nothing further, and then sits in state S with 31
  # threads indefinitely -- 3h44m on one occasion. Two campaigns, two cells each, identical
  # signature, so this is reproducible and not a race. The 30 s re-check above was the right
  # mitigation for a transient and cannot help with this.
  #
  # KILL ONLY ON POSITIVE EVIDENCE THAT IT IS FINISHED, never on "it has been a while":
  #   1. its log's last lines say "ending the run" / "Track unpublished", AND
  #   2. its CSV has stopped growing across a 3 s window.
  # A subscriber genuinely mid-cell fails both, and is left alone. SIGTERM only -- B confirmed
  # TERM is sufficient, so the process is not wedged in the kernel and SIGKILL would only risk
  # truncating a CSV that a real cell was still writing.
  #
  # This is deliberately on A's side: A's loop, A's existing ssh channel, A's own failure
  # handling. It is not a workaround for B's file-creation restriction -- B kills these by hand
  # today; what B cannot do is write the automation.
  bres=$(ssh_b '
    pid=$(pgrep -x subscriber | head -1)
    [ -z "$pid" ] && { echo none; exit 0; }
    lg=$(ls -t ~/teleop-runs/*/subscriber.out 2>/dev/null | head -1)
    cv=$(ls -t ~/teleop-runs/*/subscriber.csv 2>/dev/null | head -1)
    done_msg=$(tail -5 "$lg" 2>/dev/null | grep -cE "ending the run|Track unpublished")
    a=$(stat -c%s "$cv" 2>/dev/null || echo -1); sleep 3; b=$(stat -c%s "$cv" 2>/dev/null || echo -2)
    if [ "${done_msg:-0}" -ge 1 ] && [ "$a" = "$b" ]; then
      kill -TERM "$pid" 2>/dev/null && echo "cleared $pid" || echo "failed $pid"
    else
      echo "active $pid"
    fi' 40)
  case "${bres:-}" in
    cleared*) log "    cleared a finished-but-hung subscriber on B (${bres#cleared }); waiting 5s"; sleep 5 ;;
    active*)  log "    B has an ACTIVE subscriber (${bres#active }) -- leaving it alone; the cell will refuse" ;;
    failed*)  log "    could not signal B's hung subscriber (${bres#failed }); the cell will refuse" ;;
  esac

  # NEVER WRITE INTO AN EXISTING CELL DIRECTORY. The unique TAG above should make this
  # impossible, but a collision costs a capture that cannot be recovered, so check the product
  # rather than trusting the naming scheme -- every naming heuristic in this codebase has failed
  # at least once. Skipping a label is free; overwriting a 4 GB DLF is not.
  if [ -e "$REPO/results/$label" ]; then
    log "    REFUSING: $REPO/results/$label already exists -- would overwrite an existing"
    log "              capture. Skipping this label rather than destroying data."
    echo "$k,$label,,cell,skipped,,,,,,,SKIPPED,,,results dir already exists" >> "$INDEX"
    continue
  fi

  rc=0
  "$DIAG/paired-cell.sh" "$label" "$CAP" "$CODEC" "$CELL" >>"$CAMP/$label.out" 2>&1 || rc=$?
  D="$REPO/results/$label"

  # RETRY ONCE IF A HOST IS MERELY BUSY. On 2026-09-23 a subscriber on B reached "ending the
  # run" and then never exited -- 31 threads, state S, for 3h44m. The collision guard refused
  # bh-002 and bh-003 correctly, two consecutive failures aborted, and the night was gone. A
  # process genuinely mid-exit clears in seconds; one hung like that does not. So re-check once
  # rather than spending the campaign on a transient.
  if [ "$rc" != 0 ] && grep -qiE "already busy|holds the DIAG port|another capture" "$CAMP/$label.out"; then
    log "    busy on first attempt: $(grep -hoiE "(Host [AB]|A|B) (already )?busy[^\"]*" "$CAMP/$label.out" | tail -1)"
    log "    waiting 30s and re-checking once before counting this as a failure"
    sleep 30
    rc=0
    "$DIAG/paired-cell.sh" "$label" "$CAP" "$CODEC" "$CELL" >>"$CAMP/$label.out" 2>&1 || rc=$?
  fi

  if [ "$rc" != 0 ]; then
    fails=$((fails+1))
    # PUT THE REASON IN THE INDEX, NOT ONLY IN A FILE ON ONE HOST. The first abort recorded
    # bare "FAILED" twice; the cause lived in bh-002.out on A's disk and B could not see it,
    # which turned a two-minute fix into a four-hour loss. A failure that does not name itself
    # is a failure nobody can act on from the other side.
    why=$(grep -hoiE "REFUSING[^\"]*|cell did not go LIVE|WRONG (SFU|CLIP)|ABORT[^\"]*" "$CAMP/$label.out" \
          | tail -1 | tr ',' ';' | cut -c1-120)
    log "    cell FAILED rc=$rc (consecutive: $fails) reason: ${why:-see $CAMP/$label.out}"
    echo "$k,$label,,cell,$rc,,,,,,,FAILED,,,${why:-unknown}" >> "$INDEX"
    # A rig that cannot arm will not fix itself, and 90 more attempts cost the whole night.
    if [ "$fails" -ge 2 ]; then
      log "STOP: 2 consecutive failures. Not burning the night on a broken rig."
      log "      Last output: $(tail -3 "$CAMP/$label.out" | tr '\n' ' ')"
      break
    fi
    sleep 30; continue
  fi
  fails=0

  # ---- per-cell integrity. cell5m-r2 produced a full-length CLEAN DIAG with a header-only
  # subscriber CSV and nothing stopped it, so check the product on every axis.
  dlf=$(ls -t "$D"/*.dlf 2>/dev/null | head -1)
  partial=no; case "${dlf:-}" in *-PARTIAL.dlf) partial=YES ;; esac
  dlf_gb=""; resyncs=""
  if [ -n "$dlf" ]; then
    dlf_gb=$(awk -v b="$(stat -c%s "$dlf")" 'BEGIN{printf "%.1f", b/1073741824}')
    resyncs=$(python3 "$DIAG/dlf-check" "$dlf" 2>/dev/null | grep -oE '[0-9]+ resyncs' | grep -oE '^[0-9]+')
  fi
  bsub="$D/hostb/subscriber.csv"
  b_frames=0; [ -f "$bsub" ] && b_frames=$(( $(wc -l < "$bsub") - 1 ))

  # THE REAL TRIGGER: packets_lost, column 27 of B's subscriber.csv. This is the only
  # over-the-air loss measurement we have. Not the radio scan (5-in-6 false positives on a
  # capture with one known event) and not the pcap RTP join (degenerate: ~520 pkt/s wraps the
  # 16-bit sequence space every ~126 s, so every capture shows all 65536 values and a 100%
  # chance floor). B's qdisc_dropped / sys_rx_dropped / udp_sock_drops are LOCAL kernel drops
  # on our own interface and cannot see a packet lost over the air -- not substitutes.
  #
  # Take the MAX, not the last value: packets_lost is non-monotonic in these runs (cell5m-a3
  # peaked at 34 and ended at 15), so the final row understates what happened.
  #
  # Do not read frames_rendered from the log as a health signal. It is a libwebrtc inbound-RTP
  # stat (libwebrtc/src/stats.rs:353), and this app renders through its own sink, so WebRTC
  # never populates it: rendered=0 in EVERY run regardless of what was drawn. It cost an hour
  # and nearly cost the campaign. The row count above is the signal.
  b_lost_max=0
  if [ "$b_frames" -gt 0 ]; then
    b_lost_max=$(awk -F, 'NR>1 && $27+0>m {m=$27+0} END {print m+0}' "$bsub")
  fi
  [ "${b_lost_max:-0}" -gt 0 ] && notes="${notes}LOSS=${b_lost_max} "
  notes=""
  [ "${resyncs:-0}" != 0 ] && notes="${notes}DLF_DAMAGED "
  [ "$partial" = YES ]     && notes="${notes}PARTIAL_CAPTURE "
  [ "$b_frames" -le 0 ]    && notes="${notes}NO_MEDIA_AT_B "

  # ---- ask B for its DLF (B ships; we only record whether it landed)
  shipped=no
  [ -f "$D/hostb/$label.dlf" ] || [ -f "$D/hostb/${label}-b.dlf" ] && shipped=yes

  # ---- scan A's DLF for the signature
  verdict=clean; ev=""; step=""
  if [ -n "$dlf" ]; then
    scan="$CAMP/$label.scan.txt"
    python3 "$DIAG/find-beam-events.py" "$dlf" > "$scan" 2>&1
    if grep -q "SIGNATURE MATCH" "$scan"; then
      verdict=CANDIDATE
      ev=$(grep -m1 "SIGNATURE MATCH" -B1 "$scan" | head -1 | grep -oE '[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+Z')
      step=$(grep -m1 "SIGNATURE MATCH" -B1 "$scan" | head -1 | grep -oE '\([+-][0-9.]+\)' | tr -d '()')
      log "    candidate at $ev  step $step dB (UNVERIFIED -- 5-in-6 false-positive rate)"
    elif grep -qE "BEAM SWITCHED|SSB INDEX SET CHANGED|HANDOVER" "$scan"; then
      verdict=other-radio-event
    fi
  fi
  log "    rc=0 dlf=${dlf_gb:-?}GB resyncs=${resyncs:-?} partial=$partial b_frames=$b_frames lost_max=$b_lost_max verdict=$verdict ${notes:+[$notes]}"
  echo "$k,$label,$(date -u +%FT%TZ),cell,0,${dlf_gb},${resyncs},$partial,$b_frames,$b_lost_max,$shipped,$verdict,$ev,$step,$notes" >> "$INDEX"

  # ---- positive control
  if [ $(( k % CONTROL_EVERY )) -eq 0 ]; then
    if python3 "$DIAG/find-beam-events.py" "$CONTROL_SLICE" 2>&1 | grep -q "SIGNATURE MATCH"; then
      log "    control after cell $k: OK"
      echo "$k,CONTROL,$(date -u +%FT%TZ),control,0,,,,,,,ok,,," >> "$INDEX"
    else
      log "STOP: positive control FAILED after cell $k. Every clean verdict from here is void."
      echo "$k,CONTROL,$(date -u +%FT%TZ),control,1,,,,,,,FAILED,,,CAMPAIGN_VOID_FROM_HERE" >> "$INDEX"
      break
    fi
  fi
  sleep 20
done

log "=== campaign ended: $k cells ==="
log "radio candidates (unverified): $(grep -c ',CANDIDATE,' "$INDEX" 2>/dev/null)"
log "cells with over-the-air LOSS (the real trigger): $(awk -F, 'NR>1 && $4==\"cell\" && $10+0>0' "$INDEX" 2>/dev/null | wc -l)"
log "clean:   $(grep -c ',clean,' "$INDEX" 2>/dev/null)"
log "failed:  $(grep -c ',FAILED,' "$INDEX" 2>/dev/null)"
log "index:   $INDEX"
