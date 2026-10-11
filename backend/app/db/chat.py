"""AI Assistance chat: history storage + read-only retrieval for grounding (docs/chat-contract.md §1).

`ChatRepo` is the backend's only way into `chat_conversations` / `chat_messages` (db/migrations/011_chat.sql;
013_chat_mission.sql adds the conversation's mission, chat.3 §9), and
the retrieval side of a chat turn (RAG `documents`, the knowledge graph, and plain SQL digests over the kit tables).

- Rows are plain dicts. uuids come back as `str`, timestamps as `datetime` (also inside the nested digest lists),
  jsonb as Python lists/dicts, numbers as int/float (never Decimal).
- A malformed conversation id is treated as "not found": get/update return None, delete returns False,
  list_messages returns [], delete_messages_from returns 0. `add_message` raises `ConversationNotFound`.
- If the chat tables are missing (010 not applied) the methods raise a `psycopg` error (UndefinedTable), and a
  database that is down raises `DatabaseUnavailable`; `main.py` maps both to 503.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid as _uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from psycopg.types.json import Jsonb

from .pool import Database
from .queries import BAND_ORDER, BATTERY_BAND_SQL, rate

log = logging.getLogger("backend.db.chat")


class ConversationNotFound(LookupError):
    """add_message on a conversation that does not exist (or an id that is not a uuid)."""


# ── columns ──────────────────────────────────────────────────────────────────────────────────────────
# chat.3 (§9): every Conversation / ConversationSummary row carries mission_id and `mission` (id, name, reported
# status) or None. A correlated subquery, so every statement that selects CONV_COLS (INSERT/UPDATE ... RETURNING
# included) gets it without a join.
CONV_MISSION = ("(SELECT json_build_object('mission_id', ms.mission_id, 'mission_name', ms.mission_name, "
                "'mission_status', ms.mission_status) FROM missions ms WHERE ms.mission_id = c.mission_id) AS mission")
CONV_COLS = ("c.conversation_id::text AS conversation_id, c.title, c.title_source, c.model, c.mode, c.pinned, "
             "c.created_at, c.updated_at, c.mission_id, " + CONV_MISSION)
MSG_COLS = ("m.message_id::text AS message_id, m.conversation_id::text AS conversation_id, m.seq, m.role, m.content, "
            "m.thinking, m.thinking_ms, m.model, m.mode, m.status, m.sources, m.citations, m.grounding, m.usage, "
            "m.latency_ms, m.error, m.error_code, m.request_id, m.created_at")
MSG_COUNT = "(SELECT count(*)::int FROM chat_messages mc WHERE mc.conversation_id = c.conversation_id)"
# Last message with visible text (an error turn with empty content is skipped), whitespace collapsed, <= 120 chars.
MSG_PREVIEW = ("(SELECT left(btrim(regexp_replace(mp.content, '\\s+', ' ', 'g')), 120) FROM chat_messages mp "
               "WHERE mp.conversation_id = c.conversation_id AND mp.content ~ '\\S' ORDER BY mp.seq DESC LIMIT 1)")

MSG_WRITABLE = ("role", "content", "thinking", "thinking_ms", "model", "mode", "status", "sources", "citations",
                "grounding", "usage", "latency_ms", "error", "error_code", "request_id")
MSG_JSONB = {"sources", "citations", "grounding", "usage"}
MSG_NOT_NULL = {"content": "", "status": "complete", "sources": [], "citations": []}   # None -> column default
MSG_IGNORED = {"message_id", "seq", "created_at", "conversation_id"}                  # assigned by the database

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
MAX_IDS = 20          # entity_digests: ids considered per kind; the SQL returns the first 5 that exist
# Hybrid document search keeps a row only if its cosine similarity reaches this floor OR it is a text match
# (match_documents_hybrid's text_match: every query term, or every id-like token of the query, is in the row; that is
# how an exact id such as 'HX-204' gets through whatever its cosine). Off-topic questions
# score 0.40-0.54 against the generic manual chunks, relevant hits 0.54-0.83 (vector-store audit VS-06, 2026-10-11);
# with no floor every question pulled in k manual passages. A document with no embedding (similarity NULL) needs
# text_match. The full-text path (no vector) has no floor. Same value as retrieval.MIN_VECTOR_SCORE for graph nodes.
CHAT_MIN_DOC_SIMILARITY = 0.50
# With a mission filter the rows are already scoped to that mission, so the floor drops to 0.40 (the MCP's): at 0.50
# 'why did this mission go wrong' on M024 lost the mission's own action events (0.45-0.49) (retrieval audit RE-06).
CHAT_MISSION_MIN_DOC_SIMILARITY = 0.40
DOC_OVERFETCH = 2     # hybrid: ask the function for k * this many rows, so the floor still leaves up to k


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────
def _uid(value: Any) -> Optional[str]:
    """Canonical uuid string, or None if `value` is not a uuid."""
    try:
        return str(_uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _title(value: Any) -> str:
    t = " ".join(str(value or "").split())     # one line
    return t[:200] or "New chat"               # the column's CHECK is 1..200 chars


def _like(q: str) -> str:
    """ILIKE pattern matching `q` literally anywhere (escapes the LIKE wildcards; backslash is the default ESCAPE)."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _clamp(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _ts(v: Any) -> Any:
    """ISO timestamp string (from json_agg) -> datetime; anything else unchanged."""
    if isinstance(v, str):
        try:
            return dt.datetime.fromisoformat(v)
        except ValueError:
            return v
    return v


