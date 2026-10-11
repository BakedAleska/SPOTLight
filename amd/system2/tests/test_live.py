"""Live /v1/assess wiring (analyst.live): the checked pipeline behind the HTTP API.

These are REGRESSION tests only. Every generator here is a scripted fake or a local ThreadingHTTPServer pretending
to be llama-server; no model, GPU or AMD box is involved, so nothing here is live AMD evidence. Apps are built with
create_app(api_key=KEY, assessor=LiveAssessor(...)), never from the environment, so a developer shell with live
SYSTEM2_* variables cannot leak in.
"""
import copy
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / 'data' / 'dev_packets.json'
SERVE = ROOT / 'scripts' / 'serve.py'
INSUFFICIENT = ROOT / 'fixtures' / 'backend' / 'insufficient_context.json'
KEY = 'live-test-key-0123456789abcdef'
URL = 'http://127.0.0.1:18080'
LIVE_ENV = ('SYSTEM2_LLAMA_URL', 'SYSTEM2_MODEL', 'SYSTEM2_CACHE_DIR')

from analyst.api import create_app  # noqa: E402
from analyst.backend_contract import BackendAssessResponse  # noqa: E402
from analyst.dataset import load_dataset  # noqa: E402
from analyst.generation import LlamaGenerator  # noqa: E402
from analyst.live import LiveAssessor, assessor_from_env, to_response  # noqa: E402
from analyst.packet import normalize_packet  # noqa: E402
from analyst.pipeline import Generation, GenerationError, PipelineConfig  # noqa: E402
from analyst.prompts import example_output  # noqa: E402


# ── helpers (copied from test_pipeline.py / test_generation.py, not imported) ─────────────────

class FakeGen:
    """Scripted generator; each outcome is a Generation, an exception, or a callable(packet) -> Generation."""

    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, packet, errors=(), timeout_s=None):
        self.calls.append({'errors': errors, 'timeout_s': timeout_s, 'content': dict(packet.content)})
        out = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(out, BaseException):
            raise out
        return out(packet) if callable(out) else out


def case(category='equipment'):
    return next(c for c in load_dataset(DATA) if c.category == category)


def equipment_json():
    return case().to_dict()['request']


def gen(obj, **kw):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return Generation(text=text, model=kw.pop('model', 'fake-model'), **kw)


def valid_candidate(req):
    prepared = normalize_packet(req)
    return {'evidence': [{'claim': 'Visual score was 75 with a HIGH severity defect.', 'ids': ['F2', 'F3']}],
            'answer': 'Visual score was 75.', 'question': prepared.question,
            'recommended_action': 'SCHEDULE_MAINTENANCE', 'confidence': 'medium', 'insufficient_context': False}


def invalid_candidate(req):
    bad = valid_candidate(req)
    bad['evidence'][0]['claim'] = 'Visual score was 76.'
    return bad


def good(**kw):
    return gen(valid_candidate(case().request), **kw)


def bad():
    return gen(invalid_candidate(case().request))


def live_client(fake, cache=None):
    assessor = LiveAssessor(fake, 'fake-model', PipelineConfig(deadline_s=15.0), cache=cache)
    return TestClient(create_app(api_key=KEY, assessor=assessor))


def post(client, body, key=KEY):
    headers = {'X-API-Key': key} if key is not None else {}
    return client.post('/v1/assess', json=body, headers=headers)


# ── tracer: live /v1/assess through the checked pipeline ─────────────────────────────────────

def test_live_assess_returns_llm_with_metadata():
    fake = FakeGen([good(model='qwen-served', tokens_per_sec=41.5)])
    r = post(live_client(fake), equipment_json())
    assert r.status_code == 200, r.text
    out = BackendAssessResponse.model_validate(r.json())
    assert out.path == 'llm' and out.model == 'qwen-served'
    assert out.tokens_per_sec == 41.5
    assert type(r.json()['latency_ms']) is int and out.latency_ms >= 0
    assert r.headers['X-System2-Cache'] == 'off'
    assert len(fake.calls) == 1


def test_invalid_then_valid_is_retry():
    fake = FakeGen([bad(), good()])
    r = post(live_client(fake), equipment_json())
    assert r.status_code == 200 and r.json()['path'] == 'retry'
    assert len(fake.calls) == 2 and fake.calls[1]['errors']


