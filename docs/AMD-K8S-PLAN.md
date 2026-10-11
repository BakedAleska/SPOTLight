# Plan: Kubernetes for the AMD GPU services

Status: **gate failed, Path B running** (2026-10-11). The §3 gate failed on the current box (CapEff 0x200404db: no CAP_SYS_ADMIN; Seccomp 2; cgroups read-only; no /dev/kmsg), so there is no k3s. llm, embed and Laya run natively under supervisord (§7); `research/decisions.md` has the decision. No manifests exist. Hardware facts come from `docs/AMD-GPU.md`. Deployment decisions are in `research/decisions.md` ("Deployment").

## 1. Summary
- **What runs on the AMD box:** seven services. Four were planned: the ~30B LLM, Laya, OCR/vision and LiteLLM. The embedding model moved here from Vercel. System 2 and the orchestrator belong here too, because nearly all their calls go to the models.
- **Who calls it:** Vercel (backend and MCP server) calls four of them over public HTTPS.
- **The catch:** the box is itself a Kubernetes pod. We have root, but no Docker or Podman, Docker Hub and huggingface.co are blocked, and only SSH comes in (`docs/AMD-GPU.md`). A cluster *inside* that pod (k3s) works only if the pod is privileged enough, and that is unknown.

**So the plan has a 15-minute gate (§3).**
- **Gate passes →** Path A: k3s inside the instance, with one Deployment per service.
- **Gate fails →** Path B: the same seven services as supervised native processes, the way the box runs today.

Both paths share the service layout (§2), the public exposure (§5) and the code changes (§6).

**Recommendation, given the Sunday 10:00 deadline:**
1. Run the gate.
2. Bring up Path B first in any case (about 1–2 h), so the demo has a working backend.
3. Build Path A alongside only if the gate passes and someone owns it (about 4–6 h).
4. Cut over to Path A only after every probe in §4.6 is green.

## 2. Target services (one GPU: Radeon Pro W7900D, gfx1100, 48 GB VRAM; instance limit 16 CPUs, 55 GiB RAM)

| Service | Port | GPU | Est. VRAM | CPU / RAM request | Called by | Public? |
|---|---|---|---|---|---|---|
| `llm`: llama-server, ~30B GGUF | 8080 | yes | ~20–24 GB (Q4_K_M + KV) | 6 CPU / 8 Gi | gateway | no |
| `system1`: Laya (`amd/system1/service`) | 8082 | yes | ~1–2 GB (fp16 AMP) | 2 / 4 Gi | backend, gateway | **yes** |
| `embed`: Qwen3-Embedding-0.6B (`amd/embed`) | 8081 | yes | ~1.5–3 GB | 2 / 4 Gi | backend, MCP, db ingest | **yes** |
| `vision`: OCR + code readers (`amd/vision`) | 8084 | no | – | 2 / 2 Gi | orchestrator | no |
| `gateway`: LiteLLM (`amd/gateway`) | 4000 | no | – | 1 / 1 Gi | system2, orchestrator | no |
| `system2` (`amd/system2`) | 8083 | no | – | 1 / 1 Gi | backend, orchestrator | **yes** |
| `orchestrator` (`amd/orchestrator`) | 8085 | no | – | 1 / 1 Gi | backend | **yes** |

- **Totals:** about 15 CPU and 21 Gi RAM requested, and about 30 GB VRAM. Both fit, with headroom for the KV cache and model page cache. The 55 GiB RAM limit counts page cache, so keep the GGUF on disk and fully offloaded (`-ngl 99`).
- **The 30B model:** pick a **MoE** (about 3B active, e.g. the 35B-A3B already in `decisions.md`), not a dense 30B. Rough estimate: at the W7900's ~860 GB/s spec bandwidth (not measured), a dense 30B at Q4 would top out around 40 tok/s, and realistically a 400-token answer takes about 15 s. That's the whole System 2 budget. A MoE with ~3B active is several times faster. This needs a bake-off on the same harness before switching (`amd/system2/results/eval/`).

