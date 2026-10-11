# AMD GPU instance: how to connect, specs and what they mean for us

_Measured 2026-10-10 over SSH on an AMD one-click / lablab runtime instance. The values
come from `amd-smi`, sysfs/KFD, cgroups and a PyTorch run; nothing is copied from a datasheet._

## Connect to the AMD box (start here)
The box is an AMD one-click / lablab runtime instance: a Radeon Pro W7900D (gfx1100, 48 GB) inside a locked-down
Kubernetes pod. You get **root over SSH only**. There's no Docker, no `ssh -L` port forwarding, and outbound traffic
goes only through a proxy. Follow these steps in order. Each step says what you should see.

**1. Get SSH access (once per person).** Make a key and register its public half in the AMD console's SSH-key
settings. Keys are applied at launch, so relaunch the instance afterwards. Never share private keys.
```bash
ssh-keygen -t ed25519 -f ~/.ssh/spot_amd_ed25519 -C "<your-name>-spot-amd"
cat ~/.ssh/spot_amd_ed25519.pub        # paste this into the AMD console, then relaunch the instance
```

**2. Copy the SSH line from the console.** It looks like `ssh root@<ip> -p <port>`. **The IP and port change on
every relaunch**, and so does the host key. Keep them in your `~/.ssh/config`, never in this public repo.

**3. Add a `spot-amd` host entry.** Every script and doc uses this name.
```
# ~/.ssh/config
Host spot-amd
    HostName <ip from the console>
    Port <port from the console>
    User root
    IdentityFile ~/.ssh/spot_amd_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 4
```

**4. Test the connection.** The first connect asks you to accept the host key.
```bash
ssh spot-amd 'hostname; /opt/venv/bin/amd-smi static --asic | grep MARKET_NAME'
# expect: MARKET_NAME: AMD Radeon Pro W7900D
```
After a relaunch, ssh refuses the new host key. Remove the old one with `ssh-keygen -R "[<old ip>]:<old port>"`.

**5. Inside a box shell, export the proxy before any `pip`, `curl` or `git`.** SSH sessions don't inherit it.
```bash
export HTTPS_PROXY=http://notebook-egress-proxy.amd-oneclick-lablab.svc.cluster.local:3128
export HTTP_PROXY=$HTTPS_PROXY https_proxy=$HTTPS_PROXY http_proxy=$HTTPS_PROXY
```
- **Reachable:** GitHub, PyPI, repo.amd.com and hf-mirror.com.
- **Blocked:** huggingface.co and Docker Hub.
- **curl:** use `--http1.1` (the proxy breaks HTTP/2).
- **Uploads from a laptop:** about 16 KB/s per connection, so download on the box instead (see "Network").

**6. Check whether the box is already set up.**
```bash
ssh spot-amd '/root/venv-gpu/bin/supervisorctl -c /root/supervisord.conf status'
# set up:     llm, embed and system1 all RUNNING
# fresh box:  "No such file or directory" (a relaunch wipes /root): run step 7
```

**7. Set up a fresh box from your laptop.** Run these from the repo root; `deploy/amd-native/README.md` has the
details.
```bash
SPOT_REF=main deploy/amd-native/laptop/bootstrap.sh   # ~30 min the first time (27B download + llama.cpp build)
deploy/amd-native/laptop/push_laya.sh                 # fine-tuned Laya, ~10 min; base Laya serves until then
```
`SPOT_REF` must be a ref already pushed to GitHub, because the box downloads the repo itself.

**8. Connect your laptop to the services.** Use the exec tunnel, not `ssh -L`: the box's sshd refuses port
forwarding. Keep it running in its own terminal.
```bash
deploy/amd-native/laptop/tunnel.sh
curl -s localhost:18080/health      # {"status":"ok"}                     llama-server, Qwen3.8-27B
curl -s localhost:18081/health      # {"status":"ok",...,"device":"cuda"} embed
curl -s localhost:18082/v1/model    # "fine_tuned": true, "device": "cuda:0"   Laya
```

