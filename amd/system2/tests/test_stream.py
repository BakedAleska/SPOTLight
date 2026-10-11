"""Streaming drafts (contract "Streaming Qwen drafts"): LlamaGenerator.drafts(), the streamed llama-server call, and
POST /v1/assess/stream.

REGRESSION tests only: llama-server is a local ThreadingHTTPServer that answers `stream: true` with SSE chunks (the
shape recorded from llama-server on the AMD box: a role chunk, content chunks, a finish chunk with an empty delta, a
`choices: []` chunk with usage + timings, then [DONE]) and `stream: false` with the plain envelope, so the streamed
and plain paths can be compared on the same script. No model, GPU or AMD box is involved.
"""
import copy
import http.client
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / 'data' / 'dev_packets.json'
MM_FIXTURE = ROOT / 'fixtures' / 'multimodal' / 'gauge_disagree.json'
INSUFFICIENT = ROOT / 'fixtures' / 'backend' / 'insufficient_context.json'
KEY = 'stream-test-key-0123456789abcdef'

from analyst import api  # noqa: E402
from analyst import generation as g  # noqa: E402
from analyst.api import create_app  # noqa: E402
from analyst.backend_contract import BackendAssessResponse  # noqa: E402
from analyst.dataset import load_dataset  # noqa: E402
from analyst.live import IMAGE_DEADLINE_S, ExplanationCache, LiveAssessor, RecordingGenerator, RunRecorder  # noqa: E402
from analyst.pipeline import GenerationError, PipelineConfig, run_pipeline  # noqa: E402
from analyst.prompts import example_output  # noqa: E402

MODEL_PATH = '/root/models/fake/Qwen-Fake-Q8_0.gguf'
NO_MMPROJ = {'error': {'code': 500, 'message': 'image input is not supported - hint: you may need to provide the '
                                                  'mmproj', 'type': 'server_error'}}


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────

def case(case_id='kit-equipment'):
    return next(c for c in load_dataset(DATA) if c.case_id == case_id)


def valid_text(c=None) -> str:
    return json.dumps(example_output((c or case()).prepared), ensure_ascii=False)


def invalid_text(c=None) -> str:
    bad = example_output((c or case()).prepared)
    bad['evidence'][0]['ids'] = ['NOT-IN-PACKET']
    return json.dumps(bad)


def mm():
    d = json.loads(MM_FIXTURE.read_text(encoding='utf-8'))
    return copy.deepcopy(d['request']), json.dumps(d['reply'])


def sse_events(text, finish='stop', piece=6, model=MODEL_PATH, prompt=900, speed=22.5):
    n = (len(text) + piece - 1) // piece
    events = [{'choices': [{'finish_reason': None, 'index': 0, 'delta': {'role': 'assistant', 'content': None}}],
               'model': model, 'object': 'chat.completion.chunk'}]
    events += [{'choices': [{'finish_reason': None, 'index': 0, 'delta': {'content': text[i:i + piece]}}],
                'model': model, 'object': 'chat.completion.chunk'} for i in range(0, len(text), piece)]
    events.append({'choices': [{'finish_reason': finish, 'index': 0, 'delta': {}}], 'model': model})
    events.append({'choices': [], 'model': model,
                   'usage': {'completion_tokens': n, 'prompt_tokens': prompt, 'total_tokens': n + prompt},
                   'timings': {'prompt_n': prompt, 'predicted_n': n, 'predicted_per_second': speed}})
    return [b'data: ' + json.dumps(e).encode('utf-8') + b'\n\n' for e in events] + [b'data: [DONE]\n\n']


def plain_envelope(text, finish='stop', piece=6, model=MODEL_PATH, prompt=900, speed=22.5):
    n = (len(text) + piece - 1) // piece
    return {'model': model, 'object': 'chat.completion',
            'choices': [{'index': 0, 'finish_reason': finish, 'message': {'role': 'assistant', 'content': text}}],
            'usage': {'completion_tokens': n, 'prompt_tokens': prompt, 'total_tokens': n + prompt},
            'timings': {'prompt_n': prompt, 'predicted_n': n, 'predicted_per_second': speed}}