## 3. Step 0: feasibility gate (15 min, on the box)
Get the current SSH line from the AMD console; it changes on every relaunch. Then run:
```bash
grep -E '^Cap(Eff|Bnd)' /proc/self/status            # need full caps, e.g. CapEff 000001ffffffffff
grep Seccomp: /proc/self/status                       # 0 = unconfined (2 = filtered: likely blocks containerd)
ls -l /dev/kmsg; ls -l /dev/kfd /dev/dri              # kubelet wants /dev/kmsg; GPU nodes must exist
test -w /sys/fs/cgroup/cgroup.subtree_control && echo cgroup_writable || echo cgroup_readonly
unshare -m -p -f --mount-proc true && echo namespaces_ok
mount -t overlay overlay -o lowerdir=/etc,upperdir=$(mktemp -d),workdir=$(mktemp -d) $(mktemp -d) && echo overlay_ok
env | grep KUBERNETES_SERVICE_HOST; ls /var/run/secrets/kubernetes.io/serviceaccount   # the platform's own k8s API
curl -sI https://ghcr.io | head -1; curl -sI https://get.k3s.io | head -1               # through the egress proxy
```
| Result | Decision |
|---|---|
| Full caps + cgroup writable + namespaces_ok + overlay_ok + `/dev/kmsg` present | **Path A** (k3s inside the instance) |
| Any of those missing | **Path B** (native processes). Don't fight the platform. |
| A service-account token exists **and** `kubectl auth can-i create deployments` says yes | Ask the AMD/lablab mentors before using the platform's own cluster; assume no |

## 4. Path A: k3s inside the instance
### 4.1 Install (no systemd in a pod, no Docker Hub)
- Download the `k3s` binary and `k3s-airgap-images-amd64.tar.zst` from the k3s GitHub releases (GitHub is reachable through the proxy). Put the tarball in `/var/lib/rancher/k3s/agent/images/`.
- Start it under a supervisor, not systemd:
  ```bash
  k3s server --disable traefik --disable metrics-server --write-kubeconfig-mode 644
  ```
  Pass the proxy env to it, with `NO_PROXY` covering `10.42.0.0/16,10.43.0.0/16,localhost,127.0.0.1,.svc,.cluster.local`.
- Single node, namespace `spot`.