**9. Point the Docker stack at the tunnel.** Use `docs/RUNNING.md` "Mode 2" (`COMPOSE_PROFILES=hybrid`, with the
model URLs on `host.docker.internal:18080/18081/18082`). Then run `make up` and check:
```bash
curl -s localhost:8000/health | jq '{system1, embed, system2}'
# system1/embed "up"; system2 "model_status": "up" means the 27B answers (down -> status "degraded")
```

**When something fails:**

| Symptom | Cause and fix |
|---|---|
| `Permission denied (publickey)` | Your key isn't in the console, the instance wasn't relaunched after adding it, or `IdentityFile` is wrong |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | The instance was relaunched. Run `ssh-keygen -R "[<old ip>]:<old port>"` and update `~/.ssh/config` |
| `channel N: open failed: administratively prohibited` | You used `ssh -L`. Use `deploy/amd-native/laptop/tunnel.sh` |
| `Connection closed by ... port ...` during uploads | More than ~10 new SSH connections at once (`MaxStartups 10:30:100`). Use at most 8 |
| `curl: (7)` on `localhost:1808x` | `tunnel.sh` isn't running |
| `system2` `degraded`, `model_status: down` | llama-server is down. Run `ssh spot-amd '/root/venv-gpu/bin/supervisorctl -c /root/supervisord.conf status'`, then `ssh spot-amd 'tail -50 /root/logs/llm.log'` |
| Setup failed | `ssh spot-amd 'tail -40 /root/logs/setup.log'`, fix, then re-run `bootstrap.sh` (finished steps are skipped) |

## Hardware (this container)
| | Value |
|---|---|
| **GPU** | **AMD Radeon Pro W7900D**, PCI `1002:744b`, 1 GPU assigned to this container (`/dev/dri/card1`, `renderD128`). The host has 8 of them. |
| Architecture | **gfx1100** (RDNA 3, Navi 31) |
| Compute units | 96 CUs (`amd-smi`). PyTorch's `multi_processor_count` reports 48, which counts work-group processors (2 CUs each). |
| VRAM | **48 GiB GDDR6** (49,136 MB), Samsung, 384-bit |
| GTT (host-visible) | 252 GiB |
| Link | PCIe 4.0 x16 (16 GT/s) |
| Power limit | 241 W (min 144 W); slowdown at 100 °C edge / 110 °C hotspot |
| Measured matmul (8192³) | **fp16 65.4 TFLOPS, bf16 65.5 TFLOPS**; fp32 only 1.3 TFLOPS (see "What it means") |
| CPU | Host: 2 × AMD EPYC 9334 (32 cores each, 128 threads). **Container: 16 CPUs** (`cpu.max` 1600000/100000) |
| RAM | Host 503 GiB. **Container limit 55 GiB** (cgroup `memory.max` 59,055,800,320 B). No swap. |
| Disk | `/` overlay 3.5 TB (2.0 TB free, shared). **`/tmp` is only a 4 GB tmpfs**; `/dev/shm` is 32 GB. |

## Software
| | Value |
|---|---|
| OS / kernel | Ubuntu 26.04 LTS, Linux 6.8.0-79-generic |
| Driver | amdgpu 6.16.13 |
| ROCm | 10.0.0, installed as pip packages in **`/opt/venv`** (amd-smi 27.0.0, `hipcc`, `amdclang`). There is no `/opt/rocm`, and `rocminfo` isn't installed. |
| Python | 3.14.4 (system and `/opt/venv`) |
| PyTorch | **`/opt/venv`: torch 2.13.0+rocm10.0.0 (HIP 7.15), triton 3.8.0. It works on the GPU.** System Python has no torch. |
| User | `root`, but **no Docker or Podman** (a Kubernetes pod) |

