"""Contract 2026-10-10.backend.2: context.vision (V# evidence), context.ocr (untrusted text), context.system1
mapping, top-level image_b64 (JPEG/PNG, 2 MiB), the multimodal prompt (image first, question last), the text-only
resend when the server rejects images, and byte-identical prompts for every backend.1 request.

Transport tests use a real ThreadingHTTPServer on 127.0.0.1; no model, GPU or llama.cpp process is involved.
"""
import base64
import copy
import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
S2 = ROOT / 'amd/system2'
sys.path.insert(0, str(S2))

from analyst import api, generation as g, packet as pk, prompts as pr  # noqa: E402
from analyst.backend_contract import BACKEND_CONTRACT_VERSION, BackendAssessRequest  # noqa: E402
from analyst.dataset import load_dataset  # noqa: E402
from analyst.evaluation import packet_hash  # noqa: E402
from analyst.grounding import check_assessment  # noqa: E402
from analyst.live import IMAGE_DEADLINE_S, ExplanationCache, LiveAssessor  # noqa: E402
from analyst.pipeline import Generation, GenerationError, PipelineConfig, run_pipeline  # noqa: E402
from analyst.template import template_assessment  # noqa: E402

KEY = 'multimodal-test-key-0123456789'
FIXTURE = S2 / 'fixtures/multimodal/gauge_disagree.json'
GOLDEN = Path(__file__).resolve().parent / 'golden/prompts_pre_multimodal.json'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 32
TINY_PNG_B64 = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def mm_request(**changes):
    req = copy.deepcopy(fixture()['request'])
    req.update(changes)
    return req


def backend_request(name='data_quality'):
    return copy.deepcopy(json.loads((S2 / ('fixtures/backend/%s.json' % name)).read_text())['request'])


def prepare(req):
    return pk.normalize_packet(BackendAssessRequest.model_validate(req))


def with_context(**ctx):
    req = backend_request()
    req['context'].update(ctx)
    return req


def v_row(**kw):
    row = {'id': 'V1', 'tool': 'gauge_needle', 'kind': 'gauge', 'quantity': 'pressure', 'value': 121.0,
           'unit': 'psi', 'confidence': 0.88, 'trust': 'HIGH', 'logged_id': 'reading:R0053',
           'logged_value': 114.2, 'delta': 6.8, 'agrees': False, 'pairing': 'linked'}
    row.update(kw)
    return row


# ── contract version and request model ───────────────────────────────────────────────────────

def test_contract_version_is_backend_2():
    assert BACKEND_CONTRACT_VERSION == '2026-10-10.backend.2'


def test_request_schema_publishes_optional_image_b64():
    schema = json.loads((S2 / 'schema/assess_request.schema.json').read_text())
    assert 'image_b64' in schema['properties'] and 'image_b64' not in schema.get('required', [])
    assert 'backend.2' in schema['$comment']


def test_image_b64_must_be_a_string():
    with pytest.raises(ValidationError):
        BackendAssessRequest.model_validate(dict(backend_request(), image_b64=123))


# ── vision rows ──────────────────────────────────────────────────────────────────────────────

def test_vision_rows_become_citable_v_evidence_with_canonical_content():
    p = prepare(with_context(vision=[v_row()]))
    assert p.vision_ids == ('V1',)
    assert json.loads(p.content['V1']) == {k: v for k, v in v_row().items() if k != 'id'}
    assert p.content['V1'] == json.dumps(json.loads(p.content['V1']), sort_keys=True, ensure_ascii=False)
    assert 'V1' in g.response_schema(p)['properties']['evidence']['items']['properties']['ids']['items']['enum']
    assert p.multimodal


def test_low_trust_rows_carry_no_value_delta_or_agrees():
    p = prepare(with_context(vision=[v_row(trust='LOW')]))
    content = json.loads(p.content['V1'])
    assert content['trust'] == 'LOW' and content['logged_value'] == 114.2
    assert not {'value', 'delta', 'agrees'} & set(content)
    claim = {'claim': 'V1 reads 121.0 psi', 'ids': ['V1']}
    errs = check_assessment(dict(template_assessment(p), evidence=[claim], answer='V1 reads 121.0 psi'), p)
    assert any(e.code == 'ungrounded_number' for e in errs)


def test_vision_text_fields_are_cleaned_and_capped():
    p = prepare(with_context(vision=[v_row(tool='gauge\x00‮nee\tdle' + 'x' * 100, value=None)]))
    tool = json.loads(p.content['V1'])['tool']
    assert tool.startswith('gauge nee dle') and len(tool) == pk.VISION_TEXT_CHARS
    assert all(ord(ch) >= 32 for ch in tool)


@pytest.mark.parametrize('bad', [
    {'vision': {'V1': {}}},
    {'vision': ['V1']},
    {'vision': [v_row(id='X1')]},
    {'vision': [v_row(id='V0')]},
    {'vision': [v_row(id='v1')]},
    {'vision': [v_row(tool=None)]},
    {'vision': [v_row(tool='   ')]},
    {'vision': [v_row(trust=None)]},
    {'vision': [v_row(trust='MEDIUM')]},
    {'vision': [v_row(value='121')]},
    {'vision': [v_row(value=float('nan'))]},
    {'vision': [v_row(delta=True)]},
    {'vision': [v_row(confidence=1.5)]},
    {'vision': [v_row(agrees='false')]},
    {'vision': [v_row(pairing='other')]},
    {'vision': [v_row(unit=5)]},
    {'vision': [v_row(), v_row(value=99.0)]},
    {'vision': [v_row(id='V%d' % i) for i in range(1, pk.MAX_VISION_ROWS + 2)]},
])
def test_malformed_vision_rows_are_rejected(bad):
    with pytest.raises(ValueError):
        prepare(with_context(**bad))


