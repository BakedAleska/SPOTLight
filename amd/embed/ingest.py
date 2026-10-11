#!/usr/bin/env python3
"""Build the six RAG corpora (contract §4) and upsert them into public.documents.

  python amd/embed/ingest.py                      # all kinds; only new/changed docs are embedded
  python amd/embed/ingest.py --kinds operator_note,explanation    # runtime refresh (app/MCP can also import
                                                                  # ingest_operator_note / ingest_explanation)
  python amd/embed/ingest.py --dry-run            # build docs and print counts, no embedding, no writes
  python amd/embed/ingest.py --force              # re-embed everything (e.g. after changing EMBED_MODEL)

doc_ids are stable, so re-runs are upserts. Docs whose title/content and embed_model are unchanged are not
re-embedded. Docs of a processed kind that no longer exist are deleted (--no-prune keeps them); documents the live
simulation wrote (metadata.source = 'live') are never pruned.
Embedding goes over HTTP (EMBED_URL, OpenAI-compatible; client.py retries dropped batches); every batch is logged to
app.model_calls (system='embed'). Before any write the server's /health must report EMBED_MODEL and EMBED_DIM.
Per kind: read the stored fingerprints (no transaction), embed the changed docs (no transaction open, so a slow
embedder never leaves a session idle in transaction), then upsert + prune in one short transaction.
Run `python db/setup.py migrate && python db/setup.py seed` first (incidents come from anomalies + flags).
"""
import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "db"))
import spotdb  # noqa: E402  (puts amd/system1 on sys.path)
from client import EmbedClient, to_pgvector  # noqa: E402

KINDS = ["event", "incident", "policy", "manual", "operator_note", "explanation"]
DOC_COLS = ["doc_id", "kind", "source_id", "mission_id", "action_id", "asset_id", "location_id", "action_type",
            "status", "anomaly_type", "severity", "ts", "title", "content", "metadata"]


def _h(s: Optional[str]) -> str:
    return (s or "").replace("_", " ").lower()


def _doc(**kw) -> dict:
    d = {c: kw.get(c) for c in DOC_COLS}
    d["metadata"] = kw.get("metadata") or {}
    return d


# ── event: every kit action and mission event, as prose (render_prose) ─────────────────────────────
def build_events() -> List[dict]:
    import policy_rules as PR
    from triage.events import mission_events
    from triage.kit import load_kit
    from triage.render import render_prose
    kit = load_kit(kit_dir=spotdb.KIT_DIR)
    docs = []
    for mid, m in kit.missions.items():
        for ev in mission_events(m, kit.actions_of(mid), kit.locations, kit.assets, kit.readings, kit.anomalies,
                                 source="kit"):
            if ev["kind"] == "pre_action":
                continue
            text = render_prose(ev, random.Random(ev["event_id"]))   # seeded by id: stable across runs
            tri = PR.evaluate(ev)
            if ev["kind"] == "action":
                an, asset, loc = ev["anomaly"], ev["asset"], ev["location"]
                docs.append(_doc(
                    doc_id="event:" + ev["event_id"], kind="event", source_id=ev["action_id"], mission_id=mid,
                    action_id=ev["action_id"], asset_id=asset["asset_id"], location_id=loc["location_id"],
                    action_type=ev["action_type"], status=ev["status"],
                    anomaly_type=an["anomaly_type"] if an else None, severity=an["severity"] if an else None,
                    ts=ev["timestamp"],
                    # the action id in the title makes it searchable (content_tsv is built from title + content)
                    title="%s step %d (%s): %s %s at %s, %s" % (mid, ev["sequence"], ev["action_id"],
                                                               _h(ev["action_type"]), asset["asset_id"],
                                                               loc["location_id"], ev["status"]),
                    content=text,
                    metadata={"event_kind": "action", "sequence": ev["sequence"], "battery_pct": ev["battery_pct"],
                              "failure_type": ev["failure_type"], "reading": ev["reading"],
                              "asset_criticality": asset["criticality"],
                              "configuration_version": ev["configuration_version"],
                              "priority": tri["priority"], "rules": [r["rule"] for r in tri["reasons"]]}))
            else:
                w = ev["worst_anomaly"]
                docs.append(_doc(
                    doc_id="event:" + ev["event_id"], kind="event", source_id=mid, mission_id=mid,
                    status=ev["reported_status"], anomaly_type=w["anomaly_type"] if w else None,
                    severity=w["severity"] if w else None, ts=ev["timestamp"],
                    title="Mission %s summary: reported %s, %d of %d actions failed" % (
                        mid, ev["reported_status"], ev["actions_failed"], ev["actions_total"]),
                    content=text,
                    metadata={"event_kind": "mission", "configuration_version": ev["configuration_version"],
                              "start_battery_pct": ev["start_battery_pct"], "end_battery_pct": ev["end_battery_pct"],
                              "anomalies_by_severity": ev["anomalies_by_severity"],
                              "failure_types": ev["failure_types"], "priority": tri["priority"],
                              "rules": [r["rule"] for r in tri["reasons"]]}))
    return docs