def _fix_ts(items: Any, keys: Sequence[str] = ("timestamp", "created_at")) -> List[Dict[str, Any]]:
    items = list(items or [])
    for it in items:
        for k in keys:
            if k in it:
                it[k] = _ts(it[k])
    return items


def _mission_id(value: Any) -> Optional[str]:
    """'m007', ' M007 ', 'mission:M007' -> 'M007'; blank -> None. (Existence is checked by the caller.)"""
    ids = _text_ids([value]) if value not in (None, "") else []
    return ids[0] if ids else None


def _text_ids(ids: Iterable[Any]) -> List[str]:
    """'m007', ' M007 ', 'mission:M007' -> 'M007'; deduped, order kept, at most MAX_IDS."""
    out: List[str] = []
    for v in ids or ():
        s = str(v or "").strip()
        if ":" in s:
            s = s.split(":", 1)[1].strip()
        s = s.upper()
        if s and s not in out:
            out.append(s)
    return out[:MAX_IDS]


def _int_ids(ids: Iterable[Any]) -> List[int]:
    """14, '14', '#14', 'flag:14' -> 14; non-numbers and out-of-range values are skipped."""
    out: List[int] = []
    for v in ids or ():
        s = str(v if v is not None else "").strip()
        if ":" in s:
            s = s.split(":", 1)[1]
        s = s.strip().lstrip("#").strip()
        try:
            n = int(s)
        except ValueError:
            continue
        if 0 < n < 2 ** 63 and n not in out:
            out.append(n)
    return out[:MAX_IDS]


# ── shared SQL (fleet_snapshot and suggestion_facts must agree) ─────────────────────────────────────
SQL_HEAD = """
SELECT (SELECT count(*)::int FROM missions) AS missions,
       (SELECT count(*)::int FROM missions WHERE mission_status = 'COMPLETED') AS reported_completed,
       (SELECT count(*)::int FROM missions m WHERE m.mission_status = 'COMPLETED'
           AND EXISTS (SELECT 1 FROM actions a WHERE a.mission_id = m.mission_id AND a.status = 'FAILED'))
         AS completed_with_failures,
       (SELECT count(*)::int FROM missions WHERE mission_status = 'FAILED') AS reported_failed,
       (SELECT count(*)::int FROM actions) AS actions,
       (SELECT count(*)::int FROM actions WHERE status = 'FAILED') AS failed_actions,
       (SELECT count(*)::int FROM anomalies) AS anomalies,
       (SELECT count(*)::int FROM anomalies WHERE severity IN ('HIGH','CRITICAL')) AS serious_anomalies,
       (SELECT count(*)::int FROM flags WHERE escalated AND status = 'OPEN') AS open_flags"""
SQL_OPEN_BY_SEVERITY = ("SELECT severity, count(*)::int AS n FROM flags WHERE escalated AND status = 'OPEN' "
                        "GROUP BY 1")
# Success rates use the same population as Repo.reliability(): actions with status SUCCESS or FAILED.
SQL_BANDS = ("SELECT (%s) AS band, count(*)::int AS n, count(*) FILTER (WHERE a.status = 'SUCCESS')::int AS n_success "
             "FROM actions a WHERE a.status IN ('SUCCESS','FAILED') GROUP BY 1" % BATTERY_BAND_SQL)
SQL_CONFIG = ("SELECT a.configuration_version AS config, count(*)::int AS n, "
              "count(*) FILTER (WHERE a.status = 'SUCCESS')::int AS n_success FROM actions a "
              "WHERE a.status IN ('SUCCESS','FAILED') GROUP BY 1 ORDER BY 1")
SQL_WORST_LOCATIONS = """
SELECT a.location_id, l.location_name, l.environment, count(*)::int AS n,
       count(*) FILTER (WHERE a.status = 'SUCCESS')::int AS n_success
  FROM actions a LEFT JOIN locations l ON l.location_id = a.location_id
 WHERE a.status IN ('SUCCESS','FAILED')
 GROUP BY 1, 2, 3
 ORDER BY count(*) FILTER (WHERE a.status = 'SUCCESS')::float8 / count(*), count(*) DESC, 1
 LIMIT 5"""