@pytest.mark.parametrize('outcomes', [
    lambda: [bad(), bad()],
    lambda: [GenerationError('timeout', 'slow'), GenerationError('timeout', 'slow')],
], ids=['two_invalid', 'two_timeouts'])
def test_two_failures_fall_back_to_template(outcomes):
    fake = FakeGen(outcomes())
    r = post(live_client(fake), equipment_json())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['path'] == 'fallback' and body['model'] == 'template' and body['tokens_per_sec'] is None
    assert len(fake.calls) == 2


def test_insufficient_context_never_generates():
    req = json.loads(INSUFFICIENT.read_text(encoding='utf-8'))['request']
    fake = FakeGen([good()])
    r = post(live_client(fake), req)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['path'] == 'insufficient_context' and body['evidence'] == []
    assert body['recommended_action'] == 'MANUAL_REVIEW'
    assert fake.calls == []


@pytest.mark.parametrize('key', [None, 'wrong-key-0123456789abcdef'], ids=['missing', 'wrong'])
def test_key_checked_before_generation(key):
    fake = FakeGen([good()])
    r = post(live_client(fake), equipment_json(), key=key)
    assert r.status_code == 401
    assert fake.calls == []


def test_malformed_consumed_value_422_before_generation():
    req = copy.deepcopy(equipment_json())
    req['context']['allowed_actions'] = 5
    fake = FakeGen([good()])
    r = post(live_client(fake), req)
    assert r.status_code == 422
    assert fake.calls == []


def test_live_health_reports_model_not_secrets():
    assessor = LiveAssessor(LlamaGenerator(URL, model='qwen-live'), 'qwen-live')
    r = TestClient(create_app(api_key=KEY, assessor=assessor)).get('/health')
    assert r.status_code == 200
    assert r.json()['mode'] == 'live' and r.json()['model'] == 'qwen-live'
    assert KEY not in r.text and '127.0.0.1' not in r.text and '18080' not in r.text


def test_stub_health_and_assess_unchanged():
    c = TestClient(create_app(api_key=KEY))
    h = c.get('/health').json()
    assert set(h) == {'status', 'mode', 'contract_version'} and h['mode'] == 'stub'
    r = post(c, equipment_json())
    assert r.status_code == 200 and r.json()['path'] == 'stub'
    assert 'X-System2-Cache' not in r.headers


def test_to_response_matches_flat_contract():
    from analyst.pipeline import run_pipeline
    run = run_pipeline(case().request, FakeGen([good()]), PipelineConfig())
    flag_id = case().prepared.flag_id
    out = to_response(run, flag_id)
    assert out.path == 'llm' and out.flag_id == flag_id and out.model == 'fake-model'
    assert out.evidence[0].ids == ['F2', 'F3']


# ── assessor_from_env ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('env', [{}, {'SYSTEM2_LLAMA_URL': ''}, {'SYSTEM2_LLAMA_URL': '   ', 'SYSTEM2_MODEL': 'm'}],
                         ids=['absent', 'empty', 'blank'])
def test_env_without_url_is_stub(env):
    assert assessor_from_env(env) is None


def test_env_url_without_model_fails():
    with pytest.raises(RuntimeError, match='SYSTEM2_MODEL'):
        assessor_from_env({'SYSTEM2_LLAMA_URL': URL})
    with pytest.raises(RuntimeError, match='SYSTEM2_MODEL'):
        assessor_from_env({'SYSTEM2_LLAMA_URL': URL, 'SYSTEM2_MODEL': '  '})


def test_env_non_http_url_fails_without_credentials():
    with pytest.raises(RuntimeError) as err:
        assessor_from_env({'SYSTEM2_LLAMA_URL': 'ftp://user:hunter2-secret@box:21', 'SYSTEM2_MODEL': 'm'})
    assert 'hunter2-secret' not in str(err.value)