# ── incident: anomalies + System 1 flags with outcome ───────────────────────────────────────────
def build_incidents(conn) -> List[dict]:
    docs = []
    q = """
      SELECT an.anomaly_id, an.action_id, an.mission_id, an.location_id, an.asset_id, an.timestamp, an.anomaly_type,
             an.severity, an.operator_review_required, an.description,
             a.sequence, a.action_type, a.status, a.failure_type, a.battery_pct,
             s.asset_name, s.asset_type, s.criticality, l.location_name, l.zone_type, l.environment,
             r.measurement_type, r.value, r.unit, r.quality
      FROM anomalies an
      LEFT JOIN actions a ON a.action_id = an.action_id
      LEFT JOIN assets s ON s.asset_id = an.asset_id
      LEFT JOIN locations l ON l.location_id = an.location_id
      LEFT JOIN readings r ON r.action_id = an.action_id
      ORDER BY an.anomaly_id"""
    cur = conn.execute(q)
    cols = [c.name for c in cur.description]
    for row in cur:
        x = dict(zip(cols, row))
        txt = ("Incident %s: %s, severity %s, on %s (%s, %s, %s criticality) at %s (%s, %s zone, %s environment), "
               "mission %s step %s (%s, action %s) at %s. Description: \"%s\". Operator review required: %s. "
               "Action outcome: %s%s at %s%% battery." % (
                   x["anomaly_id"], _h(x["anomaly_type"]), x["severity"], x["asset_name"], x["asset_id"],
                   _h(x["asset_type"]), (x["criticality"] or "").lower(), x["location_name"], x["location_id"],
                   _h(x["zone_type"]), _h(x["environment"]), x["mission_id"], x["sequence"], _h(x["action_type"]),
                   x["action_id"], x["timestamp"], x["description"], "yes" if x["operator_review_required"] else "no",
                   x["status"], (" (" + _h(x["failure_type"]) + ")") if x["failure_type"] else "", x["battery_pct"]))
        if x["measurement_type"]:
            txt += " Reading: %s %g %s (quality %s)." % (_h(x["measurement_type"]), x["value"], x["unit"], x["quality"])
        docs.append(_doc(
            doc_id="incident:" + x["anomaly_id"], kind="incident", source_id=x["anomaly_id"],
            mission_id=x["mission_id"], action_id=x["action_id"], asset_id=x["asset_id"],
            location_id=x["location_id"], action_type=x["action_type"], status=x["status"],
            anomaly_type=x["anomaly_type"], severity=x["severity"], ts=x["timestamp"],
            title="Incident %s: %s (%s) on %s" % (x["anomaly_id"], _h(x["anomaly_type"]), x["severity"], x["asset_id"]),
            content=txt,
            metadata={"source": "anomaly", "operator_review_required": x["operator_review_required"],
                      "asset_criticality": x["criticality"], "failure_type": x["failure_type"]}))

    q = """
      SELECT f.flag_id, f.triage_event_id, f.mission_id, f.action_id, f.location_id, f.asset_id, f.score, f.severity,
             f.title, f.reasons, f.status AS flag_status, f.priority, f.category, f.created_at,
             a.action_type, a.status, a.failure_type, a.battery_pct, a.timestamp,
             an.anomaly_id, an.anomaly_type, an.severity AS anomaly_severity,
             m.mission_status, m.end_time,
             (SELECT string_agg(k.action || coalesce(': ' || k.note, '') || ' (' || k.operator || ')', '; '
                                ORDER BY k.created_at)
                FROM acknowledgements k WHERE k.flag_id = f.flag_id) AS acks,
             s.state, s.model, s.answer, s.recommended_action, s.severity_assessment
      FROM flags f
      LEFT JOIN actions a ON a.action_id = f.action_id
      LEFT JOIN anomalies an ON an.action_id = f.action_id
      LEFT JOIN missions m ON m.mission_id = f.mission_id
      LEFT JOIN assessments s ON s.flag_id = f.flag_id
      ORDER BY f.flag_id"""
    import policy_rules as PR
    cur = conn.execute(q)
    cols = [c.name for c in cur.description]
    for row in cur:
        x = dict(zip(cols, row))
        reasons = [r for r in (x["reasons"] or []) if isinstance(r, dict)]
        # Only policy rules are embedded: the verdict entries (laya_verdict / laya_unavailable / rules_verdict) carry
        # operational text such as ConnectError messages, which would match every "connection" query.
        pol = [r for r in reasons if r.get("rule") in PR.BY_NAME]
        rule_names = {r.get("rule") for r in reasons}
        laya = "unavailable" if "laya_unavailable" in rule_names else "ok" if "laya_verdict" in rule_names else "none"
        txt = "System 1 flag #%d (%s, severity %s, score %g, category %s): %s. Rules fired: %s." % (
            x["flag_id"], x["priority"] or "?", x["severity"], x["score"], x["category"] or "?", x["title"],
            "; ".join("%s (%s)" % (r["rule"], r["detail"]) if r.get("detail") else r["rule"] for r in pol) or "none")
        if x["action_id"]:
            outcome = "action %s %s%s at %s%% battery" % (x["action_id"], x["status"],
                                                         (" (" + _h(x["failure_type"]) + ")") if x["failure_type"] else "",
                                                         x["battery_pct"])
            if x["anomaly_id"]:
                outcome += "; anomaly %s %s (%s)" % (x["anomaly_id"], _h(x["anomaly_type"]), x["anomaly_severity"])
        else:
            outcome = "mission %s reported %s" % (x["mission_id"], x["mission_status"])
        txt += " Outcome: %s. Flag status: %s." % (outcome, x["flag_status"])
        if x["acks"]:
            txt += " Operator actions: %s." % x["acks"]
        if x["state"] == "DONE" and x["answer"] and (x["model"] or "template") != "template":   # audit M10
            txt += " System 2 assessment: %s Recommended action: %s." % (x["answer"], x["recommended_action"] or "none")
        docs.append(_doc(
            doc_id="incident:flag-%d" % x["flag_id"], kind="incident", source_id=str(x["flag_id"]),
            mission_id=x["mission_id"], action_id=x["action_id"], asset_id=x["asset_id"],
            location_id=x["location_id"], action_type=x["action_type"], status=x["status"] or x["mission_status"],
            anomaly_type=x["anomaly_type"], severity=x["severity"], ts=x["timestamp"] or x["end_time"],
            title="Flag #%d: %s" % (x["flag_id"], x["title"]), content=txt,
            metadata={"source": "flag", "flag_id": x["flag_id"], "triage_event_id": x["triage_event_id"],
                      "priority": x["priority"], "category": x["category"], "flag_status": x["flag_status"],
                      "rules": [r["rule"] for r in pol], "score": x["score"], "failure_type": x["failure_type"],
                      "action_type": x["action_type"], "laya_status": laya}))
    return docs


