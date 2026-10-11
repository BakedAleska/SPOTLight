# AMD box: native GPU services + laptop tunnel

## Connect (start here)
The full guide, with expected output and troubleshooting, is at the top of `docs/AMD-GPU.md`. In short, from the
repo root:
```bash
# 1. ~/.ssh/config: Host spot-amd, HostName/Port from the AMD console's SSH line (changes on every relaunch),
#    User root, IdentityFile ~/.ssh/spot_amd_ed25519 (your key must be registered in the console)
ssh spot-amd 'hostname; /opt/venv/bin/amd-smi static --asic | grep MARKET_NAME'   # AMD Radeon Pro W7900D
# 2. Is it set up? llm, embed, system1 RUNNING; "No such file" = fresh box, run step 3
ssh spot-amd '/root/venv-gpu/bin/supervisorctl -c /root/supervisord.conf status'
# 3. Fresh box only (~30 min + ~10 min):
SPOT_REF=main deploy/amd-native/laptop/bootstrap.sh && deploy/amd-native/laptop/push_laya.sh
# 4. Tunnel (own terminal): localhost:18080 llama-server, :18081 embed, :18082 Laya
deploy/amd-native/laptop/tunnel.sh
# 5. .env as in docs/RUNNING.md "Mode 2", then: make up && curl -s localhost:8000/health | jq .system2
```

## What runs where
The AMD SSH box (Radeon Pro W7900D, gfx1100, 48 GB; `docs/AMD-GPU.md`) is a locked-down Kubernetes pod. It has no
Docker, and k3s can't run in it (no `CAP_SYS_ADMIN`, filtered seccomp, read-only cgroups: `docs/AMD-K8S-PLAN.md` §3).
So the three GPU services run natively under supervisord. The rest of the stack runs in Docker on a laptop
(`docs/RUNNING.md` Mode 2, `hybrid`). This is the documented exception to "containerize every service": the code
is the same as the containers', with the same env vars, run from a venv.

| Service | Box port (loopback) | Laptop port | What |
|---|---|---|---|
| `llm` | 8080 | 18080 | llama-server (llama.cpp `23b0202`, HIP gfx1100), Qwen3.8-27B Q8_0 + mmproj-F16 |
| `embed` | 8081 | 18081 | `amd/embed`, Qwen3-Embedding-0.6B, bf16 |
| `system1` | 8082 | 18082 | `amd/system1/service`, fine-tuned Laya (base Laya until `push_laya.sh`) |

Measured 2026-10-11: 22.5 tok/s and 188 tok/s prefill on the 27B (350 tokens ≈ 15.7 s); 33.5 of 48 GB VRAM with all
three loaded; ~0.8 s per new tunnel connection.

## Operate
`bootstrap.sh` doubles as repair: re-run it after any failure (it follows `/root/logs/setup.log` and skips finished
steps). `SPOT_REFRESH=1` re-downloads a branch that moved. On the box: `/root/venv-gpu/bin/supervisorctl -c /root/supervisord.conf status`. Logs are in `/root/logs/`.

## Files
- `box/setup.sh`: idempotent setup, run on the box by `bootstrap.sh`. It:
  - downloads the repo at `SPOT_REF` from GitHub;
  - downloads the GGUFs from hf-mirror and checks their sha256;
  - builds llama.cpp against ROCm 7.14.1 in `/root/venv-rocm`;
  - creates `/root/venv-gpu` (it sees `/opt/venv`'s ROCm torch through a `.pth`; the install fails if torch would
    change);
  - installs supervisor and starts the services.
- `box/supervisord.conf`, `box/spot.env`: the three programs and their env (proxy, hf-mirror, `HF_HUB_DISABLE_XET`).
- `box/stdio_bridge.py`: pipes stdin/stdout to one of the three loopback ports.
- `laptop/tunnel.sh` + `laptop/exec_tunnel.py`: listen on 127.0.0.1:18080-18082 and run `stdio_bridge.py` per
  connection over one multiplexed SSH connection.
- `laptop/push_laya.sh` + `laya_patch.py`: send the fine-tuned Laya as a patch against base Laya.

## Why it looks like this (each one cost time on 2026-10-11)
- **No `ssh -L`.** The platform sshd sets `AllowTcpForwarding no`, and Unix-socket forwarding fails too. Running a
  command works, hence the exec bridge (user-approved). `MaxStartups 10:30:100` drops bursts of new connections, so
  uploads use 8 parallel connections, not 32.
- **Uploads from the laptop are ~16 KB/s per connection.** So the box downloads everything it can itself:
  - GitHub codeload (the repo is public);
  - hf-mirror at ~85 MB/s (huggingface.co is blocked).

  The private fine-tuned Laya can't come from a mirror. It goes up as an ~81 MB patch (LoRA A/B plus the trained
  norms and head) instead of the 847 MB merged file. The rebuild differs by at most one fp16 ulp. On M006 that
  changed 0 of 73 answers and no probability by more than 2e-6. The box's rebuild is byte-identical to the
  laptop's (sha256 checked).
- **ROCm 10.0.0 in `/opt/venv` has no devel package** (no HIP CMake files; repo.amd.com has no `rocm-sdk-devel`
  10.0.0). So llama.cpp uses a separate ROCm 7.14.1, and `LD_LIBRARY_PATH` in `supervisord.conf` points at it.
- **Proxy quirks:**
  - curl needs `--http1.1` (HTTP/2 streams break: exit 92);
  - hf-mirror's Xet downloads answer 401;
  - `/tmp` is 4 GB, so `TMPDIR=/root/tmp`.
- **Never `pkill -f` a pattern that also matches the remote `bash -c` command line:** it kills the SSH session
  running it.
- **System 2 health:** `GET /health` reports `model_status`, probing the 27B through the gateway and this tunnel.
  The backend shows `system2: degraded` when it is down (`docs/backend-services-contract.md` "Model probe").