### 4.2 Images (built on a laptop, shipped as tarballs)
- **Build on a laptop:** `docker buildx build --platform linux/amd64` for each service. The laptop can reach Docker Hub; the box can't.
- **GPU images** (`system1`, `embed`, `llm`) need ROCm for **gfx1100**:
  - Add `TORCH_VARIANT=rocm-gfx1100` (AMD's `amd-torch-device-gfx1100` wheel) to `amd/system1/service/Dockerfile` and `amd/embed/Dockerfile`, and add it to the compose and CLAUDE.md variant lists.
  - Build `llm` from llama.cpp's ROCm Dockerfile with `AMDGPU_TARGETS=gfx1100`.
  - Base all three on **one** ROCm base image, so containerd stores the big layers once.
- **Ship:**
  ```bash
  docker save … | zstd > img.tar.zst        # on the laptop
  rsync img.tar.zst spot-amd:               # upload
  k3s ctr images import img.tar.zst         # on the box
  ```
  ROCm images are multi-GB, so the venue upload is the slowest step of Path A. Start it first.
- Set `imagePullPolicy: Never` on every Deployment.

### 4.3 Model weights
huggingface.co is blocked. Use `rsync` from a laptop cache, or `HF_ENDPOINT=https://hf-mirror.com` (verify revision hashes). Put everything in `/root/models/{gguf,hf,checkpoints}` and mount it read-only into the pods with `hostPath`. Set `HF_HOME` to the mounted cache, never `/tmp` (4 GB).

### 4.4 GPU access
- Mount `/dev/kfd` and `/dev/dri` into `llm`, `system1` and `embed` with `hostPath`, using `securityContext.privileged: true`, or the `video`/`render` groups if that's enough.
- Don't use AMD's device plugin. It hands the whole GPU to **one** pod, and three pods need it. 48 GB VRAM covers all three (§2).
- Laya keeps AMP on (`SPOT_AMP=1`): fp32 runs about 50× slower on this card.

### 4.5 Manifests (`deploy/k8s/`, kustomize)
- **Files:**
  - `namespace.yaml`
  - `configmap-gateway.yaml` (from `amd/gateway/config.yaml`)
  - `secret.example.yaml` (names only; real values are created with `kubectl create secret generic spot-keys --from-env-file=…`, never committed)
  - one `deployment-<svc>.yaml` + `service-<svc>.yaml` per service in §2
  - `kustomization.yaml`
- **Each Deployment:**
  - `replicas: 1` and `strategy: Recreate` for GPU pods, so two copies never hold VRAM at once
  - the §2 requests and limits
  - env vars mirroring the compose service, with in-cluster URLs: `http://llm:8080`, `http://system1:8082`, `http://gateway:4000`, …
  - `readinessProbe` and `livenessProbe` on each service's existing health path (`/health`; gateway `/health/liveliness`). Give `startupProbe` up to 10 min for the model loaders.
- **Services:**
  - internal ones are `ClusterIP`;
  - the four public ones (`system1`, `embed`, `system2`, `orchestrator`) are also `NodePort`, so the relay in §5 can reach them on the instance's localhost.
- `docker-compose.yml` stays the one local-dev stack (CLAUDE.md rule). The manifests are a deployment target, kept in sync with the compose services and with the ports table in `docs/backend-services-contract.md`.

### 4.6 Done when
- `kubectl -n spot get pods`: all 7 `Running`, ready `1/1`.
- Each public service's `/health` is 200 through the relay (§5).
- The backend on Vercel shows every model URL `up`.
- One `/explain` returns `source` from System 2, not a template.
- `rocm-smi` or `amd-smi` shows the three GPU processes.

## 5. Exposure to Vercel (both paths)
Only SSH comes in, so nothing on the box is reachable from Vercel directly. Options:
1. **Recommended: a small public relay VM.** Vultr is one of this event's MLH sponsor prizes, so a Vultr VM works here.
   - It runs `autossh -N -L 127.0.0.1:18082:localhost:<system1 port> -L … spot-amd` *into* the box. Inbound SSH works today.
   - Caddy on the relay serves `https://<relay-domain>/{system1,embed,system2,orchestrator}/*` with automatic TLS, strips the prefix, and forwards to the tunnels.
   - When the box relaunches, update its host and port in the relay's `~/.ssh/config`.
2. **A tunnel agent on the box** (cloudflared or ngrok) going out through the egress proxy. It's simpler if it works, but it's untested through the proxy. Try it in the gate, and fall back to option 1.

**Never expose `llm`, `gateway` or `vision` publicly.**

**Set these in Vercel:**
- **Backend:** `SYSTEM1_URL=https://<relay>/system1`, `EMBED_URL=https://<relay>/embed`, `SYSTEM2_URL=https://<relay>/system2`, `ORCHESTRATOR_URL=https://<relay>/orchestrator`.
- **MCP server:** `EMBED_URL=https://<relay>/embed`.

## 6. Required code changes (before anything is public)
1. **Auth for Laya and embed.** Today neither service checks a key, and neither the backend nor the MCP server sends one, so a public URL would hand out free GPU time.
   - Add an optional `SYSTEM1_API_KEY` / `EMBED_API_KEY` check (`X-API-Key`, like System 2) to `amd/system1/service` and `amd/embed/server.py`.
   - Make the backend's System 1 and embed clients, the MCP embed client and the db ingest client send those keys.
   - Contract first: add them to the keys table in `docs/backend-services-contract.md` and `docs/RUNNING.md`.
   - Alternatively, enforce the key at Caddy on the relay. That avoids touching the services, but the clients must still send it.
2. **The `rocm-gfx1100` Torch variant** (§4.2), plus a llama.cpp HIP image (Path A only).
3. **Docs:** `ARCHITECTURE.md` §2 (where things run), `RUNNING.md` (a "Mode 4: AMD cluster" section), `decisions.md` (record Path A or B and the relay).

## 7. Path B: native processes (fallback, and the first step either way)
- **Built (2026-10-11):** `deploy/amd-native/` runs the three GPU services (`llm`, `embed`, `system1`) this way. The CPU services stay on the laptop in Docker (hybrid mode) until §5 exposure exists.
- **Setup:** run the same seven services from §2 on the box, using `/opt/venv` (ROCm torch already works) plus each service's `requirements.txt`. llama-server is built from source with HIP for gfx1100.
- **Supervision:** `supervisord`, installed with pip. One `[program:*]` per service with `autorestart=true`, logs to `/root/logs/`, and env from a git-ignored `/root/spot.env`.
- **Ports and exposure:** the same ports, and the same relay (§5) and auth (§6).
- **Upside:** this skips image builds and uploads entirely, which is where most of Path A's time goes.
- **Pitch framing:** "seven isolated GPU/CPU services on AMD, supervised, health-checked, behind an authenticated gateway". It's honest without claiming Kubernetes.

## 8. Risks
| Risk | Mitigation |
|---|---|
| Pod lacks privileges for k3s | gate (§3) → Path B |
| Instance relaunch changes host/port and wipes the box | weights and images under `/root` on the 3.5 TB disk; relay SSH config updated by hand; a script redoes setup |
| Multi-GB image upload from venue Wi-Fi | one shared ROCm base; start the upload first; Path B needs no upload |
| VRAM contention between three GPU pods | `Recreate` strategy, fixed model sizes (§2), llama-server `--ctx-size` capped |
| Public URLs abused | §6 auth, plus HTTPS only, plus nothing but the four public routes |
| Dense 30B too slow for the 15 s System 2 budget | use a MoE; bake off on the eval harness before switching |

## 9. Decisions needed
1. Run the gate. Which path?
2. Relay: a Vultr VM (recommended) or a tunnel agent?
3. Which ~30B model (MoE recommended)?
4. Who owns the AMD work tonight?

After these are answered, the next step is the §6 auth change: contract first, then code.