def test_vision_id_colliding_with_other_evidence_is_rejected():
    req = with_context(vision=[v_row()])
    req['context']['facts'].append({'id': 'V1', 'fact': 'something else'})
    with pytest.raises(ValueError, match='duplicate id V1'):
        prepare(req)


def test_unknown_vision_keys_are_ignored():
    p = prepare(with_context(vision=[v_row(extra_debug={'x': 1}, bbox=[1, 2, 3, 4])]))
    assert 'extra_debug' not in p.content['V1'] and 'bbox' not in p.content['V1']


# ── actions ──────────────────────────────────────────────────────────────────────────────────

def test_disagreement_removes_all_fine_actions_and_keeps_codes_known():
    p = prepare(with_context(vision=[v_row()]))
    assert 'MONITOR' not in p.allowed_actions and 'NO_ACTION' not in p.allowed_actions
    assert {'MANUAL_READING', 'CHECK_SENSOR_PAYLOAD', 'MANUAL_REVIEW'} <= set(p.allowed_actions)
    assert set(p.allowed_actions) <= set(prepare(backend_request()).allowed_actions)
    assert g.response_schema(p)['properties']['recommended_action']['enum'] == list(p.allowed_actions)


def test_disagreement_replaces_a_blocked_suggested_action():
    p = prepare(with_context(vision=[v_row()], suggested_action='MONITOR'))
    assert p.suggested_action == 'MANUAL_READING'
    req = with_context(vision=[v_row()], suggested_action='MONITOR',
                       allowed_actions={'MONITOR': 'watch', 'REINSPECT_NOW': 'again'})
    p = prepare(req)
    assert p.suggested_action is None and p.allowed_actions == ('REINSPECT_NOW', 'MANUAL_REVIEW')
    assert not p.insufficient_context


@pytest.mark.parametrize('row', [v_row(agrees=True), v_row(agrees=None), v_row(trust='LOW')])
def test_agreeing_unknown_or_low_trust_rows_keep_actions(row):
    assert prepare(with_context(vision=[row])).allowed_actions == prepare(backend_request()).allowed_actions


def test_only_blocked_actions_still_answers_with_manual_review():
    p = prepare(with_context(vision=[v_row()], suggested_action=None, allowed_actions=['MONITOR']))
    assert p.allowed_actions == ('MANUAL_REVIEW',) and not p.insufficient_context


# ── OCR ──────────────────────────────────────────────────────────────────────────────────────

def test_ocr_lines_are_cleaned_capped_and_never_evidence():
    lines = [{'text': '  PSI\x07\n ', 'conf': 0.9}, {'text': '   ', 'conf': None}, {'text': 'y' * 200, 'conf': 0.5}]
    lines += [{'text': 'line %d' % i, 'conf': None} for i in range(60)]
    p = prepare(with_context(ocr=lines))
    assert p.ocr[0] == 'PSI' and p.ocr[1] == 'y' * pk.OCR_LINE_CHARS
    assert len(p.ocr) == pk.MAX_OCR_LINES
    assert sum(len(x) for x in p.ocr) <= pk.OCR_TOTAL_CHARS
    assert not any(line in p.content.values() for line in p.ocr)
    assert p.content == prepare(backend_request()).content


def test_ocr_total_cap():
    p = prepare(with_context(ocr=[{'text': 'z' * 80, 'conf': None}] * 30))
    assert sum(len(x) for x in p.ocr) == pk.OCR_TOTAL_CHARS and len(p.ocr) == 15


@pytest.mark.parametrize('bad', [{'ocr': 'PSI'}, {'ocr': ['PSI']}, {'ocr': [{'text': 5}]},
                                 {'ocr': [{'text': 'a', 'conf': 2}]}, {'ocr': [{'text': 'a', 'conf': 'high'}]}])
def test_malformed_ocr_is_rejected(bad):
    with pytest.raises(ValueError):
        prepare(with_context(**bad))


def test_number_seen_only_in_ocr_is_not_citable():
    p = prepare(with_context(vision=[v_row()], ocr=[{'text': 'PI-114 reads 130 psi', 'conf': 0.9}]))
    claim = 'V1 reads 130 psi'
    errs = check_assessment(dict(template_assessment(p), evidence=[{'claim': claim, 'ids': ['V1']}], answer=claim), p)
    assert [e.code for e in errs if e.field == 'evidence'] == ['ungrounded_number']


# ── system1 rows ─────────────────────────────────────────────────────────────────────────────

def test_system1_mcp_rows_render_question_answer_probability():
    p = prepare(mm_request())
    assert json.loads(p.content['s1:5502']) == {'answer': 'B', 'meaning': 'no, the reading should not be trusted',
                                                'p': 0.71, 'q': 'reading_trust', 'when': 'action'}


