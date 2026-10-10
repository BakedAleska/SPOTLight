# Research index

One line per file. Open a file only when the task needs it. Grep `research/` before researching anything new.
The project is decided and being built; `decisions.md` is the source of truth and `docs/ARCHITECTURE.md` is the system map.
On 2026-10-10 we removed the research we no longer use: winners logs for other regions and themes, and the Databricks plan. They are still in git history (`git show d523e60:research/<file>`).

## Decisions and event
- [decisions.md](decisions.md): **Read first.** What we decided and why.
- [knighthacks-ix-tracks.md](knighthacks-ix-tracks.md): the sponsor challenges we entered (verbatim prompts) and the AMD server specs.
- [spot-data-insights-2026-10-09.md](spot-data-insights-2026-10-09.md): findings from NextEra's SPOT data. 21 of 26 "COMPLETED" missions hide failures; there's a battery cliff at ≤25%; about 60% of V2's gain is battery.

## Build research (the tech we use)
- [tech-laya-rocm-finetune-2026-10-10.md](tech-laya-rocm-finetune-2026-10-10.md): Laya on gfx1151. Probably works but untested (SDPA, no flash-attn). Use eager mode, no `laya[fast]`, soft-ce loss, ≥350 updates; watch for prior collapse (#963).
- [tech-lora-gap-2026-10-10.md](tech-lora-gap-2026-10-10.md): why LoRA trailed full FT on Laya: undertrained (3 epochs, still rising), LLRD starved lower adapters, head LR too low, frozen norms. Train loss isn't comparable (smoothing floor ~0.16). Run A = existing flags only.
- [tech-smalldata-finetune-eval-2026-10-10.md](tech-smalldata-finetune-eval-2026-10-10.md): split by mission (3 folds of 9), keep synthetic separate from the real test, mission-level bootstrap CIs, calibrate on held-out data rather than random items.
- [tech-system1-alternatives-2026-10-10.md](tech-system1-alternatives-2026-10-10.md): Laya is sound after fine-tuning + calibration. Benchmark it against logistic regression, gradient boosting and TabICL. Time-series foundation models don't fit (3–7 points per asset).
- [tech-cascade-alarm-mgmt-2026-10-10.md](tech-cascade-alarm-mgmt-2026-10-10.md): serial Laya → validate → LLM can't catch Laya misses, so run rules ‖ Laya in parallel with a rules priority floor. ISA-18.2/EEMUA 191 priorities (5/15/80), alarm-rate metrics.
- [tech-system2-context-distill-2026-10-10.md](tech-system2-context-distill-2026-10-10.md): System 2 LLM. Prompt + grammar-JSON first, LoRA distillation only as a stretch. Templated SQL context packets with row-ID citations, plus numeric faithfulness checks.
- [tech-amd-rocm-hf-serving-2026-10-09.md](tech-amd-rocm-hf-serving-2026-10-09.md): serving on gfx1151. Primary is llama.cpp built for ROCm/HIP; fallback is AMD's PyTorch wheels + `transformers serve`. VLM is Qwen3.5-9B (backup Qwen3-VL-8B). Since confirmed on the box: llama.cpp HIP serves Qwen3.5-9B Q4_K_M (see decisions).
- [tech-embedding-model-amd-2026-10-10.md](tech-embedding-model-amd-2026-10-10.md): embedding model for the pgvector RAG. Qwen3-Embedding-0.6B (1024-d, Apache-2.0) on ROCm sentence-transformers; fallback snowflake-arctic-embed-l-v2.0; exact search at our scale; Neon pgvector 0.8.x.
- [tech-spot-vision-cv-2026-10-10.md](tech-spot-vision-cv-2026-10-10.md): reading the kit's thermal/acoustic/gauge/path images. Code computes numbers (VLMs score ~30% on gauges, fail thermal); RapidOCR + RANSAC axes; Qwen3.5-9B + mmproj; route from CSV, not CV; no CSV row links an image, so use synthetic renders + a `vision` segment; Orbit keyResults schema. §6 also lists the HF dataset survey (exact-psi gauges, corrosion/valve defects, machine audio; no labelled thermal on HF).

## Pitch, judging and prizes
- [synthesis-winning-patterns-2026-10-09.md](synthesis-winning-patterns-2026-10-09.md): The winning formula, Knight Hacks IX prize strategy, idea scorecard, anti-patterns and pre-ceremony checklist. Some files it cites were removed; they're in git history.
- [winners-playbook-judging-2026-10-09.md](winners-playbook-judging-2026-10-09.md): **Use this weekend.** The KH IX rubric verbatim, MLH stack-ranking judging, winners' post-mortems (FaceTimeOS, Alfred), and a timed checklist from the ceremony through judging.
- [winners-knighthacks-history-2026-10-09.md](winners-knighthacks-history-2026-10-09.md): **Key file.** Knight Hacks VI–VIII and Project Launch 2026 winners (59), IX logistics and rubric, and every sponsor's past prompts plus its IX status. Hardware + AI for health/accessibility won Overall in 2023, 2024 and 2025.
- [winners-taikai-hackerearth-enterprise-2026-10-09.md](winners-taikai-hackerearth-enterprise-2026-10-09.md): **AMD, NVIDIA and Hugging Face-style enterprise winners** (Knight Hacks IX sponsors); 30 rows. Measured gains beat polish; build the core on the sponsor's newest product; human-in-the-loop safety layers win. TAIKAI and HackerEarth yielded no confirmed wins.
- [winners-lablab-ai-2026-10-09.md](winners-lablab-ai-2026-10-09.md): lablab.ai AI hackathons 2024–26; 33 logged. Narrow multi-agent business workflows with human approval win; LLM-proposes, hard-gate-decides guardrails; **AMD winners showed benchmark gains or offline demos** (relevant to the AMD challenge).
- [winners-design-creative-categories-2026-10-09.md](winners-design-creative-categories-2026-10-09.md): Most Useful, Best Game, First-Time, Design and Creative winners; 22 logged. Game winners use unusual input or multiplayer; Most Useful means one everyday pain point demoed end to end; realistic ceiling is 2 prizes.
- [winners-mlh-sponsor-prizes-2026-10-09.md](winners-mlh-sponsor-prizes-2026-10-09.md): MLH category winners (29), the confirmed Knight Hacks IX MLH prize list, and cheap stackable prizes (domain, DEV write-up, Vultr). Projects win at most one MLH prize each.
