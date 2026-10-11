"""Normalize supplied packet evidence; no retrieval, fixture substitution, or tools.

Contract 2026-10-10.backend.2 adds optional multimodal inputs (docs/backend-services-contract.md, "System 2
multimodal inputs"): context.vision rows become ordinary citable V# evidence (values computed upstream by code),
context.ocr lines are kept as capped, untrusted, never-citable text, and top-level image_b64 is validated (JPEG/PNG
signature, 2 MiB decoded) and carried as context only. A request without them normalizes exactly as before.

Vision rows (audit fixes, 2026-10-10): `pairing: "demo"` rows come from an illustrative kit image, not from this
asset, so their comparison fields (agrees, delta, logged_id, logged_value) are dropped and they never trigger the
agrees=false action restriction. An optional `flags` list of uppercase reader caveats is kept (at most 8);
AMBIGUOUS_NEEDLE / DEFAULT_SWEEP force trust LOW, and CLIPPED adds `bound: "lower"` (the value is a lower bound).
"""
import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Callable, Optional
import unicodedata

from .backend_contract import BackendAction, BackendAssessRequest, BackendAssessResponse


@dataclass(frozen=True)
class ImageInput:
    """A validated request image: never evidence, only context for the model (contract backend.2)."""
    mime: str        # image/jpeg | image/png, from the magic bytes
    b64: str         # canonical base64 of the decoded bytes (no data-URL prefix, no whitespace)
    sha256: str      # hex digest of the decoded bytes; part of packet_hash and the cache key
    size: int        # decoded bytes

    @property
    def data_url(self) -> str:
        return 'data:%s;base64,%s' % (self.mime, self.b64)

    def __repr__(self):  # never dumps megabytes of base64 into logs or test output
        return 'ImageInput(%s, %d bytes, sha256=%s)' % (self.mime, self.size, self.sha256[:12])


@dataclass(frozen=True)
class PreparedPacket:
    flag_id: int
    question: str
    content: dict[str, str]
    allowed_actions: tuple[str, ...]
    suggested_action: str | None
    insufficient_context: bool
    # backend.2 multimodal context; defaults keep every backend.1 packet (and positional construction) unchanged
    ocr: tuple[str, ...] = ()
    image: Optional[ImageInput] = None
    vision_ids: tuple[str, ...] = ()

    @property
    def multimodal(self) -> bool:
        """True when any backend.2 input is present: the prompt then adds the vision rules."""
        return bool(self.vision_ids or self.ocr or self.image is not None)


# Only emitted evidence fields are consumed. Additive unknown data and identifiers
# cannot masquerade as grounding content. Nested IDs get their own resolution entry.
TEXT_FIELDS = frozenset({"fact", "text", "content", "status", "reported_status", "mission_status",
    "description", "action_type", "failure_type", "battery_status", "configuration_version", "name",
    "mission_name", "type", "criticality", "zone", "environment", "measurement_type", "unit", "quality",
    "trust", "assessment", "anomaly_type", "severity", "when", "q", "answer", "meaning", "priority",
    "category", "escalate", "reading_trust", "suggested_action", "mission_misleading", "why", "rule",
    "detail", "action", "note", "operator"})
NUMBER_FIELDS = frozenset({"sequence", "battery_pct", "duration_sec", "duration_min", "value", "p",
    "start_battery_pct", "end_battery_pct", "actions_total", "actions_failed", "true_health_pct", "score",
    "n", "n_success", "success_rate_pct"})
BOOL_FIELDS = frozenset({"operator_review_required", "correct", "status_misleading", "escalated", "in_safe_band"})
OBJECT_FIELDS = frozenset({"mission", "asset", "location", "validator", "true_health", "worst_anomaly"})
LIST_FIELDS = frozenset({"readings", "anomalies", "actions", "operator_acks", "reasons"})