@pytest.mark.parametrize('row,p_value', [
    ({'question': 'reading_trust', 'answer': 'B', 'probability': 0.71}, 0.71),
    ({'question': 'reading_trust', 'answer': 'B', 'confidence': 0.66}, 0.66),
    ({'question': 'reading_trust', 'answer': 'B', 'probs': {'A': 0.2, 'B': 0.8}}, 0.8),
])
def test_system1_laya_native_rows_are_mapped(row, p_value):
    p = prepare(with_context(system1=[dict(row, id='s1:9')]))
    content = json.loads(p.content['s1:9'])
    assert content['q'] == 'reading_trust' and content['answer'] == 'B' and content['p'] == p_value


# ── image ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw,mime', [(b64(PNG), 'image/png'), (b64(JPEG), 'image/jpeg'),
                                      ('data:image/png;base64,' + b64(PNG), 'image/png'),
                                      ('data:image/jpeg;base64,' + b64(JPEG), 'image/jpeg'),
                                      ('data:image/jpg;base64,' + b64(JPEG), 'image/jpeg'),
                                      ('  ' + TINY_PNG_B64 + '\n', 'image/png')])
def test_valid_images_parse(raw, mime):
    img = pk.parse_image(raw)
    data = base64.b64decode(img.b64)
    assert img.mime == mime and img.size == len(data) and img.sha256 == hashlib.sha256(data).hexdigest()
    assert img.data_url.startswith('data:%s;base64,' % mime)
    assert len(repr(img)) < 80


@pytest.mark.parametrize('raw', [None, ''])
def test_absent_image(raw):
    assert pk.parse_image(raw) is None


@pytest.mark.parametrize('raw,why', [
    (b64(b'GIF89a' + b'\x00' * 20), 'signature'),
    (b64(b'%PDF-1.7' + b'\x00' * 20), 'signature'),
    ('not base64!!', 'base64'),
    (b64(PNG)[:-2] + '!!', 'base64'),
    ('data:image/gif;base64,' + b64(PNG), 'data URL'),
    ('data:text/plain;base64,' + b64(PNG), 'data URL'),
    ('data:image/jpeg;base64,' + b64(PNG), 'does not match'),
    (b64(PNG[:4]), 'signature'),
])
def test_bad_images_are_rejected(raw, why):
    with pytest.raises(ValueError, match=why):
        pk.parse_image(raw)


def test_image_size_limit_is_2_mib_decoded():
    at_limit = PNG + b'\x00' * (pk.MAX_IMAGE_BYTES - len(PNG))
    assert pk.parse_image(b64(at_limit)).size == pk.MAX_IMAGE_BYTES
    with pytest.raises(ValueError, match='more than'):
        pk.parse_image(b64(at_limit + b'\x00'))
    with pytest.raises(ValueError, match='more than'):
        pk.parse_image(b64(at_limit + b'\x00' * 4096))


def test_image_is_never_evidence():
    p = prepare(mm_request())
    assert p.image is not None and all(p.image.b64 not in v for v in p.content.values())
    assert 'image' not in p.content


# ── HTTP (stub mode) ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(api.create_app(api_key=KEY))


def post(client, body):
    return client.post('/v1/assess', json=body, headers={'X-API-Key': KEY})


@pytest.mark.parametrize('image', [b64(b'GIF89a' + b'\x00' * 8), 'zz', 17,
                                   b64(PNG + b'\x00' * pk.MAX_IMAGE_BYTES)])
def test_http_bad_image_is_422(client, image):
    assert post(client, mm_request(image_b64=image)).status_code == 422


@pytest.mark.parametrize('ctx', [{'vision': [v_row(id='W1')]}, {'ocr': 'text'}, {'vision': [v_row(trust='x')]}])
def test_http_bad_multimodal_context_is_422(client, ctx):
    assert post(client, with_context(**ctx)).status_code == 422


def test_http_stub_accepts_multimodal_and_marks_image_not_used(client):
    r = post(client, mm_request())
    assert r.status_code == 200 and r.headers['X-System2-Image'] == 'not_used'
    body = r.json()
    assert body['path'] == 'stub' and set(body) == set(post(client, backend_request()).json())


def test_http_stub_without_image_has_no_image_header(client):
    r = post(client, with_context(vision=[v_row()]))
    assert r.status_code == 200 and 'X-System2-Image' not in r.headers


def test_http_vision_only_packet_cites_v_ids(client):
    req = backend_request()
    ctx = req['context']
    for k in ('facts', 'policy', 'event', 'flag', 'citation_ids'):
        ctx.pop(k, None)
    ctx['vision'] = [v_row()]
    body = post(client, req).json()
    assert body['path'] == 'stub' and body['evidence'][0]['ids'] == ['V1']


# ── template / fallback ignore the new inputs gracefully ─────────────────────────────────────

def test_template_and_fallback_with_multimodal_packet():
    def failing(packet, errors=(), timeout_s=None):
        raise GenerationError('http', 'status 500')
    run = run_pipeline(mm_request(), failing, PipelineConfig())
    assert run.path == 'fallback' and run.model == 'template' and run.image_used is None
    assert not check_assessment(run.assessment, prepare(mm_request()))


