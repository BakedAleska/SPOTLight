"""In-memory stand-in for LiveRepo (app/db/live.py), for unit tests."""
from __future__ import annotations

import datetime as dt
import itertools
from typing import Any, Dict, List, Optional


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class FakeLiveRepo:
    def __init__(self):
        self.runs: Dict[int, Dict[str, Any]] = {}
        self.caps: Dict[tuple, Dict[str, Any]] = {}
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: Dict[tuple, Dict[str, Any]] = {}
        self.base_nodes = set()           # kit nodes the graph build would have made
        self.docs: Dict[str, Dict[str, Any]] = {}   # set by tests to the FakeRepo's documents
        self._run_ids = itertools.count(1)
        self._cap_ids = itertools.count(1)

    async def create_run(self, route_id, total_stops, requested_by):
        if any(r["status"] in ("pending", "running") for r in self.runs.values()):
            return None
        rid = next(self._run_ids)
        self.runs[rid] = {"run_id": rid, "route_id": route_id, "mission_id": "LIVE-%d" % rid, "status": "pending",
                          "current_stop": 0, "total_stops": total_stops, "runner": None,
                          "requested_by": requested_by, "error": None, "counts": {}, "created_at": _now(),
                          "claimed_at": None, "finished_at": None, "updated_at": _now()}
        return dict(self.runs[rid])

    async def active_run(self):
        act = [r for r in self.runs.values() if r["status"] in ("pending", "running")]
        return dict(act[-1]) if act else None

    async def claim(self, runner):
        for r in sorted(self.runs.values(), key=lambda r: r["run_id"]):
            if r["status"] == "pending":
                r.update(status="running", runner=runner, claimed_at=_now(), updated_at=_now())
                return dict(r)
        return None

    async def get_run(self, run_id):
        r = self.runs.get(run_id)
        return dict(r) if r else None

    async def list_runs(self, limit):
        out = []
        for r in sorted(self.runs.values(), key=lambda r: -r["run_id"])[:limit]:
            caps = [c for (rid, _), c in self.caps.items() if rid == r["run_id"]]
            out.append({**r, "red": sum(c.get("verdict") == "red" for c in caps),
                        "green": sum(c.get("verdict") == "green" for c in caps)})
        return out

    async def update_run(self, run_id, **fields):
        r = self.runs[run_id]
        if fields.pop("finished", False):
            r["finished_at"] = _now()
        r.update(fields, updated_at=_now())
        return dict(r)

    async def touch_run(self, run_id, current_stop=None):
        r = self.runs[run_id]
        if current_stop is not None:
            r["current_stop"] = max(r["current_stop"], current_stop)
        r["updated_at"] = _now()

    async def upsert_capture(self, run_id, stop_index, stop, fields, stamp=None):
        key = (run_id, stop_index)
        c = self.caps.get(key)
        if c is None:
            c = {"capture_id": next(self._cap_ids), "run_id": run_id, "stop_index": stop_index, "stop": stop,
                 "kg": {}, "timings": {}, "arrived_at": _now()}
            self.caps[key] = c
        c.update(fields)
        if stamp:
            c[stamp] = _now()
        c["updated_at"] = _now()
        return dict(c)

    async def merge_capture_json(self, run_id, stop_index, col, patch):
        c = self.caps[(run_id, stop_index)]
        c[col] = {**(c.get(col) or {}), **patch}

    async def get_capture(self, run_id, stop_index):
        c = self.caps.get((run_id, stop_index))
        return dict(c) if c else None

    async def captures(self, run_id, after=None):
        rows = [dict(c) for (rid, _), c in sorted(self.caps.items()) if rid == run_id]
        return [r for r in rows if after is None or r["updated_at"] > after]

    async def kg_upsert(self, nodes, edges):
        for n in nodes:
            self.nodes[n["node_id"]] = {**self.nodes.get(n["node_id"], {}), **n, "embedding": None}
        known = set(self.nodes) | self.base_nodes
        n_edges = 0
        for e in edges:
            if e["src"] in known and e["dst"] in known:
                self.edges[(e["src"], e["rel"], e["dst"])] = e
                n_edges += 1
        return {"nodes": len(list(nodes)), "edges": n_edges}

    async def embed_document(self, doc_id, text, vec, model):
        d = self.docs.get(doc_id)
        if not d or d["content"] != text:
            return False
        d.update(embedding=vec, embed_model=model)
        for n in self.nodes.values():
            if n.get("doc_id") == doc_id:
                n["embedding"] = vec
        return True

    async def embed_node(self, node_id, text, vec, model):
        n = self.nodes.get(node_id)
        if not n or n["summary"] != text:
            return False
        n["embedding"] = vec
        return True

    async def kg_for_run(self, run_id):
        nodes = [n for n in self.nodes.values() if n["props"].get("run_id") == str(run_id)]
        edges = [e for e in self.edges.values() if e["props"].get("run_id") == str(run_id)]
        return {"nodes": nodes, "edges": edges, "totals": {"nodes": len(self.nodes), "edges": len(self.edges)}}