# ── backend.2 multimodal inputs ───────────────────────────────────────────────────────────────
MAX_IMAGE_BYTES = 2 * 1024 * 1024
IMAGE_MAGIC = ((b'\xff\xd8\xff', 'image/jpeg'), (b'\x89PNG\r\n\x1a\n', 'image/png'))
DATA_URL_RE = re.compile(r'^data:(image/(?:jpeg|jpg|png));base64,', re.IGNORECASE)
MAX_VISION_ROWS = 16
VISION_ID_RE = re.compile(r'^V[1-9][0-9]{0,2}$')
VISION_TEXT = ('tool', 'kind', 'quantity', 'unit', 'logged_id')
VISION_NUMBER = ('value', 'logged_value', 'delta')
VISION_ENUM = {'trust': ('HIGH', 'LOW'), 'pairing': ('demo', 'linked'), 'bound': ('lower', 'upper')}
VISION_TEXT_CHARS = 64
LOW_TRUST_DROPPED = ('value', 'delta', 'agrees')   # a LOW-trust tool reading carries no value
DEMO_DROPPED = ('agrees', 'delta', 'logged_id', 'logged_value')   # a demo image is not a reading of this asset
VISION_FLAG_RE = re.compile(r'^[A-Z][A-Z_]{0,31}$')   # no digits: a flag can never ground a number
MAX_VISION_FLAGS = 8
FORCE_LOW_FLAGS = frozenset({'AMBIGUOUS_NEEDLE', 'DEFAULT_SWEEP'})   # the reader itself is unsure which value it saw
MAX_OCR_LINES, OCR_LINE_CHARS, OCR_TOTAL_CHARS = 30, 80, 1200
# agrees=false: "everything is fine" is not an answer; preferred replacements for a removed suggested_action
DISAGREE_BLOCKED = ('MONITOR', 'NO_ACTION')
DISAGREE_PREFERRED = ('MANUAL_READING', 'CHECK_SENSOR_PAYLOAD')


def clean_text(value: str, limit: int) -> str:
    """Control/format/separator characters become spaces, whitespace collapses, then cap at `limit` chars."""
    s = ''.join(' ' if unicodedata.category(ch)[0] in ('C', 'Z') else ch for ch in value)
    return ' '.join(s.split())[:limit].strip()