# ── grounding: numbers must cite the V#/R# row holding them ──────────────────────────────────

def test_reply_numbers_must_cite_the_v_row():
    p = prepare(mm_request())
    reply = fixture()['reply']
    assert check_assessment(reply, p) == ()
    g.validate_generated(reply, p)
    moved = copy.deepcopy(reply)
    moved['evidence'][0]['ids'] = ['F2']
    codes = {e.code for e in check_assessment(moved, p) if e.field == 'evidence' and e.claim == 0}
    assert 'ungrounded_number' in codes


def test_units_khz_and_degree_signs():
    rows = [v_row(id='V1', kind='acoustic', quantity='peak_freq', value=12.5, unit='kHz', logged_id=None,
                  logged_value=None, delta=None, agrees=None),
            v_row(id='V2', kind='thermal', quantity='max_temp', value=84.0, unit='°C', logged_id=None,
                  logged_value=71.3, delta=12.7, agrees=False)]
    p = prepare(with_context(vision=rows))

    def errs(claim, ids):
        return [e.code for e in check_assessment(
            dict(template_assessment(p), evidence=[{'claim': claim, 'ids': ids}], answer=claim), p)
            if e.field == 'evidence']
    assert errs('V1 peak at 12.5 kHz', ['V1']) == []
    assert errs('V1 peak at 12.5 Hz', ['V1']) == ['unit_mismatch']
    assert errs('V2 hotspot 84.0 °C, logged 71.3 °C, delta 12.7 °C', ['V2']) == []
    assert errs('V2 hotspot 84℃', ['V2']) == []
    assert errs('V2 hotspot 84.0 °F', ['V2']) == ['unit_mismatch']


# ── prompt format ────────────────────────────────────────────────────────────────────────────

def examples():
    return pr.load_examples()


def test_image_message_is_image_first_then_packet_with_question_last():
    p = prepare(mm_request())
    chosen = pr.select_examples(examples(), p)
    msgs = pr.build_messages(p, chosen)
    assert msgs[0] == {'role': 'system', 'content': pr.SYSTEM + ' ' + pr.VISION_RULES}
    assert all(isinstance(m['content'], str) for m in msgs[:-1])
    content = msgs[-1]['content']
    assert [part['type'] for part in content] == ['image_url', 'text']
    assert content[0]['image_url']['url'] == p.image.data_url
    text = content[1]['text']
    view = json.loads(text[text.index('{'):])
    assert list(view) == ['evidence', 'allowed_actions', 'suggested_action', 'ocr_untrusted', 'image', 'question']
    assert view['image'] == pr.IMAGE_NOTE and view['ocr_untrusted'] == list(p.ocr)


def test_text_only_variant_drops_image_part_and_note_but_keeps_rules():
    p = prepare(mm_request())
    msgs = pr.build_messages(p, pr.select_examples(examples(), p), include_image=False)
    assert isinstance(msgs[-1]['content'], str) and 'image' not in json.loads(
        msgs[-1]['content'][msgs[-1]['content'].index('{'):])
    assert msgs[0]['content'].endswith(pr.VISION_RULES)


def test_vision_without_image_adds_rules_but_no_content_array():
    p = prepare(with_context(vision=[v_row()]))
    msgs = pr.build_messages(p, pr.select_examples(examples(), p))
    assert msgs[0]['content'].endswith(pr.VISION_RULES) and isinstance(msgs[-1]['content'], str)


def test_retry_errors_follow_the_packet_in_the_text_part():
    p = prepare(mm_request())
    msgs = pr.build_messages(p, pr.select_examples(examples(), p), ('evidence[0]: ungrounded_number',))
    assert msgs[-1]['content'][0]['type'] == 'image_url'
    assert msgs[-1]['content'][1]['text'].endswith('- evidence[0]: ungrounded_number')


def test_vision_rules_text_is_the_documented_text():
    doc = (S2 / 'PROMPT_FORMAT.md').read_text(encoding='utf-8')
    assert pr.VISION_RULES in doc and pr.SYSTEM in doc and pr.IMAGE_NOTE in doc
    assert pr.PROMPT_VERSION == 'analyst-prompt-2026-10-10.5' and pr.PROMPT_VERSION in doc


@pytest.mark.parametrize('name', ['gauge_disagree', 'gauge_demo'])
def test_committed_prompt_example_matches_code(name):
    sys.path.insert(0, str(S2 / 'scripts'))
    import prompt_example
    fx = S2 / ('fixtures/multimodal/%s.json' % name)
    committed = prompt_example.output_path(fx).read_text(encoding='utf-8')
    assert committed == prompt_example.dumps(prompt_example.render(fx)), \
        'run: python scripts/prompt_example.py --fixture %s --write' % fx.relative_to(S2)


# ── backward compatibility: backend.1 prompts are byte-identical ─────────────────────────────

def golden_cases():
    out = []
    for p in sorted((S2 / 'fixtures/backend').glob('*.json')):
        out.append(('fixtures/backend/' + p.name, prepare(json.loads(p.read_text())['request'])))
    req = backend_request('equipment')
    req['context']['system1'] = [{'id': 's1:101', 'when': 'action', 'q': 'escalate', 'answer': 'A',
                                  'meaning': 'escalate to an operator', 'p': 0.83},
                                 {'id': 's1:102', 'when': 'action', 'q': 'priority', 'answer': 'P2', 'p': 0.7}]
    req['image_note'] = 'unknown top-level keys are ignored'
    out.append(('fixtures/backend/equipment.json+system1', prepare(req)))
    for c in load_dataset(S2 / 'data/dev_packets.json'):
        out.append(('data/dev_packets.json#' + c.case_id, c.prepared))
    return out


