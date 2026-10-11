"""Backend transport tracer: authenticate first and assess only supplied evidence."""
import sys
import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyst.api import create_app

KEY = "backend-test-key-123456"


def request_body():
    return {"flag_id": 42, "flag": {"category": "equipment", "flag_id": 42},
            "event": {"event_id": "e-42"}, "reasons": ["Hot bearing", None],
            "context": {"facts": [{"id": "F1", "fact": "Bearing temperature is 94 C."}],
                        "manual": [{"id": "manual:cool", "text": "Inspect the bearing."}],
                        "policy": [{"id": "policy:hot", "text": "Dispatch for overheating."}],
                        "event": {"id": "action:A42", "status": "FAILED"},
                        "citation_ids": ["F1", "manual:cool", "policy:hot", "action:A42"],
                        "allowed_actions": {"DISPATCH_TECHNICIAN": "send technician"},
                        "suggested_action": "DISPATCH_TECHNICIAN", "question": "What needs attention?"}}


@pytest.mark.parametrize("canonical,alias", [(KEY, None), (None, KEY), (KEY, KEY)])
def test_factory_resolves_key_environment(monkeypatch, canonical, alias):
    for name, value in (("SYSTEM2_API_KEY", canonical), ("AMD_API_KEY", alias)):
        monkeypatch.delenv(name, raising=False)
        if value is not None:
            monkeypatch.setenv(name, value)
    with TestClient(create_app()) as client:
        response = client.post("/v1/assess", json=request_body(), headers={"X-API-Key": KEY})
    assert response.status_code == 200


def test_factory_rejects_conflict_without_disclosing_keys(monkeypatch):
    other = "other-key-0123456789"
    monkeypatch.setenv("SYSTEM2_API_KEY", KEY)
    monkeypatch.setenv("AMD_API_KEY", other)
    with pytest.raises(RuntimeError, match="disagree") as error:
        create_app()
    assert KEY not in str(error.value) and other not in str(error.value)
    # An explicitly injected key remains independent of process environment.
    with TestClient(create_app(api_key=KEY)) as client:
        assert client.get("/health").status_code == 200


def test_backend_equipment_request():
    client = TestClient(create_app(api_key=KEY))
    r = client.post("/v1/assess", json=request_body(), headers={"X-API-Key": KEY})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["flag_id"] == 42
    assert out["answer"] == "Bearing temperature is 94 C."
    assert out["evidence"] == [{"claim": out["answer"], "ids": ["F1"]}]
    assert out["recommended_action"] == "DISPATCH_TECHNICIAN"
    assert out["model"] == "stub"
    assert "packet" not in out
    # Execute the actual pure backend normalizer without importing its DB client dependencies.
    source = Path(__file__).resolve().parents[3] / "backend/app/services/system2.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ("evidence_reasoning", "normalize_assessment")]
    assert len(functions) == 2
    namespace = {}
    exec("from __future__ import annotations\n" + "\n\n".join(ast.unparse(f) for f in functions), namespace)
    stored = namespace["normalize_assessment"](out)
    assert stored["answer"] == out["answer"]
    assert "F1" in stored["reasoning"]


@pytest.mark.parametrize("key", [None, "wrong"])
def test_key_precedes_parsing(key):
    c = TestClient(create_app(api_key=KEY))
    headers = {"X-API-Key": key} if key else {}
    assert c.post("/v1/assess", content="{", headers=headers).status_code == 401


@pytest.mark.parametrize("field,value", [("flag_id", True), ("flag_id", "42"), ("flag_id", 0),
                                        ("flag", []), ("context", []), ("reasons", [12]), ("event", "bad")])
def test_consumed_types(field, value):
    body = request_body()
    body[field] = value
    c = TestClient(create_app(api_key=KEY))
    assert c.post("/v1/assess", json=body, headers={"X-API-Key": KEY}).status_code == 422


@pytest.mark.parametrize("field,value", [("value", 10**400), ("safe_band", [10**400, None]),
                                        ("ci95_pct", [-10**400, 10**400])])
@pytest.mark.parametrize("fact", [None, "Valid supplied text"])
def test_huge_finite_integers_do_not_crash(field, value, fact):
    item = {"id": "F1", field: value}
    if fact is not None:
        item["fact"] = fact
    body = {"flag_id": 1, "flag": {}, "context": {"allowed_actions": ["MONITOR"], "facts": [item]}}
    with TestClient(create_app(api_key=KEY), raise_server_exceptions=False) as client:
        response = client.post("/v1/assess", json=body, headers={"X-API-Key": KEY})
    assert response.status_code == 200, response.text
    assert response.json()["insufficient_context"] is False