## Network (outbound only through the proxy in step 5)
| Destination | Result |
|---|---|
| repo.amd.com (ROCm wheels), pypi.org, github.com, download.pytorch.org | ✅ 200 |
| hf-mirror.com (public HF mirror) | ✅ 200 |
| **huggingface.co, cdn-lfs.huggingface.co** | ❌ **times out (blocked or hanging at the proxy)** |
| Docker Hub (registry-1.docker.io) | ❌ blocked |
| Inbound | SSH only, via the platform's port mapping. **The platform sshd (`/tmp/amd-oneclick-sshd_config`) refuses `-L` TCP and Unix-socket forwarding**, and `MaxStartups 10:30:100` drops bursts of new connections |
| Laptop → box upload (instance of 2026-10-11, a China Mobile IP) | **~16 KB/s per SSH connection**; ~170 KB/s with 8 in parallel. Download on the box instead (hf-mirror ~85 MB/s) |
| hf-mirror Xet downloads | some return 401: set `HF_HUB_DISABLE_XET=1` |
| ROCm devel | `/opt/venv` ROCm 10.0.0 has no `rocm-sdk-devel` (no HIP CMake files, and repo.amd.com has none for 10.0.0); llama.cpp builds against a separate ROCm 7.14.1 venv |

## What it means for SPOTlight
1. **The GPU is not the 8060S/R9700 we planned around.** It's `gfx1100`, which our Docker `TORCH_VARIANT`s
   (`cpu|rocm-gfx1151|rocm-gfx1201`) don't cover. There's no Docker here anyway, so services run **natively**.
   Use the already-working `/opt/venv` torch and install our requirements into it (`laya` and the rest don't replace
   torch; re-check `torch.__version__` shows `+rocm10.0.0` after installing). AMD's index also carries
   `amd-torch-device-gfx1100` if a separate venv is needed.
2. **Use fp16/bf16, not fp32.** The fp32 GEMM path measured ~50× slower (1.3 vs 65 TFLOPS). Keep AMP on: don't
   pass `--no-amp` to `finetune.py`, and keep `SPOT_AMP=1` for the Laya service.
3. **Everything fits on one card.** 48 GB VRAM holds Laya (421M), Qwen3-Embedding-0.6B and Qwen3.5-9B (Q4 GGUF ≈ 6 GB
   plus KV cache) at the same time. The container's 55 GiB RAM limit and 16 CPUs are the tighter constraints. Laya
   fine-tuning should run much faster here than on the 8060S (65 fp16 TFLOPS on a dedicated card).
4. **Models can't come from huggingface.co.** In order of preference:
   - copy them from a laptop that has them cached:
     `rsync -a ~/.cache/huggingface/hub/models--convaiinnovations--laya ~/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B spot-amd:/root/.cache/huggingface/hub/`
   - fine-tuned checkpoints: `rsync -a amd/system1/checkpoints/<run>/best spot-amd:/root/models/`
   - otherwise `export HF_ENDPOINT=https://hf-mirror.com` (a third-party mirror; Laya ships safetensors, but verify the
     revision hash matches).
   Point `HF_HOME` at disk (e.g. `/root/.cache/huggingface`), never `/tmp` (4 GB).
5. **Hybrid mode works through an exec tunnel, not `ssh -L`** (this box's sshd refuses port forwarding). The
   scripts in `deploy/amd-native/` set up `llm` (8080), `embed` (8081) and `system1` (8082) under supervisord.
   `deploy/amd-native/laptop/tunnel.sh` serves them on the laptop's 18080/18081/18082, the ports `docs/RUNNING.md`
   "Mode 2" expects.
6. **Treat the instance as ephemeral.** A relaunch changes the address and port. Copy results off (or push to the HF
   Hub through the mirror/rsync route) as soon as a run finishes.

## Other AMD instances seen this weekend
| Instance | GPU | Arch | Memory | Notes |
|---|---|---|---|---|
| `jupyter-amducf07` (`/ryzers/notebooks`) | Radeon 8060S (Strix Halo iGPU) | gfx1151 | 64 GB unified | Jupyter only, user `jovyan`, no root. run2 fine-tune ran here. |
| `jupyter-amducf07` (`/opt/workspace`) | Radeon AI PRO R9700 | gfx1201 | 32 GB | needed `amd-torch-device-gfx1201` |
| **SSH instance (this one)** | **Radeon Pro W7900D** | **gfx1100** | **48 GB** | root, SSH, proxy egress, HF blocked |