def test_backend_1_requests_produce_byte_identical_prompts_and_hashes():
    golden = json.loads(GOLDEN.read_text())
    ex = examples()
    seen = set()
    for name, prepared in golden_cases():
        assert not prepared.multimodal
        try:
            chosen = pr.select_examples(ex, prepared, pr.FEWSHOT_COUNT)
        except ValueError:
            continue
        for errs in ((), ("evidence[0]: ungrounded_number '99' (number not in the evidence this claim cites)",)):
            key = name + (':retry' if errs else '')
            body = g.request_body(prepared, chosen, errs, 'qwen3.5-9b-q4km', 350)
            raw = json.dumps(body, ensure_ascii=False, sort_keys=False).encode('utf-8')
            assert hashlib.sha256(raw).hexdigest() == golden[key]['request_body_sha256'], key
            assert packet_hash(prepared) == golden[key]['packet_hash'], key
            seen.add(key)
    assert seen == set(golden)


def test_packet_hash_covers_ocr_and_image_only_when_present():
    base = prepare(with_context(vision=[v_row()]))
    with_ocr = prepare(with_context(vision=[v_row()], ocr=[{'text': 'PSI', 'conf': 0.9}]))
    with_img = prepare(dict(with_context(vision=[v_row()]), image_b64=b64(PNG)))
    other_img = prepare(dict(with_context(vision=[v_row()]), image_b64=b64(JPEG)))
    assert len({packet_hash(base), packet_hash(with_ocr), packet_hash(with_img), packet_hash(other_img)}) == 4


# ── generation over HTTP: model choice, content array, text-only resend ──────────────────────

def envelope(obj, model='qwen-fake'):
    return {'model': model, 'choices': [{'index': 0, 'finish_reason': 'stop',
                                         'message': {'role': 'assistant', 'content': json.dumps(obj)}}],
            'usage': {'prompt_tokens': 900, 'completion_tokens': 120},
            'timings': {'predicted_per_second': 30.0}}


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            self.server.requests.append({'headers': dict(self.headers), 'body': json.loads(raw)})
            step = self.server.script.pop(0) if len(self.server.script) > 1 else self.server.script[0]
            time.sleep(step.get('delay', 0))
            data = step['body'] if isinstance(step['body'], bytes) else json.dumps(step['body']).encode('utf-8')
            self.send_response(step.get('status', 200))
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):   # a client that gave up (deadline) closed the socket
            pass

    http = Server(('127.0.0.1', 0), Handler)
    http.daemon_threads = True
    http.requests, http.script = [], []
    worker = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    worker.start()
    yield http, 'http://127.0.0.1:%d' % http.server_port
    http.shutdown()
    http.server_close()
    worker.join()


NO_MMPROJ = {'error': {'code': 500, 'message': 'image input is not supported - hint: if this is unexpected, '
                                                  'you may need to provide the mmproj', 'type': 'server_error'}}


def is_image_body(body):
    return isinstance(body['messages'][-1]['content'], list)


def test_image_request_uses_vision_model_and_content_array(server):
    http, url = server
    http.script[:] = [{'body': envelope(fixture()['reply'])}]
    gen = g.LlamaGenerator(url, model='system2', vision_model='system2-vision', api_key='sk-gateway-test-key-1')
    run = run_pipeline(mm_request(), gen, PipelineConfig(deadline_s=IMAGE_DEADLINE_S))
    assert run.path == 'llm' and run.image_used is True and run.attempts[0].to_dict()['image_used'] is True
    sent = http.requests[0]
    assert sent['body']['model'] == 'system2-vision' and is_image_body(sent['body'])
    assert sent['headers']['Authorization'] == 'Bearer sk-gateway-test-key-1'
    assert sent['body']['response_format']['type'] == 'json_schema'


def test_text_request_never_sends_image_parts_and_uses_text_model(server):
    http, url = server
    req = backend_request()
    p = prepare(req)
    http.script[:] = [{'body': envelope(pr.example_output(p))}]
    run = run_pipeline(req, g.LlamaGenerator(url, model='system2', vision_model='system2-vision'),
                       PipelineConfig(deadline_s=IMAGE_DEADLINE_S))
    assert run.path == 'llm' and run.image_used is None and 'image_used' not in run.attempts[0].to_dict()
    assert http.requests[0]['body']['model'] == 'system2' and not is_image_body(http.requests[0]['body'])


