#!/usr/bin/env bash
# Per-hop RECEIVE-side recorder for Host B. Unprivileged: no sudo, no setcap, nothing granted.
#
# WHY A SECOND RECORDER. hop-recorder.sh is built for the sender: qdisc backlog, driver
# tx_dropped, QMI tx handoff. Host B is the receiver, and on 2026-09-18 it had only the two
# ENDS of its chain instrumented -- the wire (pcap) and the baseband (DIAG) -- with nothing in
# between. So when packets were seen leaving A and not arriving at B, we could prove the loss
# was real but could not say whether B's own kernel discarded them after they reached the NIC.
# That is the gap this closes, and it closes it with counters any user can read.
#
# THE INSTRUMENT THAT MATTERS MOST HERE is per-socket UDP drops. A receive buffer that
# overflows discards datagrams AFTER they have arrived and been counted by the NIC, so on the
# wire the packet is present and in the application it is missing -- indistinguishable from
# path loss unless you read this counter. /proc/net/udp's last column is exactly that, per
# socket, and its rx_queue field is the live backlog.
#
# WHAT IS DELIBERATELY ABSENT. qmicli needs sudo on this host and sudo is not granted here, so
# the QMI tx/rx handoff columns DO NOT EXIST in this output. They are omitted rather than
# emitted as zeros: a zero in a column named qmi_rx_dropped would be read as "the modem
# dropped nothing", when the truth is "nobody looked". An absent column cannot be misread.
#
# Usage: hop-recorder-b.sh <label> <duration_s> <outdir>
set -uo pipefail

label=${1:?usage: hop-recorder-b.sh <label> <duration_s> <outdir>}
dur=${2:?duration_s required}
outdir=${3:?outdir required}
mkdir -p "$outdir"
out="$outdir/$label.hops-b.csv"
IF=${IF:-wwan0}

command -v mmcli >/dev/null || echo "note: mmcli absent, radio columns will be empty" >&2
[ -r "/sys/class/net/$IF/statistics/rx_packets" ] || { echo "no such interface: $IF" >&2; exit 1; }

# Identify the modem once. mmcli -L is unprivileged on this host; the index is stable for the
# life of the connection, so re-resolving it every second would only add a fork per sample.
MODEM=$(mmcli -L 2>/dev/null | grep -oE '/Modem/[0-9]+' | head -1); MODEM=${MODEM##*/}

echo "unix_ms,qdisc_sent_pkts,qdisc_dropped,qdisc_backlog_pkts,sys_rx_packets,sys_rx_dropped,sys_rx_errors,sys_rx_missed,sys_tx_packets,sys_tx_dropped,udp_sock_max_rxq,udp_sock_drops,udp_indatagrams,udp_inerrors,udp_rcvbuferrors,nr_rsrp_dbm,nr_rsrq_db,nr_snr_db" > "$out"

end=$(( $(date +%s) + dur ))
while [ "$(date +%s)" -lt "$end" ]; do
  ms=$(date +%s%3N)

  q=$(tc -s qdisc show dev "$IF" 2>/dev/null | head -20)
  qsent=$(printf '%s' "$q" | grep -oE 'Sent [0-9]+ bytes [0-9]+ pkt' | head -1 | awk '{print $4}')
  qdrop=$(printf '%s' "$q" | grep -oE 'dropped [0-9]+' | head -1 | awk '{print $2}')
  qback=$(printf '%s' "$q" | grep -oE 'backlog [0-9]+b [0-9]+p' | head -1 | awk '{print $3}' | tr -d 'p')

  s=/sys/class/net/$IF/statistics
  rxp=$(cat $s/rx_packets 2>/dev/null); rxd=$(cat $s/rx_dropped 2>/dev/null)
  rxe=$(cat $s/rx_errors 2>/dev/null);  rxm=$(cat $s/rx_missed_errors 2>/dev/null)
  txp=$(cat $s/tx_packets 2>/dev/null); txd=$(cat $s/tx_dropped 2>/dev/null)

  # Per-socket UDP: the largest live backlog and the total discarded, across this host's UDP
  # sockets. The subscriber's socket is the one that moves; summing is robust to not knowing
  # which row is his without parsing hex addresses against a port we would have to be told.
  # hex2dec by hand: strtonum() is a GAWK extension and this host's awk is mawk, where it
  # silently yields nothing -- which emptied both columns in the first smoke test rather than
  # failing. Portable conversion keeps this working on whatever awk either host ships.
  read -r maxrxq sockdrops <<<"$(awk '
      function hex2dec(s,   i,c,n,v) {
        n=0; s=tolower(s);
        for (i=1; i<=length(s); i++) {
          c=substr(s,i,1); v=index("0123456789abcdef", c) - 1;
          if (v<0) continue; n = n*16 + v }
        return n }
      # NF>=13, NOT 15. The header line of /proc/net/udp carries 15 names but every DATA row
      # has 13 fields -- "sl" merges with its colon and the address pairs are single tokens. A
      # guard of NF>=15 matches the header and nothing else, so both columns come out empty on
      # a host that is dropping datagrams. Verified against a socket with a real 212992-byte
      # backlog: $5 is tx:rx and $NF is the per-socket drop count.
      NR>1 && NF>=13 { split($5,q,":"); rq=hex2dec(q[2]);
                       if (rq>m) m=rq; d+=$NF }
      END { printf "%d %d", m+0, d+0 }' /proc/net/udp 2>/dev/null)"

  read -r uin uerr ubuf <<<"$(awk '/^Udp:/{ if (h=="") {for(i=2;i<=NF;i++) k[i]=$i; h=1}
        else { for(i=2;i<=NF;i++) v[k[i]]=$i;
               printf "%s %s %s", v["InDatagrams"], v["InErrors"], v["RcvbufErrors"] } }' /proc/net/snmp 2>/dev/null)"

  rsrp="" rsrq="" snr=""
  if [ -n "$MODEM" ]; then
    sig=$(mmcli -m "$MODEM" --signal-get 2>/dev/null)
    rsrp=$(printf '%s' "$sig" | grep -oE 'rsrp: *-?[0-9.]+' | head -1 | grep -oE '\-?[0-9.]+$')
    rsrq=$(printf '%s' "$sig" | grep -oE 'rsrq: *-?[0-9.]+' | head -1 | grep -oE '\-?[0-9.]+$')
    snr=$(printf '%s' "$sig"  | grep -oE 's/n: *-?[0-9.]+|snr: *-?[0-9.]+' | head -1 | grep -oE '\-?[0-9.]+$')
  fi

  echo "$ms,${qsent:-},${qdrop:-},${qback:-},${rxp:-},${rxd:-},${rxe:-},${rxm:-},${txp:-},${txd:-},${maxrxq:-},${sockdrops:-},${uin:-},${uerr:-},${ubuf:-},${rsrp:-},${rsrq:-},${snr:-}" >> "$out"
  sleep 1
done
echo "wrote $out ($(wc -l < "$out") lines incl header)"