SQL_REPEAT_OFFENDERS = """
SELECT n.asset_id, s.asset_name, s.criticality, count(*)::int AS anomalies,
       count(*) FILTER (WHERE n.severity IN ('HIGH','CRITICAL'))::int AS serious
  FROM anomalies n LEFT JOIN assets s ON s.asset_id = n.asset_id
 GROUP BY 1, 2, 3 ORDER BY 4 DESC, 5 DESC, 1 LIMIT 5"""
SQL_FAILURE_TYPES = ("SELECT coalesce(failure_type, 'UNKNOWN') AS failure_type, count(*)::int AS n FROM actions "
                     "WHERE status = 'FAILED' GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 8")
SQL_LATEST_SERIOUS_FLAG = ("SELECT flag_id, title, mission_id FROM flags WHERE escalated AND severity IN "
                           "('HIGH','CRITICAL') ORDER BY created_at DESC, flag_id DESC LIMIT 1")

# ── entity digests ──────────────────────────────────────────────────────────────────────────────────
SQL_MISSIONS = """
SELECT m.mission_id, m.mission_name, m.scenario, m.configuration_version, m.start_time, m.end_time, m.duration_min,
       m.start_battery_pct, m.end_battery_pct, m.mission_status,
       (SELECT count(*)::int FROM actions a WHERE a.mission_id = m.mission_id) AS actions,
       (SELECT count(*)::int FROM actions a WHERE a.mission_id = m.mission_id AND a.status = 'FAILED') AS failed,
       (SELECT coalesce(json_agg(json_build_object('action_id', x.action_id, 'sequence', x.sequence,
                 'action_type', x.action_type, 'asset_id', x.asset_id, 'location_id', x.location_id,
                 'battery_pct', x.battery_pct, 'failure_type', x.failure_type) ORDER BY x.sequence, x.action_id),
               '[]'::json)
          FROM (SELECT a.* FROM actions a WHERE a.mission_id = m.mission_id AND a.status = 'FAILED'
                 ORDER BY a.sequence, a.action_id LIMIT 12) x) AS failures,
       (SELECT coalesce(json_agg(json_build_object('anomaly_id', x.anomaly_id, 'asset_id', x.asset_id,
                 'anomaly_type', x.anomaly_type, 'severity', x.severity, 'description', x.description)
                 ORDER BY x.rk, x.timestamp, x.anomaly_id), '[]'::json)
          FROM (SELECT n.*, CASE n.severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3
                                WHEN 'LOW' THEN 4 ELSE 5 END AS rk
                  FROM anomalies n WHERE n.mission_id = m.mission_id
                 ORDER BY rk, n.timestamp, n.anomaly_id LIMIT 10) x) AS anomalies,
       (SELECT coalesce(json_agg(json_build_object('flag_id', x.flag_id, 'severity', x.severity,
                 'status', x.status, 'title', x.title) ORDER BY x.score DESC, x.flag_id), '[]'::json)
          FROM (SELECT f.flag_id, f.severity, f.status, f.title, f.score FROM flags f
                 WHERE f.mission_id = m.mission_id ORDER BY f.score DESC, f.flag_id LIMIT 10) x) AS flags
  FROM missions m
 WHERE m.mission_id = ANY (%s::text[])
 ORDER BY array_position(%s::text[], m.mission_id)
 LIMIT 5"""
SQL_ASSETS = """
SELECT s.asset_id, s.asset_name, s.asset_type, s.criticality, s.location_id, l.location_name,
       (SELECT count(*)::int FROM actions a WHERE a.asset_id = s.asset_id) AS inspections,
       (SELECT count(*)::int FROM actions a WHERE a.asset_id = s.asset_id AND a.status = 'FAILED') AS failed,
       (SELECT coalesce(json_agg(json_build_object('anomaly_id', x.anomaly_id, 'mission_id', x.mission_id,
                 'anomaly_type', x.anomaly_type, 'severity', x.severity, 'timestamp', x.timestamp)
                 ORDER BY x.timestamp DESC NULLS LAST, x.anomaly_id), '[]'::json)
          FROM (SELECT n.* FROM anomalies n WHERE n.asset_id = s.asset_id
                 ORDER BY n.timestamp DESC NULLS LAST, n.anomaly_id LIMIT 10) x) AS anomalies,
       (SELECT count(*)::int FROM flags f WHERE f.asset_id = s.asset_id AND f.escalated AND f.status = 'OPEN')
         AS open_flags
  FROM assets s LEFT JOIN locations l ON l.location_id = s.location_id
 WHERE s.asset_id = ANY (%s::text[])
 ORDER BY array_position(%s::text[], s.asset_id)
 LIMIT 5"""