@pytest.mark.parametrize('status,body,vision_model', [
    (500, NO_MMPROJ, None),
    (400, {'error': {'message': 'this model does not support multimodal input'}}, None),
    (400, {'error': {'message': 'Invalid model name passed in model=system2-vision'}}, 'system2-vision'),
    (404, {'error': {'message': 'model not found'}}, 'system2-vision'),
])
def test_image_rejection_resends_once_text_only(server, status, body, vision_model):
    http, url = server
    http.script[:] = [{'status': status, 'body': body}, {'body': envelope(fixture()['reply'])}]
    gen = g.LlamaGenerator(url, model='system2', vision_model=vision_model)
    run = run_pipeline(mm_request(), gen, PipelineConfig(deadline_s=IMAGE_DEADLINE_S))
    assert run.path == 'llm' and run.image_used is False
    assert len(http.requests) == 2
    first, second = http.requests[0]['body'], http.requests[1]['body']
    assert is_image_body(first) and not is_image_body(second)
    assert second['model'] == 'system2'
    text = second['messages'][-1]['content']
    assert '"image"' not in text[text.index('{'):] and second['messages'][0]['content'].endswith(pr.VISION_RULES)


@pytest.mark.parametrize('status,body', [(500, {'error': {'message': 'out of memory'}}), (401, NO_MMPROJ),
                                         (503, NO_MMPROJ)])
def test_other_http_errors_are_not_image_rejections(server, status, body):
    http, url = server
    http.script[:] = [{'status': status, 'body': body}]
    run = run_pipeline(mm_request(), g.LlamaGenerator(url, model='system2'),
                       PipelineConfig(deadline_s=IMAGE_DEADLINE_S))
    assert run.path == 'fallback' and len(http.requests) == 2      # two attempts, no text-only resend
    assert all(is_image_body(r['body']) for r in http.requests)


def test_text_only_resend_failing_counts_as_one_attempt(server):
    http, url = server
    http.script[:] = [{'status': 500, 'body': NO_MMPROJ}, {'status': 500, 'body': {'error': 'boom'}},
                      {'status': 500, 'body': NO_MMPROJ}, {'status': 500, 'body': {'error': 'boom'}}]
    run = run_pipeline(mm_request(), g.LlamaGenerator(url, model='system2'),
                       PipelineConfig(deadline_s=IMAGE_DEADLINE_S))
    assert run.path == 'fallback' and len(run.attempts) == 2 and len(http.requests) == 4


def test_image_rejected_classifier():
    assert g.image_rejected(g.HTTPStatusError(500, 'image input is not supported'), False)
    assert g.image_rejected(g.HTTPStatusError(400, 'mmproj missing'), False)
    assert not g.image_rejected(g.HTTPStatusError(500, 'kv cache full'), False)
    assert not g.image_rejected(g.HTTPStatusError(400, 'bad request'), False)
    assert g.image_rejected(g.HTTPStatusError(400, 'bad request'), True)
    assert not g.image_rejected(GenerationError('timeout', 'image'), True)
    assert str(g.HTTPStatusError(500, 'x')) == 'http: status 500'


# ── live assessor: header and cache ──────────────────────────────────────────────────────────

class FakeGen:
    examples = None

    def __init__(self, image_used, vision_model='vision-fake'):
        self.image_used, self.vision_model, self.calls = image_used, vision_model, 0

    def __call__(self, packet, errors=(), timeout_s=None):
        self.calls += 1
        reply = fixture()['reply'] if packet.vision_ids else pr.example_output(packet)
        return Generation(json.dumps(reply), 'qwen-fake', 'stop', 900, 120, 30.0,
                          image_used=self.image_used if packet.image is not None else None)


def live_client(fake, cache=None):
    assessor = LiveAssessor(fake, 'text-fake', PipelineConfig(), cache)
    return TestClient(api.create_app(api_key=KEY, assessor=assessor)), assessor


@pytest.mark.parametrize('image_used,header', [(True, 'used'), (False, 'text_only')])
def test_live_image_header(image_used, header):
    client, _ = live_client(FakeGen(image_used))
    r = post(client, mm_request())
    assert r.status_code == 200 and r.headers['X-System2-Image'] == header and r.json()['path'] == 'llm'
    assert set(r.json()) == set(post(client, backend_request()).json())
    assert 'X-System2-Image' not in post(client, backend_request()).headers


def test_live_text_only_answers_are_not_cached_but_image_answers_are(tmp_path):
    cache = ExplanationCache(tmp_path)
    client, assessor = live_client(FakeGen(False), cache)
    assert post(client, mm_request()).headers['X-System2-Cache'] == 'miss'
    assert list(tmp_path.glob('*.json')) == []
    client, assessor = live_client(FakeGen(True), cache)
    assert assessor.key_fields(prepare(mm_request()))['model'] == 'vision-fake'
    assert assessor.key_fields(prepare(backend_request()))['model'] == 'text-fake'
    first = post(client, mm_request())
    assert first.headers['X-System2-Cache'] == 'miss' and len(list(tmp_path.glob('*.json'))) == 1
    second = post(client, mm_request())
    assert second.headers['X-System2-Cache'] == 'hit' and second.headers['X-System2-Image'] == 'used'
    assert second.json() == first.json()
    other = post(client, mm_request(image_b64=b64(JPEG)))
    assert other.headers['X-System2-Cache'] == 'miss'