class FakeLlama(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):   # a client that gave up closed the socket
        pass


@pytest.fixture
def llama():
    """Script steps: {'text', 'finish'?, 'gap'?} answer either mode ('ignore_stream': True answers a streamed call
    with the plain JSON envelope); {'status', 'body'} is an HTTP error; {'raw': [bytes]} is a literal SSE body."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_POST(self):
            srv = self.server
            raw = self.rfile.read(int(self.headers['Content-Length']))
            body = json.loads(raw)
            srv.requests.append({'body': body, 'raw': raw, 'headers': dict(self.headers)})
            step = srv.script.pop(0) if len(srv.script) > 1 else srv.script[0]
            self.close_connection = True
            try:
                if step.get('status', 200) != 200 or (not body.get('stream') and 'text' not in step):
                    data = json.dumps(step.get('body', {})).encode('utf-8')
                    self.send_response(step.get('status', 200))
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if not body.get('stream') or step.get('ignore_stream'):
                    data = json.dumps(plain_envelope(step['text'], step.get('finish', 'stop'))).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Transfer-Encoding', 'chunked')
                self.send_header('Connection', 'close')
                self.end_headers()
                chunks = step['raw'] if 'raw' in step else sse_events(step['text'], step.get('finish', 'stop'))
                for data in chunks:
                    self.wfile.write(b'%x\r\n%s\r\n' % (len(data), data))
                    self.wfile.flush()
                    srv.last_sent = time.monotonic()
                    time.sleep(step.get('gap', 0))
                self.wfile.write(b'0\r\n\r\n')
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    http = FakeLlama(('127.0.0.1', 0), Handler)
    http.requests, http.script, http.last_sent = [], [], None
    worker = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    worker.start()
    yield http, 'http://127.0.0.1:%d' % http.server_port
    http.shutdown()
    http.server_close()
    worker.join()


class Drafts:
    """Collects generator draft callbacks with arrival times."""

    def __init__(self):
        self.seen = []

    def __call__(self, call, tokens, text):
        self.seen.append((time.monotonic(), call, tokens, text))


def parse_sse(text: str):
    """[(event, data)] plus ('comment', line) for ':' lines."""
    out, event = [], None
    for line in text.split('\n'):
        if line.startswith(':'):
            out.append(('comment', line))
        elif line.startswith('event: '):
            event = line[7:]
        elif line.startswith('data: '):
            out.append((event, json.loads(line[6:])))
            event = None
    return out


def stream(client, body, key=KEY):
    headers = {'X-API-Key': key} if key is not None else {}
    return client.post('/v1/assess/stream', json=body, headers=headers)


def without_latency(d):
    return {k: v for k, v in d.items() if k != 'latency_ms'}


# ── generator: the streamed call ─────────────────────────────────────────────────────────────

def test_streamed_body_is_the_plain_body_plus_stream_flags_and_plain_path_is_unchanged(llama):
    http, url = llama
    c = case()
    http.script[:] = [{'text': valid_text(c)}]
    gen = g.LlamaGenerator(url, model='qwen3.8-27b-q8_0')
    plain = gen(c.prepared, ())
    with gen.drafts(Drafts()):
        streamed = gen(c.prepared, ())
    after = gen(c.prepared, ())                         # the block ended: plain again
    first, second, third = (r['body'] for r in http.requests)
    assert first['stream'] is False and 'stream_options' not in first
    assert second['stream'] is True and second['stream_options'] == {'include_usage': True}
    assert {k: v for k, v in second.items() if k != 'stream_options'} == dict(first, stream=True)
    assert http.requests[0]['raw'] == http.requests[2]['raw'] == gen._body(c.prepared, gen_examples(gen, c), (),
                                                                          gen.model, True)
    assert third == first
    assert streamed == plain == after
    assert (plain.model, plain.finish_reason, plain.prompt_tokens, plain.tokens_per_sec) == (MODEL_PATH, 'stop', 900,
                                                                                            22.5)


def gen_examples(gen, c):
    from analyst.prompts import FEWSHOT_COUNT, select_examples
    return select_examples(gen.examples, c.prepared, FEWSHOT_COUNT)


def test_drafts_arrive_incrementally_as_growing_text(llama):
    http, url = llama
    text = valid_text()
    http.script[:] = [{'text': text, 'gap': 0.01}]
    gen, seen = g.LlamaGenerator(url), Drafts()
    with gen.drafts(seen):
        out = gen(case().prepared, ())
    assert out.text == text and out.finish_reason == 'stop'
    texts = [s[3] for s in seen.seen]
    assert len(texts) == (len(text) + 5) // 6 and texts[-1] == text
    assert all(b.startswith(a) and len(b) > len(a) for a, b in zip(texts, texts[1:]))
    assert [s[2] for s in seen.seen] == list(range(1, len(texts) + 1))
    assert {s[1] for s in seen.seen} == {1}
    assert seen.seen[0][0] < http.last_sent - 0.2           # the first draft came long before the stream ended


def test_pipeline_retry_and_truncation_through_the_stream(llama):
    http, url = llama
    c = case()
    http.script[:] = [{'text': invalid_text(c)}, {'text': valid_text(c)}]
    gen, seen = g.LlamaGenerator(url), Drafts()
    with gen.drafts(seen):
        run = run_pipeline(c.request, gen)
    assert run.path == 'retry' and [a.status for a in run.attempts] == ['rejected', 'accepted']
    calls = [s[1] for s in seen.seen]
    assert calls == sorted(calls) and set(calls) == {1, 2}
    assert [s[3] for s in seen.seen if s[1] == 2][-1] == valid_text(c)

    http.script[:] = [{'text': valid_text(c), 'finish': 'length'}]
    with gen.drafts(Drafts()):
        run = run_pipeline(c.request, gen)
    assert run.path == 'fallback' and [a.status for a in run.attempts] == ['truncated', 'truncated']
    assert run.attempts[0].finish_reason == 'length'


@pytest.mark.parametrize('phase', ['body', 'headers'])
def test_trickling_stream_cannot_outlive_the_budget(phase):
    stop = threading.Event()

    class Trickle(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            head = b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n'
            chunk = b'data: ' + json.dumps({'choices': [{'delta': {'content': ' '}}]}).encode() + b'\n\n'
            try:
                if phase == 'headers':
                    for i in range(len(head)):
                        if stop.is_set():
                            return
                        self.wfile.write(head[i:i + 1])
                        self.wfile.flush()
                        time.sleep(0.2)
                    return
                self.wfile.write(head)
                while not stop.is_set():
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    http = FakeLlama(('127.0.0.1', 0), Trickle)
    worker = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    worker.start()
    try:
        gen = g.LlamaGenerator('http://127.0.0.1:%d' % http.server_port, timeout_s=1.0)
        t0 = time.monotonic()
        with gen.drafts(Drafts()), pytest.raises(GenerationError) as err:
            gen(case().prepared, ())
        assert time.monotonic() - t0 < 2.0
        assert err.value.kind == 'timeout'
    finally:
        stop.set()
        http.shutdown()
        http.server_close()
        worker.join()


@pytest.mark.parametrize('step,kind,status', [
    ({'status': 503, 'body': {'error': {'message': 'loading model'}}}, 'http', 503),
    ({'raw': [b'data: {"error": {"code": 500, "message": "image input is not supported"}}\n\n']}, 'http', 500),
    ({'raw': [b'data: {not json\n\n']}, 'protocol', None),
    ({'raw': [b'data: [1, 2]\n\n']}, 'protocol', None),
    ({'raw': [b'data: {"choices": {"delta": {}}}\n\n']}, 'protocol', None),
    ({'raw': [b'data: {"choices": [{"delta": {"content": 5}}]}\n\n']}, 'protocol', None),
    ({'raw': [b'data: {"choices": [{"delta": {"content": "{\\"a"}}]}\n\n']}, 'protocol', None),   # cut, no [DONE]
    ({'raw': [b'data: {"usage": {}}\n\n', b'data: [DONE]\n\n']}, 'protocol', None),             # no choices
    ({'raw': [b'data: {"choices": [{"delta": {"content": "x"}}]}\n\n', b'data: NaN\n\n']}, 'protocol', None),
])
def test_bad_streams_are_generation_errors(llama, step, kind, status):
    http, url = llama
    http.script[:] = [step]
    gen = g.LlamaGenerator(url)
    with gen.drafts(Drafts()), pytest.raises(GenerationError) as err:
        gen(case().prepared, ())
    assert err.value.kind == kind
    if status is not None:
        assert isinstance(err.value, g.HTTPStatusError) and err.value.status == status


def test_oversized_content_and_lenient_framing(llama):
    http, url = llama
    http.script[:] = [{'text': 'x' * 400}]
    with pytest.raises(GenerationError) as err:
        gen = g.LlamaGenerator(url, max_response_bytes=300)
        with gen.drafts(Drafts()):
            gen(case().prepared, ())
    assert err.value.kind == 'oversized'
    # CRLF lines, comments, event fields, no trailing newline after [DONE], finish without usage: all accepted
    http.script[:] = [{'raw': [b': ping\r\n\r\n', b'event: message\r\ndata: {"choices": [{"delta": {"content": "ab"}}]}'
                               b'\r\n\r\n', b'data:{"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n',
                               b'data: [DONE]']}]
    gen = g.LlamaGenerator(url, model='m')
    with gen.drafts(Drafts()):
        out = gen(case().prepared, ())
    assert (out.text, out.finish_reason, out.model, out.completion_tokens) == ('ab', 'stop', 'm', None)


def test_upstream_ignoring_stream_true_is_read_as_a_plain_completion(llama):
    http, url = llama
    c = case()
    http.script[:] = [{'text': valid_text(c), 'ignore_stream': True}]
    gen, seen = g.LlamaGenerator(url, model='qwen3.8-27b-q8_0'), Drafts()
    plain = gen(c.prepared, ())
    with gen.drafts(seen):
        streamed = gen(c.prepared, ())
    assert http.requests[1]['body']['stream'] is True
    assert streamed == plain and streamed.text == valid_text(c) and seen.seen == []
    # the plain path's size cap still applies to such a reply
    http.script[:] = [{'text': 'x' * 400, 'ignore_stream': True}]
    gen = g.LlamaGenerator(url, max_response_bytes=300)
    with gen.drafts(Drafts()), pytest.raises(GenerationError) as err:
        gen(c.prepared, ())
    assert err.value.kind == 'oversized'


def test_a_failing_draft_consumer_never_fails_generation(llama):
    http, url = llama
    http.script[:] = [{'text': valid_text()}]
    gen = g.LlamaGenerator(url)

    def boom(*_):
        raise RuntimeError('consumer broke')

    with gen.drafts(boom):
        assert gen(case().prepared, ()).text == valid_text()


@pytest.mark.parametrize('first', [
    {'status': 500, 'body': NO_MMPROJ},
    {'raw': [b'data: ' + json.dumps(NO_MMPROJ).encode() + b'\n\n']},     # the same rejection inside a 200 stream
])
def test_image_rejection_resends_text_only_as_the_next_call(llama, first):
    http, url = llama
    req, reply = mm()
    http.script[:] = [first, {'text': reply}]
    gen, seen = g.LlamaGenerator(url, model='system2'), Drafts()
    with gen.drafts(seen):
        run = run_pipeline(req, gen, PipelineConfig(deadline_s=IMAGE_DEADLINE_S))
    assert run.path == 'llm' and run.image_used is False
    assert isinstance(http.requests[0]['body']['messages'][-1]['content'], list)
    assert isinstance(http.requests[1]['body']['messages'][-1]['content'], str)
    assert {s[1] for s in seen.seen} == {2} and seen.seen[-1][3] == reply


# ── live assessor ────────────────────────────────────────────────────────────────────────────

def test_recorder_keeps_the_full_streamed_text(llama, tmp_path):
    http, url = llama
    c = case()
    http.script[:] = [{'text': valid_text(c)}]
    recorder = RunRecorder(tmp_path)
    assessor = LiveAssessor(RecordingGenerator(g.LlamaGenerator(url)), 'fake-model', PipelineConfig(deadline_s=15.0),
                            recorder=recorder)
    assert assessor.streams_drafts
    seen = Drafts()
    response, status, stored, image = assessor.assess_image(c.to_dict()['request'], on_draft=seen)
    assert response.path == 'llm' and status == 'off' and image is None
    record = json.loads(recorder.path.read_text().splitlines()[0])
    assert record['calls'][0]['text'] == valid_text(c) and seen.seen[-1][3] == valid_text(c)
    assert http.requests[0]['body']['stream'] is True


# ── POST /v1/assess/stream ───────────────────────────────────────────────────────────────────

def live_app(url, cache=None, **kw):
    assessor = LiveAssessor(g.LlamaGenerator(url, **kw), 'qwen-test', PipelineConfig(deadline_s=15.0), cache=cache)
    return TestClient(create_app(api_key=KEY, assessor=assessor))


def test_stream_stub_mode_sends_start_then_the_assess_body():
    client = TestClient(create_app(api_key=KEY))
    body = case().to_dict()['request']
    r = stream(client, body)
    assert r.status_code == 200 and r.headers['content-type'].startswith('text/event-stream')
    assert r.headers['cache-control'] == 'no-cache, no-transform' and r.headers['x-accel-buffering'] == 'no'
    events = parse_sse(r.text)
    assert [e for e, _ in events] == ['start', 'final']
    assert events[0][1] == {'flag_id': body['flag_id'], 'request_id': body.get('request_id'), 'model': 'stub',
                            'mode': 'stub'}
    plain = client.post('/v1/assess', json=body, headers={'X-API-Key': KEY})
    assert without_latency(events[1][1]['response']) == without_latency(plain.json())
    assert events[1][1]['cache'] is None and events[1][1]['image'] is None
    req, _ = mm()
    final = parse_sse(stream(client, req).text)[-1][1]
    assert final['image'] == 'not_used' and final['cache'] is None


def test_stream_auth_and_validation_errors_are_plain_json_before_the_stream(llama):
    http, url = llama
    client = live_app(url)
    good = case().to_dict()['request']
    r = stream(client, good, key=None)
    assert r.status_code == 401 and r.json() == {'detail': 'missing or invalid X-API-Key'}
    assert stream(client, good, key='x' * 32).status_code == 401
    for bad in (dict(good, flag_id=0), dict(good, image_b64='not-an-image')):
        r, plain = stream(client, bad), client.post('/v1/assess', json=bad, headers={'X-API-Key': KEY})
        assert r.status_code == plain.status_code == 422
        assert r.headers['content-type'] == 'application/json' and r.json() == plain.json()
    assert http.requests == []


def test_stream_live_sends_drafts_then_the_same_final_as_assess(llama):
    http, url = llama
    c = case()
    text = valid_text(c)
    http.script[:] = [{'text': text, 'gap': 0.005}]
    client = live_app(url)
    body = c.to_dict()['request']
    events = parse_sse(stream(client, body).text)
    names = [e for e, _ in events]
    assert names[0] == 'start' and names[-1] == 'final' and set(names[1:-1]) == {'draft'} and len(names) >= 4
    assert events[0][1] == {'flag_id': body['flag_id'], 'request_id': body.get('request_id'), 'model': 'qwen-test',
                            'mode': 'live'}
    drafts = [d for e, d in events if e == 'draft']
    assert all(set(d) == {'call', 'tokens', 'fields'} and d['call'] == 1 for d in drafts)
    assert [d['tokens'] for d in drafts] == sorted(d['tokens'] for d in drafts)
    assert all(set(d['fields']) <= {'evidence', 'answer', 'recommended_action', 'confidence'} for d in drafts)
    full = json.loads(text)
    assert drafts[-1]['fields'].get('evidence', [{}])[0].get('claim', '') in full['evidence'][0]['claim']
    final = events[-1][1]
    plain = client.post('/v1/assess', json=body, headers={'X-API-Key': KEY})
    assert plain.status_code == 200 and http.requests[-1]['body']['stream'] is False
    assert without_latency(final['response']) == without_latency(plain.json())
    assert final['response']['path'] == 'llm' and final['response']['model'] == MODEL_PATH
    assert final['cache'] == plain.headers['X-System2-Cache'] == 'off' and final['image'] is None


def test_stream_retry_drafts_carry_call_2(llama):
    http, url = llama
    c = case()
    http.script[:] = [{'text': invalid_text(c)}, {'text': valid_text(c)}]
    events = parse_sse(stream(live_app(url), c.to_dict()['request']).text)
    calls = [d['call'] for e, d in events if e == 'draft']
    assert calls[0] == 1 and calls[-1] == 2 and calls == sorted(calls)
    assert events[-1][1]['response']['path'] == 'retry'


def test_stream_image_request_reports_the_image_header_value(llama):
    http, url = llama
    req, reply = mm()
    http.script[:] = [{'status': 500, 'body': NO_MMPROJ}, {'text': reply}]
    assessor = LiveAssessor(g.LlamaGenerator(url, model='system2', vision_model='system2-vision'), 'system2',
                            PipelineConfig(deadline_s=15.0))
    events = parse_sse(stream(TestClient(create_app(api_key=KEY, assessor=assessor)), req).text)
    assert events[0][1]['model'] == 'system2-vision'
    assert {d['call'] for e, d in events if e == 'draft'} == {2}
    assert events[-1][0] == 'final' and events[-1][1]['image'] == 'text_only'


def test_stream_cache_hit_and_insufficient_context_send_no_drafts(llama, tmp_path):
    http, url = llama
    c = case()
    http.script[:] = [{'text': valid_text(c)}]
    client = live_app(url, cache=ExplanationCache(tmp_path))
    body = c.to_dict()['request']
    first = parse_sse(stream(client, body).text)
    assert any(e == 'draft' for e, _ in first) and first[-1][1]['cache'] == 'miss'
    second = parse_sse(stream(client, body).text)
    assert [e for e, _ in second] == ['start', 'final'] and second[-1][1]['cache'] == 'hit'
    assert second[-1][1]['response'] == first[-1][1]['response']
    assert len(http.requests) == 1
    insufficient = json.loads(INSUFFICIENT.read_text())['request']
    events = parse_sse(stream(client, insufficient).text)
    assert [e for e, _ in events] == ['start', 'final']
    assert events[-1][1]['response']['path'] == 'insufficient_context' and len(http.requests) == 1


def test_stream_failure_after_start_is_a_secret_free_error_event():
    class Broken:
        model = 'qwen-test'

        def assess_image(self, body, refresh=False, on_draft=None):
            raise RuntimeError('boom http://user:pw@host:8080 Authorization: Bearer sk-secret')

    events = parse_sse(stream(TestClient(create_app(api_key=KEY, assessor=Broken())), case().to_dict()['request']).text)
    assert [e for e, _ in events] == ['start', 'error']
    assert events[-1][1] == {'detail': 'assessment failed (RuntimeError)'}


def test_stream_concurrency_limit_is_a_503_before_the_stream_and_slots_are_released():
    body = case().to_dict()['request']
    stub = TestClient(create_app(api_key=KEY)).post('/v1/assess', json=body, headers={'X-API-Key': KEY}).json()
    started, release = threading.Event(), threading.Event()

    class Blocking:
        model = 'qwen-test'

        def assess_image(self, body, refresh=False, on_draft=None):
            started.set()
            assert release.wait(10)
            return BackendAssessResponse.model_validate(stub), 'off', False, None

    assert api.STREAM_MAX_CONCURRENT == 8
    client = TestClient(create_app(api_key=KEY, assessor=Blocking(), max_streams=1))
    first = {}
    worker = threading.Thread(target=lambda: first.update(r=stream(client, body)), daemon=True)
    worker.start()
    try:
        assert started.wait(10)
        busy = stream(client, body)
        assert busy.status_code == 503 and busy.headers['content-type'] == 'application/json'
        assert busy.json() == {'detail': api.STREAM_BUSY_DETAIL}
    finally:
        release.set()
        worker.join(10)
    assert first['r'].status_code == 200 and parse_sse(first['r'].text)[-1][0] == 'final'
    again = parse_sse(stream(client, body).text)     # the finished stream gave its slot back
    assert [e for e, _ in again] == ['start', 'final']
    assert stream(TestClient(create_app(api_key=KEY, max_streams=0)), body).status_code == 503


def test_stream_keep_alive_comment_while_idle(monkeypatch):
    monkeypatch.setattr(api, 'STREAM_KEEPALIVE_S', 0.05)
    body = case().to_dict()['request']
    stub = TestClient(create_app(api_key=KEY)).post('/v1/assess', json=body, headers={'X-API-Key': KEY}).json()

    class Slow:   # no on_draft parameter: an assessor without drafts still streams start/final
        model = 'qwen-test'

        def assess_image(self, body, refresh=False):
            time.sleep(0.4)
            return BackendAssessResponse.model_validate(stub), 'off', False, None

    events = parse_sse(stream(TestClient(create_app(api_key=KEY, assessor=Slow())), body).text)
    assert ('comment', ': keep-alive') in events
    assert [e for e, _ in events if e != 'comment'] == ['start', 'final'] and events[-1][1]['response'] == stub


# ── over a real server: incremental delivery and client disconnect ───────────────────────────

@pytest.fixture
def served(llama, tmp_path):
    import uvicorn
    http, url = llama
    recorder = RunRecorder(tmp_path)
    assessor = LiveAssessor(RecordingGenerator(g.LlamaGenerator(url)), 'qwen-test', PipelineConfig(deadline_s=15.0),
                            recorder=recorder)
    server = uvicorn.Server(uvicorn.Config(create_app(api_key=KEY, assessor=assessor), host='127.0.0.1', port=0,
                                           log_level='error', lifespan='off'))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield http, port, recorder
    server.should_exit = True
    worker.join(10)


def open_stream(port, body):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=20)
    conn.request('POST', '/v1/assess/stream', body=json.dumps(body),
                 headers={'X-API-Key': KEY, 'Content-Type': 'application/json'})
    return conn, conn.getresponse()


def test_real_server_delivers_drafts_before_the_model_finishes_and_survives_disconnect(served):
    http, port, recorder = served
    c = case()
    text = valid_text(c)
    http.script[:] = [{'text': text, 'gap': 0.02}]          # ~1.5 s of streaming
    conn, resp = open_stream(port, c.to_dict()['request'])
    assert resp.status == 200
    arrivals, event = [], None
    while True:
        line = resp.readline().decode('utf-8')
        if not line:
            break
        if line.startswith('event: '):
            event = line[7:].strip()
        elif line.startswith('data: '):
            arrivals.append((time.monotonic(), event, json.loads(line[6:])))
    conn.close()
    names = [e for _, e, _ in arrivals]
    assert names[0] == 'start' and names[-1] == 'final' and names.count('draft') >= 5
    first_draft = next(t for t, e, _ in arrivals if e == 'draft')
    assert first_draft < arrivals[-1][0] - 0.5                # streamed through the key middleware, not buffered
    span = arrivals[-1][0] - arrivals[0][0]
    assert names.count('draft') <= span / 0.1 + 2              # at most one draft per 100 ms per call

    # a client that leaves after the first draft does not cancel the assessment: it finishes and is recorded
    conn, resp = open_stream(port, c.to_dict()['request'])
    while b'event: draft' not in resp.readline():
        pass
    conn.sock.close()
    conn.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(recorder.path.read_text().splitlines()) < 2:
        time.sleep(0.05)
    records = [json.loads(x) for x in recorder.path.read_text().splitlines()]
    assert len(records) == 2 and records[1]['response']['path'] == 'llm'
    assert records[1]['calls'][0]['text'] == text
