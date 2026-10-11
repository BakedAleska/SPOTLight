#!/usr/bin/env bash
# Laptop -> AMD box tunnel for hybrid mode (docs/RUNNING.md): 127.0.0.1:18080 llama-server, :18081 embed, :18082 Laya.
# Keeps one multiplexed SSH master connection up and runs exec_tunnel.py on top of it (the box's sshd refuses -L).
# Leave it running in a terminal; Ctrl-C stops both. Check: curl -s localhost:18082/health
set -uo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
export SPOT_AMD_HOST="${SPOT_AMD_HOST:-spot-amd}" SPOT_AMD_CTL="${SPOT_AMD_CTL:-$HOME/.ssh/spot-amd-ctl}"
python3 "$D/exec_tunnel.py" & PY=$!
trap 'kill $PY 2>/dev/null; ssh -S "$SPOT_AMD_CTL" -O exit "$SPOT_AMD_HOST" 2>/dev/null; exit 0' INT TERM EXIT
while true; do
  echo "$(date '+%H:%M:%S') connecting to $SPOT_AMD_HOST"
  ssh -M -S "$SPOT_AMD_CTL" -N -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=4 "$SPOT_AMD_HOST"
  echo "$(date '+%H:%M:%S') connection dropped (exit $?), retrying in 3 s"; sleep 3
done
