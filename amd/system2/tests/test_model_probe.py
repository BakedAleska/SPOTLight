"""Live /health model_status: the background probe that says whether the model behind System 2 answers.

REGRESSION tests only: fake probe functions, a scripted generator and a local ThreadingHTTPServer stand in for
llama-server / the gateway. No model, GPU or AMD box is involved.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / 'data' / 'dev_packets.json'
KEY = 'probe-test-key-0123456789abcdef'

from analyst.api import create_app  # noqa: E402
from analyst.dataset import load_dataset  # noqa: E402
from analyst.generation import HTTPStatusError, LlamaGenerator  # noqa: E402
from analyst.live import LiveAssessor, ModelProbe, assessor_from_env  # noqa: E402
from analyst.packet import normalize_packet  # noqa: E402
from analyst.pipeline import Generation, GenerationError, PipelineConfig  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def ok_probe(timeout_s):
    return None


def failing(exc):
    def probe(timeout_s):
        raise exc
    return probe


# ── ModelProbe ────────────────────────────────────────────────────────────────────────────────

def test_unknown_until_first_check():
    snap = ModelProbe(ok_probe).snapshot()
    assert snap == {'model_status': 'unknown', 'model_checked_s_ago': None, 'model_probe_ms': None}


def test_up_after_a_successful_check():
    clock = Clock()
    p = ModelProbe(ok_probe, clock=clock)
    assert p.run_once()
    clock.t += 7
    snap = p.snapshot()
    assert snap['model_status'] == 'up' and snap['model_checked_s_ago'] == 7 and 'model_error' not in snap


@pytest.mark.parametrize('exc,reason', [
    (GenerationError('timeout', 'no response within 15.0 s'), 'timeout'),
    (GenerationError('transport', 'ConnectionRefusedError'), 'transport'),
    (HTTPStatusError(401, 'bad key sk-secret'), 'http 401'),
    (RuntimeError('http://10.0.0.5:4000 exploded'), 'RuntimeError'),
])
def test_down_reason_is_short_and_secret_free(exc, reason):
    p = ModelProbe(failing(exc))
    p.run_once()
    snap = p.snapshot()
    assert snap['model_status'] == 'down' and snap['model_error'] == reason


def test_skipped_while_an_assessment_runs():
    calls = []
    p = ModelProbe(lambda t: calls.append(t))
    p.begin()
    assert p.run_once() is False and calls == []
    p.end(answered=False)
    assert p.snapshot()['model_status'] == 'unknown'
    assert p.run_once() and calls == [p.timeout_s]


def test_an_answered_assessment_counts_as_up():
    p = ModelProbe(failing(GenerationError('timeout')))
    p.run_once()
    assert p.snapshot()['model_status'] == 'down'
    p.begin()
    p.end(answered=True)
    assert p.snapshot()['model_status'] == 'up'


def test_thread_checks_on_start_and_stops():
    seen = threading.Event()
    p = ModelProbe(lambda t: seen.set(), interval_s=3600)
    p.start()
    assert seen.wait(5)
    p.stop()


# ── LiveAssessor wiring ───────────────────────────────────────────────────────────────────────

def case():
    return next(c for c in load_dataset(DATA) if c.category == 'equipment')


def request():
    return case().to_dict()['request']


def answer():
    prepared = normalize_packet(case().request)
    return json.dumps({'evidence': [{'claim': 'Visual score was 75 with a HIGH severity defect.', 'ids': ['F2', 'F3']}],
                       'answer': 'Visual score was 75.', 'question': prepared.question,
                       'recommended_action': 'SCHEDULE_MAINTENANCE', 'confidence': 'medium',
                       'insufficient_context': False})


class FakeGen:
    def __init__(self, outcome):
        self.outcome = outcome

    def __call__(self, packet, errors=(), timeout_s=None):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def assessor(gen, probe):
    return LiveAssessor(gen, 'fake-model', PipelineConfig(deadline_s=15.0), probe=probe)


def test_model_answer_marks_up_and_transport_failure_does_not():
    probe = ModelProbe(failing(GenerationError('timeout')))
    probe.run_once()
    assessor(FakeGen(GenerationError('transport', 'ConnectionRefusedError')), probe).assess(request())
    assert probe.snapshot()['model_status'] == 'down'
    assessor(FakeGen(Generation(text=answer(), model='fake-model')), probe).assess(request())
    assert probe.snapshot()['model_status'] == 'up'


def test_live_health_reports_model_status_without_secrets():
    probe = ModelProbe(failing(HTTPStatusError(401)), interval_s=3600)
    a = LiveAssessor(LlamaGenerator('http://127.0.0.1:18080', model='qwen-live', api_key='sk-secret-key'),
                     'qwen-live', probe=probe)
    with TestClient(create_app(api_key=KEY, assessor=a)) as c:   # lifespan starts the probe thread
        for _ in range(100):
            body = c.get('/health').json()
            if body['model_status'] != 'unknown':
                break
            threading.Event().wait(0.02)
    assert body['mode'] == 'live' and body['model_status'] == 'down' and body['model_error'] == 'http 401'
    text = json.dumps(body)
    assert 'sk-secret-key' not in text and KEY not in text and '18080' not in text and '127.0.0.1' not in text


def test_stub_health_has_no_model_keys():
    h = TestClient(create_app(api_key=KEY)).get('/health').json()
    assert set(h) == {'status', 'mode', 'contract_version'}


# ── LlamaGenerator.probe over HTTP ────────────────────────────────────────────────────────────

@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            self.server.requests.append({'path': self.path, 'body': json.loads(raw),
                                         'auth': self.headers.get('Authorization')})
            status, reply = self.server.reply
            data = json.dumps(reply).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    http.daemon_threads = True
    http.requests, http.reply = [], (200, {'choices': [{'message': {'content': 'ok'}}]})
    worker = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    worker.start()
    yield http, 'http://127.0.0.1:%d' % http.server_port
    http.shutdown()
    http.server_close()
    worker.join()


def test_generator_probe_sends_one_token_request(server):
    http, url = server
    LlamaGenerator(url, model='system2', api_key='sk-k').probe(5)
    req = http.requests[0]
    assert req['path'] == '/v1/chat/completions' and req['auth'] == 'Bearer sk-k'
    assert req['body']['model'] == 'system2' and req['body']['max_tokens'] == 1
    assert req['body']['chat_template_kwargs'] == {'enable_thinking': False}


def test_generator_probe_raises_on_http_error_and_non_completion(server):
    http, url = server
    http.reply = (503, {'error': 'loading'})
    with pytest.raises(HTTPStatusError):
        LlamaGenerator(url, model='m').probe(5)
    http.reply = (200, {'status': 'ok'})
    with pytest.raises(GenerationError):
        LlamaGenerator(url, model='m').probe(5)


# ── env ───────────────────────────────────────────────────────────────────────────────────────

def test_env_sets_probe_interval_and_timeout():
    a = assessor_from_env({'SYSTEM2_LLAMA_URL': 'http://127.0.0.1:18080', 'SYSTEM2_MODEL': 'm',
                           'SYSTEM2_MODEL_PROBE_S': '60', 'SYSTEM2_MODEL_PROBE_TIMEOUT_S': '8'})
    assert a.probe.interval_s == 60 and a.probe.timeout_s == 8
    d = assessor_from_env({'SYSTEM2_LLAMA_URL': 'http://127.0.0.1:18080', 'SYSTEM2_MODEL': 'm'})
    assert d.probe.interval_s == 30 and d.probe.timeout_s == 15


@pytest.mark.parametrize('name,value', [('SYSTEM2_MODEL_PROBE_S', '2'), ('SYSTEM2_MODEL_PROBE_S', '9000'),
                                        ('SYSTEM2_MODEL_PROBE_TIMEOUT_S', '0'), ('SYSTEM2_MODEL_PROBE_S', 'x')])
def test_env_rejects_out_of_range(name, value):
    with pytest.raises(RuntimeError, match=name):
        assessor_from_env({'SYSTEM2_LLAMA_URL': 'http://127.0.0.1:18080', 'SYSTEM2_MODEL': 'm', name: value})


def test_env_keeps_recorder_and_probe_together(tmp_path):
    """Regression: a merge of the model probe with SYSTEM2_RECORD_DIR recording once dropped the recorder."""
    a = assessor_from_env({'SYSTEM2_LLAMA_URL': 'http://127.0.0.1:18080', 'SYSTEM2_MODEL': 'm',
                           'SYSTEM2_RECORD_DIR': str(tmp_path)})
    assert a.recorder is not None and a.probe is not None
    assert a.probe._probe.__self__ is a.generator.inner   # probes the real generator, not the recording wrapper


def test_env_keeps_recorder_and_probe_together(tmp_path):
    """Regression: a merge of the model probe with SYSTEM2_RECORD_DIR recording once dropped the recorder."""
    a = assessor_from_env({'SYSTEM2_LLAMA_URL': 'http://127.0.0.1:18080', 'SYSTEM2_MODEL': 'm',
                           'SYSTEM2_RECORD_DIR': str(tmp_path)})
    assert a.recorder is not None and a.probe is not None
    assert a.probe._probe.__self__ is a.generator.inner   # probes the real generator, not the recording wrapper
