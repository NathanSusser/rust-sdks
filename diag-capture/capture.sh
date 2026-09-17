#!/usr/bin/env bash
# Diag-port capture for RM520N-GL via QCSuper.
#
# Writes two artifacts per run:
#   *.dlf   -> opens natively in QXDM / QCAT (hand this to whoever holds the license)
#   *.pcap  -> opens in Wireshark on Linux, no license needed (RRC/NAS dissected)
#
# The diag port is /dev/ttyUSB0. ModemManager lists it as "(ignored)", so capturing
# here does not contend with the AT port (ttyUSB2) or the live bearer on wwan0.
set -euo pipefail

PORT=${PORT:-/dev/ttyUSB0}
OUTDIR=${OUTDIR:-$(cd "$(dirname "$0")" && pwd)/logs}
QCSUPER=${QCSUPER:-$(cd "$(dirname "$0")" && pwd)/venv/bin/qcsuper}
LABEL=${1:-run}

mkdir -p "$OUTDIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BASE="$OUTDIR/${LABEL}-${STAMP}"

echo "diag port : $PORT"
echo "dlf       : ${BASE}.dlf"
echo "Ctrl-C to stop."

# QCSuper requires root for the diag port; invoke sudo explicitly so the password
# prompt is visible here rather than QCSuper silently re-execing via pkexec.
#
# DLF ONLY, deliberately. DlfDumper sets no "limit_registered_logs", so it enables
# every log bit the baseband offers -- including the 5G NR ML1/MAC scheduler items.
# PcapDumper DOES restrict the mask to RRC/NAS, and the mask is global device
# state, so asking for both makes the full capture depend on module init order.
# Capture everything here; regenerate the pcap offline afterwards with:
#     qcsuper --dlf-read FILE.dlf --pcap-dump FILE.pcap
exec sudo "$QCSUPER" \
  --usb-modem "$PORT" \
  --dlf-dump  "${BASE}.dlf"
