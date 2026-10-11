"""Vercel deployment (README "Deploy on Vercel"): the entrypoint, the build step's bundle and the serverless settings.

The bundle test rebuilds what Vercel ships: a copy of backend/ after `vercel_build.py`, with no repo around it, imported
the way the Python runtime does (`index.py` from the function root) in a fresh interpreter."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.services.calllog import LogSink

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
SHIPPED_TRIAGE = {"__init__.py", "events.py", "kit.py", "policy.py", "questions.py", "render.py"}

PROBE = r"""
import asyncio, json, sys
sys.path.insert(0, ".")                       # the function root, like Vercel's runtime
import index
from fastapi import FastAPI
from app.services import triage

async def lifespan_and_health():
    import httpx
    async with index.app.router.lifespan_context(index.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=index.app), base_url="http://x") as c:
            return (await c.get("/health", params={"deep": "false"})).json()

health = asyncio.run(lifespan_and_health())
svc = index.app.state.svc
print(json.dumps({
    "fastapi": isinstance(index.app, FastAPI), "torch": "torch" in sys.modules,
    "triage_dir": str(triage.TRIAGE_DIR), "triage_file": triage.T_policy.__file__, "kit_dir": str(svc.settings.kit_dir),
    "missions": len(svc.ref.missions), "replay_file": svc.ref.replay_path.exists(),
    "serverless": svc.settings.serverless, "replay_mode": svc.settings.replay_mode,
    "replay_cls": type(svc.replay).__name__, "pool_min": svc.db.pool_min, "check": svc.db.check,
    "health_ok": health["ok"], "health_replay": health["replay"]}))
"""


def _probe(cwd: Path, **env) -> dict:
    clean = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), **env}
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=cwd, env=clean, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_vercel_json_targets_the_entrypoint_and_the_build_step():
    cfg = json.loads((BACKEND / "vercel.json").read_text())
    fn = cfg["functions"]["index.py"]
    assert 0 < fn["maxDuration"] <= 300                                    # Hobby + Fluid compute cap
    assert "tests/**" in fn["excludeFiles"] and "includeFiles" not in fn   # the Python runtime ignores includeFiles
    assert cfg["buildCommand"].split()[-1] == "vercel_build.py" and (BACKEND / "vercel_build.py").is_file()
    assert len(cfg["regions"]) == 1                                        # Hobby: one region (match Neon's)
    # Vercel tries app.py before index.py: a backend/app.py would shadow the `app` package.
    assert not (BACKEND / "app.py").exists() and (BACKEND / "index.py").is_file()
    assert (BACKEND / ".python-version").read_text().strip() == "3.12"     # the Dockerfile's python:3.12-slim


def test_entrypoint_in_the_checkout_uses_the_repo_copy():
    got = _probe(BACKEND, VERCEL="1")
    assert got["fastapi"] and got["health_ok"] and not got["torch"]
    assert Path(got["triage_dir"]) == REPO / "amd" / "system1"
    assert Path(got["kit_dir"]) == REPO / "amd" / "system1" / "data" / "kit" and got["missions"] > 0
    assert got["serverless"] and got["replay_mode"] == "stepped" and got["replay_cls"] == "SteppedReplay"
    assert got["pool_min"] == 0 and got["check"]


def test_bundle_built_by_vercel_build_imports_without_the_repo(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(BACKEND / "app", repo / "backend" / "app", ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("index.py", "vercel_build.py", "requirements.txt"):
        shutil.copy2(BACKEND / f, repo / "backend" / f)
    shutil.copytree(REPO / "amd" / "system1" / "triage", repo / "amd" / "system1" / "triage",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "amd" / "system1" / "data" / "kit", repo / "amd" / "system1" / "data" / "kit")
    build = subprocess.run([sys.executable, "vercel_build.py"], cwd=repo / "backend", capture_output=True, text=True)
    assert build.returncode == 0, build.stderr

    shared = repo / "backend" / "_shared" / "amd" / "system1"
    assert {p.name for p in (shared / "triage").iterdir()} == SHIPPED_TRIAGE   # harness/trainer (torch) stay out
    assert (shared / "data" / "kit" / "replay.ndjson").read_bytes() == \
        (REPO / "amd" / "system1" / "data" / "kit" / "replay.ndjson").read_bytes()

    task = tmp_path / "task"                     # what the function sees: backend/ alone, no amd/ next to it
    shutil.move(str(repo / "backend"), str(task))
    got = _probe(task, VERCEL="1")
    assert got["fastapi"] and got["health_ok"] and not got["torch"]
    assert Path(got["triage_dir"]) == task / "_shared" / "amd" / "system1"
    assert Path(got["triage_file"]).parent == task / "_shared" / "amd" / "system1" / "triage"
    assert Path(got["kit_dir"]) == task / "_shared" / "amd" / "system1" / "data" / "kit"
    assert got["missions"] > 0 and got["replay_file"]
    assert got["health_replay"] == {"state": "idle", "mode": "stepped"}


def test_vercel_build_fails_loudly_without_the_shared_sources(tmp_path):
    (tmp_path / "backend").mkdir()
    shutil.copy2(BACKEND / "vercel_build.py", tmp_path / "backend" / "vercel_build.py")
    out = subprocess.run([sys.executable, "vercel_build.py"], cwd=tmp_path / "backend", capture_output=True, text=True)
    assert out.returncode == 1 and "Include source files outside of the Root Directory" in out.stderr


def test_serverless_settings_follow_vercel_and_can_be_overridden(monkeypatch):
    for k in ("SERVERLESS", "VERCEL", "REPLAY_MODE", "DB_POOL_MIN"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert (s.serverless, s.replay_mode, s.db_pool_min) == (False, "background", 1)       # Docker / local
    monkeypatch.setenv("VERCEL", "1")
    s = Settings.from_env()
    assert (s.serverless, s.replay_mode, s.db_pool_min) == (True, "stepped", 0)
    monkeypatch.setenv("SERVERLESS", "0")
    monkeypatch.setenv("REPLAY_MODE", "stepped")
    s = Settings.from_env()
    assert (s.serverless, s.replay_mode, s.db_pool_min) == (False, "stepped", 1)
    monkeypatch.setenv("REPLAY_MODE", "cron")
    with pytest.raises(ValueError):
        Settings.from_env()


async def test_drain_with_a_timeout_never_waits_for_a_slow_log_write():
    sink = LogSink(None)
    sink.spawn(asyncio.sleep(5))
    t0 = time.perf_counter()
    await sink.drain(timeout_s=0.05)
    assert time.perf_counter() - t0 < 1
    for t in list(sink._tasks):
        t.cancel()
    await sink.drain()


async def test_serverless_requests_finish_their_log_writes_before_responding(make_harness):
    h = make_harness(serverless=True)
    drained = []
    real = h.sink.drain

    async def spy(timeout_s=None):
        drained.append(timeout_s)
        await real(timeout_s)
    h.sink.drain = spy
    assert (await h.client.get("/api/v1/replay/status")).status_code == 200
    assert drained and drained[-1] is not None
    await h.client.aclose()
