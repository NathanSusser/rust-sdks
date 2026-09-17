#!/usr/bin/env bash
# Capture RTP/RTCP packet HEADERS on the 5G interface for one test cell.
# Usage: pcap.sh SECONDS [label] [iface]
#
# Headers only (-s 96): enough for IP/UDP/RTP sequence number, SSRC, timestamp and
# marker bit, which is all the cross-host RTP matching needs. Payload is never
# recorded -- it would be ~2 Mbps x 600 s per cell for data we cannot use, and the
# video content is the operator's.
#
# Needs no root: tcpdump carries cap_net_raw,cap_net_admin=eip on this host
#   sudo setcap cap_net_raw,cap_net_admin=eip /usr/bin/tcpdump
# Verify with `getcap /usr/bin/tcpdump` before a campaign; a package upgrade drops it.
#
# THIS SCRIPT NEVER SIGNALS TCPDUMP, AND MUST NOT BE CHANGED TO.
# Tested 2026-09-17, the hard way: a capability-carrying tcpdump cannot be killed by
# the unprivileged user that launched it. Raising caps from file capabilities clears
# the process's dumpable flag, so the kernel demands CAP_KILL from the *sender* --
# `kill -TERM` and even `kill -KILL` return EPERM from our own uid (verified: "kill:
# (1486321) - Permission denied", process reparented to systemd --user and left
# running). An earlier version of this script used `timeout -s TERM -k 20` plus a
# SIGINT/SIGTERM trap; none of that could stop the capture, and the orphan held the
# interface lock and blocked every later cell. On a remote day with no sudo there is
# no way to clear it.
#
# So the capture is given its own deadline instead: -G <secs> with -W 1 makes tcpdump
# close the file and exit by itself (verified: rc=0, "Maximum file limit reached: 1",
# no leftover process). Note -G closes on the first packet arriving after the deadline,
# so on a quiet link exit can lag the nominal duration by seconds -- that is the flag
# working, not an overrun. The wrapper reports actual elapsed time so the lag is visible.
set -u
# Launched unattended (setsid nohup over ssh), stdout may be a pipe that closes
# mid-run. Never let a failed echo kill this shell before the summary runs.
trap '' PIPE
dur=${1:?usage: pcap.sh SECONDS [label] [iface]}
label=${2:-cap}
iface=${3:-wwan0}
dir=$(dirname "$(readlink -f "$0")")
mkdir -p ~/pcap-logs
out=~/pcap-logs/${label}-$(date -u +%Y%m%dT%H%M%SZ)

if ! getcap /usr/bin/tcpdump 2>/dev/null | grep -q cap_net_raw; then
  echo "not starting: /usr/bin/tcpdump has no cap_net_raw; run" >&2
  echo "  sudo setcap cap_net_raw,cap_net_admin=eip /usr/bin/tcpdump" >&2
  exit 1
fi
ip link show "$iface" >/dev/null 2>&1 || { echo "not starting: no interface $iface" >&2; exit 1; }

# One capture per interface per cell: two writers produce two partial files that each
# look complete, which is worse than failing here. Refuse if a live capture holds it --
# and because we cannot kill one, say plainly what the operator has to do.
exec 9>"$dir/.pcap-${iface}.lock"
if ! flock -n 9; then
  echo "another pcap holds $iface (lock $dir/.pcap-${iface}.lock); not starting" >&2
  echo "  a capability-carrying tcpdump cannot be killed without sudo -- find it with" >&2
  echo "  'ps -eo pid,args | grep tcpdump' and either wait for its -G deadline or" >&2
  echo "  'sudo kill <pid>'." >&2
  exit 1
fi

# Disk: 96-byte snapshots at the observed ~1.8 kpps ceiling is ~0.2 MB/s, but a
# retransmission storm raises packet rate sharply (37,959 retransmits in one AV1
# cell). Budget 1 MB/s and keep 5 GB spare, matching capture.sh's policy.
need_kb=$(( dur * 1024 + 5 * 1024 * 1024 ))
avail_kb=$(df -Pk ~/pcap-logs | awk 'NR==2 {print $4}')
if [ "$avail_kb" -lt "$need_kb" ]; then
  echo "not starting: ${avail_kb} KB free in ~/pcap-logs, need ${need_kb} KB (1 MB/s x ${dur}s + 5 GB)" >&2
  exit 1
fi

echo "capturing ${dur}s of $iface headers -> $out.pcap (snaplen 96, self-terminating)"
# -U: flush per packet, so the file is complete up to the last packet at all times.
# -G "$dur" -W 1: tcpdump's own deadline. No signal is sent, ever -- see the note above.
# UDP only: RTP, RTCP and any QUIC/UDP signalling. TCP signalling is deliberately
# excluded; it is not on the media path and adds bulk.
started=$(date +%s)
# 9<&- closes the lock fd for the child. Without it tcpdump INHERITS fd 9 and
# therefore holds the flock itself, so the lock outlives this shell -- and because
# a capability-carrying tcpdump cannot be signalled by its own uid, a leaked
# capture holds the interface lock until reboot. Diagnosed 2026-09-17 after exactly
# that happened: the wrapper had exited, yet `flock -n` still reported the lock
# held, by the tcpdump that had inherited it.
#
# Note the lock only ever guarded THIS script against itself. The kernel allows
# concurrent AF_PACKET capturers, so a leaked capture never actually prevented
# packet capture on the interface -- verified by Host A starting a second tcpdump
# on this interface successfully while the orphan was still running.
tcpdump -i "$iface" -n -s 96 -U -G "$dur" -W 1 -w "$out.pcap" udp >>"$out.log" 2>&1 9<&-
rc=$?
elapsed=$(( $(date +%s) - started ))

shopt -s nullglob
files=("$out.pcap"*)
if [ ${#files[@]} -eq 0 ]; then
  echo "WARNING: no pcap file written after ${elapsed}s (rc=$rc); see $out.log" >&2
  sed -n '1,20p' "$out.log" >&2
  exit 1
fi
for f in "${files[@]}"; do
  # 2>/dev/null on the read: tcpdump writes its "reading from file" banner to stderr,
  # which would otherwise land in the packet count.
  n=$(tcpdump -r "$f" -nn 2>/dev/null | wc -l)
  printf '%s: %s bytes, %s packets\n' "$f" "$(stat -c %s "$f")" "$n"
done
# -G closes on the first packet after the deadline, so elapsed >= dur on a quiet link.
printf 'tcpdump exited on its own after %ss (nominal %ss), rc=%s\n' "$elapsed" "$dur" "$rc"
if pgrep -f "tcpdump.*$out" >/dev/null 2>&1; then
  echo "WARNING: a tcpdump for this capture is STILL RUNNING and cannot be killed without sudo" >&2
  exit 1
fi