# ── policy: one doc per triage rule ──────────────────────────────────────────────────────────────
def build_policy() -> List[dict]:
    import policy_rules as PR
    from triage.policy import POLICY_VERSION
    docs = []
    for r in PR.ALL_RULES:
        if r.priority:
            head = "Triage rule %s (%s events): %s. Priority %s, %s." % (
                r.name, r.scope, r.title, r.priority,
                "escalated to System 2" if r.priority in ("P1", "P2") else "shown to the operator, not escalated")
        else:
            head = "Triage policy reference %s: %s." % (r.name, r.title)
        docs.append(_doc(doc_id="policy:" + r.name, kind="policy", source_id=r.name, severity=r.priority,
                         title=r.title, content=head + " " + r.text,
                         metadata={"rule": r.name, "scope": r.scope, "priority": r.priority,
                                   "policy_version": POLICY_VERSION, "source": "amd/system1/triage/policy.py"}))
    return docs


# ── manual: Spot User Guide PDFs, page-aware chunks with overlap ───────────────────────────────────
CHUNK_WORDS, OVERLAP_WORDS, MIN_WORDS = 220, 40, 20     # ~220 words ≈ 290 tokens; 40-word overlap


# Every numbered page starts with its footer: the printed page number and the revision ("44 R 1.1 V A ").
FOOTER_RX = re.compile(r"^(\d{2}) R \d\.\d V A ")
PRINTED_OFFSET = 8                                      # PDF page - printed page for PDF pages >= 9
HEAD_TOKEN_RX = re.compile(r"^[A-Z0-9&/()'\u2019.,:\-\u2013]*[A-Z][A-Z0-9&/()'\u2019.,:\-\u2013]*$")
HEAD_WORDS = 8


