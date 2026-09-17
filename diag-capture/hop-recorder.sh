#!/usr/bin/env bash
# Per-hop uplink recorder: where does a packet (or a rate collapse) stop?
#
# Samples every hop we can read on THIS host once a second, on one clock, while a
# stream runs, plus a header-only packet capture on the modem interface. Joined
# with the publisher's WebRTC stats, the modem diag log and the receiving host's
# logs, each loss or collapse can be placed at the first hop where it appears:
#
#   app (WebRTC)  -> harness jsonl: target bitrate, bytes/packets sent
#   kernel queue  -> fq_codel on wwan0: drops, backlog, requeues (driver pushback)
#   driver        -> /sys wwan0 counters: tx_dropped / tx_errors
#   modem (host)  -> QMI WDS: TX packets OK / dropped (host->modem handoff)
#   radio         -> QMI NAS signal: 5G RSRP / RSRQ / SNR
#   wire          -> wwan0.pcap: every UDP packet's headers (RTP seq/SSRC are
#                    cleartext under SRTP), so packets can be matched end to end
#   modem stack   -> diag DLF (optional, DIAG=1): PDCP discard, BSR, grants
#
# No sudo prompt: tcpdump and qmicli are NOPASSWD on this host.
#
# tcpdump STOPS ITSELF (-G <duration> -W 1). It runs as root, keeps root's
# credentials after -Z, and neither this user nor sudo can signal it -- verified
# 2026-09-14, when a SIGINT-stopped design left an unkillable capture behind.
# Everything else is stopped by PID, never `pkill -f`.
#
# Usage: hop-recorder.sh <label> <duration_s> <outdir>      (DIAG=1 to add modem log)
set -uo pipefail
[ $# -ge 3 ] || { echo "usage: $0 <label> <duration_s> <outdir>" >&2; exit 2; }
label=$1 dur=$2 out=$3
DIAG_DIR=$(cd "$(dirname "$0")" && pwd)
IF=${IF:-wwan0} QMI=${QMI:-/dev/cdc-wdm8}
mkdir -p "$out"
csv="$out/${label}.hops.csv" pcap="$out/${label}.${IF}.pcap"

# Say up front which hops this host can record. Hosts without the NOPASSWD rule for
# tcpdump/qmicli still record queue, driver and modem-log hops; they just cannot
# capture the wire or read QMI counters -- and must not pretend they did.
# Capture capability has TWO independent routes and the gate must test both, or a paired
# run silently captures at one end while looking complete. Host A reaches tcpdump through
# scoped NOPASSWD sudo (sudo -n -l: /usr/bin/tcpdump, /usr/bin/qmicli); Host B reaches it
# through file capabilities (cap_net_admin,cap_net_raw=eip) and needs no sudo at all.
# Testing only `sudo -n` reports a capability-carrying host as incapable -- which is what
# a `sudo -n` gate would now do on B, skipping its capture without saying so.
can_wire=0; TCPDUMP=""
if sudo -n tcpdump --version >/dev/null 2>&1; then can_wire=1; TCPDUMP="sudo -n tcpdump"
elif getcap "$(command -v tcpdump 2>/dev/null)" 2>/dev/null | grep -q cap_net_raw; then can_wire=1; TCPDUMP="tcpdump"
elif timeout 5 tcpdump -i "$IF" -c 1 -w /dev/null >/dev/null 2>&1; then can_wire=1; TCPDUMP="tcpdump"
fi
can_qmi=0;  sudo -n qmicli --version >/dev/null 2>&1 && can_qmi=1
echo "recording: queue+driver=yes  wire(pcap)=$([ $can_wire = 1 ] && echo yes || echo 'NO - neither NOPASSWD sudo nor cap_net_raw')  qmi+signal=$([ $can_qmi = 1 ] && echo yes || echo 'NO - no NOPASSWD qmicli')  modem-log=$([ "${DIAG:-0}" = 1 ] && echo yes || echo no)"

tpid="" dpid="" cleaned=0
cleanup() {
  [ "$cleaned" = 1 ] && return; cleaned=1
  if [ -n "$dpid" ]; then kill -INT "$dpid" 2>/dev/null; sleep 2; kill -0 "$dpid" 2>/dev/null && kill -TERM "$dpid"; pkill -P $$ -x sleep 2>/dev/null
    DIAG_LOCK_HELD=1 "$DIAG_DIR/venv/bin/python3" "$DIAG_DIR/diag-log-off" /dev/ttyUSB0 || echo "WARNING: modem diag logging may still be on" >&2; fi
  # tcpdump exits at the first packet after its -G window; give it a bounded wait.
  [ -n "$tpid" ] && for _ in $(seq 30); do kill -0 "$tpid" 2>/dev/null || break; sleep 0.5; done
  [ -n "$tpid" ] && kill -0 "$tpid" 2>/dev/null && echo "note: tcpdump will exit at the next packet on $IF (pid $tpid)" >&2
  echo "hops csv : $csv ($(($(wc -l < "$csv") - 1)) samples)"
  [ -n "$tpid" ] && echo "pcap     : $pcap ($(du -h "$pcap" 2>/dev/null | cut -f1))" || echo "pcap     : not recorded on this host"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Header-only capture: 128 bytes covers IP+UDP+RTP+extensions, never media.
if [ $can_wire = 1 ]; then
  $TCPDUMP -i "$IF" -nn -s 128 -Z "$(id -un)" --time-stamp-precision=nano \
    -G "$dur" -W 1 -w "$pcap" udp >/dev/null 2>"$out/${label}.tcpdump.log" &
  tpid=$!
fi

if [ "${DIAG:-0}" = 1 ]; then
  # One reader per DIAG port, enforced with the same lock Host B's ~/diag-capture/capture.sh
  # takes. Where that tool lives (Host B), share its lock file so the two can never collide;
  # elsewhere keep the lock beside these scripts.
  if [ -d "$HOME/diag-capture" ]; then DIAG_LOCK=${DIAG_LOCK:-$HOME/diag-capture/.ttyUSB0.lock}
  else DIAG_LOCK=${DIAG_LOCK:-$DIAG_DIR/.ttyUSB0.lock}; fi
  exec 9>"$DIAG_LOCK"
  if ! flock -n 9; then echo "another capture holds the DIAG port (lock $DIAG_LOCK); not starting" >&2; exit 1; fi
  sleep infinity | "$DIAG_DIR/venv/bin/python3" "$DIAG_DIR/qcsuper-noroot" --usb-modem /dev/ttyUSB0 \
    --dlf-dump "$out/${label}.dlf" > >(trap '' INT TERM HUP; exec sed -u -E 's/(Wrong CRC).*/\1/; s/(unmatched response received: [0-9]+).*/\1/' > "$out/${label}.qcsuper.log") 2>&1 &
  dpid=$!
fi

echo "unix_ms,qdisc_sent_pkts,qdisc_dropped,qdisc_overlimits,qdisc_requeues,qdisc_backlog_bytes,qdisc_backlog_pkts,sys_tx_packets,sys_tx_dropped,sys_tx_errors,qmi_tx_ok,qmi_tx_dropped,qmi_rx_ok,qmi_rx_dropped,nr_rsrp_dbm,nr_rsrq_db,nr_snr_db" > "$csv"
end=$(( $(date +%s) + dur ))
while [ "$(date +%s)" -lt "$end" ]; do
  t=$(date +%s%3N)
  q=$(tc -s qdisc show dev "$IF")
  qs=$(sed -n 's/.*Sent [0-9]* bytes \([0-9]*\) pkt (dropped \([0-9]*\), overlimits \([0-9]*\) requeues \([0-9]*\)).*/\1,\2,\3,\4/p' <<<"$q" | head -1)
  qb=$(sed -n 's/.*backlog \([0-9]*\)b \([0-9]*\)p.*/\1,\2/p' <<<"$q" | head -1)
  s=/sys/class/net/$IF/statistics
  st="$(cat $s/tx_packets),$(cat $s/tx_dropped),$(cat $s/tx_errors)"
  w=""; n=""
  [ $can_qmi = 1 ] && w=$(sudo -n qmicli -d "$QMI" -p --wds-get-packet-statistics 2>/dev/null)
  wq="$(sed -n "s/.*TX packets OK: //p" <<<"$w"),$(sed -n "s/.*TX packets dropped: //p" <<<"$w"),$(sed -n "s/.*RX packets OK: //p" <<<"$w"),$(sed -n "s/.*RX packets dropped: //p" <<<"$w")"
  [ $can_qmi = 1 ] && n=$(sudo -n qmicli -d "$QMI" -p --nas-get-signal-info 2>/dev/null | sed -n '/5G/,$p')
  ns="$(sed -n "s/.*RSRP: '\(-*[0-9.]*\).*/\1/p" <<<"$n" | head -1),$(sed -n "s/.*RSRQ: '\(-*[0-9.]*\).*/\1/p" <<<"$n" | head -1),$(sed -n "s/.*SNR: '\(-*[0-9.]*\).*/\1/p" <<<"$n" | head -1)"
  echo "$t,${qs:-,,,},${qb:-,},$st,$wq,$ns" >> "$csv"
  sleep $(awk -v t="$t" -v n="$(date +%s%3N)" 'BEGIN{d=1-(n-t)/1000; print (d>0?d:0)}')
done