SQL_LOCATIONS = """
SELECT l.location_id, l.location_name, l.zone_type, l.environment,
       st.actions, st.failed, st.n_rated, st.n_success,
       (SELECT coalesce(json_agg(json_build_object('failure_type', x.failure_type, 'n', x.n)
                 ORDER BY x.n DESC, x.failure_type), '[]'::json)
          FROM (SELECT coalesce(a.failure_type, 'UNKNOWN') AS failure_type, count(*)::int AS n FROM actions a
                 WHERE a.location_id = l.location_id AND a.status = 'FAILED' GROUP BY 1) x) AS failure_types,
       (SELECT coalesce(json_agg(json_build_object('asset_id', s.asset_id, 'asset_name', s.asset_name)
                 ORDER BY s.asset_id), '[]'::json)
          FROM assets s WHERE s.location_id = l.location_id) AS assets
  FROM locations l
  CROSS JOIN LATERAL (
    SELECT count(*)::int AS actions, count(*) FILTER (WHERE a.status = 'FAILED')::int AS failed,
           count(*) FILTER (WHERE a.status IN ('SUCCESS','FAILED'))::int AS n_rated,
           count(*) FILTER (WHERE a.status = 'SUCCESS')::int AS n_success
      FROM actions a WHERE a.location_id = l.location_id) st
 WHERE l.location_id = ANY (%s::text[])
 ORDER BY array_position(%s::text[], l.location_id)
 LIMIT 5"""
SQL_FLAGS = """
SELECT f.flag_id, f.title, f.severity, f.status, f.escalated, f.mission_id, f.action_id, f.asset_id, f.location_id,
       f.reasons, f.created_at,
       (SELECT json_build_object('answer', s.answer, 'recommended_action', s.recommended_action, 'model', s.model)
          FROM assessments s WHERE s.flag_id = f.flag_id AND s.answer IS NOT NULL
         ORDER BY s.assessment_id DESC LIMIT 1) AS assessment,
       (SELECT coalesce(json_agg(json_build_object('action', x.action, 'note', x.note, 'operator', x.operator,
                 'created_at', x.created_at) ORDER BY x.created_at, x.ack_id), '[]'::json)
          FROM (SELECT k.* FROM acknowledgements k WHERE k.flag_id = f.flag_id
                 ORDER BY k.created_at DESC, k.ack_id DESC LIMIT 10) x) AS acks
  FROM flags f
 WHERE f.flag_id = ANY (%s::bigint[])
 ORDER BY array_position(%s::bigint[], f.flag_id)
 LIMIT 5"""

SQL_LIST_MISSIONS = """
SELECT m.mission_id, m.mission_name, m.scenario, m.configuration_version, m.start_time, m.duration_min,
       m.start_battery_pct, m.end_battery_pct, m.mission_status, st.actions, st.failed,
       (SELECT count(*)::int FROM anomalies n WHERE n.mission_id = m.mission_id) AS anomalies,
       (SELECT count(*)::int FROM anomalies n WHERE n.mission_id = m.mission_id
           AND n.severity IN ('HIGH','CRITICAL')) AS serious_anomalies,
       (SELECT count(*)::int FROM flags f WHERE f.mission_id = m.mission_id AND f.escalated AND f.status = 'OPEN')
         AS open_flags,
       coalesce(m.mission_status = 'COMPLETED' AND st.failed > 0, false) AS hidden_failure,
       (SELECT count(*)::int FROM chat_conversations c WHERE c.mission_id = m.mission_id
           AND EXISTS (SELECT 1 FROM chat_messages me WHERE me.conversation_id = c.conversation_id)) AS chats
  FROM missions m
  CROSS JOIN LATERAL (
    SELECT count(*)::int AS actions, count(*) FILTER (WHERE a.status = 'FAILED')::int AS failed
      FROM actions a WHERE a.mission_id = m.mission_id) st
 %s
 ORDER BY m.mission_id
 LIMIT %%s"""


