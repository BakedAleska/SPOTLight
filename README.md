## Inspiration

NextEra asked us to help plant operators understand what SPOT is seeing, live and after a mission, without having to become technical experts. Before designing anything, we loaded NextEra's challenge kit (27 synthetic SPOT missions) into pandas. One number set the direction: **26 of the 27 missions are marked COMPLETED, yet 21 of those 26 contain failed actions**, and 14 of the 15 HIGH or CRITICAL anomalies happened inside "completed" missions.

$$\frac{21}{26} \approx 81\% \text{ of green missions were hiding a failure.}$$

That is the "wall of green dots" operators see in Orbit today. We wanted a console that shows what actually went wrong, explains why in plain language, and says what to do next. It should earn trust by showing its evidence instead of asking for it.

## What it does

- **Live patrol:** start one of five 8-stop routes and watch SPOT walk the plant blueprint. Each stop pings red or green. At photo stops, the image is read and Qwen's explanation types itself into the card as the model writes it, then the checked answer replaces it. The patrol is a simulation: it re-sends kit data as live events, and its photos are labelled as simulated.
- **Mission replay and review:** replay any recorded mission on the map and rewind its timeline. Work through a priority queue of flags. Open a flag to see its readings, photo, cited evidence and recommended action. Acknowledge, escalate, resolve it or add a note.
- **Analytics:** fleet charts by category, such as hidden failures, the battery cliff, the worst waypoints and how reliable each inspection type is, with drill-down.
- **SPOTbot:** a chat assistant grounded in the plant's records. Every citation and number in its answer is checked against the sources, and the answer gets a grounding badge.

Status is never shown by color alone: every status has a color, an icon and a word.

## How we built it

Our rule was **gates first, model second**. Explainable rules and a small classifier decide what deserves attention, a large model explains it, and a deterministic checker decides whether that explanation is shown.

