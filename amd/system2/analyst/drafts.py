"""Streaming drafts: partial model JSON -> display-only draft fields (docs/backend-services-contract.md, "Streaming
Qwen drafts").

A draft is never an answer. draft_fields(text) reads the incomplete JSON the model has generated so far (the
grammar-constrained GEN-01 object) and returns {evidence: [{claim, ids}], answer, recommended_action, confidence} with
only the keys whose value has started. An in-progress string is included as decoded so far (an escape cut mid-way,
including half of a surrogate pair, is left out until it completes); `ids` holds only complete strings; incomplete
keys, numbers and literals are dropped; `question` and `insufficient_context` are never returned (they repeat the
packet). It never raises: anything it cannot read ends the parse and returns what was read before it.

DraftThrottle turns the generator's per-chunk callback (call, tokens, text) into at most one draft per `interval_s`
per call: the first chunk of a new call always emits, an unchanged snapshot is skipped, and the text is parsed only
when a draft is emitted.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

DRAFT_KEYS = ('evidence', 'answer', 'recommended_action', 'confidence')   # GEN-01 order, question omitted
DRAFT_INTERVAL_S = 0.1
MAX_DEPTH = 16                     # the GEN-01 object nests 4 deep; anything deeper ends the parse
_WS = ' \t\n\r'
_ESCAPES = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}
_HEX = frozenset('0123456789abcdefABCDEF')
_MISSING = object()                # a value that has not started (or cannot be read)


def _ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in _WS:
        i += 1
    return i


def _string(s: str, i: int):
    """s[i] == '"'. (decoded so far, next index, complete)."""
    out, n, high = [], len(s), None   # high: a pending high surrogate waiting for its low half
    i += 1
    while i < n:
        c = s[i]
        if c == '"':
            if high is not None:
                out.append('�')
            return ''.join(out), i + 1, True
        if c == '\\':
            if i + 1 >= n:
                break                                  # escape cut after the backslash
            e = s[i + 1]
            if e == 'u':
                h = s[i + 2:i + 6]
                if len(h) < 4:
                    if all(x in _HEX for x in h):
                        break                          # \uXX cut mid-escape
                    return ''.join(out), n, False      # invalid escape: stop here
                if not all(x in _HEX for x in h):
                    return ''.join(out), n, False
                cp, i = int(h, 16), i + 6
                if 0xD800 <= cp < 0xDC00:              # high half: wait for the low half
                    if high is not None:
                        out.append('�')
                    high = cp
                    continue
                if 0xDC00 <= cp < 0xE000:
                    if high is not None:
                        out.append(chr(0x10000 + ((high - 0xD800) << 10) + (cp - 0xDC00)))
                        high = None
                    else:
                        out.append('�')           # lone low half: display-safe replacement
                    continue
                ch = chr(cp)
            elif e in _ESCAPES:
                ch, i = _ESCAPES[e], i + 2
            else:
                return ''.join(out), n, False          # invalid escape: stop here
        else:
            ch, i = c, i + 1
        if high is not None:                           # a high half not followed by a low half
            out.append('�')
            high = None
        out.append(ch)
    return ''.join(out), n, False                      # a pending high half is dropped until its pair completes


def _scalar(s: str, i: int):
    """Number or literal: complete only when a delimiter follows it; otherwise dropped."""
    n, j = len(s), i
    while j < n and s[j] not in ',]}' and s[j] not in _WS:
        j += 1
    if j >= n:
        return _MISSING, n, False                      # may still be growing (12 -> 123, tr -> true)
    token = s[i:j]
    if token in ('true', 'false', 'null'):
        return {'true': True, 'false': False, 'null': None}[token], j, True
    try:
        return (float(token) if any(x in token for x in '.eE') else int(token)), j, True
    except ValueError:
        return _MISSING, n, False


def _array(s: str, i: int, depth: int):
    out, n = [], len(s)
    i = _ws(s, i + 1)
    if i < n and s[i] == ']':
        return out, i + 1, True
    while i < n:
        v, i, done = _value(s, i, depth + 1)
        if v is not _MISSING and (done or not isinstance(v, str)):   # array strings (ids) only when complete
            out.append(v)
        if not done:
            return out, n, False
        i = _ws(s, i)
        if i >= n:
            break
        if s[i] == ']':
            return out, i + 1, True
        if s[i] != ',':
            break
        i = _ws(s, i + 1)
    return out, n, False


def _object(s: str, i: int, depth: int):
    out, n = {}, len(s)
    i = _ws(s, i + 1)
    if i < n and s[i] == '}':
        return out, i + 1, True
    while i < n:
        if s[i] != '"':
            break
        key, i, done = _string(s, i)
        if not done:
            break                                      # incomplete key: dropped
        i = _ws(s, i)
        if i >= n or s[i] != ':':
            break
        i = _ws(s, i + 1)
        if i >= n:
            break                                      # value not started: key dropped
        v, i, done = _value(s, i, depth + 1)
        if v is not _MISSING:
            out[key] = v
        if not done:
            return out, n, False
        i = _ws(s, i)
        if i >= n:
            break
        if s[i] == '}':
            return out, i + 1, True
        if s[i] != ',':
            break
        i = _ws(s, i + 1)
    return out, n, False


def _value(s: str, i: int, depth: int = 0):
    i = _ws(s, i)
    if i >= len(s) or depth > MAX_DEPTH:
        return _MISSING, len(s), False
    c = s[i]
    if c == '"':
        return _string(s, i)
    if c == '{':
        return _object(s, i, depth)
    if c == '[':
        return _array(s, i, depth)
    return _scalar(s, i)


def parse_partial(text: str):
    """The value read from a JSON prefix (partial strings/arrays/objects kept), or None. Never raises."""
    try:
        v, _, _ = _value(text if isinstance(text, str) else '', 0)
    except Exception:   # defensive: a draft is display-only and must never fail generation
        return None
    return None if v is _MISSING else v


def _evidence(rows) -> list:
    out = []
    for row in rows:
        if not isinstance(row, dict) or ('claim' not in row and 'ids' not in row):
            continue                                   # an item counts once its claim or ids has started
        claim = row.get('claim')
        ids = row.get('ids')
        out.append({'claim': claim if isinstance(claim, str) else '',
                    'ids': [x for x in ids if isinstance(x, str)] if isinstance(ids, list) else []})
    return out


def draft_fields(text: str) -> dict:
    """Draft fields from the model's partial JSON text; {} until the first draft key's value has started."""
    obj = parse_partial(text)
    if not isinstance(obj, dict):
        return {}
    out = {}
    if isinstance(obj.get('evidence'), list):
        out['evidence'] = _evidence(obj['evidence'])
    for k in DRAFT_KEYS[1:]:
        if isinstance(obj.get(k), str):
            out[k] = obj[k]
    return out


class DraftThrottle:
    """feed(call, tokens, text) from the generator thread -> emit({call, tokens, fields}) at most once per interval
    per call. The first chunk of a new call always emits; a snapshot equal to the last one sent is skipped."""

    def __init__(self, emit: Callable[[dict], None], interval_s: float = DRAFT_INTERVAL_S,
                 clock: Callable[[], float] = time.monotonic):
        self._emit, self.interval_s, self._clock = emit, interval_s, clock
        self._lock = threading.Lock()
        self._call: Optional[int] = None
        self._last_t = 0.0
        self._last_fields: Optional[dict] = None
        self.sent = 0

    def feed(self, call: int, tokens: int, text: str) -> None:
        now = self._clock()
        with self._lock:
            new_call = call != self._call
            if not new_call and now - self._last_t < self.interval_s:
                return
            fields = draft_fields(text)
            if not new_call and fields == self._last_fields:
                return
            self._call, self._last_t, self._last_fields = call, now, fields
            self.sent += 1
        self._emit({'call': call, 'tokens': tokens, 'fields': fields})
