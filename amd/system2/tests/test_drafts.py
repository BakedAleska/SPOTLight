"""Partial model JSON -> draft fields (analyst.drafts): every prefix parses, never raises, grows monotonically and
ends equal to the complete JSON; the throttle sends at most one draft per interval per call."""
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyst.dataset import load_dataset  # noqa: E402
from analyst.drafts import DRAFT_KEYS, DraftThrottle, draft_fields, parse_partial  # noqa: E402
from analyst.prompts import example_output  # noqa: E402

DATA = ROOT / 'data' / 'dev_packets.json'


def expected(obj: dict) -> dict:
    """What the final prefix must equal: json.loads of the full text restricted to the draft keys."""
    out = {k: obj[k] for k in DRAFT_KEYS if k in obj}
    if 'evidence' in out:
        out['evidence'] = [{'claim': e['claim'], 'ids': e['ids']} for e in out['evidence']]
    return out


def realistic_texts():
    """Compact, spaced and pretty-printed outputs (llama.cpp's grammar allows all three), unicode and escapes."""
    cases = [c for c in load_dataset(DATA)][:6]
    objs = [example_output(c.prepared) for c in cases]
    objs.append({'evidence': [{'claim': 'PUMP-205 "B" vibration 7.1 mm/s\nabove the 7.0 limit \\ policy',
                               'ids': ['E7', 'DOC-3']},
                              {'claim': 'Temp 82 °C, Δ=+4 — 😀 ok\ttab   sep', 'ids': ['F2']}],
                 'answer': 'Dispatch a technician: “vibration” is over the limit (7.1 > 7.0) 🚨.',
                 'question': 'Why is "PUMP-205" flagged?', 'recommended_action': 'DISPATCH_TECHNICIAN',
                 'confidence': 'high', 'insufficient_context': False})
    objs.append({'evidence': [], 'answer': 'Insufficient supplied context; request manual review.',
                 'question': 'Why?', 'recommended_action': 'MANUAL_REVIEW', 'confidence': 'low',
                 'insufficient_context': True})
    texts = []
    for o in objs:
        texts.append((json.dumps(o, ensure_ascii=False), o))
        texts.append((json.dumps(o, ensure_ascii=True), o))                 # \uXXXX escapes and surrogate pairs
        texts.append((json.dumps(o, ensure_ascii=False, indent=2), o))
        texts.append((json.dumps(o, separators=(',', ':')), o))
    return texts


def _grows(before: dict, after: dict) -> None:
    for k, v in before.items():
        assert k in after, 'key %s disappeared' % k
        if k == 'evidence':
            assert len(after[k]) >= len(v)
            for a, b in zip(v, after[k]):
                assert b['claim'].startswith(a['claim'])
                assert b['ids'][:len(a['ids'])] == a['ids']
        else:
            assert after[k].startswith(v), (k, v, after[k])


@pytest.mark.parametrize('text,obj', realistic_texts())
def test_every_prefix_parses_grows_and_ends_equal_to_json(text, obj):
    previous = {}
    for k in range(len(text) + 1):
        fields = draft_fields(text[:k])
        assert isinstance(fields, dict)
        assert set(fields) <= set(DRAFT_KEYS)
        assert 'question' not in fields and 'insufficient_context' not in fields
        for e in fields.get('evidence', []):
            assert set(e) == {'claim', 'ids'} and isinstance(e['claim'], str)
            assert all(i in {x for row in obj['evidence'] for x in row['ids']} for i in e['ids'])   # complete IDs only
        _grows(previous, fields)
        json.dumps(fields, ensure_ascii=False).encode('utf-8')   # always valid UTF-8 for the SSE line
        previous = fields
    assert draft_fields(text) == expected(obj)