def _clean(t: str) -> str:
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)              # re-join words hyphenated across lines
    return re.sub(r"\s+", " ", t).strip()


def _footer(text: str, pdf_page: int):
    """(number of leading footer-residue words, printed page number or None)."""
    m = FOOTER_RX.match(text)
    if m:
        return len(m.group(0).split()), int(m.group(1))
    return 0, (pdf_page - PRINTED_OFFSET if pdf_page > PRINTED_OFFSET else None)


def _heading(text: str) -> Optional[str]:
    """The page's leading all-caps run ("TROUBLESHOOTING AUTOWALK ..."), at most HEAD_WORDS words; None if absent."""
    words = []
    for w in text.split()[:HEAD_WORDS]:
        if not HEAD_TOKEN_RX.match(w):
            break
        words.append(w)
    head = " ".join(words).strip(" ,.:")
    return head if len(head) >= 3 else None


def _chunks(words: List[str]) -> Iterable[List[str]]:
    if len(words) <= CHUNK_WORDS + OVERLAP_WORDS:
        yield words
        return
    step = CHUNK_WORDS - OVERLAP_WORDS
    for i in range(0, len(words), step):
        part = words[i:i + CHUNK_WORDS]
        if i > 0 and len(part) < MIN_WORDS:             # tail fully covered by the previous chunk's overlap
            break
        yield part
        if i + CHUNK_WORDS >= len(words):
            break


def build_manual(manual_dir: Path) -> List[dict]:
    import logging
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    from pypdf import PdfReader
    docs = []
    files = sorted(manual_dir.glob("*.pdf"), key=lambda p: int((re.search(r"(\d+)-\d+\.pdf$", p.name) or [0, 0])[1]))
    if not files:
        print("warning: no PDFs in %s; manual corpus empty" % manual_dir, file=sys.stderr)
    for f in files:
        m = re.search(r"(\d+)-(\d+)\.pdf$", f.name)
        first = int(m.group(1)) if m else 1                 # "Spot User Guide - 21-23.pdf" holds guide pages 21..23
        for i, page in enumerate(PdfReader(str(f)).pages):
            pno = first + i
            words = _clean(page.extract_text() or "").split()
            if len(words) < MIN_WORDS:
                continue
            n_footer, printed = _footer(" ".join(words), pno)
            head = _heading(" ".join(words[n_footer:]))
            cite = "Spot User Guide %s(PDF p.%d)" % ("p.%d " % printed if printed else "", pno)
            # Chunk boundaries (and so doc_ids) are computed with the footer words in place, then the footer is cut
            # from the first chunk. doc_id stays manual:p<PDF page>-<chunk> (cited by the contract and tests); the
            # title cites the page number printed in the guide.
            for j, part in enumerate(_chunks(words), 1):
                if j == 1:
                    part = part[n_footer:]
                docs.append(_doc(
                    doc_id="manual:p%d-%d" % (pno, j), kind="manual", source_id="p%d" % pno,
                    title="%s%s%s" % (cite, ": " + head if head else "", "" if j == 1 else " (part %d)" % j),
                    content=" ".join(part),
                    metadata={"file": f.name, "page": pno, "printed_page": printed, "heading": head,
                              "page_in_file": i + 1, "chunk": j,
                              "words": len(part), "source": "Boston Dynamics Spot User Guide (NextEra kit part two)"}))
    return docs


