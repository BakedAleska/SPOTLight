"""MCP get_flag_context's `graph` section (docs/system2-rag-contract.md §7) is evidence: its node ids appear in
citation_ids, so System 2 must resolve them, otherwise every packet with a graph trips the orphan-citation guard
and returns insufficient_context.
"""
import sys
from pathlib import Path

S2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S2))

from analyst.backend_contract import BackendAssessRequest  # noqa: E402
from analyst.packet import normalize_packet  # noqa: E402

GRAPH = [
    {"id": "action_type:NAVIGATION",
     "fact": "NAVIGATION —fails_more_in→ HIGH_NOISE (n=43, success 79.1% vs 81.9% overall)", "ev": "event:act-A0001"},
    {"id": "manual:p47-2", "fact": "Spot User Guide, page 47 (part 2) —mentions→ NAVIGATION, localization",
     "text": "recorded mission to autonomously navigate through the environment during mission replay."},
    {"id": "topic:safety", "fact": "topic safety —mentioned_in→ manual:p53-1"},
]


def _request(graph):
    context = {"question": "Why did navigation fail at WP05?",
               "allowed_actions": {"MONITOR": "keep watching", "MANUAL_REVIEW": "operator review"},
               "suggested_action": "MONITOR",
               "facts": [{"id": "event:act-A0001", "fact": "NAVIGATION at WP05 FAILED: OBSTACLE_DETECTION"}],
               "citation_ids": ["event:act-A0001"] + [g["id"] for g in graph]}
    if graph:
        context["graph"] = graph
    return BackendAssessRequest.model_validate(
        {"flag_id": 7, "flag": {"id": 7, "title": "nav failure"}, "context": context})


def test_graph_ids_are_citable_evidence():
    prepared = normalize_packet(_request(GRAPH))
    assert not prepared.insufficient_context
    for item in GRAPH:
        assert item["fact"] in prepared.content[item["id"]]


def test_orphan_citation_without_graph_still_guards():
    req = _request([])
    req.context["citation_ids"].append("topic:safety")
    assert normalize_packet(req).insufficient_context