**System 1, triage on every event.** [Laya](https://huggingface.co/convaiinnovations/laya) (421M, ModernBERT-large) answers typed questions in a single forward pass, with calibrated probabilities: escalate? priority? category? can this reading be trusted? It never generates text, so it can't hallucinate. Next to it runs a rules validator modelled on alarm rationalization (ISA-18.2: consequence × urgency). Simplified:

$$
\text{priority} =
\begin{cases}
\text{P1} & s = 4 \;\lor\; (s = 3 \land c \ge 3) \;\lor\; (\text{failed} \land b \le 15\%) \\
\text{P2} & s = 3 \;\lor\; (s = 2 \land c \ge 3) \;\lor\; (\text{failed} \land c \ge 3) \;\lor\; b \le 25\% \\
\text{P3} & s \ge 1 \;\lor\; \text{failed} \;\lor\; b \le 35\% \\
\text{P4} & \text{otherwise}
\end{cases}
$$

Here $s$ is anomaly severity, $c$ is asset criticality (both 1 to 4) and $b$ is battery. The battery thresholds come from the data: actions succeed 33% of the time at $b \le 25\%$, against 80 to 90% above it. Either Laya or the rules can escalate, and the rules' priority is a floor that Laya can raise but never lower.

We fine-tuned Laya with PyTorch on ROCm, on an AMD Radeon 8060S. The data is split by mission. A simulator fitted only on the training missions adds more cases, and counterfactual pairs (one-field edits that flip the label) teach it that battery or criticality alone can change the answer. Untuned Laya is near chance. Our best fine-tune reached a macro-F1 of **0.805** on held-out real missions and **0.846** on counterfactuals.

**System 2, explanations for escalated flags.** Qwen3.8-27B (8-bit GGUF) runs in llama.cpp, which we built with HIP for an AMD Radeon Pro W7900D, at about 22 tokens/s. It gets one prepared context packet from our read-only MCP server and makes no tool calls. It answers in Orbit's format: question, answer, cited evidence, a recommended action from a fixed list, and a confidence. A deterministic checker then requires, for every claim $k$ citing the rows $R_k$:

$$\text{numbers}(k) \subseteq \text{numbers}(R_k) \quad\land\quad \text{ids}(k) \subseteq \text{ids}(R_k)$$

A rejected answer is retried. If the retry also fails, the operator gets a template labelled as a template. We tested it on 38 held-out flags from 8 missions it had never seen. 36 answers (94.7%) were accepted on the first attempt, the other 2 fell back to the template, and every answer served passed the schema, citation, number and name checks. Latency was 11.2 s at p50 and 15.0 s at p95.

**Streaming.** An empty box for 10 to 15 seconds kills trust. System 2 streams llama-server's partial JSON as draft snapshots over SSE, through the FastAPI backend and Next.js to the browser. Drafts, labelled "unverified", appear in about 3 seconds. The checked answer then replaces them, and only checked answers are ever stored.

**Retrieval.** We use Neon Postgres with pgvector. It holds 503 documents (the kit, the Spot manual, incidents, policy and operator notes), embedded with Qwen3-Embedding-0.6B running on the AMD box. Search combines vector and full-text matching. It also uses a 574-node knowledge graph stored as plain tables and walked with `WITH RECURSIVE`, because Neon has no graph extension. Chat drops matches below cosine similarity 0.50, unless they also match the question's text or IDs:

$$\cos(\mathbf{q}, \mathbf{d}) = \frac{\mathbf{q} \cdot \mathbf{d}}{\lVert \mathbf{q} \rVert \, \lVert \mathbf{d} \rVert} \ge 0.50$$

Operator notes are embedded too, so later explanations can cite what a human wrote.

**Vision.** Code, not a vision-language model, computes every number taken from a photo. Readers for dial gauges, thermal colorbars, spectrograms and Orbit screenshots turn an image into citable readings and an annotated picture. Published results put VLMs at 25 to 30% on gauge and thermal reading.

**The stack.**
- The Next.js frontend, FastAPI backend and MCP server run on Vercel.
- Neon is one database for operations, the RAG documents, the knowledge graph and the audit logs. The MCP server connects with a read-only role.
- On the AMD box, the LLM, the embedding model, Laya and System 2 run under supervisord.
- The compose stack adds a LangGraph orchestrator and a LiteLLM gateway.
- SPOTbot answers through the Gemini API.
- Every service has a Dockerfile, and one `docker-compose.yml` with profiles runs it all.
- We wrote each API contract in `docs/` before its code. CI runs 14 checks on every PR.
- We built with Claude Code as a pair programmer, with separate agents for implementation and validation.

## Challenges we ran into

- **The GPU box fought us.**
  - It had no Docker, and Hugging Face was blocked. We pulled the 29 GB model through a mirror and checked its SHA-256 against Hugging Face's API.
  - The pip ROCm install had no HIP CMake files, so we installed the developer package by hand to build llama.cpp.
  - Kubernetes wouldn't run (no `CAP_SYS_ADMIN`), and sshd refused port forwarding. We bridged the services over stdio on one SSH connection, then added Cloudflare tunnels so Vercel could reach the GPU.
- **15 seconds is a long time.** At 8-bit, the 27B model's p95 missed our 15 s budget by 3 ms (15,003 ms). We kept the 8-bit quality and fixed the experience with streaming instead of shrinking the model.
- **Silent failures.**
  - llama.cpp once got stuck returning "Unexpected empty grammar stack" while `/health` still said OK, so every explanation quietly became the template. `/health` now runs a real one-token model probe.
  - In another bug, one provenance note sat in the wrong field (`gaps` instead of `facts`), which made the model refuse every live stop.
- **Our checker was too strict.** It rejected correct answers because it read `flag:14` as the number 14 and `action:A0030` as an unknown name. It now masks cited IDs and the flag's own subject, but still checks every number.
- **Gauges are hard.**
  - Our first code reader was right on 22% of 87 real gauge photos. It also marked 7 readings as trusted, and 6 of those were wrong.
  - Untuned Qwen got 46% right, but said "ok" to all 47 of its wrong answers.
  - So we shipped an acceptance policy. A photo reading counts only when two independent readers agree within 2% of the dial's span, $|x_{\text{geo}} - x_{\text{qwen}}| \le 0.02\,(x_{\max} - x_{\min})$. Otherwise the operator sees the photo and is asked for a retake or a human reading.
- **Small data.** The kit has 27 missions and 40 anomalies. We split by mission, fit the simulator on training missions only and report mission-level bootstrap confidence intervals. We also pre-registered a fresh gauge test so we can't fool ourselves.

## What we learned

- **Read the data before designing.** Our best feature came out of a pandas notebook, not a brainstorm.
- **Gates first, model second.** Rules people can audit and a calibrated classifier decide what matters. The LLM explains, and a checker decides whether the explanation is shown.
- **Fine-tuning teaches format, not facts.** Qwen is not fine-tuned: its facts come from retrieval, and the checker holds it to them.
- **A 200 from `/health` doesn't mean the model is answering.** Test a real request.
- **Perceived latency is a design problem.** Streaming made a 10-second answer feel immediate without changing the model.
- **Evaluations have bugs too.** Our first gauge scorer assumed every gauge read psi. Keep every run, report coverage next to accuracy, and pre-register the test that counts.
- **ROCm handles real workloads.** We fine-tuned Laya with PyTorch on a Radeon 8060S (gfx1151) and served a 27B model with llama.cpp on a W7900D (gfx1100).

## What's next for SPOTlight

- Continue to develop the pipeline and improve the QWEN model for better output and more trustworth responses.
