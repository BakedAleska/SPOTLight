#!/usr/bin/env bash
# Set up (or repair) the AMD box from this laptop: copy ../box to spot-amd:/root/spot-native, run setup.sh there
# detached (it survives a dropped SSH session), and follow it until it finishes. First run ~30 min (27B download,
# llama.cpp build); later runs skip finished steps. Then: ./push_laya.sh (fine-tuned Laya) and ./tunnel.sh.
#   SPOT_REF=<branch|tag|sha> ./bootstrap.sh      # repo ref the box runs (default main; must be pushed to GitHub)
set -euo pipefail
D="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${SPOT_AMD_HOST:-spot-amd}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=40 "$HOST")

"${SSH[@]}" 'mkdir -p /root/spot-native /root/logs'
scp -q -o BatchMode=yes "$D"/box/* "$D/laya_patch.py" "$HOST:/root/spot-native/"
"${SSH[@]}" "cd /root/spot-native && nohup env SPOT_REF='${SPOT_REF:-main}' SPOT_REFRESH='${SPOT_REFRESH:-0}' \
  SKIP_LLM='${SKIP_LLM:-0}' sh -c 'bash setup.sh; echo SETUP_EXIT=\$?' > /root/logs/setup.log 2>&1 < /dev/null &"
echo "setup running on $HOST (log: /root/logs/setup.log)"

seen=0
while true; do
  sleep 15
  log=$("${SSH[@]}" 'cat /root/logs/setup.log' 2>/dev/null) || continue
  printf '%s\n' "$log" | tail -n +$((seen + 1)) | grep -E '^== |SETUP_EXIT|ERROR|FAILED|error:' || true
  seen=$(printf '%s\n' "$log" | wc -l)
  if grep -q 'SETUP_EXIT=' <<<"$log"; then
    grep -q 'SETUP_EXIT=0' <<<"$log" && { printf '%s\n' "$log" | sed -n '/== .*supervisord/,$p'; exit 0; }
    echo "setup failed; last lines:"; printf '%s\n' "$log" | tail -25; exit 1
  fi
done