def test_assessor_from_env_reads_vision_model_and_gateway_key():
    from analyst.live import assessor_from_env
    a = assessor_from_env({'SYSTEM2_LLAMA_URL': 'http://gateway:4000', 'SYSTEM2_MODEL': 'system2',
                           'SYSTEM2_VISION_MODEL': 'system2-vision', 'SYSTEM2_LLAMA_API_KEY': 'sk-secret-123456',
                           'SYSTEM2_CACHE_DIR': ''})
    assert a.generator.vision_model == 'system2-vision' and a.vision_model == 'system2-vision'
    assert a.generator._api_key == 'sk-secret-123456' and 'sk-secret' not in repr(a)
    b = assessor_from_env({'SYSTEM2_LLAMA_URL': 'http://gateway:4000', 'SYSTEM2_MODEL': 'system2'})
    assert b.generator.vision_model == 'system2' and b.generator._api_key is None


# ── audit fixes 2026-10-10: demo pairing, reader flags, image deadline, gateway auth errors ────────────

def test_demo_rows_never_judge_the_asset():
    """A-1: a demo kit image is not a reading of this asset: comparison fields dropped, actions unrestricted."""
    forged = v_row(pairing='demo')                       # what an un-fixed orchestrator sent: agrees=false, HIGH
    p = prepare(with_context(vision=[forged], suggested_action='MONITOR'))
    content = json.loads(p.content['V1'])
    assert content['pairing'] == 'demo' and content['value'] == 121.0 and content['trust'] == 'HIGH'
    assert not set(pk.DEMO_DROPPED) & set(content)
    assert p.allowed_actions == prepare(backend_request()).allowed_actions and 'MONITOR' in p.allowed_actions
    assert p.suggested_action == 'MONITOR'
    clean = v_row(pairing='demo', logged_id=None, logged_value=None, delta=None, agrees=None)
    assert prepare(with_context(vision=[clean])).content == p.content          # same evidence either way


def test_linked_disagreement_still_restricts_actions_next_to_a_demo_row():
    p = prepare(with_context(vision=[v_row(), v_row(id='V2', pairing='demo')]))
    assert 'MONITOR' not in p.allowed_actions and json.loads(p.content['V1'])['agrees'] is False


def test_demo_fixture_reply_passes_and_shows_no_comparison():
    fx = json.loads((S2 / 'fixtures/multimodal/gauge_demo.json').read_text(encoding='utf-8'))
    p = prepare(fx['request'])
    assert check_assessment(fx['reply'], p) == ()
    g.validate_generated(fx['reply'], p)
    assert 'MONITOR' in p.allowed_actions and json.loads(p.content['V1'])['pairing'] == 'demo'
    assert not {'agrees', 'delta', 'logged_id', 'logged_value'} & set(json.loads(p.content['V1']))


def test_golden_disagreement_example_is_linked():
    rows = fixture()['request']['context']['vision']
    assert all(r['pairing'] == 'linked' for r in rows) and rows[0]['agrees'] is False


def test_vision_rules_cover_demo_rows_and_lower_bounds():
    assert 'pairing=demo' in pr.VISION_RULES and 'bound=lower' in pr.VISION_RULES and 'RELATIVE_ONLY' in pr.VISION_RULES
    assert 'demo' not in pr.SYSTEM


def test_reader_flags_are_kept_deduplicated_and_capped():
    flags = ['LABEL_OUTLIER', 'USED_HINT', 'LABEL_OUTLIER'] + ['F_%s' % c for c in 'ABCDEFGHIJ']
    content = json.loads(prepare(with_context(vision=[v_row(flags=flags)])).content['V1'])
    assert content['flags'] == list(dict.fromkeys(flags))[:pk.MAX_VISION_FLAGS]
    assert 'flags' not in json.loads(prepare(with_context(vision=[v_row(flags=[])])).content['V1'])


@pytest.mark.parametrize('flag', ['AMBIGUOUS_NEEDLE', 'DEFAULT_SWEEP'])
def test_ambiguous_readings_are_forced_low(flag):
    p = prepare(with_context(vision=[v_row(flags=[flag])]))
    content = json.loads(p.content['V1'])
    assert content['trust'] == 'LOW' and not {'value', 'delta', 'agrees'} & set(content)
    assert 'MONITOR' in p.allowed_actions                           # a LOW row never restricts the actions


def test_clipped_reading_is_a_lower_bound():
    content = json.loads(prepare(with_context(vision=[v_row(flags=['CLIPPED'], agrees=None)])).content['V1'])
    assert content['bound'] == 'lower' and content['trust'] == 'HIGH' and content['value'] == 121.0
    explicit = json.loads(prepare(with_context(vision=[v_row(bound='lower')])).content['V1'])
    assert explicit['bound'] == 'lower'


@pytest.mark.parametrize('bad', [v_row(flags='CLIPPED'), v_row(flags=['clipped']), v_row(flags=['CLIP PED']),
                                 v_row(flags=['DELTA_4C']), v_row(flags=[7]), v_row(bound='maybe')])
def test_malformed_flags_or_bound_are_rejected(bad):
    with pytest.raises(ValueError):
        prepare(with_context(vision=[bad]))


def test_rows_without_new_fields_keep_their_content():
    """Byte-identical evidence (and so packet_hash) for rows that carry no flags/bound and are not demo."""
    p = prepare(with_context(vision=[v_row()]))
    assert json.loads(p.content['V1']) == {k: v for k, v in v_row().items() if k != 'id'}


