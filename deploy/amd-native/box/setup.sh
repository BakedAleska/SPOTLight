#!/usr/bin/env bash
# One-shot, idempotent setup of the SPOT GPU services on the AMD box (docs/AMD-K8S-PLAN.md Path B; no Docker or k3s
# there). Run as root on the box, normally through ../laptop/bootstrap.sh, which copies this folder to
# /root/spot-native and starts it detached (log: /root/logs/setup.log). Every step is skipped when already done, so
# re-running after a failure or a relaunch is safe.
#
#   llm      127.0.0.1:8080  llama-server, Qwen3.8-27B Q8_0 + mmproj (docs/AMD-RUNS-2026-10-10.md)
#   embed    127.0.0.1:8081  amd/embed, Qwen3-Embedding-0.6B
#   system1  127.0.0.1:8082  amd/system1/service, Laya (base until ../laptop/push_laya.sh installs the fine-tune)
#   system2  127.0.0.1:8083  amd/system2, live checked pipeline straight to llm (key: /root/spot-secrets.env)
#
# Env overrides: SPOT_REF (git ref of the repo to run, default main; SPOT_REFRESH=1 re-downloads the same ref, e.g.
# a branch that moved), LLAMA_CPP_REF, SKIP_LLM=1 (no GGUF download or llama.cpp build).
set -euo pipefail
# curl: --http1.1 because the egress proxy breaks HTTP/2 streams (curl exit 92); -S keeps errors in the log

export HTTPS_PROXY=http://notebook-egress-proxy.amd-oneclick-lablab.svc.cluster.local:3128
export HTTP_PROXY=$HTTPS_PROXY https_proxy=$HTTPS_PROXY http_proxy=$HTTPS_PROXY
export TMPDIR=/root/tmp HF_HOME=/root/.cache/huggingface
HERE="$(cd "$(dirname "$0")" && pwd)"
SPOT_REF="${SPOT_REF:-main}"
# llama.cpp master at 2026-10-10T19:25Z: the build verified on the W7900D (22.5 tok/s on the 27B Q8_0)
LLAMA_CPP_REF="${LLAMA_CPP_REF:-23b0202a189c44a54625aadcb37a946dd1d6278d}"
GGUF_REPO=unsloth/Qwen3.8-27B-GGUF
GGUF_REV=4ca720788d1e01f1bff70c033e0d0028fd02e502
MODELS=/root/models/q38-27b
SUPERVISORCTL="/root/venv-gpu/bin/supervisorctl -c /root/supervisord.conf"
mkdir -p "$TMPDIR" "$HF_HOME" /root/logs /root/models/checkpoints "$MODELS"

