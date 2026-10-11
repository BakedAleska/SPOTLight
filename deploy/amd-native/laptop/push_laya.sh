#!/usr/bin/env bash
# Install the fine-tuned Laya on the AMD box as a small patch (../laya_patch.py): uploads there run ~16 KB/s per
# connection, so the 847 MB checkpoint is impractical but an ~85 MB patch takes ~10 min over 8 connections.
#   1. make the patch against base Laya and rebuild it here (sha256 = what the box must produce)
#   2. upload it in 4 MB chunks, 8 scp at a time (sshd drops bursts above ~10), resuming on retry
#   3. on the box: rebuild against its base Laya, check the sha256, install, restart system1
# Inputs default to the compose hf-cache volume (filled by `make up` with HF_TOKEN set); override with
#   LAYA_RUN=<dir with model.safetensors, lora_adapter/, encoder/, tokenizer/>  BASE=<base Laya model.safetensors>
#   LAYA_SUBDIR (default run4_B_lora_lr4/best; another run also needs LAYA_CHECKPOINT in ../box/supervisord.conf)  PY (python with torch + safetensors; default amd/system1/.venv)
set -euo pipefail
D="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$D/../.." && pwd)"
HOST="${SPOT_AMD_HOST:-spot-amd}"
SUBDIR="${LAYA_SUBDIR:-run4_B_lora_lr4/best}"
PY="${PY:-$ROOT/amd/system1/.venv/bin/python}"
W="$(mktemp -d "${TMPDIR:-/tmp}/push-laya.XXXXXX")"
echo "work dir: $W"

if [ -z "${LAYA_RUN:-}" ] || [ -z "${BASE:-}" ]; then
  echo "== copying the checkpoint and base Laya out of the hf-cache volume"
  mkdir -p "$W/src"
  docker run --rm -v hf-cache:/c:ro -v "$W/src:/out" alpine sh -c "
    set -e
    run=\$(ls -d /c/hub/models--Jyler--KnightHacks-Laya-Model/snapshots/*/$SUBDIR | tail -1)
    base=\$(ls /c/hub/models--convaiinnovations--laya/snapshots/*/model.safetensors | tail -1)
    mkdir -p /out/run && cp -rL \"\$run\"/. /out/run/ && cp -L \"\$base\" /out/base.safetensors"
  LAYA_RUN="${LAYA_RUN:-$W/src/run}" BASE="${BASE:-$W/src/base.safetensors}"
fi

echo "== making the patch"
mkdir -p "$W/pkg/best"
"$PY" "$D/laya_patch.py" make "$LAYA_RUN/model.safetensors" "$BASE" "$LAYA_RUN/lora_adapter" "$W/pkg/patch.safetensors"
"$PY" "$D/laya_patch.py" apply "$BASE" "$W/pkg/patch.safetensors" "$W/rebuilt.safetensors"
(cd "$W" && shasum -a 256 rebuilt.safetensors | awk '{print $1"  model.safetensors"}') > "$W/pkg/rebuilt.sha256"
for f in encoder tokenizer questions.json rl_agent_config.json; do cp -R "$LAYA_RUN/$f" "$W/pkg/best/"; done
cp "$D/laya_patch.py" "$W/pkg/"
COPYFILE_DISABLE=1 tar cf "$W/pkg.tar" -C "$W" pkg
TAR_SHA=$(shasum -a 256 "$W/pkg.tar" | cut -d' ' -f1)
mkdir "$W/chunks" && split -b 4m -a 3 "$W/pkg.tar" "$W/chunks/p."
echo "patch package: $(du -h "$W/pkg.tar" | cut -f1), $(ls "$W/chunks" | wc -l | tr -d ' ') chunks"

echo "== uploading"
export W HOST
for pass in 1 2 3 4 5 6 7 8; do
  remote=$(ssh -o BatchMode=yes -o ConnectTimeout=40 "$HOST" \
    'mkdir -p /root/xfer/laya && cd /root/xfer/laya && for f in p.*; do [ -f "$f" ] && echo "$f $(stat -c %s "$f")"; done; true') \
    || { sleep 10; continue; }
  todo=$(cd "$W/chunks" && for f in *; do grep -qx "$f $(stat -f %z "$f")" <<<"$remote" || echo "$f"; done)
  [ -z "$todo" ] && break
  echo "pass $pass: $(grep -c . <<<"$todo") chunks to send"
  echo "$todo" | xargs -P 8 -I{} sh -c 'scp -q -o BatchMode=yes -o ConnectTimeout=40 "$W/chunks/{}" "$HOST:/root/xfer/laya/{}" || true'
done
[ -z "${todo:-}" ] || { echo "upload incomplete after 8 passes; re-run to resume" >&2; exit 1; }

echo "== installing on $HOST"
ssh -o BatchMode=yes "$HOST" "set -e
  cd /root/xfer && cat laya/p.* > laya.tar && echo '$TAR_SHA  laya.tar' | sha256sum -c --quiet -
  rm -rf pkg && tar xf laya.tar 2>/dev/null
  set -a; . /root/spot.env; set +a
  BASE=\$(/root/venv-gpu/bin/python -c 'from huggingface_hub import hf_hub_download as d; print(d(\"convaiinnovations/laya\", \"model.safetensors\"))')
  OUT=/root/models/checkpoints/$SUBDIR
  rm -rf \$OUT.new && mkdir -p \$OUT.new && cp -R pkg/best/. \$OUT.new/
  /root/venv-gpu/bin/python pkg/laya_patch.py apply \$BASE pkg/patch.safetensors \$OUT.new/model.safetensors
  (cd \$OUT.new && sha256sum -c /root/xfer/pkg/rebuilt.sha256)
  rm -rf \$OUT && mv \$OUT.new \$OUT && rm -rf /root/xfer/laya /root/xfer/laya.tar
  /root/venv-gpu/bin/supervisorctl -c /root/supervisord.conf restart system1"
echo "== done. Check: curl -s localhost:18082/v1/model (fine_tuned: true) with ./tunnel.sh running"