def test_gateway_wrong_key_is_not_an_image_rejection():
    """A-11: LiteLLM answers a wrong key with 400 'No connected db.'; never resend text-only for it."""
    no_db = '{"error":{"message":"No connected db.","type":"no_db_connection","param":null,"code":"400"}}'
    assert not g.image_rejected(g.HTTPStatusError(400, no_db), True)
    assert not g.image_rejected(g.HTTPStatusError(401, 'Authentication Error, No api key passed in.'), True)
    assert g.image_rejected(g.HTTPStatusError(400, 'Invalid model name passed in model=system2-vision'), True)


def test_gateway_wrong_key_over_http_falls_back_without_text_only_resend(server):
    http, url = server
    http.script[:] = [{'status': 400, 'body': {'error': {'message': 'No connected db.', 'type': 'no_db_connection'}}}]
    run = run_pipeline(mm_request(), g.LlamaGenerator(url, model='system2', vision_model='system2-vision'),
                       PipelineConfig(deadline_s=IMAGE_DEADLINE_S))
    assert run.path == 'fallback' and len(http.requests) == 2 and all(is_image_body(r['body']) for r in http.requests)


def test_image_timeout_resends_text_only_inside_the_image_deadline(server, monkeypatch):
    """A-6: the image call keeps TEXT_RESERVE_S back; when it times out the same attempt goes text-only."""
    monkeypatch.setattr(g, 'TEXT_RESERVE_S', 1.0)
    monkeypatch.setattr(g, 'MIN_IMAGE_CALL_S', 0.2)
    http, url = server
    http.script[:] = [{'body': envelope(fixture()['reply']), 'delay': 2.0}, {'body': envelope(fixture()['reply'])}]
    gen = g.LlamaGenerator(url, model='system2', vision_model='system2-vision', image_timeout_s=2.0)
    t0 = time.monotonic()
    run = run_pipeline(mm_request(), gen, PipelineConfig(deadline_s=2.0))
    assert time.monotonic() - t0 < 2.5
    assert run.path == 'llm' and run.image_used is False and len(run.attempts) == 1
    first, second = http.requests[0]['body'], http.requests[1]['body']
    assert is_image_body(first) and not is_image_body(second) and second['model'] == 'system2'


def test_image_call_is_skipped_when_only_the_text_reserve_is_left(server, monkeypatch):
    monkeypatch.setattr(g, 'TEXT_RESERVE_S', 5.0)
    http, url = server
    http.script[:] = [{'body': envelope(fixture()['reply'])}]
    gen = g.LlamaGenerator(url, model='system2', vision_model='system2-vision')
    out = gen(prepare(mm_request()), (), timeout_s=6.0)                 # 6 - 5 = 1 s < MIN_IMAGE_CALL_S
    assert out.image_used is False and len(http.requests) == 1 and not is_image_body(http.requests[0]['body'])


def test_image_budget_cap_applies_only_to_image_packets(server):
    http, url = server
    gen = g.LlamaGenerator(url, model='system2', timeout_s=15.0, image_timeout_s=25.0)
    seen = []
    gen._post = lambda body, headers, budget: seen.append(round(budget)) or json.dumps(
        envelope(fixture()['reply'])).encode()
    gen(prepare(backend_request()), (), timeout_s=30.0)
    gen(prepare(mm_request()), (), timeout_s=30.0)
    assert seen == [15, 25 - round(g.TEXT_RESERVE_S)]                 # text path unchanged; image keeps the reserve


class BudgetGen(FakeGen):
    def __init__(self):
        super().__init__(True)
        self.budgets = []

    def __call__(self, packet, errors=(), timeout_s=None):
        self.budgets.append((packet.image is not None, round(timeout_s)))
        return super().__call__(packet, errors, timeout_s)


def test_live_assessor_uses_the_image_deadline_only_for_images():
    gen = BudgetGen()
    a = LiveAssessor(gen, 'text-fake', PipelineConfig(deadline_s=15.0), image_deadline_s=25.0)
    a.assess(BackendAssessRequest.model_validate(backend_request()))
    a.assess(BackendAssessRequest.model_validate(mm_request()))
    assert gen.budgets == [(False, 15), (True, 25)]


@pytest.mark.parametrize('raw,expect', [(None, 25.0), ('', 25.0), ('30', 30.0), ('7.5', 7.5)])
def test_image_deadline_env(raw, expect):
    from analyst.live import assessor_from_env
    env = {'SYSTEM2_LLAMA_URL': 'http://gateway:4000', 'SYSTEM2_MODEL': 'system2', 'SYSTEM2_CACHE_DIR': ''}
    if raw is not None:
        env['SYSTEM2_IMAGE_DEADLINE_S'] = raw
    a = assessor_from_env(env)
    assert a.image_config.deadline_s == expect and a.generator.image_timeout_s == expect
    assert a.config.deadline_s == 15.0


@pytest.mark.parametrize('raw', ['soon', '0', '-3', '500', 'nan'])
def test_bad_image_deadline_env_is_refused(raw):
    from analyst.live import assessor_from_env
    with pytest.raises(RuntimeError, match='SYSTEM2_IMAGE_DEADLINE_S'):
        assessor_from_env({'SYSTEM2_LLAMA_URL': 'http://gateway:4000', 'SYSTEM2_MODEL': 'system2',
                           'SYSTEM2_IMAGE_DEADLINE_S': raw})