def _bands(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every battery band in BAND_ORDER (empty ones as n=0, success_rate_pct None)."""
    by = {r["band"]: r for r in rows}
    return [{"band": b, "n": by[b]["n"] if b in by else 0,
             "success_rate_pct": rate(by[b]["n_success"], by[b]["n"]) if b in by else None} for b in BAND_ORDER]


class ChatRepo:
    def __init__(self, db: Database):
        self.db = db

    # ════ conversations ═════════════════════════════════════════════════════════════════════════════
    async def list_conversations(self, q: Optional[str] = None, limit: int = 50, offset: int = 0,
                                 mission_id: Optional[str] = None) -> Tuple[List[Dict[str, Any]], int]:
        """ConversationSummary rows ordered pinned DESC, updated_at DESC (then id), and the total matching.
        Conversations with no messages are not listed (`chat.2`: a chat whose first send never started leaves no
        ghost row). `q`: case-insensitive substring of the title OR of any message's content (taken literally).
        `mission_id` (chat.3): only the chats about that mission (matched case-insensitively)."""
        limit, offset = _clamp(limit, 1, 500, 50), _clamp(offset, 0, 10 ** 9, 0)
        q = (q or "").strip()
        where = "WHERE EXISTS (SELECT 1 FROM chat_messages me WHERE me.conversation_id = c.conversation_id)"
        params: List[Any] = []
        if q:
            pat = _like(q)
            where += (" AND (c.title ILIKE %s OR EXISTS (SELECT 1 FROM chat_messages m "
                      "WHERE m.conversation_id = c.conversation_id AND m.content ILIKE %s))")
            params = [pat, pat]
        mid = _mission_id(mission_id)
        if mid:
            where += " AND c.mission_id = %s"
            params.append(mid)
        rows = await self.db.fetch(
            "WITH page AS (SELECT c.*, count(*) OVER () AS _total FROM chat_conversations c %s "
            "ORDER BY c.pinned DESC, c.updated_at DESC, c.conversation_id LIMIT %%s OFFSET %%s) "
            "SELECT %s, %s AS message_count, %s AS last_message_preview, c._total FROM page c "
            "ORDER BY c.pinned DESC, c.updated_at DESC, c.conversation_id" % (where, CONV_COLS, MSG_COUNT, MSG_PREVIEW),
            params + [limit, offset])
        if rows:
            total = int(rows[0]["_total"])
        elif offset:
            row = await self.db.fetchone("SELECT count(*)::int AS n FROM chat_conversations c %s" % where, params)
            total = row["n"] if row else 0
        else:
            total = 0
        for r in rows:
            r.pop("_total", None)
        return rows, total

    async def create_conversation(self, *, title: str, model: str, mode: str,
                                  mission_id: Optional[str] = None) -> Dict[str, Any]:
        """`mission_id` (chat.3): the mission this chat is about, stored canonical ('m007' -> 'M007'). The caller
        checks it exists; an unknown id raises psycopg's ForeignKeyViolation."""
        return await self.db.fetchone(
            "INSERT INTO chat_conversations AS c (title, model, mode, mission_id) VALUES (%%s, %%s, %%s, %%s) "
            "RETURNING %s, 0 AS message_count" % CONV_COLS, (_title(title), model, mode, _mission_id(mission_id)))

    async def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        cid = _uid(conversation_id)
        if cid is None:
            return None
        return await self.db.fetchone("SELECT %s, %s AS message_count FROM chat_conversations c "
                                      "WHERE c.conversation_id = %%s::uuid" % (CONV_COLS, MSG_COUNT), (cid,))

    async def update_conversation(self, conversation_id: str, *, title: Optional[str] = None,
                                  title_source: Optional[str] = None, pinned: Optional[bool] = None,
                                  model: Optional[str] = None, mode: Optional[str] = None,
                                  keep_user_title: bool = False) -> Optional[Dict[str, Any]]:
        """Sets only the given (non-None) fields; always bumps updated_at. None if the conversation doesn't exist.
        keep_user_title: skip the update (return None) when the operator has renamed the chat. The check is in the
        UPDATE's WHERE, so a rename that lands while a model title is being generated can never be overwritten."""
        cid = _uid(conversation_id)
        if cid is None:
            return None
        sets, params = [], []
        for col, val in (("title", None if title is None else _title(title)), ("title_source", title_source),
                         ("pinned", None if pinned is None else bool(pinned)), ("model", model), ("mode", mode)):
            if val is not None:
                sets.append("%s = %%s" % col)
                params.append(val)
        sets.append("updated_at = now()")
        return await self.db.fetchone(
            "WITH c AS (UPDATE chat_conversations SET %s WHERE conversation_id = %%s::uuid%s RETURNING *) "
            "SELECT %s, %s AS message_count FROM c" % (", ".join(sets),
                                                       " AND title_source <> 'user'" if keep_user_title else "",
                                                       CONV_COLS, MSG_COUNT), params + [cid])

    async def delete_conversation(self, conversation_id: str) -> bool:
        """Deletes the conversation and (ON DELETE CASCADE) its messages."""
        cid = _uid(conversation_id)
        if cid is None:
            return False
        return await self.db.execute("DELETE FROM chat_conversations WHERE conversation_id = %s::uuid", (cid,)) > 0

    # ════ messages ══════════════════════════════════════════════════════════════════════════════════
    async def list_messages(self, conversation_id: str) -> List[Dict[str, Any]]:
        cid = _uid(conversation_id)
        if cid is None:
            return []
        return await self.db.fetch("SELECT %s FROM chat_messages m WHERE m.conversation_id = %%s::uuid "
                                   "ORDER BY m.seq" % MSG_COLS, (cid,))

    async def add_message(self, conversation_id: str, m: Dict[str, Any]) -> Dict[str, Any]:
        """Stores a message and returns it. `m`: Message columns (role required; message_id/seq/created_at and
        conversation_id are ignored, unknown keys are dropped with a warning; None -> the column default for
        content/status/sources/citations). In one transaction: lock the conversation row (FOR UPDATE), seq =
        max(seq)+1 (a separate statement, so it sees a concurrent writer's committed row), insert, bump the
        conversation's updated_at. Raises ConversationNotFound, or ValueError without a role."""
        cid = _uid(conversation_id)
        if cid is None:
            raise ConversationNotFound(conversation_id)
        data: Dict[str, Any] = {}
        for k, v in (m or {}).items():
            if k in MSG_WRITABLE:
                data[k] = v
            elif k not in MSG_IGNORED:
                log.warning("add_message: ignoring unknown message field %r", k)
        if not data.get("role"):
            raise ValueError("message role is required ('user' or 'assistant')")
        for k, default in MSG_NOT_NULL.items():
            if data.get(k) is None:
                data[k] = default
        cols = list(data)
        vals = [Jsonb(data[c]) if c in MSG_JSONB and data[c] is not None else data[c] for c in cols]
        async with self.db.transaction() as cur:
            await cur.execute("SELECT 1 FROM chat_conversations WHERE conversation_id = %s::uuid FOR UPDATE", (cid,))
            if await cur.fetchone() is None:
                raise ConversationNotFound(cid)
            await cur.execute("SELECT coalesce(max(seq), 0) + 1 AS seq FROM chat_messages "
                              "WHERE conversation_id = %s::uuid", (cid,))
            seq = (await cur.fetchone())["seq"]
            await cur.execute(
                "INSERT INTO chat_messages AS m (conversation_id, seq, %s) VALUES (%%s::uuid, %%s, %s) RETURNING %s"
                % (", ".join(cols), ", ".join(["%s"] * len(cols)), MSG_COLS), [cid, seq] + vals)
            row = await cur.fetchone()
            await cur.execute("UPDATE chat_conversations SET updated_at = now() WHERE conversation_id = %s::uuid",
                              (cid,))
        return row

    async def delete_messages_from(self, conversation_id: str, seq: int) -> int:
        """Deletes the messages with seq >= `seq` (edit / regenerate drop the tail). Returns how many."""
        cid = _uid(conversation_id)
        if cid is None:
            return 0
        async with self.db.transaction() as cur:
            await cur.execute("SELECT 1 FROM chat_conversations WHERE conversation_id = %s::uuid FOR UPDATE", (cid,))
            if await cur.fetchone() is None:
                return 0
            await cur.execute("DELETE FROM chat_messages WHERE conversation_id = %s::uuid AND seq >= %s",
                              (cid, int(seq)))
            return max(cur.rowcount, 0)

    # ════ retrieval (read-only, bounded) ════════════════════════════════════════════════════════════
    async def search_documents(self, q: str, vec: Optional[str], k: int = 8, kinds: Optional[List[str]] = None,
                               mission_id: Optional[str] = None) -> Tuple[List[Dict[str, Any]], str]:
        """RAG documents. `vec` (pgvector literal) -> match_documents_hybrid (RRF score), mode 'hybrid', keeping only
        rows with similarity >= CHAT_MIN_DOC_SIMILARITY (CHAT_MISSION_MIN_DOC_SIMILARITY with a mission filter) or
        text_match (so an off-topic question can return no
        documents; the function is asked for k * DOC_OVERFETCH rows, best RRF first, at most k kept).
        `vec` None -> full text (query terms ORed, ts_rank_cd score; if nothing matches, a literal ILIKE pass
        for ids such as 'HX-204', score 0), mode 'fulltext'. Same approach as Repo.search_fulltext.
        `mission_id` (chat.3): only documents of that mission (the hybrid function's filter_mission, and the same
        filter on both full-text passes)."""
        k = _clamp(k, 1, 50, 8)
        q = (q or "").strip()
        kinds = [str(x) for x in kinds] if kinds else None
        mid = _mission_id(mission_id)
        cols = "doc_id, kind, title, content, asset_id, location_id, mission_id, action_id, ts"
        if vec:
            # similarity / text_match are only filtered on, not returned: the row shape stays the contract's.
            rows = await self.db.fetch(
                "SELECT %s, h.score::float8 AS score FROM match_documents_hybrid(%%s::vector, %%s, %%s::text[], "
                "NULL, NULL, %%s, %%s) h WHERE h.text_match OR h.similarity >= %%s "
                "ORDER BY h.score DESC, h.doc_id LIMIT %%s" % ", ".join("h." + c.strip() for c in cols.split(",")),
                (vec, q, kinds, mid, k * DOC_OVERFETCH,
                 CHAT_MISSION_MIN_DOC_SIMILARITY if mid else CHAT_MIN_DOC_SIMILARITY, k))
            return rows, "hybrid"
        if not q:
            return [], "fulltext"
        kf = (" AND d.kind = ANY (%s::text[])" if kinds else "") + (" AND d.mission_id = %s" if mid else "")
        kp = ([kinds] if kinds else []) + ([mid] if mid else [])
        rows = await self.db.fetch(
            "WITH q AS (SELECT replace(plainto_tsquery('english', %%s)::text, ' & ', ' | ') AS t) "
            "SELECT %s, ts_rank_cd(d.content_tsv, q.t::tsquery)::float8 AS score FROM documents d, q "
            "WHERE q.t <> '' AND d.content_tsv @@ q.t::tsquery%s ORDER BY score DESC, d.doc_id LIMIT %%s"
            % (", ".join("d." + c.strip() for c in cols.split(",")), kf), [q] + kp + [k])
        if not rows:
            pat = _like(q)
            rows = await self.db.fetch(
                "SELECT %s, 0.0::float8 AS score FROM documents d WHERE (d.content ILIKE %%s OR d.title ILIKE %%s)%s "
                "ORDER BY d.doc_id LIMIT %%s" % (", ".join("d." + c.strip() for c in cols.split(",")), kf),
                [pat, pat] + kp + [k])
        return rows, "fulltext"

    async def match_graph_nodes(self, q: str, vec: Optional[str], k: int = 6) -> List[Dict[str, Any]]:
        """Graph entry nodes: match_kg_nodes_text (score 0..1) + when `vec`, match_kg_nodes (cosine similarity,
        clamped to 0..1), merged by node_id (score = the better of the two; match 'text' | 'vector' | 'both'),
        best first, at most k."""
        k = _clamp(k, 1, 50, 6)
        q = (q or "").strip()
        merged: Dict[str, Dict[str, Any]] = {}
        if q:
            for r in await self.db.fetch("SELECT node_id, kind, label, summary, doc_id, score::float8 AS score "
                                         "FROM match_kg_nodes_text(%s, NULL, %s)", (q, k)):
                r["score"] = max(0.0, min(1.0, float(r["score"] or 0.0)))
                r["match"] = "text"
                merged[r["node_id"]] = r
        if vec:
            for r in await self.db.fetch("SELECT node_id, kind, label, summary, doc_id, similarity::float8 AS score "
                                         "FROM match_kg_nodes(%s::vector, NULL, %s)", (vec, k)):
                s = max(0.0, min(1.0, float(r["score"] or 0.0)))
                hit = merged.get(r["node_id"])
                if hit is not None:
                    hit["match"], hit["score"] = "both", max(hit["score"], s)
                else:
                    r["score"], r["match"] = s, "vector"
                    merged[r["node_id"]] = r
        rows = sorted(merged.values(), key=lambda r: (-r["score"], r["match"] != "both", r["node_id"]))
        return rows[:k]

    async def graph_neighborhood(self, seed_ids: List[str], max_hops: int = 2, max_nodes: int = 25,
                                 kind_filter: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Rows of the SQL graph_neighborhood() (hop, via_rel, via_node, path as list[str], weight, evidence)."""
        seeds = [s for s in dict.fromkeys(str(x).strip() for x in (seed_ids or ())) if s]
        if not seeds:
            return []
        rows = await self.db.fetch(
            "SELECT node_id, kind, label, summary, doc_id, hop, via_rel, via_node, path, weight::float8 AS weight, "
            "evidence FROM graph_neighborhood(%s::text[], %s, NULL, %s::text[], %s)",
            (seeds, _clamp(max_hops, 0, 3, 2), list(kind_filter) if kind_filter else None,
             _clamp(max_nodes, 0, 200, 25)))
        for r in rows:
            r["path"] = list(r["path"] or [])
        return rows

    async def _many(self, statements: Sequence[Tuple[str, Optional[Sequence[Any]]]]) -> List[List[Dict[str, Any]]]:
        """Several read statements on one pooled connection (one checkout instead of one per statement)."""
        out: List[List[Dict[str, Any]]] = []
        async with self.db.connection() as conn:
            async with conn.cursor() as cur:
                for sql, params in statements:
                    await cur.execute(sql, params)
                    out.append(list(await cur.fetchall()) if cur.description else [])
        return out

    async def fleet_snapshot(self) -> Dict[str, Any]:
        head, sev, bands, config, worst, offenders, ftypes = await self._many([
            (SQL_HEAD, None), (SQL_OPEN_BY_SEVERITY, None), (SQL_BANDS, None), (SQL_CONFIG, None),
            (SQL_WORST_LOCATIONS, None), (SQL_REPEAT_OFFENDERS, None), (SQL_FAILURE_TYPES, None)])
        out: Dict[str, Any] = dict(head[0])
        by_sev = {r["severity"]: r["n"] for r in sev}
        out["open_by_severity"] = {s: by_sev.pop(s, 0) for s in SEVERITY_ORDER}
        out["open_by_severity"].update(sorted((str(s), n) for s, n in by_sev.items()))   # any other value
        out["battery_bands"] = _bands(bands)
        out["by_config"] = [{"config": r["config"], "n": r["n"], "success_rate_pct": rate(r["n_success"], r["n"])}
                            for r in config]
        out["worst_locations"] = [{"location_id": r["location_id"], "location_name": r["location_name"],
                                   "environment": r["environment"], "n": r["n"],
                                   "success_rate_pct": rate(r["n_success"], r["n"])} for r in worst]
        out["repeat_offenders"] = offenders
        out["failure_types"] = ftypes
        return out

    async def entity_digests(self, *, mission_ids: Iterable[Any] = (), asset_ids: Iterable[Any] = (),
                             location_ids: Iterable[Any] = (), flag_ids: Iterable[Any] = ()) -> Dict[str, Any]:
        """Per-entity fact sheets for the ids named in the question. Unknown ids are skipped; <= 5 per kind, in the
        order the ids were given. Ids are matched case-insensitively and may carry a 'kind:' prefix."""
        out: Dict[str, Any] = {"missions": [], "assets": [], "locations": [], "flags": []}
        todo = [(name, sql, ids) for name, sql, ids in (
            ("missions", SQL_MISSIONS, _text_ids(mission_ids)), ("assets", SQL_ASSETS, _text_ids(asset_ids)),
            ("locations", SQL_LOCATIONS, _text_ids(location_ids)), ("flags", SQL_FLAGS, _int_ids(flag_ids))) if ids]
        if not todo:
            return out
        results = await self._many([(sql, (ids, ids)) for _, sql, ids in todo])
        for (name, _, _), rows in zip(todo, results):
            out[name] = rows
        for r in out["missions"]:
            r["failures"] = list(r["failures"] or [])
            r["anomalies"] = list(r["anomalies"] or [])
            r["flags"] = list(r["flags"] or [])
        for r in out["assets"]:
            r["anomalies"] = _fix_ts(r["anomalies"])
        for r in out["locations"]:
            n_rated, n_success = r.pop("n_rated"), r.pop("n_success")
            r["success_rate_pct"] = rate(n_success, n_rated)
            r["failure_types"] = list(r["failure_types"] or [])
            r["assets"] = list(r["assets"] or [])
        for r in out["flags"]:
            r["acks"] = _fix_ts(r["acks"])
        return out

    async def list_missions(self, q: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """chat.3 (§9): one MissionSummary row per mission for the picker, ordered by mission_id. `q`: ILIKE on the
        id or the name (taken literally). `hidden_failure`: reported COMPLETED with >= 1 failed action (the same
        rule as fleet_snapshot's completed_with_failures). `open_flags`: escalated and OPEN (as the snapshot).
        `chats`: conversations about the mission with >= 1 message (the ones the history panel lists)."""
        limit = _clamp(limit, 1, 500, 100)
        q = (q or "").strip()
        where, params = "", []
        if q:
            pat = _like(q)
            where = "WHERE (m.mission_id ILIKE %s OR m.mission_name ILIKE %s)"
            params = [pat, pat]
        return await self.db.fetch(SQL_LIST_MISSIONS % where, params + [limit])

    async def suggestion_facts(self) -> Dict[str, Any]:
        """Facts for the starter prompts (no LLM call). Same queries as fleet_snapshot, so the numbers agree."""
        head, bands, worst, offenders, serious = await self._many([
            (SQL_HEAD, None), (SQL_BANDS, None), (SQL_WORST_LOCATIONS, None), (SQL_REPEAT_OFFENDERS, None),
            (SQL_LATEST_SERIOUS_FLAG, None)])
        h = head[0]
        low = next((b for b in _bands(bands) if b["n"] > 0), None)
        return {
            "hidden_failures": {"count": h["completed_with_failures"], "of": h["reported_completed"]},
            "top_asset": ({"asset_id": offenders[0]["asset_id"], "asset_name": offenders[0]["asset_name"],
                           "anomalies": offenders[0]["anomalies"]} if offenders else None),
            "worst_location": ({"location_id": worst[0]["location_id"], "location_name": worst[0]["location_name"],
                                "success_rate_pct": rate(worst[0]["n_success"], worst[0]["n"])} if worst else None),
            "latest_serious_flag": dict(serious[0]) if serious else None,
            "low_battery": {"band": low["band"], "success_rate_pct": low["success_rate_pct"]} if low else None,
        }