def test_env_live_builds_llama_generator_without_network():
    a = assessor_from_env({'SYSTEM2_LLAMA_URL': ' http://127.0.0.1:9 ', 'SYSTEM2_MODEL': 'qwen3.5-9b-q4km'})
    assert isinstance(a, LiveAssessor) and isinstance(a.generator, LlamaGenerator)
    assert a.generator.endpoint.endswith('/v1/chat/completions')
    assert a.generator.model == 'qwen3.5-9b-q4km' == a.model
    assert a.config.deadline_s == 15.0 and a.config.catalog is not None


# ── end to end over HTTP to a local fake llama-server (regression only, not AMD evidence) ────

def envelope(content, finish='stop', completion=120, prompt=900, speed=35.6, model='qwen-fake'):
    body = {'model': model, 'choices': [{'index': 0, 'finish_reason': finish,
                                          'message': {'role': 'assistant', 'content': content}}],
            'usage': {'prompt_tokens': prompt, 'completion_tokens': completion}}
    if speed is not None:
        body['timings'] = {'predicted_per_second': speed, 'prompt_n': prompt, 'predicted_n': completion}
    return body


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            self.server.requests.append({'path': self.path, 'body': json.loads(raw)})
            data = json.dumps(self.server.reply).encode('utf-8')
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    http.daemon_threads = True
    http.requests, http.reply = [], None
    worker = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    worker.start()
    yield http, 'http://127.0.0.1:%d' % http.server_port
    http.shutdown()
    http.server_close()
    worker.join()


def test_live_request_over_http_to_fake_llama_server(server):
    http, url = server
    c = case()
    http.reply = envelope(json.dumps(example_output(c.prepared), ensure_ascii=False), speed=27.25)
    assessor = LiveAssessor(LlamaGenerator(url, model='qwen-fake'), 'qwen-fake')
    r = post(TestClient(create_app(api_key=KEY, assessor=assessor)), c.to_dict()['request'])
    assert r.status_code == 200, r.text
    body = BackendAssessResponse.model_validate(r.json())
    assert body.path == 'llm' and body.model == 'qwen-fake' and body.tokens_per_sec == 27.25
    assert len(http.requests) == 1 and http.requests[0]['path'] == '/v1/chat/completions'
    assert http.requests[0]['body']['model'] == 'qwen-fake'


# ── serve.py fails closed on an incomplete live config ───────────────────────────────────────

