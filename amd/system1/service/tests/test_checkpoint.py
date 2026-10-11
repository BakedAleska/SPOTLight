"""Checkpoint resolution: an HF repo subfolder (LAYA_CHECKPOINT_SUBDIR) and the base-Laya fallback. No network.

The resolve_checkpoint tests need the model stack (service.engine imports triage.harness -> numpy/torch, and they
patch huggingface_hub / laya.train), so they skip when it isn't installed, e.g. the fast CI job. The load_engine
and Settings tests only need the service itself and always run.
"""
import os

import pytest

from service.app import load_engine
from service.config import Settings


def _engine_mod():
    """service.engine, or skip when the model stack (numpy/torch, huggingface_hub, laya) isn't installed."""
    for mod in ("numpy", "torch", "huggingface_hub", "laya.train"):
        pytest.importorskip(mod, reason="model stack not installed (fast mode)")
    from service import engine
    return engine


def test_subdir_downloads_only_that_folder(tmp_path, monkeypatch):
    engine_mod = _engine_mod()
    calls = {}

    def fake_snapshot(repo, allow_patterns):
        calls["repo"], calls["patterns"] = repo, allow_patterns
        (tmp_path / "run4_B_lora_lr4" / "best").mkdir(parents=True)
        return str(tmp_path)

    monkeypatch.setattr("huggingface_hub.snapshot_download", fake_snapshot)
    path = engine_mod.resolve_checkpoint("Jyler/KnightHacks-Laya-Model", "run4_B_lora_lr4/best")
    assert path == os.path.join(str(tmp_path), "run4_B_lora_lr4/best")
    assert calls == {"repo": "Jyler/KnightHacks-Laya-Model", "patterns": ["run4_B_lora_lr4/best/*"]}


def test_missing_subdir_is_an_error(tmp_path, monkeypatch):
    engine_mod = _engine_mod()
    monkeypatch.setattr("huggingface_hub.snapshot_download", lambda repo, allow_patterns: str(tmp_path))
    with pytest.raises(FileNotFoundError):
        engine_mod.resolve_checkpoint("Jyler/KnightHacks-Laya-Model", "nope/best")


def test_local_dir_ignores_subdir(tmp_path, monkeypatch):
    engine_mod = _engine_mod()
    monkeypatch.setattr("huggingface_hub.snapshot_download", lambda *a, **k: pytest.fail("must not download"))
    monkeypatch.setattr("laya.train.resolve_checkpoint_dir", lambda c: c)
    assert engine_mod.resolve_checkpoint(str(tmp_path), "run/best") == str(tmp_path)


class FakeEngine:
    loaded = []

    def __init__(self, checkpoint):
        self.checkpoint = checkpoint

    @classmethod
    def load(cls, checkpoint, device, amp, max_batch, subdir=None):
        cls.loaded.append((checkpoint, subdir))
        if checkpoint.startswith("private/"):
            raise OSError("401 Client Error: Unauthorized\nmore detail")
        return cls(checkpoint)


def settings(**kw):
    return Settings(**dict(dict(checkpoint="private/laya", checkpoint_subdir="run/best",
                                fallback_checkpoint="convaiinnovations/laya"), **kw))


def test_fallback_when_primary_fails():
    FakeEngine.loaded = []
    eng = load_engine(FakeEngine, settings())
    assert eng.checkpoint == "convaiinnovations/laya"
    assert FakeEngine.loaded == [("private/laya", "run/best"), ("convaiinnovations/laya", None)]


def test_primary_name_includes_subdir():
    eng = load_engine(FakeEngine, settings(checkpoint="Jyler/KnightHacks-Laya-Model"))
    assert eng.checkpoint == "Jyler/KnightHacks-Laya-Model/run/best"


def test_no_fallback_raises():
    with pytest.raises(OSError):
        load_engine(FakeEngine, settings(fallback_checkpoint=None))


def test_env_parsing(monkeypatch):
    monkeypatch.setenv("LAYA_CHECKPOINT_SUBDIR", "/run4_B_lora_lr4/best/")
    monkeypatch.setenv("LAYA_FALLBACK_CHECKPOINT", "")
    s = Settings.from_env()
    assert s.checkpoint_subdir == "run4_B_lora_lr4/best" and s.fallback_checkpoint is None
    monkeypatch.delenv("LAYA_FALLBACK_CHECKPOINT")
    monkeypatch.delenv("LAYA_CHECKPOINT_SUBDIR")
    s = Settings.from_env()
    assert s.checkpoint_subdir is None and s.fallback_checkpoint == "convaiinnovations/laya"