# ── operator_note / explanation: filled at runtime ───────────────────────────────────────────────
def build_operator_notes(conn, ack_id: Optional[int] = None) -> List[dict]:
    cur = conn.execute("""
      SELECT k.ack_id, k.flag_id, k.action, k.note, k.operator, k.created_at,
             f.mission_id, f.action_id, f.asset_id, f.location_id, f.title, f.severity, f.priority
      FROM acknowledgements k JOIN flags f ON f.flag_id = k.flag_id
      WHERE k.note IS NOT NULL AND btrim(k.note) <> '' AND (%s::bigint IS NULL OR k.ack_id = %s::bigint)
      ORDER BY k.ack_id""", (ack_id, ack_id))
    cols = [c.name for c in cur.description]
    docs = []
    for row in cur:
        x = dict(zip(cols, row))
        docs.append(_doc(
            doc_id="note:%d" % x["ack_id"], kind="operator_note", source_id=str(x["ack_id"]),
            mission_id=x["mission_id"], action_id=x["action_id"], asset_id=x["asset_id"],
            location_id=x["location_id"], severity=x["severity"], ts=x["created_at"].replace(tzinfo=None),
            title="Operator note on flag #%d" % x["flag_id"],
            content="Operator %s (%s) on flag #%d [%s]: %s" % (x["operator"], x["action"], x["flag_id"], x["title"],
                                                               x["note"].strip()),
            metadata={"flag_id": x["flag_id"], "ack_action": x["action"], "operator": x["operator"],
                      "priority": x["priority"]}))
    return docs


def build_explanations(conn, assessment_id: Optional[int] = None) -> List[dict]:
    cur = conn.execute("""
      SELECT s.assessment_id, s.flag_id, s.question, s.answer, s.reasoning, s.recommended_action,
             s.severity_assessment, s.confidence, s.model, coalesce(s.completed_at, s.created_at) AS at,
             f.mission_id, f.action_id, f.asset_id, f.location_id, f.title, f.priority
      FROM assessments s JOIN flags f ON f.flag_id = s.flag_id
      -- Only real System 2 output: template fallbacks (state TEMPLATE, or older DONE rows with model 'template')
      -- are not explanations and must not be retrieved as "earlier System 2 explanations" (audit M10).
      WHERE s.state = 'DONE' AND coalesce(s.model, 'template') <> 'template' AND s.answer IS NOT NULL
        AND (%s::bigint IS NULL OR s.assessment_id = %s::bigint)
      ORDER BY s.assessment_id""", (assessment_id, assessment_id))
    cols = [c.name for c in cur.description]
    docs = []
    for row in cur:
        x = dict(zip(cols, row))
        txt = "System 2 assessment of flag #%d [%s]. Question: %s Answer: %s Reasoning: %s Recommended action: %s" % (
            x["flag_id"], x["title"], x["question"] or "-", x["answer"], x["reasoning"] or "-",
            x["recommended_action"] or "-")
        if x["severity_assessment"]:
            txt += " Severity assessment: %s." % x["severity_assessment"]
        docs.append(_doc(
            doc_id="explanation:%d" % x["assessment_id"], kind="explanation", source_id=str(x["assessment_id"]),
            mission_id=x["mission_id"], action_id=x["action_id"], asset_id=x["asset_id"],
            location_id=x["location_id"], severity=x["severity_assessment"], ts=x["at"].replace(tzinfo=None),
            title="System 2 explanation of flag #%d" % x["flag_id"], content=txt,
            metadata={"flag_id": x["flag_id"], "model": x["model"], "confidence": x["confidence"],
                      "priority": x["priority"]}))
    return docs


# ── upsert ─────────────────────────────────────────────────────────────────────────────────────
def _fingerprint(d: dict) -> str:
    return hashlib.md5(((d["title"] or "") + "\x1f" + d["content"]).encode()).hexdigest()


