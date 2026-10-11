"""Claim-local mechanical grounding checks for six-field generation candidates.

check_assessment(candidate, prepared, catalog) validates the internal generation schema, packet-scoped
citation IDs and actions, then compares every claim only with the joined content of the evidence IDs
that claim cites (the answer is compared with the union of IDs the claims cite). Unrelated packet
evidence can never rescue a claim. Checked tokens:
  - signed decimal numbers, exact via Decimal, with a half-unit tolerance at the claim's displayed final
    decimal place (inclusive) for rounding only; sign, unit and percent context are retained;
  - ISO dates/datetimes and clock times;
  - asset/waypoint names and IDs from the kit catalog (casefolded whole-token text match);
  - identifier-shaped tokens (letters with digits, e.g. A0003, HX-201) — never treated as measurements;
  - spelled-out numbers (NUMBER_WORDS: zero-nineteen, tens, hundred/thousand/million, dozen, ordinals third
    onward) — no value is parsed; the word must appear verbatim in the cited content ('first', 'second' and
    'no' are not checked: their usual sense here is order, time unit or negation, not a count).
NaN/infinity are always rejected; any other digit-bearing form we cannot parse fails closed unless it
appears verbatim in the cited content.

These are MECHANICAL CONSISTENCY checks only. Passing does not establish that a claim is entailed by its
evidence, that the explanation is correct, or that a recommended action is safe for autonomous robot
operation. No arithmetic is performed or trusted. Errors are deterministic: top-level shape, then evidence
in claim order (ID order, then text position), then answer, then the remaining fields.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
import re
from typing import Iterable, List, Optional, Tuple
import unicodedata

from .packet import PreparedPacket
from .paths import display, kit_dir  # kit_dir: SPOT_KIT_DIR or the checked-in kit (shared with analyst.dataset)

GENERATION_FIELDS = ('evidence', 'answer', 'question', 'recommended_action', 'confidence', 'insufficient_context')
CONFIDENCE = ('high', 'medium', 'low')
VALUE_CHARS = 80  # stored offending-token length; model output is untrusted and never becomes evidence


# ── entity catalog ───────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Entity:
    kind: str
    key: str
    aliases: Tuple[str, ...]


@dataclass(frozen=True)
class EntityCatalog:
    entities: Tuple[Entity, ...]
    source: Optional[str] = None   # kit directory the catalog was read from (display form), for run identity


def _entity(kind, key, name) -> Entity:
    if kind not in ('asset', 'location') or not isinstance(key, str) or not key.strip():
        raise ValueError('invalid entity metadata %r' % ((kind, key),))
    if name is not None and not isinstance(name, str):
        raise ValueError('entity name must be a string or null')
    aliases = tuple(dict.fromkeys(a.strip() for a in (key, name or '') if a and a.strip()))
    return Entity(kind, key.strip(), aliases)


def load_entity_catalog(kit: Optional[Path] = None, extra: Iterable[dict] = ()) -> EntityCatalog:
    """Assets and waypoints from kit assets.csv/locations.csv plus supplied {kind, id, name} metadata."""
    kit = Path(kit) if kit is not None else kit_dir()
    found = {}
    try:
        for table, kind, key, name in (('assets', 'asset', 'asset_id', 'asset_name'),
                                       ('locations', 'location', 'location_id', 'location_name')):
            with (kit / (table + '.csv')).open(encoding='utf-8', newline='') as f:
                for r in csv.DictReader(f):
                    e = _entity(kind, r[key], r.get(name))
                    found[(e.kind, e.key)] = e
    except OSError as exc:
        raise ValueError('entity catalog source unavailable') from exc
    for m in extra:
        if not isinstance(m, dict) or set(m) - {'kind', 'id', 'name'}:
            raise ValueError('entity metadata must be {kind, id, name}')
        e = _entity(m.get('kind'), m.get('id'), m.get('name'))
        old = found.get((e.kind, e.key))
        found[(e.kind, e.key)] = Entity(e.kind, e.key, tuple(dict.fromkeys((old.aliases if old else ()) + e.aliases)))
    return EntityCatalog(tuple(found[k] for k in sorted(found)), display(kit))


@lru_cache(maxsize=1)
def default_catalog() -> EntityCatalog:
    return load_entity_catalog()


@lru_cache(maxsize=16)
def _alias_patterns(catalog: EntityCatalog):
    aliases = {a.casefold() for e in catalog.entities for a in e.aliases}
    return tuple(re.compile(r'(?<!\w)%s(?!\w)' % re.escape(a)) for a in sorted(aliases, key=lambda a: (-len(a), a)))


# ── errors ───────────────────────────────────────────────────────────────────────────────────

def _clip(value, n=VALUE_CHARS) -> str:
    s = value if isinstance(value, str) else repr(value)
    return ''.join('?' if unicodedata.category(ch).startswith('C') else ch for ch in s[:n])


@dataclass(frozen=True)
class GroundingError:
    code: str
    field: str
    claim: Optional[int]
    value: str

    def to_dict(self) -> dict:
        return {'code': self.code, 'field': self.field, 'claim': self.claim, 'value': self.value}


def _err(code, field, claim=None, value='') -> GroundingError:
    return GroundingError(code, field, claim, _clip(value))


HINTS = {
    'schema': 'field missing, extra or wrong type', 'empty_evidence': 'cite at least one evidence item',
    'empty_claim': 'claim text is blank', 'empty_ids': 'claim cites no evidence IDs',
    'unknown_id': 'ID is not in this packet (exact spelling)', 'duplicate_id': 'ID repeated within one claim',
    'empty_answer': 'answer is blank', 'question_mismatch': 'repeat the packet question exactly',
    'unauthorized_action': 'action not in this packet\'s allowed actions',
    'insufficient_mismatch': 'insufficient_context must match the packet',
    'insufficient_inconsistent': 'insufficient context needs no evidence, low confidence, MANUAL_REVIEW',
    'nonfinite_number': 'NaN/infinity are not allowed',
    'ungrounded_number': 'number not in the evidence this claim cites',
    'unit_mismatch': 'unit/percent differs from the cited evidence',
    'unsupported_number': 'unparseable or spelled-out number; copy it exactly or drop it',
    'ungrounded_time': 'date/time not in the cited evidence',
    'ungrounded_entity': 'asset/waypoint not named in the cited evidence',
    'ungrounded_identifier': 'identifier not in the cited evidence',
}


def describe_errors(errors: Iterable[GroundingError]) -> Tuple[str, ...]:
    """One sanitized single-line message per error, for the bounded retry prompt."""
    out = []
    for e in errors:
        where = 'evidence[%d]' % e.claim if e.claim is not None else e.field
        out.append(('%s: %s %r (%s)' % (where, e.code, e.value[:60], HINTS.get(e.code, '')))[:200])
    return tuple(out)


# ── token scanning ───────────────────────────────────────────────────────────────────────────

DATETIME_RE = re.compile(r'(?<![\w.:/-])(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?'
                         r'(?![\w:]|[.\-/]\d)')
TIME_RE = re.compile(r'(?<![\w.:/-])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\w:]|[.\-/]\d)')
NONFINITE_RE = re.compile(r'(?<!\w)(?:nan|inf|infinity)(?!\w)|∞', re.IGNORECASE)
IDENT_RE = re.compile(r'(?<![\w-])[^\W\d_]\w*(?:[-.]\w*\d\w*)*')
NUMBER_RE = re.compile(r'(?<![\w.])([-+−]?)((?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?|\.\d+)')
_WORDS = (r'percent|per\s?cent|pct|psi|kpa|bar|dba|db|decibels?|degrees?\s?(?:celsius|fahrenheit|c|f)|deg\s?[cf]'
          r'|celsius|fahrenheit|seconds?|secs?|minutes?|mins?|hours?|hrs?|rpm|khz|hz|volts?|amps?|kw|mm/s|mm')
UNIT_RE = re.compile(r'[  -]?(?:%%|°\s?[CF](?!\w)|[℃℉]|(?i:%s)(?!\w)|[CFVAWsmh](?!\w))' % _WORDS)
ORDINAL_RE = re.compile(r'(?:st|nd|rd|th)(?!\w)')
_UNITS_W = ('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen '
            'seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety '
            'hundred thousand million dozen')
_ORDINALS_W = ('third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth '
               'sixteenth seventeenth eighteenth nineteenth twentieth')
NUMBER_WORDS = frozenset(_UNITS_W.split()) | frozenset(_ORDINALS_W.split()) | frozenset(
    w + 's' for w in ('hundred', 'thousand', 'million', 'dozen'))
NUMBER_WORD_RE = re.compile(r'(?<!\w)(?:%s)(?!\w)' % '|'.join(sorted(NUMBER_WORDS, key=lambda w: (-len(w), w))),
                            re.IGNORECASE)
KEY_RE = re.compile(r'(\w+)(?:\s*=\s*|"\s*:\s*)$')
DECLARED_RE = re.compile(r'(?<!\w)unit(?:\s*=\s*|"\s*:\s*")([^;|,"\n]+)')
UNITS = {'%': '%', 'percent': '%', 'pct': '%', 'c': 'C', 'celsius': 'C', 'f': 'F', 'fahrenheit': 'F',
         'psi': 'psi', 'kpa': 'kPa', 'bar': 'bar', 'db': 'dB', 'dba': 'dB', 'decibel': 'dB', 'decibels': 'dB',
         's': 's', 'sec': 's', 'secs': 's', 'second': 's', 'seconds': 's', 'min': 'min', 'mins': 'min',
         'minute': 'min', 'minutes': 'min', 'h': 'h', 'hr': 'h', 'hrs': 'h', 'hour': 'h', 'hours': 'h',
         'rpm': 'rpm', 'hz': 'Hz', 'khz': 'kHz', '℃': 'C', '℉': 'F', 'v': 'V', 'volt': 'V', 'volts': 'V', 'a': 'A', 'amp': 'A', 'amps': 'A',
         'w': 'W', 'kw': 'kW', 'mm/s': 'mm/s', 'mm': 'mm', 'm': 'm'}
KEY_UNITS = {'pct': '%', 'percent': '%', 'sec': 's', 'min': 'min', 'psi': 'psi', 'db': 'dB'}


def _unit(raw: str) -> str:
    u = re.sub(r'[\s °-]+', '', raw).lower()
    for p in ('degrees', 'degree', 'deg'):
        if u.startswith(p) and len(u) > len(p):
            u = u[len(p):]
            break
    return UNITS.get(u, u)


def _mask(text: str, spans) -> str:
    chars = list(text)
    for a, b in spans:
        chars[a:b] = ' ' * (b - a)
    return ''.join(chars)


def _fold(text: str):
    """Casefolded text plus owner index back to the original character (casefold may change length)."""
    out, owner = [], []
    for i, ch in enumerate(text):
        f = ch.casefold()
        out.append(f)
        owner.extend([i] * len(f))
    return ''.join(out), owner


def _whole(token: str):
    return re.compile(r'(?<!\w)%s(?!\w)' % re.escape(token.casefold()))


def _chunk(text: str, i: int) -> Tuple[int, int]:
    a = i
    while a > 0 and not text[a - 1].isspace():
        a -= 1
    b = i
    while b < len(text) and not text[b].isspace():
        b += 1
    while b > a and text[b - 1] in '.,;:!?)]}"\'':
        b -= 1
    while a < b and text[a] in '([{"\'':
        a += 1
    return a, b


def _scan(text: str, catalog: Optional[EntityCatalog] = None) -> dict:
    """Positioned tokens: dates/times, nonfinite, entities (claims only), identifiers, numbers, unsupported."""
    tok = {'dt': [], 'nonfinite': [], 'entity': [], 'ident': [], 'number': [], 'unsupported': [], 'word': []}
    work = text
    spans = []
    for m in DATETIME_RE.finditer(work):
        time = (int(m.group(4)), int(m.group(5)), int(m.group(6)) if m.group(6) else None) if m.group(4) else None
        tok['dt'].append((m.start(), m.end(), m.group(), (int(m.group(1)), int(m.group(2)), int(m.group(3))), time))
        spans.append(m.span())
    work = _mask(work, spans)
    spans = []
    for m in TIME_RE.finditer(work):
        tok['dt'].append((m.start(), m.end(), m.group(), None,
                          (int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None)))
        spans.append(m.span())
    for m in NONFINITE_RE.finditer(work):
        tok['nonfinite'].append((m.start(), m.end(), m.group()))
        spans.append(m.span())
    work = _mask(work, spans)
    if catalog is not None:
        folded, owner = _fold(work)
        taken = []
        for pat in _alias_patterns(catalog):
            for m in pat.finditer(folded):
                if any(m.start() < b and a < m.end() for a, b in taken):
                    continue
                taken.append(m.span())
                a, b = owner[m.start()], owner[m.end() - 1] + 1
                tok['entity'].append((a, b, text[a:b]))
        work = _mask(work, [(a, b) for a, b, _ in tok['entity']])
    spans = []
    for m in IDENT_RE.finditer(work):
        if any(ch.isdigit() for ch in m.group()):
            tok['ident'].append((m.start(), m.end(), m.group()))
            spans.append(m.span())
    work = _mask(work, spans)
    spans = []
    for m in NUMBER_RE.finditer(work):
        end = m.end()
        if re.match(r'[eE][-+]?\d|\.\d', work[end:end + 3]):
            continue  # scientific notation / dotted sequences: left for the fail-closed residual check
        unit = None
        u = UNIT_RE.match(work, end)
        if u:
            unit, end = _unit(u.group()), u.end()
        else:
            o = ORDINAL_RE.match(work, end)
            end = o.end() if o else end
        sign = '-' if m.group(1) in ('-', '−') else ''
        digits = m.group(2).replace(',', '')
        value = Decimal(sign + ('0' + digits if digits.startswith('.') else digits))
        places = len(digits.split('.', 1)[1]) if '.' in digits else 0
        tok['number'].append((m.start(), end, text[m.start():end], value, places, unit))
        spans.append((m.start(), end))
    work = _mask(work, spans)
    last = -1
    for m in re.finditer(r'\d', work):
        if m.start() < last:
            continue  # same whitespace chunk: keeps the residual pass linear
        a, b = _chunk(text, m.start())
        last = max(b, m.start() + 1)
        tok['unsupported'].append((a, b, text[a:b]))
    for m in NUMBER_WORD_RE.finditer(work):   # after entity/identifier masking: names keep their own check
        tok['word'].append((m.start(), m.end(), m.group()))
    return tok


@lru_cache(maxsize=4096)
def _support_item(content: str):
    """Scanned cited content: folded text, numbers with unit sets, dates, times."""
    numbers, dts = [], []
    for segment in content.split(' | '):
        declared = {_unit(m.group(1)) for m in DECLARED_RE.finditer(segment)}
        tok = _scan(segment)
        for start, _, _, value, _, unit in tok['number']:
            units = set()
            if unit:
                units.add(unit)
            k = KEY_RE.search(segment[max(0, start - 80):start])
            if k and k.group(1).lower().rsplit('_', 1)[-1] in KEY_UNITS:
                units.add(KEY_UNITS[k.group(1).lower().rsplit('_', 1)[-1]])
            if not units:
                units |= declared
            numbers.append((value, frozenset(units)))
        dts.extend((d, t) for _, _, _, d, t in tok['dt'])
    return content.casefold(), tuple(numbers), tuple(dts)


def _time_ok(claim_t, support_t) -> bool:
    return (support_t is not None and claim_t[:2] == support_t[:2]
            and (claim_t[2] is None or claim_t[2] == support_t[2]))


def _mask_cited(text: str, cited_ids) -> str:
    """Blank exact, whole cited IDs written in the text (action:A0030, flag:14): a cited ID is already allowed
    as a reference, but a namespaced one would otherwise split into an identifier (A0030) or a number (14).
    Uncited IDs, partial IDs and every other token are still checked."""
    spans = [m.span() for i in cited_ids
             for m in re.finditer(r'(?<![\w:.-])%s(?![\w:-]|\.\d)' % re.escape(i), text)]
    return _mask(text, spans)


def _content_errors(text, support_ids, cited_ids, prepared, catalog, field, claim) -> List[GroundingError]:
    items = [_support_item(prepared.content[i]) for i in support_ids]
    folded = [f for f, _, _ in items]
    numbers = [n for _, ns, _ in items for n in ns]
    dts = [d for _, _, ds in items for d in ds]

    def present(token):
        pat = _whole(token)
        return any(pat.search(f) for f in folded)

    found = []
    tok = _scan(_mask_cited(text, cited_ids), catalog)
    for a, _, raw in tok['nonfinite']:
        found.append((a, 'nonfinite_number', raw))
    for a, _, raw, date, time in tok['dt']:
        if date is not None:
            ok = any(d == date and (time is None or _time_ok(time, t)) for d, t in dts)
        else:
            ok = any(_time_ok(time, t) for _, t in dts)
        if not ok:
            found.append((a, 'ungrounded_time', raw))
    for a, _, raw in tok['entity']:
        if not present(raw):
            found.append((a, 'ungrounded_entity', raw))
    for a, _, raw in tok['ident']:
        if raw not in cited_ids and not present(raw):
            found.append((a, 'ungrounded_identifier', raw))
    for a, _, raw, value, places, unit in tok['number']:
        tol = Decimal(5).scaleb(-(places + 1))
        near = [units for v, units in numbers if abs(v - value) <= tol]
        if not near:
            found.append((a, 'ungrounded_number', raw))
        elif unit is not None and not any(unit in units for units in near):
            found.append((a, 'unit_mismatch', raw))
    for a, _, raw in tok['unsupported'] + tok['word']:
        if not present(raw):
            found.append((a, 'unsupported_number', raw))
    found.sort(key=lambda f: (f[0], f[1]))
    return [_err(code, field, claim, raw) for _, code, raw in found]


# ── checker ──────────────────────────────────────────────────────────────────────────────────

def check_assessment(candidate, prepared: PreparedPacket,
                     entity_catalog: Optional[EntityCatalog] = None) -> Tuple[GroundingError, ...]:
    """() when mechanically consistent with the cited packet content; otherwise ordered errors."""
    catalog = entity_catalog if entity_catalog is not None else default_catalog()
    if not isinstance(candidate, dict):
        return (_err('schema', 'candidate', None, 'expected object'),)
    errors = [_err('schema', name, None, 'missing') for name in GENERATION_FIELDS if name not in candidate]
    errors += [_err('schema', 'candidate', None, 'extra field %s' % k)
               for k in sorted(str(k) for k in candidate if k not in GENERATION_FIELDS)]
    insufficient = candidate.get('insufficient_context')
    evidence = candidate.get('evidence')
    claims, support = [], []
    if 'evidence' in candidate:
        if not isinstance(evidence, list):
            errors.append(_err('schema', 'evidence', None, 'expected list'))
        else:
            if not evidence and insufficient is not True:
                errors.append(_err('empty_evidence', 'evidence'))
            for i, item in enumerate(evidence):
                if (not isinstance(item, dict) or set(item) != {'claim', 'ids'} or not isinstance(item['claim'], str)
                        or not isinstance(item['ids'], list) or any(not isinstance(x, str) for x in item['ids'])):
                    errors.append(_err('schema', 'evidence', i, 'expected {claim: string, ids: [string]}'))
                    continue
                if not item['claim'].strip():
                    errors.append(_err('empty_claim', 'evidence', i))
                if not item['ids']:
                    errors.append(_err('empty_ids', 'evidence', i))
                valid, seen = [], set()
                for x in item['ids']:
                    if x in seen:
                        errors.append(_err('duplicate_id', 'evidence', i, x))
                    elif x not in prepared.content:
                        errors.append(_err('unknown_id', 'evidence', i, x))
                    else:
                        valid.append(x)
                    seen.add(x)
                support.extend(valid)
                if valid and item['claim'].strip():
                    errors += _content_errors(item['claim'], sorted(valid), set(valid), prepared, catalog, 'evidence', i)
    answer = candidate.get('answer')
    if 'answer' in candidate:
        if not isinstance(answer, str):
            errors.append(_err('schema', 'answer', None, 'expected string'))
        elif not answer.strip():
            errors.append(_err('empty_answer', 'answer'))
        else:
            ids = sorted(set(support))
            errors += _content_errors(answer, ids, set(ids), prepared, catalog, 'answer', None)
    if 'question' in candidate:
        if not isinstance(candidate['question'], str):
            errors.append(_err('schema', 'question', None, 'expected string'))
        elif candidate['question'] != prepared.question:
            errors.append(_err('question_mismatch', 'question', None, candidate['question']))
    action = candidate.get('recommended_action')
    if 'recommended_action' in candidate:
        if not isinstance(action, str):
            errors.append(_err('schema', 'recommended_action', None, 'expected string'))
        elif action not in prepared.allowed_actions:
            errors.append(_err('unauthorized_action', 'recommended_action', None, action))
    confidence = candidate.get('confidence')
    if 'confidence' in candidate and not (type(confidence) is str and confidence in CONFIDENCE):
        errors.append(_err('schema', 'confidence', None, 'expected high|medium|low'))
    if 'insufficient_context' in candidate:
        if type(insufficient) is not bool:
            errors.append(_err('schema', 'insufficient_context', None, 'expected boolean'))
        else:
            if insufficient != prepared.insufficient_context:
                errors.append(_err('insufficient_mismatch', 'insufficient_context', None, str(insufficient).lower()))
            if insufficient:
                if evidence:
                    errors.append(_err('insufficient_inconsistent', 'evidence'))
                if confidence != 'low':
                    errors.append(_err('insufficient_inconsistent', 'confidence', None, confidence))
                if action != 'MANUAL_REVIEW':
                    errors.append(_err('insufficient_inconsistent', 'recommended_action', None, action))
    return tuple(errors)