def test_keys_appear_only_once_their_value_has_started():
    assert draft_fields('') == {} and draft_fields('{') == {}
    assert draft_fields('{"evidence"') == {} and draft_fields('{"evidence": ') == {}
    assert draft_fields('{"evidence": [') == {'evidence': []}
    assert draft_fields('{"evidence": [{"cl') == {'evidence': []}
    assert draft_fields('{"evidence": [{"claim": "') == {'evidence': [{'claim': '', 'ids': []}]}
    assert draft_fields('{"evidence": [{"claim": "Vib 7", "ids": ["E7", "DO') == {
        'evidence': [{'claim': 'Vib 7', 'ids': ['E7']}]}
    assert draft_fields('{"evidence": [], "answer": "Dis') == {'evidence': [], 'answer': 'Dis'}
    assert draft_fields('{"evidence": [], "answer": "a", "question": "Why') == {'evidence': [], 'answer': 'a'}
    assert draft_fields('{"evidence": [], "answer": "a", "recommended_action": "SCHED') == {
        'evidence': [], 'answer': 'a', 'recommended_action': 'SCHED'}


@pytest.mark.parametrize('prefix,answer', [
    ('{"answer": "a\\', 'a'),
    ('{"answer": "a\\u00', 'a'),
    ('{"answer": "a\\u00e9', 'aé'),
    ('{"answer": "a\\ud83d', 'a'),                 # high half waits for its low half
    ('{"answer": "a\\ud83d\\ude', 'a'),
    ('{"answer": "a\\ud83d\\ude00', 'a😀'),
    ('{"answer": "a\\n\\"b\\\\', 'a\n"b\\'),
    ('{"answer": "a\\ud83dx', 'a�x'),         # a lone half becomes a display-safe replacement
])
def test_escapes_cut_mid_way_are_dropped_until_complete(prefix, answer):
    assert draft_fields(prefix)['answer'] == answer


@pytest.mark.parametrize('text', [
    '[' * 50000, '{"evidence": ' + '[' * 50000, '{"a": tru', '{"a": 12', '{"a": -', 'null', '"str', '42', ']]]',
    '{"answer": "x\\q"', '{"answer": "x\\uZZZZ"', '{,}', '{"a" "b"}', '{"evidence": [1, "x", {"claim": 3}]}',
    '{"evidence": {"claim": "x"}}', '{"answer": 5, "confidence": ["high"]}', '\x00\xff\ud800', None, b'{"a"', 17,
])
def test_garbage_never_raises(text):
    fields = draft_fields(text)
    assert isinstance(fields, dict) and set(fields) <= set(DRAFT_KEYS)


def test_random_mutations_never_raise():
    rng = random.Random(7)
    base = realistic_texts()[6][0]
    alphabet = '{}[]",:\\u0123456789abcdefntrue -.\n'
    for _ in range(400):
        chars = list(base[:rng.randrange(len(base))])
        for _ in range(rng.randrange(1, 6)):
            if chars:
                chars[rng.randrange(len(chars))] = rng.choice(alphabet)
        draft_fields(''.join(chars))
        parse_partial(''.join(chars))


def test_duplicate_or_misplaced_keys_are_tolerated():
    assert draft_fields('{"answer": "a", "answer": "b') == {'answer': 'b'}
    assert draft_fields('{"confidence": "hi') == {'confidence': 'hi'}


# ── throttle ─────────────────────────────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_throttle_sends_first_draft_of_each_call_and_at_most_one_per_interval():
    sent, clock = [], Clock()
    th = DraftThrottle(sent.append, interval_s=0.1, clock=clock)
    th.feed(1, 1, '{"evidence": [{"claim": "A')
    clock.t += 0.03
    th.feed(1, 2, '{"evidence": [{"claim": "AB')      # inside the interval: skipped
    clock.t += 0.08
    th.feed(1, 3, '{"evidence": [{"claim": "ABC')     # 0.11 s later: sent
    clock.t += 0.01
    th.feed(2, 1, '{')                                # a new call is never throttled
    clock.t += 0.2
    th.feed(2, 2, '{"evid')                           # fields unchanged ({}): skipped
    clock.t += 0.2
    th.feed(2, 3, '{"evidence": [')
    assert [(d['call'], d['tokens']) for d in sent] == [(1, 1), (1, 3), (2, 1), (2, 3)]
    assert sent[1]['fields'] == {'evidence': [{'claim': 'ABC', 'ids': []}]}
    assert sent[2]['fields'] == {} and sent[3]['fields'] == {'evidence': []}
    assert th.sent == 4