def plan(conn, docs: List[dict], model: Optional[str], force: bool = False) -> List[dict]:
    """Phase 1 (read only): the docs that need a new vector (new, changed title/content, other model, or none)."""
    if not docs:
        return []
    existing = {r[0]: r[1:] for r in conn.execute(
        "SELECT doc_id, md5(coalesce(title,'') || chr(31) || content), embed_model, embedding IS NOT NULL "
        "FROM documents WHERE doc_id = ANY(%s)", ([d["doc_id"] for d in docs],))}
    return [d for d in docs if force or d["doc_id"] not in existing
            or existing[d["doc_id"]] != (_fingerprint(d), model, True)]


def embed_docs(client: Optional[EmbedClient], todo: List[dict]) -> Dict[str, Optional[List[float]]]:
    """Phase 2 (HTTP, call it with no transaction open): doc_id -> vector (None without a client)."""
    vecs = client.embed([d["content"] for d in todo], kind="doc") if (todo and client) else [None] * len(todo)
    return {d["doc_id"]: v for d, v in zip(todo, vecs)}


def write(conn, docs: List[dict], emb: Dict[str, Optional[List[float]]], model: Optional[str]) -> Dict[str, int]:
    """Phase 3: upsert the embedded docs (with their vector) and refresh the others' columns. The caller wraps it
    (and the prune) in one transaction."""
    todo = [d for d in docs if d["doc_id"] in emb]
    cols = DOC_COLS + ["embedding", "embed_model"]
    upd_meta = ", ".join("%s = EXCLUDED.%s" % (c, c) for c in DOC_COLS if c != "doc_id")
    with conn.cursor() as cur:
        # changed/new docs: everything, including the vector
        if todo:
            cur.executemany(
                "INSERT INTO documents (%s) VALUES (%s) ON CONFLICT (doc_id) DO UPDATE SET %s, "
                "embedding = EXCLUDED.embedding, embed_model = EXCLUDED.embed_model" % (
                    ", ".join(cols), ", ".join(["%s"] * (len(cols) - 2)) + ", %s::vector, %s", upd_meta),
                [tuple(json.dumps(d[c], default=str) if c == "metadata" else d[c] for c in DOC_COLS)
                 + (to_pgvector(emb[d["doc_id"]]) if emb[d["doc_id"]] is not None else None,
                    model if emb[d["doc_id"]] is not None else None) for d in todo])
        # unchanged docs: refresh filter columns + metadata only
        same = [d for d in docs if d["doc_id"] not in emb]
        if same:
            cur.executemany(
                "UPDATE documents SET %s WHERE doc_id = %%s" % ", ".join("%s = %%s" % c for c in DOC_COLS[1:]),
                [tuple(json.dumps(d[c], default=str) if c == "metadata" else d[c] for c in DOC_COLS[1:])
                 + (d["doc_id"],) for d in same])
    return {"embedded": len(todo), "unchanged": len(docs) - len(todo)}


def prune(conn, kind: str, docs: List[dict]) -> int:
    """Delete docs of `kind` that the builders no longer produce. Live-simulation docs (event:act-LIVE-*,
    explanation:live-*, metadata.source = 'live') are never built here and always survive."""
    return conn.execute("DELETE FROM documents WHERE kind = %s AND NOT (doc_id = ANY(%s)) "
                        "AND coalesce(metadata->>'source', '') <> 'live'",
                        (kind, [d["doc_id"] for d in docs])).rowcount


def upsert(conn, docs: List[dict], client: Optional[EmbedClient], force: bool = False) -> Dict[str, int]:
    """plan + embed + write in the caller's connection state (the runtime hooks below; the caller commits).
    Returns {'embedded': n, 'unchanged': n}."""
    if not docs:
        return {"embedded": 0, "unchanged": 0}
    model = client.model if client else None
    return write(conn, docs, embed_docs(client, plan(conn, docs, model, force)), model)


def ingest_operator_note(conn, ack_id: int, client: Optional[EmbedClient] = None) -> Dict[str, int]:
    """Runtime hook: embed one acknowledgement note right after the app inserts it. The caller commits."""
    return upsert(conn, build_operator_notes(conn, ack_id), client or EmbedClient())