step() { printf '\n== %s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# ── 1. repo (only amd/ is used; the box has no GitHub credentials, the repo is public) ───────────────
step "repo $SPOT_REF"
REPO_CHANGED=0
if [ "$(cat /root/SpotTheDOT/.deployed_ref 2>/dev/null)" != "$SPOT_REF" ] || [ "${SPOT_REFRESH:-0}" = 1 ]; then
  REPO_CHANGED=1
  curl -fsSL --http1.1 --retry 5 --retry-all-errors "https://codeload.github.com/JylerTackson/SpotTheDOT/tar.gz/$SPOT_REF" -o "$TMPDIR/spot.tgz"
  rm -rf /root/SpotTheDOT.new && mkdir /root/SpotTheDOT.new
  tar xzf "$TMPDIR/spot.tgz" -C /root/SpotTheDOT.new --strip-components=1 && rm "$TMPDIR/spot.tgz"
  echo "$SPOT_REF" > /root/SpotTheDOT.new/.deployed_ref
  rm -rf /root/SpotTheDOT && mv /root/SpotTheDOT.new /root/SpotTheDOT
fi
echo "deployed: $(cat /root/SpotTheDOT/.deployed_ref)"

# ── 2. System 2 model: GGUF + mmproj from hf-mirror (huggingface.co is blocked), sha256-checked ─────
if [ "${SKIP_LLM:-0}" != 1 ]; then
  step "gguf $GGUF_REPO@${GGUF_REV:0:7}"
  cat > "$MODELS/SHA256SUMS" <<'EOF2'
a680f44a06920e5d689774823782006aa3acc8db95750323373b24139b67e348  Qwen3.8-27B-Q8_0.gguf
cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e  mmproj-F16.gguf
EOF2
  for f in mmproj-F16.gguf Qwen3.8-27B-Q8_0.gguf; do
    [ -f "$MODELS/$f.ok" ] && continue
    if ! (cd "$MODELS" && grep " $f\$" SHA256SUMS | sha256sum -c --quiet - 2>/dev/null); then
      # resume a partial file; a corrupt complete one is removed so the next run starts over
      curl -fL --http1.1 --retry 5 --retry-all-errors -C - -o "$MODELS/$f" "https://hf-mirror.com/$GGUF_REPO/resolve/$GGUF_REV/$f" || true
      (cd "$MODELS" && grep " $f\$" SHA256SUMS | sha256sum -c -) || { rm -f "$MODELS/$f"; exit 1; }
    fi
    touch "$MODELS/$f.ok"
  done

  # ── 3. llama.cpp (HIP, gfx1100). /opt/venv's ROCm 10.0.0 has no devel package (no HIP CMake files, and
  #       repo.amd.com has no rocm-sdk-devel 10.0.0), so it builds and runs against ROCm 7.14.1 in /root/venv-rocm.
  step "llama.cpp ${LLAMA_CPP_REF:0:7}"
  if [ ! -x /root/venv-rocm/bin/hipconfig ]; then
    /opt/venv/bin/python -m venv /root/venv-rocm
    /root/venv-rocm/bin/pip install -q --retries 10 --timeout 120 --index-url https://repo.amd.com/rocm/whl-multi-arch/ \
      --extra-index-url https://pypi.org/simple "rocm[libraries,devel]==7.14.1" "rocm-sdk-device-gfx1100==7.14.1" cmake
    /root/venv-rocm/bin/rocm-sdk init
  fi
  if [ "$(cat /root/llama.cpp/.ref 2>/dev/null)" != "$LLAMA_CPP_REF" ] || [ ! -x /root/llama.cpp/build/bin/llama-server ]; then
    curl -fsSL --http1.1 --retry 5 --retry-all-errors "https://codeload.github.com/ggml-org/llama.cpp/tar.gz/$LLAMA_CPP_REF" -o "$TMPDIR/llama.tgz"
    rm -rf /root/llama.cpp && mkdir /root/llama.cpp
    tar xzf "$TMPDIR/llama.tgz" -C /root/llama.cpp --strip-components=1 && rm "$TMPDIR/llama.tgz"
    (
      source /root/venv-rocm/bin/activate
      ROCM_ROOT="$(hipconfig -R)"
      cd /root/llama.cpp
      HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$ROCM_ROOT" \
        cmake -S . -B build -G Ninja -DGGML_HIP=ON -DGPU_TARGETS=gfx1100 -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF \
        -DCMAKE_PREFIX_PATH="$ROCM_ROOT" -DCMAKE_BUILD_RPATH="$ROCM_ROOT/lib"
      cmake --build build -j 16 --target llama-server
    )
    echo "$LLAMA_CPP_REF" > /root/llama.cpp/.ref
  fi
fi

# ── 4. Python for Laya + embed: a venv that sees /opt/venv's ROCm torch through a .pth (/opt/venv is itself a
#       venv, so --system-site-packages would point at the system Python). Fails if an install changes torch.
step "venv-gpu"
if [ ! -f /root/venv-gpu/.ok ]; then
  rm -rf /root/venv-gpu && /opt/venv/bin/python -m venv /root/venv-gpu
  SPK=$(/root/venv-gpu/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
  /opt/venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' > "$SPK/zz-opt-venv.pth"
  before=$(/root/venv-gpu/bin/python -c 'import torch; print(torch.__version__)')
  /root/venv-gpu/bin/pip install -q --retries 10 --timeout 120 supervisor "peft>=0.13" \
    -r /root/SpotTheDOT/amd/system1/service/requirements.txt -r /root/SpotTheDOT/amd/embed/requirements.txt
  after=$(/root/venv-gpu/bin/python -c 'import torch; print(torch.__version__)')
  [ "$before" = "$after" ] || { echo "torch changed: $before -> $after" >&2; exit 1; }
  touch /root/venv-gpu/.ok
fi

# ── 5. System 2 (CPU; amd/system2): own venv, and an API key generated once on the box, never in the repo ───────
step "system2"
if [ ! -f /root/venv-s2/.ok ]; then
  rm -rf /root/venv-s2 && /opt/venv/bin/python -m venv /root/venv-s2
  /root/venv-s2/bin/pip install -q --retries 10 --timeout 120 -r /root/SpotTheDOT/amd/system2/requirements.txt
  touch /root/venv-s2/.ok
fi
if ! grep -q '^SYSTEM2_API_KEY=' /root/spot-secrets.env 2>/dev/null; then
  (umask 077; echo "SYSTEM2_API_KEY=s2-$(openssl rand -hex 24)" >> /root/spot-secrets.env)
fi
mkdir -p /root/s2cache

# ── 6. services under supervisord (no systemd in the pod) ───────────────────────────────────────────
step "supervisord"
cp "$HERE/supervisord.conf" /root/supervisord.conf
cp "$HERE/stdio_bridge.py" /root/stdio_bridge.py
cp "$HERE/spot.env" /root/spot.env
[ -x /root/llama.cpp/build/bin/llama-server ] || sed -i 's/^autostart=true ; llm$/autostart=false ; llm/' /root/supervisord.conf
if $SUPERVISORCTL pid >/dev/null 2>&1; then
  $SUPERVISORCTL reread && $SUPERVISORCTL update
  [ "$REPO_CHANGED" = 1 ] && $SUPERVISORCTL restart embed system1 system2
else
  /root/venv-gpu/bin/supervisord -c /root/supervisord.conf
fi
sleep 5
$SUPERVISORCTL status || true
echo SETUP_DONE