def parse_image(raw) -> Optional[ImageInput]:
    """None for null/""; ImageInput for a valid JPEG/PNG of at most 2 MiB; ValueError (422) otherwise."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError('image_b64 must be a base64 string or null')
    text = raw.strip()
    if not text:
        return None
    declared = None
    m = DATA_URL_RE.match(text)
    if m:
        declared = 'image/jpeg' if m.group(1).lower() in ('image/jpeg', 'image/jpg') else 'image/png'
        text = text[m.end():]
    elif text.startswith('data:'):
        raise ValueError('image_b64 data URL must be image/jpeg or image/png with base64 encoding')
    if len(text) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError('image_b64 decodes to more than %d bytes' % MAX_IMAGE_BYTES)
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('image_b64 is not valid base64') from None
    if not data:
        raise ValueError('image_b64 is empty')
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('image_b64 decodes to more than %d bytes' % MAX_IMAGE_BYTES)
    mime = next((t for magic, t in IMAGE_MAGIC if data.startswith(magic)), None)
    if mime is None:
        raise ValueError('image_b64 must be a JPEG or PNG image (signature check failed)')
    if declared is not None and declared != mime:
        raise ValueError('image_b64 data URL type does not match the image bytes')
    return ImageInput(mime, base64.b64encode(data).decode('ascii'), hashlib.sha256(data).hexdigest(), len(data))


def _vision_row(row) -> tuple[str, dict]:
    """(V# id, retained fields) for one context.vision row; ValueError (422) for malformed consumed values."""
    if not isinstance(row, dict):
        raise ValueError('vision rows must be objects')
    identifier = row.get('id')
    if not isinstance(identifier, str) or not VISION_ID_RE.match(identifier):
        raise ValueError('vision row id must look like V1')
    values = {}
    for key in VISION_TEXT:
        v = row.get(key)
        if v is not None and not isinstance(v, str):
            raise ValueError('vision %s must be a string or null' % key)
        v = clean_text(v, VISION_TEXT_CHARS) if v else ''
        if v:
            values[key] = v
    for key in VISION_NUMBER:
        v = row.get(key)
        if v is not None and not _finite_number(v):
            raise ValueError('vision %s must be a finite number or null' % key)
        if v is not None:
            values[key] = v
    conf = row.get('confidence')
    if conf is not None and (not _finite_number(conf) or not 0 <= conf <= 1):
        raise ValueError('vision confidence must be a number in [0, 1] or null')
    if conf is not None:
        values['confidence'] = conf
    agrees = row.get('agrees')
    if agrees is not None and type(agrees) is not bool:
        raise ValueError('vision agrees must be a boolean or null')
    if agrees is not None:
        values['agrees'] = agrees
    for key, allowed in VISION_ENUM.items():
        v = row.get(key)
        if v is not None and v not in allowed:
            raise ValueError('vision %s must be one of %s' % (key, '|'.join(allowed)))
        if v is not None:
            values[key] = v
    flags = row.get('flags')
    if flags is not None:
        if not isinstance(flags, list) or any(not isinstance(f, str) or not VISION_FLAG_RE.match(f) for f in flags):
            raise ValueError('vision flags must be a list of uppercase tokens like CLIPPED')
        kept = list(dict.fromkeys(flags))[:MAX_VISION_FLAGS]
        if kept:
            values['flags'] = kept
    if 'tool' not in values:
        raise ValueError('vision tool must be a nonblank string')
    if 'trust' not in values:
        raise ValueError('vision trust must be HIGH or LOW')
    if FORCE_LOW_FLAGS & set(values.get('flags', ())):
        values['trust'] = 'LOW'
    if 'CLIPPED' in values.get('flags', ()) and 'bound' not in values:
        values['bound'] = 'lower'
    if values.get('pairing') == 'demo':
        for key in DEMO_DROPPED:
            values.pop(key, None)
    if values['trust'] == 'LOW':
        for key in LOW_TRUST_DROPPED:
            values.pop(key, None)
    return identifier, values


def _vision(items, resolved: dict[str, str]) -> tuple[tuple[str, ...], bool]:
    """Adds each V# row to `resolved`; returns (ids, any HIGH-trust non-demo row with agrees=false)."""
    if not isinstance(items, list):
        raise ValueError('vision must be a list')
    if len(items) > MAX_VISION_ROWS:
        raise ValueError('vision has more than %d rows' % MAX_VISION_ROWS)
    ids, disagree = [], False
    for row in items:
        identifier, values = _vision_row(row)
        content = json.dumps(values, sort_keys=True, ensure_ascii=False)
        if identifier in resolved and resolved[identifier] != content:
            raise ValueError('conflicting content for duplicate id %s' % identifier)
        resolved[identifier] = content
        ids.append(identifier)
        disagree = disagree or (values['trust'] == 'HIGH' and values.get('pairing') != 'demo'
                                and values.get('agrees') is False)
    return tuple(dict.fromkeys(ids)), disagree


def _ocr(items) -> tuple[str, ...]:
    """Kept OCR lines: first 30 nonblank, cleaned, 80 chars each, 1,200 total. Never evidence."""
    if not isinstance(items, list):
        raise ValueError('ocr must be a list')
    out, total = [], 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('ocr lines must be objects')
        text, conf = item.get('text'), item.get('conf')
        if not isinstance(text, str):
            raise ValueError('ocr text must be a string')
        if conf is not None and (not _finite_number(conf) or not 0 <= conf <= 1):
            raise ValueError('ocr conf must be a number in [0, 1] or null')
        line = clean_text(text, OCR_LINE_CHARS)
        if not line or len(out) >= MAX_OCR_LINES:
            continue
        line = line[:max(0, OCR_TOTAL_CHARS - total)].strip()
        if not line:
            break
        out.append(line)
        total += len(line)
    return tuple(out)


def _system1_row(item: dict) -> dict:
    """Map Laya-native keys onto the MCP row shape (q, p) without touching rows already in that shape."""
    if not isinstance(item, dict) or not any(k in item for k in ('question', 'probability', 'confidence', 'probs')):
        return item
    row = dict(item)
    if 'q' not in row and isinstance(row.get('question'), str):
        row['q'] = row['question']
    if 'p' not in row:
        for key in ('probability', 'confidence'):
            if _finite_number(row.get(key)):
                row['p'] = row[key]
                break
        else:
            probs, answer = row.get('probs'), row.get('answer')
            if isinstance(probs, dict) and isinstance(answer, str) and _finite_number(probs.get(answer)):
                row['p'] = probs[answer]
    return row


def _finite_number(value) -> bool:
    # Integers are finite without a lossy float conversion (which can overflow).
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _evidence_object(obj: dict, resolved: dict[str, str]) -> dict:
    identifier = obj.get("id")
    if identifier is not None and (not isinstance(identifier, str) or not identifier.strip()):
        raise ValueError("id must be a nonblank string")
    values = {}
    for key, value in obj.items():
        if key in TEXT_FIELDS:
            if value is not None and not isinstance(value, str):
                raise ValueError("%s must be a string or null" % key)
            if isinstance(value, str) and value.strip():
                values[key] = value.strip()
        elif key in NUMBER_FIELDS:
            if value is not None and not _finite_number(value):
                raise ValueError("%s must be a finite number or null" % key)
            if value is not None:
                values[key] = value
        elif key in BOOL_FIELDS:
            if value is not None and type(value) is not bool:
                raise ValueError("%s must be a boolean or null" % key)
            if value is not None:
                values[key] = value
        elif key in OBJECT_FIELDS:
            if value is not None and not isinstance(value, dict):
                raise ValueError("%s must be an object or null" % key)
            nested = _evidence_object(value, resolved) if value is not None else {}
            if nested:
                values[key] = nested
        elif key in LIST_FIELDS:
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise ValueError("%s must be a list of objects" % key)
            nested = [_evidence_object(item, resolved) for item in value]
            if any(nested):
                values[key] = [n for n in nested if n]
        elif key in ("safe_band", "ci95_pct"):
            if (not isinstance(value, list) or len(value) != 2 or
                    any(v is not None and not _finite_number(v) for v in value)):
                raise ValueError("%s must be a numeric pair" % key)
            if any(v is not None for v in value):
                values[key] = value
        elif key == "anomalies_by_severity":
            if not isinstance(value, dict) or any(type(v) is not int or v < 0 for v in value.values()):
                raise ValueError("anomalies_by_severity must map severities to nonnegative integers")
            counts = {k: v for k, v in value.items() if k in ("LOW", "MEDIUM", "HIGH", "CRITICAL")}
            if counts:
                values[key] = counts
        elif key == "failure_types":
            if not isinstance(value, dict) or any(not isinstance(k, str) or type(v) is not int or v < 0
                                                  for k, v in value.items()):
                raise ValueError("failure_types must map failure types to nonnegative counts")
            if value:
                values[key] = value
    # A document's title/metadata alone must not rescue blank content.
    text = next((values[k] for k in ("fact", "text", "content") if k in values), None)
    if any(k in obj for k in ("fact", "text", "content")):
        content = text or ""
    else:
        content = json.dumps(values, sort_keys=True, ensure_ascii=False) if values else ""
    if identifier:
        if identifier in resolved and resolved[identifier] != content:
            raise ValueError("conflicting content for duplicate id %s" % identifier)
        resolved[identifier] = content
    return values


def normalize_packet(body: BackendAssessRequest) -> PreparedPacket:
    p = body.context
    question = p.get("question", "What needs operator attention?")
    if not isinstance(question, str):
        raise ValueError("question must be a string")
    raw_actions = p.get("allowed_actions", {})
    if not isinstance(raw_actions, (dict, list)):
        raise ValueError("allowed_actions must be a mapping or list")
    actions = list(raw_actions)
    for code in actions:
        if not isinstance(code, str) or code not in BackendAction._value2member_map_:
            raise ValueError("unknown action code")
    if isinstance(raw_actions, dict) and any(not isinstance(v, str) for v in raw_actions.values()):
        raise ValueError("action meanings must be strings")
    suggested = p.get("suggested_action")
    if suggested is not None and (not isinstance(suggested, str) or suggested not in actions):
        raise ValueError("suggested_action must belong to supplied allowed_actions")
    content = {}
    for name in ("facts", "manual", "policy", "similar_incidents", "asset_history", "system1", "graph"):
        items = p.get(name, [])
        if not isinstance(items, list):
            raise ValueError("%s must be a list" % name)
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("evidence items must be objects")
            _evidence_object(_system1_row(item) if name == "system1" else item, content)
    vision_ids, disagree = _vision(p.get("vision", []), content)
    ocr = _ocr(p.get("ocr", []))
    image = parse_image(getattr(body, "image_b64", None))
    for name in ("event", "mission", "flag"):
        if name in p:
            if not isinstance(p[name], dict):
                raise ValueError("%s must be an object" % name)
            _evidence_object(p[name], content)
    guard = False
    for name in ("missing", "gaps", "citation_ids"):
        items = p.get(name, [])
        if not isinstance(items, list) or any(not isinstance(v, str) for v in items):
            raise ValueError("%s must be a string list" % name)
        if name == "citation_ids":
            guard = guard or any(not content.get(i) for i in items)
        else:
            guard = guard or bool(items)
    if "insufficient_context" in p and type(p["insufficient_context"]) is not bool:
        raise ValueError("insufficient_context must be boolean")
    content = {i: text for i, text in content.items() if text}
    no_actions = not actions
    if disagree:   # tool and log disagree: drop the "all fine" actions (no new codes are ever added)
        actions = [a for a in actions if a not in DISAGREE_BLOCKED]
        if suggested in DISAGREE_BLOCKED:
            suggested = next((a for a in DISAGREE_PREFERRED if a in actions), None)
    return PreparedPacket(body.flag_id, question, content, tuple(dict.fromkeys(actions + ["MANUAL_REVIEW"])),
                          suggested, guard or p.get("insufficient_context", False) or not content or no_actions,
                          ocr, image, vision_ids)


def stub_assessment(p: PreparedPacket) -> BackendAssessResponse:
    identifier, claim = next(iter(p.content.items()))
    return BackendAssessResponse(flag_id=p.flag_id, question=p.question, answer=claim,
                                 evidence=[{"claim": claim, "ids": [identifier]}],
                                 recommended_action=p.suggested_action or p.allowed_actions[0],
                                 confidence="medium", insufficient_context=False, path="stub")


def assess_prepared_packet(p: PreparedPacket,
                           generate: Callable[[PreparedPacket], BackendAssessResponse] = stub_assessment
                           ) -> BackendAssessResponse:
    if p.insufficient_context:
        return BackendAssessResponse(flag_id=p.flag_id, question=p.question,
                                     answer="Insufficient supplied context; request manual review.", evidence=[],
                                     recommended_action="MANUAL_REVIEW", confidence="low",
                                     insufficient_context=True, path="insufficient_context")
    result = BackendAssessResponse.model_validate(generate(p))
    if result.flag_id != p.flag_id or result.insufficient_context:
        raise ValueError("assessment does not match prepared packet")
    if result.recommended_action.value not in p.allowed_actions:
        raise ValueError("assessment action not allowed by packet")
    for evidence in result.evidence:
        if any(not p.content.get(i) for i in evidence.ids):
            raise ValueError("assessment cites unresolved evidence")
    return result