def ingest_explanation(conn, assessment_id: int, client: Optional[EmbedClient] = None) -> Dict[str, int]:
    """Runtime hook: embed one finished System 2 assessment. The caller commits."""
    return upsert(conn, build_explanations(conn, assessment_id), client or EmbedClient())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kinds", default=",".join(KINDS), help="comma list of: " + ", ".join(KINDS))
    ap.add_argument("--force", action="store_true", help="re-embed every doc")
    ap.add_argument("--dry-run", action="store_true", help="build docs and print counts only")
    ap.add_argument("--no-prune", action="store_true", help="keep docs of processed kinds that no longer exist")
    ap.add_argument("--manual-dir", type=Path, default=spotdb.MANUAL_DIR)
    args = ap.parse_args()
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    bad = set(kinds) - set(KINDS)
    if bad:
        sys.exit("unknown kinds: %s" % ", ".join(sorted(bad)))

    conn = spotdb.connect(autocommit=True)   # reads in autocommit; each kind's write commits in its own transaction
    builders = {
        "event": build_events, "incident": lambda: build_incidents(conn), "policy": build_policy,
        "manual": lambda: build_manual(args.manual_dir), "operator_note": lambda: build_operator_notes(conn),
        "explanation": lambda: build_explanations(conn)}
    built = {k: builders[k]() for k in kinds}
    if args.dry_run:
        for k, docs in built.items():
            lens = [len(d["content"].split()) for d in docs] or [0]
            print("%-14s %4d docs, words min/avg/max %d/%d/%d" % (k, len(docs), min(lens), sum(lens) // len(lens), max(lens)))
        if built.get("manual"):
            print("sample manual chunk:", built["manual"][0]["doc_id"], built["manual"][0]["content"][:200])
        return

    client = EmbedClient()
    with spotdb.job_run("ingest", {"kinds": kinds, "embed_model": client.model, "embed_url": spotdb.redact(client.url),
                                   "force": args.force}) as run:
        logs = None
        try:
            logs = spotdb.connect(spotdb.logs_database_url(), autocommit=True)
            if not spotdb.has_table(logs, "app.model_calls"):
                logs.close()
                logs = None
        except Exception as e:  # logging must never break ingestion
            print("warning: no model-call logging: %s" % e, file=sys.stderr)

        def on_call(c: dict) -> None:
            if logs is None:
                return
            tps = (c["input_tokens"] / (c["latency_ms"] / 1000)) if c.get("input_tokens") and c["latency_ms"] else None
            try:
                spotdb.log_model_call(logs, request_id="ingest-%s" % run.run_id, system="embed", model=c["model"],
                                      endpoint=c["endpoint"], input_tokens=c.get("input_tokens"), output_tokens=0,
                                      latency_ms=c["latency_ms"], tokens_per_sec=tps,
                                      device=c.get("gpu") or c.get("device"), ok=True,
                                      metadata={"batch": c["n"], "kind": c["kind"], "server_ms": c.get("server_ms"),
                                                "job": "ingest"})
            except Exception as e:  # noqa: BLE001 - logging must never break ingestion
                print("warning: could not log embedding call: %s" % e, file=sys.stderr)

        client.on_call = on_call
        health = client.wait_ready()
        run.metadata["embed_health"] = {k: health.get(k) for k in ("model", "dim", "device", "dtype", "gpu", "hip")}
        client.check_health(health)            # SystemExit on a model/dim mismatch, before any write
        run.metadata["embed_model"] = client.model
        print("embedding with %s (%s, dim %s) at %s" % (client.model, health.get("device"), health.get("dim"),
                                                       spotdb.redact(client.url)))
        for k in kinds:
            docs = built[k]
            todo = plan(conn, docs, client.model, args.force)    # autocommit read, no transaction
            # Embed with no transaction open: Neon ends sessions idle in transaction after 5 minutes.
            emb = embed_docs(client, todo)
            with conn.transaction():
                res = write(conn, docs, emb, client.model)
                pruned = 0 if args.no_prune else prune(conn, k, docs)
            run.counts[k] = dict(res, total=len(docs), pruned=pruned)
            print("%-14s %4d docs (%d embedded, %d unchanged, %d pruned)" % (k, len(docs), res["embedded"],
                                                                             res["unchanged"], pruned))
        if logs is not None:
            logs.close()
    conn.close()


if __name__ == "__main__":
    main()