def test_serve_live_without_model_exits_2():
    env = {k: v for k, v in os.environ.items()
           if k not in {'AMD_API_KEY', 'SYSTEM2_API_KEY', 'SYSTEM2_HOST', 'SYSTEM2_PORT', *LIVE_ENV}}
    env.update(SYSTEM2_API_KEY=KEY, SYSTEM2_LLAMA_URL='http://127.0.0.1:9')
    r = subprocess.run([sys.executable, str(SERVE), '--port', '0'], env=env, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert 'SYSTEM2_MODEL' in r.stderr
    assert KEY not in r.stdout + r.stderr


# ── explanation cache (SRV-02) ───────────────────────────────────────────────────────────────

from analyst.evaluation import packet_hash  # noqa: E402
from analyst.live import ExplanationCache, cache_dir_from_env, cache_key, prompt_version  # noqa: E402
from analyst.prompts import PROMPT_VERSION, load_examples  # noqa: E402
from scripts.precompute import load_requests, main as precompute_main, run_precompute  # noqa: E402

FIXTURE = ROOT / 'fixtures' / 'backend' / 'equipment.json'


def echo_gen(**kw):
    """Valid model reply for whatever packet arrives (the few-shot reply builder, checked on every dev case)."""
    return lambda packet: gen(example_output(packet), **kw)


def cached_client(tmp_path, fake, model='fake-model'):
    assessor = LiveAssessor(fake, model, PipelineConfig(deadline_s=15.0), cache=ExplanationCache(tmp_path / 'c'))
    return assessor, TestClient(create_app(api_key=KEY, assessor=assessor))


def entries(tmp_path):
    d = tmp_path / 'c'
    return sorted(d.glob('*.json')) if d.exists() else []


def test_cache_key_deterministic_and_sensitive():
    base = (2, 'a' * 64, 'qwen', 'p1')
    assert cache_key(*base) == cache_key(*base)
    assert len(cache_key(*base)) == 64 and int(cache_key(*base), 16) >= 0
    for i, other in enumerate((3, 'b' * 64, 'qwen-27b', 'p2')):
        changed = list(base)
        changed[i] = other
        assert cache_key(*changed) != cache_key(*base)


def test_prompt_version_tracks_few_shot_pool():
    assert prompt_version(FakeGen([])) == PROMPT_VERSION
    ex = load_examples()
    a, b = type('G', (), {'examples': ex})(), type('G', (), {'examples': ex[1:]})()
    assert prompt_version(a).startswith(PROMPT_VERSION + ':') and len(prompt_version(a)) == len(PROMPT_VERSION) + 17
    assert prompt_version(a) == prompt_version(type('G', (), {'examples': ex})())
    assert prompt_version(a) != prompt_version(b)


def test_miss_then_hit_is_byte_identical(tmp_path):
    fake = FakeGen([echo_gen(model='qwen-served', tokens_per_sec=30.0)])
    _, c = cached_client(tmp_path, fake)
    first = post(c, equipment_json())
    assert first.status_code == 200 and first.headers['X-System2-Cache'] == 'miss'
    assert len(fake.calls) == 1 and len(entries(tmp_path)) == 1
    second = post(c, equipment_json())
    assert second.headers['X-System2-Cache'] == 'hit'
    assert second.content == first.content
    assert len(fake.calls) == 1


def test_changed_fact_model_or_prompt_regenerates(tmp_path):
    fake = FakeGen([echo_gen()])
    assessor, c = cached_client(tmp_path, fake)
    assert post(c, equipment_json()).headers['X-System2-Cache'] == 'miss'
    req = copy.deepcopy(equipment_json())
    req['context']['facts'][0]['fact'] += ' Rechecked on the next route.'
    assert post(c, req).headers['X-System2-Cache'] == 'miss'
    assert len(fake.calls) == 2
    assert post(c, req).headers['X-System2-Cache'] == 'hit' and len(fake.calls) == 2
    _, c2 = cached_client(tmp_path, fake, model='other-model')
    assert post(c2, equipment_json()).headers['X-System2-Cache'] == 'miss' and len(fake.calls) == 3
    assessor.prompt_ver = PROMPT_VERSION + ':changed'
    assert post(c, equipment_json()).headers['X-System2-Cache'] == 'miss' and len(fake.calls) == 4
    assert len(entries(tmp_path)) == 4


def test_fallback_and_insufficient_never_stored(tmp_path):
    fake = FakeGen([bad(), bad()])
    _, c = cached_client(tmp_path, fake)
    r = post(c, equipment_json())
    assert r.json()['path'] == 'fallback' and r.headers['X-System2-Cache'] == 'miss'
    req = json.loads(INSUFFICIENT.read_text(encoding='utf-8'))['request']
    r = post(c, req)
    assert r.json()['path'] == 'insufficient_context' and r.headers['X-System2-Cache'] == 'miss'
    assert entries(tmp_path) == []


def _stored(tmp_path):
    (p,) = entries(tmp_path)
    return p, json.loads(p.read_text(encoding='utf-8'))


@pytest.mark.parametrize('corrupt', ['not_json', 'key_mismatch', 'ungrounded_claim', 'invalid_response',
                                     'flag_mismatch'])
def test_bad_entry_is_miss_and_overwritten(tmp_path, corrupt):
    fake = FakeGen([echo_gen()])
    _, c = cached_client(tmp_path, fake)
    good_body = post(c, equipment_json()).json()
    p, entry = _stored(tmp_path)
    if corrupt == 'not_json':
        p.write_text('{"key": ', encoding='utf-8')
    else:
        if corrupt == 'key_mismatch':
            entry['key']['model'] = 'someone-else'
        elif corrupt == 'ungrounded_claim':
            entry['response']['evidence'][0]['claim'] = 'Battery was 99% and the asset exploded at 13:37.'
        elif corrupt == 'invalid_response':
            entry['response']['path'] = 'teleport'
        else:
            entry['response']['flag_id'] = 777
        p.write_text(json.dumps(entry), encoding='utf-8')
    r = post(c, equipment_json())
    assert r.status_code == 200 and r.headers['X-System2-Cache'] == 'miss'
    assert r.json()['evidence'] == good_body['evidence']
    assert len(fake.calls) == 2
    _, fresh = _stored(tmp_path)
    assert fresh['response']['evidence'] == good_body['evidence']
    assert post(c, equipment_json()).headers['X-System2-Cache'] == 'hit'


def test_unwritable_cache_still_answers(tmp_path):
    blocker = tmp_path / 'file'
    blocker.write_text('x', encoding='utf-8')
    cache = ExplanationCache(blocker / 'sub')
    assessor = LiveAssessor(FakeGen([echo_gen()]), 'fake-model', PipelineConfig(), cache=cache)
    resp, status, stored = assessor.assess_detail(case().request)
    assert resp.path == 'llm' and status == 'miss' and stored is False
    assert cache.put(assessor.key_fields(case().prepared), resp) is False


def test_cache_key_uses_eval_packet_hash(tmp_path):
    assessor, c = cached_client(tmp_path, FakeGen([echo_gen()]))
    post(c, equipment_json())
    _, entry = _stored(tmp_path)
    assert entry['key'] == {'flag_id': case().prepared.flag_id, 'packet_hash': packet_hash(case().prepared),
                            'model': 'fake-model', 'prompt_ver': assessor.prompt_ver}
    assert set(entry) == {'key', 'response', 'created_at'}


def test_cache_dir_off_disables_the_cache():
    assert cache_dir_from_env({'SYSTEM2_CACHE_DIR': 'off'}) is None
    assert cache_dir_from_env({'SYSTEM2_CACHE_DIR': 'OFF'}) is None


def test_recorder_keeps_each_attempts_raw_text_and_the_final_path(tmp_path):
    from analyst.live import RecordingGenerator, RunRecorder
    req = equipment_json()
    bad, good = json.dumps(invalid_candidate(case().request)), json.dumps(valid_candidate(case().request))
    fake = FakeGen([gen(bad), gen(good)])
    assessor = LiveAssessor(RecordingGenerator(fake), 'fake-model', PipelineConfig(deadline_s=15.0),
                            cache=ExplanationCache(tmp_path / 'cache'), recorder=RunRecorder(tmp_path / 'rec'))
    first, status = assessor.assess(req)
    again, status2 = assessor.assess(req)
    assert (status, status2) == ('miss', 'hit') and first == again
    lines = [json.loads(x) for x in (tmp_path / 'rec' / 'system2-records.jsonl').read_text().splitlines()]
    assert [r['cache'] for r in lines] == ['miss', 'hit']
    miss, hit = lines
    assert [c['text'] for c in miss['calls']] == [bad, good]          # raw model text of both attempts
    assert miss['calls'][1]['retry_errors']                          # the retry saw the checker's errors
    assert [a['status'] for a in miss['run']['attempts']] == ['rejected', 'accepted']
    assert miss['response']['path'] == 'retry' and miss['deadline_s'] == 15.0
    assert hit['run'] is None and hit['calls'] == [] and hit['response'] == miss['response']
    assert miss['packet_hash'] == hit['packet_hash'] and miss['prompt_ver'] == assessor.prompt_ver


def test_cache_dir_from_env():
    assert cache_dir_from_env({'SYSTEM2_CACHE_DIR': '/tmp/x'}) == Path('/tmp/x')
    assert cache_dir_from_env({'SYSTEM2_CACHE_DIR': '  '}) == ROOT / 'cache'
    assert cache_dir_from_env({}) == ROOT / 'cache'


def test_env_assessor_uses_cache_dir(tmp_path):
    a = assessor_from_env({'SYSTEM2_LLAMA_URL': URL, 'SYSTEM2_MODEL': 'm', 'SYSTEM2_CACHE_DIR': str(tmp_path)})
    assert isinstance(a.cache, ExplanationCache) and a.cache.root == tmp_path


def test_prompts_and_evaluation_never_read_the_cache():
    for name in ('prompts.py', 'evaluation.py', 'dataset.py'):
        src = (ROOT / 'analyst' / name).read_text(encoding='utf-8')
        for needle in ('from .live', 'analyst.live', 'import live', 'ExplanationCache', 'SYSTEM2_CACHE_DIR'):
            assert needle not in src, (name, needle)


# ── precompute ───────────────────────────────────────────────────────────────────────────────

def test_load_requests_accepts_all_forms(tmp_path):
    assert len(load_requests([DATA])) == 6
    assert [s for s, _ in load_requests([FIXTURE])] == ['%s[0]' % FIXTURE]
    req = equipment_json()
    lst, jl = tmp_path / 'list.json', tmp_path / 'reqs.jsonl'
    lst.write_text(json.dumps([req, {'request': req}]), encoding='utf-8')
    jl.write_text(json.dumps(req) + '\n\n' + json.dumps(req) + '\n', encoding='utf-8')
    assert len(load_requests([lst])) == 2 and len(load_requests([jl])) == 2
    assert len(load_requests([DATA, FIXTURE, lst, jl])) == 11


def test_load_requests_names_bad_source_and_index(tmp_path):
    bad_req = copy.deepcopy(equipment_json())
    bad_req['context']['allowed_actions'] = 5
    lst = tmp_path / 'list.json'
    lst.write_text(json.dumps([equipment_json(), bad_req]), encoding='utf-8')
    with pytest.raises(ValueError, match=r'list\.json\[1\]'):
        load_requests([lst])
    lst.write_text(json.dumps([{'flag_id': True, 'flag': {}, 'context': {}}]), encoding='utf-8')
    with pytest.raises(ValueError, match=r'list\.json\[0\]'):
        load_requests([lst])


def sufficient_items():
    return [(s, r) for s, r in load_requests([DATA]) if not normalize_packet(r).insufficient_context]


def test_precompute_twice_generates_nothing_second_time(tmp_path, capsys):
    fake = FakeGen([echo_gen()])
    assessor = LiveAssessor(fake, 'fake-model', PipelineConfig(), cache=ExplanationCache(tmp_path / 'c'))
    items = sufficient_items()
    first = run_precompute(items, assessor)
    assert first['requests'] == len(items) == 5 and first['generated'] == 5 and first['cached'] == 0
    assert first['not_stored'] == 0 and first['latency']['n'] == 5
    calls = len(fake.calls)
    second = run_precompute(items, assessor)
    assert len(fake.calls) == calls
    assert second['cached'] == second['requests'] == 5 and second['generated'] == 0
    mutated = copy.deepcopy(items)
    body = mutated[0][1].model_dump()
    body['context']['facts'][0]['fact'] += ' Rechecked.'
    mutated[0] = (mutated[0][0], type(items[0][1]).model_validate(body))
    third = run_precompute(mutated, assessor)
    assert third['generated'] == 1 and third['cached'] == 4 and len(fake.calls) == calls + 1
    out = capsys.readouterr().out
    assert 'flag %d | hit | path llm' % normalize_packet(items[1][1]).flag_id in out


def test_precompute_insufficient_never_calls_model(tmp_path):
    fake = FakeGen([echo_gen()])
    assessor = LiveAssessor(fake, 'fake-model', PipelineConfig(), cache=ExplanationCache(tmp_path / 'c'))
    items = [(s, r) for s, r in load_requests([DATA]) if normalize_packet(r).insufficient_context]
    for _ in range(2):
        c = run_precompute(items, assessor)
        assert c['insufficient_context'] == 1 and c['generated'] == 0 and c['not_stored'] == 1
    assert fake.calls == []


def test_precompute_main_without_url_exits_2(monkeypatch, tmp_path):
    for name in LIVE_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as e:
        precompute_main(['--requests', str(DATA), '--model', 'm', '--cache-dir', str(tmp_path)])
    assert e.value.code == 2


def test_precompute_main_rejects_bad_input_with_1(monkeypatch, tmp_path):
    for name in LIVE_ENV:
        monkeypatch.delenv(name, raising=False)
    bad_file = tmp_path / 'bad.json'
    bad_file.write_text('[{"flag_id": 0}]', encoding='utf-8')
    with pytest.raises(SystemExit) as e:
        precompute_main(['--requests', str(bad_file), '--model-url', URL, '--model', 'm',
                         '--cache-dir', str(tmp_path / 'c')])
    assert e.value.code == 1
    assert not (tmp_path / 'c').exists()
