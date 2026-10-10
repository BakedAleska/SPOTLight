"""The model side of the service: one LayaRunner, loaded once, answering many events per forward pass.

`Engine.predict` renders each canonical event with `render_json` (the rendering the model was trained
on), builds one harness row per event and scores every (event, question) pair through
`LayaRunner.predict` in a single batch, with the checkpoint's calibrated temperatures.
"""
import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

from triage.harness import LayaRunner
from triage.questions import QUESTION_SETS, QUESTIONS
from triage.render import render_json

log = logging.getLogger("system1.engine")

# (kind, canonical event dict, question ids)
Job = Tuple[str, dict, Sequence[str]]

# A tiny, valid action event used to warm the model up at startup (first-call latency).
WARMUP_EVENT = {
    "kind": "action", "source": "warmup", "event_id": "act-WARMUP", "action_id": "WARMUP",
    "mission_id": "M000", "configuration_version": "V1", "timestamp": "2026-10-10T00:00:00", "sequence": 1,
    "action_type": "THERMAL_INSPECTION", "status": "SUCCESS", "failure_type": None, "duration_sec": 30,
    "battery_pct": 80,
    "location": {"location_id": "WP01", "location_name": "Main Entrance", "zone_type": "ACCESS", "environment": "NORMAL"},
    "asset": {"asset_id": "X-1", "asset_name": "Warmup Asset", "asset_type": "Pump", "criticality": "LOW"},
    "reading": None, "anomaly": None,
}


def _training_summary(cfg: dict) -> Optional[dict]:
    tr = cfg.get("training")
    if not tr:
        return None
    # The top-level keys describe Laya's own pretraining (finetune.py keeps them); ours live under spot_triage.
    out = {"laya_pretraining": {k: tr[k] for k in ("updates", "epochs_completed", "hours") if k in tr}}
    st = tr.get("spot_triage")
    if st:
        args = st.get("args") or {}
        out["spot_triage"] = {k: args.get(k) for k in ("base", "epochs", "lora", "encoder_lr", "oversample",
                                                        "select_metric", "max_minutes") if k in args}
        out["spot_triage"]["trained_on"] = st.get("device")
        if st.get("budget"):
            out["spot_triage"]["budget"] = st["budget"]
    return out


def resolve_checkpoint(checkpoint: str, subdir: Optional[str] = None) -> str:
    """Local dir for `checkpoint`. With `subdir`, an HF repo id is fetched as `<subdir>/*` only (a private repo
    reads HF_TOKEN) and the returned dir is `<snapshot>/<subdir>`; a local `checkpoint` ignores `subdir`."""
    if subdir and not os.path.isdir(checkpoint):
        from huggingface_hub import snapshot_download

        root = snapshot_download(checkpoint, allow_patterns=["%s/*" % subdir])
        path = os.path.join(root, subdir)
        if not os.path.isdir(path):
            raise FileNotFoundError("%s has no folder %r" % (checkpoint, subdir))
        return path
    from laya.train import resolve_checkpoint_dir

    return resolve_checkpoint_dir(checkpoint)


class Engine:
    def __init__(self, runner: LayaRunner, checkpoint: str, max_batch: int = 0,
                 resolved_dir: Optional[str] = None):
        self.runner = runner
        self.checkpoint = checkpoint
        # 0 = auto. On GPU a whole 64-event request (<= 256 pairs) is one forward pass. On CPU a single pass over
        # ~200 pairs needs > 7 GB of activations (it OOM-killed an 8 GB Docker VM), and CPU throughput is
        # flat in batch size, so chunk by 16.
        auto = 256 if runner.device.type == "cuda" else 16
        self.max_batch = max_batch if max_batch and max_batch > 0 else auto
        self.resolved_dir = resolved_dir
        self.lock = threading.Lock()          # one model, one forward pass at a time
        self.questions_match = self._check_questions()

    # ── loading ──
    @classmethod
    def load(cls, checkpoint: str, device: str = "auto", amp: bool = True, max_batch: int = 0,
             subdir: Optional[str] = None) -> "Engine":
        t0 = time.perf_counter()
        resolved = resolve_checkpoint(checkpoint, subdir)   # downloads to HF_HOME on first use
        runner = LayaRunner.load(resolved, device=device, amp=None if amp else False)
        runner.name = checkpoint
        eng = cls(runner, checkpoint, max_batch=max_batch, resolved_dir=resolved)
        log.info("loaded %s (%s) on %s, amp=%s in %.1fs", checkpoint, resolved, runner.device, runner.amp,
                 time.perf_counter() - t0)
        return eng

    def _check_questions(self) -> Optional[bool]:
        """Does the checkpoint's questions.json (written by finetune.py) match triage/questions.py?"""
        if not self.resolved_dir:
            return None
        path = os.path.join(self.resolved_dir, "questions.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                theirs = json.load(f)
        except (OSError, ValueError):
            return None
        ok = all(QUESTIONS.get(q) == spec for q, spec in theirs.items())
        if not ok:
            log.warning("checkpoint questions.json differs from triage/questions.py; answers may be off")
        return ok

    def warmup(self) -> float:
        t0 = time.perf_counter()
        self.predict([("action", WARMUP_EVENT, QUESTION_SETS["action"])])
        return time.perf_counter() - t0

    # ── info ──
    @property
    def device(self) -> str:
        return str(self.runner.device)

    @property
    def fine_tuned(self) -> bool:
        return bool(self.runner.cfg.get("fine_tuned", False))

    def model_info(self) -> dict:
        cfg = self.runner.cfg
        info = {"checkpoint": self.checkpoint, "fine_tuned": self.fine_tuned,
                "temperatures": {"temperature": cfg.get("temperature"),
                                 "temperature_by_options": cfg.get("temperature_by_options") or {}},
                "decision_thresholds": cfg.get("decision_thresholds") or {},
                "device": self.device, "amp": bool(self.runner.amp), "max_batch": self.max_batch,
                "resolved_dir": self.resolved_dir,
                "encoder": cfg.get("encoder"), "max_len": self.runner.max_len,
                "questions_match": self.questions_match}
        summary = _training_summary(cfg)
        if summary:
            info["training_summary"] = summary
        return info

    # ── inference ──
    def predict(self, jobs: List[Job]) -> Tuple[List[Dict[str, dict]], dict]:
        """Answers per job, in order, plus stats. All (event, question) pairs of the request are scored
        together, in forward passes of up to `max_batch` pairs (one pass on GPU)."""
        rows = []
        for i, (kind, event, qids) in enumerate(jobs):
            qs = {q: QUESTIONS[q] for q in qids}
            rows.append({"id": str(i), "state": render_json(dict(event, kind=kind)), "questions": qs,
                         # the harness builds training targets too; inference ignores them
                         "expected": {q: next(iter(QUESTIONS[q]["criteria"])) for q in qids},
                         "meta": {}})
        n_pairs = sum(len(r["questions"]) for r in rows)
        with self.lock:
            res = self.runner.predict(rows, batch_size=min(self.max_batch, max(1, n_pairs)), calibrated=True)
        out: List[Dict[str, dict]] = [{} for _ in jobs]
        for p in res["predictions"]:
            out[int(p["id"])][p["qid"]] = {"answer": p["pred"], "probs": p["probs"], "confidence": p["confidence"]}
        if res["skipped"]:
            log.warning("skipped question items: %s", res["skipped"])
        stats = {"items": len(jobs), "decisions": res["items"], "forward_ms": round(1000 * res["seconds"], 1),
                 "skipped": res["skipped"]}
        return out, stats
