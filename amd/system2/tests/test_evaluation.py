"""One reproducible template/model evaluation harness over the approved saved packets (plan 02-04).

Every CLI test runs the real scripts/evaluate.py in a subprocess. The model runner talks to a real
ThreadingHTTPServer on 127.0.0.1 that imitates llama-server's /v1/chat/completions. No model, GPU or llama.cpp
process is involved, so every number asserted here is a regression check of the harness, never a measurement.
"""
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SYS2 = ROOT / 'amd/system2'
sys.path.insert(0, str(SYS2))
DATA = SYS2 / 'data/dev_packets.json'
SCRIPT = SYS2 / 'scripts/evaluate.py'

from analyst.dataset import dataset_manifest, load_dataset, validate_dataset  # noqa: E402
from analyst.prompts import PROMPT_VERSION, example_output  # noqa: E402

CASES = load_dataset(DATA)
CASE_IDS = sorted(c.case_id for c in CASES)
SUFFICIENT = sorted(c.case_id for c in CASES if not c.prepared.insufficient_context)
ANSWERS = {c.prepared.question: example_output(c.prepared) for c in CASES if not c.prepared.insufficient_context}
NULL0 = {'value': None, 'count': 0, 'denominator': 0}


def rate(k, n):
    return {'value': k / n, 'count': k, 'denominator': n}


def _reject(name):
    raise AssertionError('non-finite JSON constant %s in artifact' % name)


def evaluate(*args, timeout=180):
    assert SCRIPT.exists(), 'scripts/evaluate.py (template/model evaluation CLI) must exist'
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=timeout,
                          cwd=str(SYS2))


def metrics(out: Path) -> dict:
    return json.loads((out / 'metrics.json').read_text(encoding='utf-8'), parse_constant=_reject)


def records(out: Path) -> list:
    lines = (out / 'cases.jsonl').read_text(encoding='utf-8').splitlines()
    return [json.loads(line, parse_constant=_reject) for line in lines if line.strip()]


def by_key(rows):
    return {(r['runner'], r['case_id']): r for r in rows}


def write_dataset(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def approved(cases) -> dict:
    """Re-approve a subset/modified case list through the real validator and manifest."""
    checked = validate_dataset({'cases': cases})
    return {'cases': [c.to_dict() for c in checked], 'manifest': dataset_manifest(checked)}


def raw_dataset() -> dict:
    return json.loads(DATA.read_text(encoding='utf-8'))


# ── fake llama-server ────────────────────────────────────────────────────────────────────────

def envelope(content, finish='stop', completion=120, prompt=900, speed=35.6, model='qwen-fake'):
    return {'model': model, 'choices': [{'index': 0, 'finish_reason': finish,
                                         'message': {'role': 'assistant', 'content': content}}],
            'usage': {'prompt_tokens': prompt, 'completion_tokens': completion},
            'timings': {'predicted_per_second': speed, 'prompt_n': prompt, 'predicted_n': completion}}


def current_packet(body):
    text = body['messages'][-1]['content']
    return json.loads(text.split('\n')[1])


def good(body):
    return {'body': envelope(json.dumps(ANSWERS[current_packet(body)['question']]))}


def ungrounded(body):
    """Schema-valid, packet-cited, but a number the cited row never states."""
    ans = copy.deepcopy(ANSWERS[current_packet(body)['question']])
    ans['evidence'][0]['claim'] += '; value=987654.5'
    return {'body': envelope(json.dumps(ans))}


MODES = {
    'good': good,
    'ungrounded': ungrounded,
    'protocol': lambda body: {'body': b'this is not a chat completion'},
    'unparseable': lambda body: {'body': envelope('{"evidence": [')},
    'http500': lambda body: {'status': 500, 'body': {'error': 'boom'}},
    'slow': lambda body: dict(good(body), delay=2.0),
}


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            body = json.loads(raw)
            self.server.requests.append({'path': self.path, 'raw': raw.decode('utf-8'), 'body': body})
            step = MODES[self.server.mode](body)
            time.sleep(step.get('delay', 0))
            payload = step['body']
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
            try:
                self.send_response(step.get('status', 200))
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
    http.requests, http.mode = [], 'good'
    worker = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    worker.start()
    yield http, 'http://127.0.0.1:%d' % http.server_port
    http.shutdown()
    http.server_close()
    worker.join()


def run_model(url, out, *extra, runner='model'):
    return evaluate('--runner', runner, '--model-url', url, '--model', 'qwen-fake', '--out-dir', str(out),
                    '--label', 'fake-http-regression', *extra)


# ── tracer: identical saved packets through template and HTTP model ─────────────────────────

def test_both_runners_score_identical_six_cases_over_http(server, tmp_path):
    http, url = server
    out = tmp_path / 'out'
    r = run_model(url, out, runner='both')
    assert r.returncode == 0, r.stderr
    m, rows = metrics(out), records(out)
    by = by_key(rows)
    assert sorted(m['runners']) == ['model', 'template']
    assert len(rows) == 12 and (out / 'report.md').read_text(encoding='utf-8').strip()
    assert m['comparison']['case_ids'] == CASE_IDS and m['comparison']['packet_hashes_match'] is True
    for cid in CASE_IDS:
        t, mo = by[('template', cid)], by[('model', cid)]
        assert t['packet_hash'] == mo['packet_hash'] and t['request_hash'] == mo['request_hash']
        assert t['split'] == mo['split'] == 'train'
    ident = m['identity']
    assert ident['dataset']['content_hash'] == raw_dataset()['manifest']['content_hash']
    assert ident['dataset']['file_sha256'] == hashlib.sha256(DATA.read_bytes()).hexdigest()
    assert ident['dataset']['selected_case_ids'] == CASE_IDS and ident['dataset']['case_count'] == 6
    assert ident['config']['model'] == 'qwen-fake' and ident['config']['max_tokens'] == 512
    assert ident['prompt']['prompt_version'] == PROMPT_VERSION and ident['prompt']['prompt_sha256s']
    assert isinstance(ident['identity_hash'], str) and len(ident['identity_hash']) == 64
    assert m['scope']['split'] == 'train' and m['scope']['headline_eligible'] is False
    assert 'not entailment' in m['scope']['grounding_label']
    meas = m['measurement']
    assert meas['label'] == 'fake-http-regression' and meas['endpoint_loopback'] is True
    assert meas['latency_quantile_method'] == 'nearest-rank' and meas['latency_unit'] == 'ms'
    assert meas['started_at'] <= meas['finished_at']

    t, mo = m['runners']['template'], m['runners']['model']
    assert t['kind'] == 'template' and mo['kind'] == 'model'
    assert t['cases'] == mo['cases'] == 6 and t['sufficient_cases'] == mo['sufficient_cases'] == 5
    assert t['public_response_valid'] == mo['public_response_valid'] == rate(6, 6)
    assert t['guarded_insufficient'] == mo['guarded_insufficient'] == {'cases': 1, 'safe_manual_review': rate(1, 1)}
    assert t['paths'] == {'llm': 0, 'retry': 0, 'fallback': 5, 'insufficient_context': 1}
    assert mo['paths'] == {'llm': 5, 'retry': 0, 'fallback': 0, 'insufficient_context': 1}
    for key in ('schema_valid', 'citation_valid', 'numeric_consistent', 'name_consistent'):
        assert t['final'][key] == mo['final'][key] == rate(5, 5)
        assert mo['first_attempt'][key] == rate(5, 5)
        assert t['first_attempt'][key] == NULL0   # the template makes no model attempt
    gen = mo['generation']
    assert gen['cases'] == 5 and gen['attempted_outputs'] == 5
    assert gen['model_accepted'] == gen['first_attempt_accepted'] == rate(5, 5)
    assert gen['retry'] == gen['fallback'] == rate(0, 5)
    assert t['generation']['model_accepted'] == t['generation']['fallback'] == NULL0
    assert t['generation']['attempted_outputs'] == 0
    for agg in (t, mo):
        assert agg['action']['metric_kind'] == 'agreement_with_validator'
        assert agg['action']['reference_origin'] == 'none'
        assert agg['action']['action_quality'] == NULL0
        assert agg['action']['agreement_with_validator'] == rate(6, 6)
        assert agg['latency_ms']['n'] == 6 and agg['latency_ms']['unit'] == 'ms'
        assert agg['latency_ms']['p50'] <= agg['latency_ms']['p95'] <= agg['latency_ms']['max']
    assert mo['tokens']['completion_tokens'] == 600 and mo['tokens']['prompt_tokens'] == 4500
    assert mo['tokens']['tokens_per_sec_mean'] == {'value': 35.6, 'n': 5}
    assert t['tokens']['completion_tokens'] is None and t['tokens']['tokens_per_sec_mean'] == {'value': None, 'n': 0}

    eq = by[('model', 'kit-equipment')]
    assert eq['path'] == 'llm' and eq['model'] == 'qwen-fake' and eq['attempt_count'] == 1
    assert eq['first_attempt']['status'] == 'accepted' and eq['public_valid'] is True
    assert eq['prompt']['prompt_version'] == PROMPT_VERSION
    assert len(eq['prompt']['example_case_ids']) == 3 and 'kit-equipment' not in eq['prompt']['example_case_ids']
    te = by[('template', 'kit-equipment')]
    assert te['path'] == 'fallback' and te['model'] == 'template' and te['attempts'] == []
    assert te['first_attempt'] is None and te['prompt'] is None
    ins = by[('model', 'kit-insufficient_context')]
    assert ins['path'] == 'insufficient_context' and ins['attempt_count'] == 0 and ins['packet_insufficient'] is True
    assert len(http.requests) == 5
    for req in http.requests:
        assert req['path'] == '/v1/chat/completions'
        assert req['body']['temperature'] == 0 and req['body']['max_tokens'] == 512


def test_template_runner_is_offline_and_reproducible(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    assert evaluate('--runner', 'template', '--out-dir', str(a)).returncode == 0
    assert evaluate('--runner', 'template', '--out-dir', str(b)).returncode == 0
    ma, mb = metrics(a), metrics(b)
    assert ma['identity'] == mb['identity']           # schema/config/data identity is reproducible
    assert ma['identity']['prompt'] is None and ma['identity']['config']['model_url'] is None
    assert ma['measurement']['endpoint_loopback'] is None
    strip = lambda rows: [{k: v for k, v in r.items() if k != 'latency_ms'} for r in rows]  # noqa: E731
    assert strip(records(a)) == strip(records(b))   # timing itself may vary; outcomes do not
    assert list(ma['runners']) == ['template']


# ── failures stay in denominators; safe outputs stay distinguishable from model attempts ─────

def test_broken_model_keeps_failures_in_denominators(server, tmp_path):
    http, url = server
    http.mode = 'ungrounded'
    out = tmp_path / 'out'
    r = run_model(url, out)
    assert r.returncode == 0, r.stderr      # a completed run with model failures still publishes metrics
    m = metrics(out)['runners']['model']
    gen = m['generation']
    assert gen['cases'] == 5 and gen['attempted_outputs'] == 10
    assert gen['model_accepted'] == gen['first_attempt_accepted'] == rate(0, 5)
    assert gen['retry'] == gen['fallback'] == rate(5, 5)
    assert gen['attempt_statuses'] == {'rejected': 10}
    first = m['first_attempt']
    assert first['parsed'] == first['schema_valid'] == first['citation_valid'] == first['name_consistent'] == rate(5, 5)
    assert first['numeric_consistent'] == rate(0, 5) and first['accepted'] == rate(0, 5)
    assert m['final']['numeric_consistent'] == rate(5, 5)   # the safe fallback is checked, not the model reply
    assert m['paths'] == {'llm': 0, 'retry': 0, 'fallback': 5, 'insufficient_context': 1}
    assert m['public_response_valid'] == rate(6, 6)
    row = by_key(records(out))[('model', 'kit-equipment')]
    assert row['path'] == 'fallback' and row['model'] == 'template' and row['fallback'] is True and row['retry'] is True
    assert [a['status'] for a in row['attempts']] == ['rejected', 'rejected']
    assert any(e['code'] == 'ungrounded_number' for e in row['attempts'][0]['errors'])
    assert row['first_attempt']['numeric_consistent'] is False and row['final']['numeric_consistent'] is True


@pytest.mark.parametrize('mode,status', [('protocol', 'transport_error'), ('http500', 'transport_error'),
                                         ('unparseable', 'parse_error')])
def test_unusable_model_reply_is_a_failed_raw_attempt(server, tmp_path, mode, status):
    http, url = server
    http.mode = mode
    out = tmp_path / 'out'
    r = run_model(url, out)
    assert r.returncode == 0, r.stderr
    m = metrics(out)['runners']['model']
    assert m['generation']['attempt_statuses'] == {status: 10}
    for key in ('parsed', 'schema_valid', 'citation_valid', 'numeric_consistent', 'name_consistent', 'accepted'):
        assert m['first_attempt'][key] == rate(0, 5)
    assert m['generation']['fallback'] == rate(5, 5) and m['public_response_valid'] == rate(6, 6)
    # A transport failure carries no usage; an unparseable completion still reports the server's own throughput.
    speed = {'value': None, 'n': 0} if status == 'transport_error' else {'value': 35.6, 'n': 10}
    assert m['tokens']['tokens_per_sec_mean'] == speed


def test_slow_model_times_out_then_safe_fallback(server, tmp_path):
    http, url = server
    http.mode = 'slow'
    out = tmp_path / 'out'
    r = run_model(url, out, '--deadline-s', '0.4', '--timeout-s', '0.4')
    assert r.returncode == 0, r.stderr
    m = metrics(out)
    assert m['identity']['config']['deadline_s'] == 0.4 and m['identity']['config']['timeout_s'] == 0.4
    agg = m['runners']['model']
    assert agg['generation']['model_accepted'] == rate(0, 5) and agg['generation']['fallback'] == rate(5, 5)
    assert set(agg['generation']['attempt_statuses']) == {'timeout'}
    for row in records(out):
        if not row['packet_insufficient']:
            assert row['path'] == 'fallback' and row['public_valid'] is True
            assert row['latency_ms'] >= 300     # latency covers the failed attempt and the fallback


# ── honest denominators ──────────────────────────────────────────────────────────────────────

def test_no_eligible_denominator_reports_null_with_count_zero(server, tmp_path):
    http, url = server
    only = [c for c in raw_dataset()['cases'] if c['case_id'] == 'kit-insufficient_context']
    path = write_dataset(tmp_path / 'insufficient.json', approved(only))
    out = tmp_path / 'out'
    r = run_model(url, out, '--dataset', str(path))
    assert r.returncode == 0, r.stderr
    m = metrics(out)['runners']['model']
    assert m['cases'] == 1 and m['sufficient_cases'] == 0
    assert m['generation']['cases'] == 0 and m['generation']['attempted_outputs'] == 0
    for key in ('model_accepted', 'first_attempt_accepted', 'retry', 'fallback'):
        assert m['generation'][key] == NULL0
    for key in ('schema_valid', 'citation_valid', 'numeric_consistent', 'name_consistent'):
        assert m['final'][key] == NULL0 and m['first_attempt'][key] == NULL0
    assert m['guarded_insufficient'] == {'cases': 1, 'safe_manual_review': rate(1, 1)}
    assert http.requests == []


def test_single_case_dataset(server, tmp_path):
    http, url = server
    only = [c for c in raw_dataset()['cases'] if c['case_id'] == 'kit-equipment']
    path = write_dataset(tmp_path / 'one.json', approved(only))
    out = tmp_path / 'out'
    r = run_model(url, out, '--dataset', str(path), runner='both')
    assert r.returncode == 0, r.stderr
    m = metrics(out)
    assert m['comparison']['case_ids'] == ['kit-equipment']
    for agg in m['runners'].values():
        lat = agg['latency_ms']
        assert lat['n'] == 1 and lat['p50'] == lat['p95'] == lat['max'] == lat['mean']
    assert m['runners']['model']['generation']['model_accepted'] == rate(1, 1)
    assert len(http.requests) == 1


def test_limit_and_seed_select_a_stable_subset(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    assert evaluate('--runner', 'template', '--limit', '2', '--seed', '7', '--out-dir', str(a)).returncode == 0
    assert evaluate('--runner', 'template', '--limit', '2', '--seed', '7', '--out-dir', str(b)).returncode == 0
    ids = metrics(a)['identity']['dataset']['selected_case_ids']
    assert len(ids) == 2 and ids == sorted(ids) and set(ids) <= set(CASE_IDS)
    assert metrics(b)['identity']['dataset']['selected_case_ids'] == ids
    assert metrics(a)['identity']['config']['seed'] == 7 and metrics(a)['identity']['config']['limit'] == 2
    assert metrics(a)['identity']['dataset']['case_count'] == 6


# ── provenance and leakage are rejected before any model call ────────────────────────────────

def _case(value, case_id):
    return next(c for c in value['cases'] if c['case_id'] == case_id)


def _future(v):
    _case(v, 'kit-equipment')['as_of'] = '2026-09-01T18:00:00'


def _label(v):
    _case(v, 'kit-equipment')['request']['context']['expected_action'] = 'DISPATCH_TECHNICIAN'


def _prior(v):
    _case(v, 'kit-equipment')['request']['context']['previous_output'] = 'LEAKMARKER-prior-answer'


def _edited(v):
    fact = _case(v, 'kit-equipment')['request']['context']['facts'][0]
    fact['fact'] = fact['fact'].replace('status=', 'status=LEAKMARKER')


def _holdout(v):
    _case(v, 'kit-equipment')['mission_id'] = 'M006'


def _manifest(v):
    v['manifest']['content_hash'] = '0' * 64


def _no_manifest(v):
    v.pop('manifest')


def _empty(v):
    v['cases'] = []


@pytest.mark.parametrize('tamper', [_future, _label, _prior, _edited, _holdout, _manifest, _no_manifest, _empty],
                         ids=lambda f: f.__name__.strip('_'))
def test_tampered_or_leaking_dataset_rejected_before_any_call(server, tmp_path, tamper):
    http, url = server
    value = raw_dataset()
    tamper(value)
    path = write_dataset(tmp_path / 'bad.json', value)
    out = tmp_path / 'out'
    r = run_model(url, out, '--dataset', str(path), runner='both')
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert not (out / 'metrics.json').exists() and not (out / 'cases.jsonl').exists()
    assert http.requests == []
    assert 'LEAKMARKER' not in r.stderr + r.stdout


def test_unreadable_dataset_is_a_data_failure(tmp_path):
    path = tmp_path / 'garbage.json'
    path.write_text('{not json', encoding='utf-8')
    r = evaluate('--runner', 'template', '--dataset', str(path), '--out-dir', str(tmp_path / 'out'))
    assert r.returncode == 1 and not (tmp_path / 'out' / 'metrics.json').exists()


# ── independent references score action quality, outside every prompt ──────────────────────

def with_references(refs: dict) -> dict:
    cases = copy.deepcopy(raw_dataset()['cases'])
    for c in cases:
        if c['case_id'] in refs:
            c['reference'] = refs[c['case_id']]
    return approved(cases)


def test_independent_reference_scores_action_quality_outside_prompt(server, tmp_path):
    http, url = server
    value = with_references({
        'kit-equipment': {'expected_action': 'SCHEDULE_MAINTENANCE', 'source': 'operator-label-QZX7'},
        'kit-mission': {'expected_action': 'ABORT_MISSION', 'source': 'operator-label-QZX7'}})
    path = write_dataset(tmp_path / 'refs.json', value)
    out = tmp_path / 'out'
    r = run_model(url, out, '--dataset', str(path), runner='both')
    assert r.returncode == 0, r.stderr
    m = metrics(out)
    for agg in m['runners'].values():
        act = agg['action']
        assert act['metric_kind'] == 'mixed' and act['reference_origin'] == 'independent'
        assert act['action_quality'] == rate(1, 2)
        assert act['agreement_with_validator'] == rate(6, 6)
    row = by_key(records(out))[('model', 'kit-mission')]
    assert row['action']['reference_action'] == 'ABORT_MISSION' and row['action']['matches_reference'] is False
    assert row['action']['validator_suggested'] == 'REVIEW_MISSION' and row['action']['agrees_with_validator'] is True
    assert len(http.requests) == 5
    for req in http.requests:
        assert 'QZX7' not in req['raw'] and 'expected_action' not in req['raw'] and 'reference' not in req['raw']


@pytest.mark.parametrize('ref', [{'expected_action': 'SCHEDULE_MAINTENANCE', 'source': 'validator'},
                                 {'expected_action': 'NOT_AN_ACTION', 'source': 'operator'},
                                 {'expected_action': 'SCHEDULE_MAINTENANCE'},
                                 'SCHEDULE_MAINTENANCE'])
def test_validator_derived_or_malformed_reference_rejected(tmp_path, ref):
    path = write_dataset(tmp_path / 'refs.json', with_references({'kit-equipment': ref}))
    r = evaluate('--runner', 'template', '--dataset', str(path), '--out-dir', str(tmp_path / 'out'))
    assert r.returncode == 1 and not (tmp_path / 'out' / 'metrics.json').exists()


# ── atomic artifacts and setup errors ────────────────────────────────────────────────────────

def test_atomic_output_and_failed_run_keeps_previous_artifact(tmp_path):
    out = tmp_path / 'out'
    assert evaluate('--runner', 'template', '--out-dir', str(out)).returncode == 0
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    assert set(before) == {'metrics.json', 'cases.jsonl', 'report.md'}
    bad = raw_dataset()
    _label(bad)
    r = evaluate('--runner', 'template', '--dataset', str(write_dataset(tmp_path / 'bad.json', bad)),
                 '--out-dir', str(out))
    assert r.returncode == 1
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before   # nothing replaced, no temp files left


@pytest.mark.parametrize('failing', ['cases.jsonl', 'report.md', 'metrics.json'])
def test_partial_write_never_leaves_stale_metrics_beside_new_artifacts(tmp_path, monkeypatch, failing):
    """metrics.json present => cases.jsonl and report.md are from the same completed run."""
    from analyst import evaluation as ev
    out = tmp_path / 'out'
    dataset = ev.load_eval_dataset(DATA)
    run1 = ev.run_evaluation(dataset, [ev.TemplateRunner()], ev.EvalConfig(label='run1'))
    ev.write_artifacts(out, run1)
    assert metrics(out)['measurement']['label'] == 'run1'
    run2 = ev.run_evaluation(dataset, [ev.TemplateRunner()], ev.EvalConfig(label='run2', limit=2))
    real = ev.os.replace

    def flaky(src, dst):
        if Path(dst).name == failing:
            raise OSError(28, 'No space left on device')
        return real(src, dst)

    monkeypatch.setattr(ev.os, 'replace', flaky)
    with pytest.raises(OSError):
        ev.write_artifacts(out, run2)
    assert not (out / 'metrics.json').exists()
    assert not [p for p in out.iterdir() if p.name.endswith('.tmp')]


@pytest.mark.parametrize('stage', ['runner setup', 'evaluation run'])
def test_post_dataset_failures_are_not_reported_as_dataset_rejection(tmp_path, monkeypatch, capsys, stage):
    """Errors after the dataset passed get their own exit code and class name, never 'dataset rejected'."""
    spec = importlib.util.spec_from_file_location('evaluate_cli', SCRIPT)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    def boom(*args, **kwargs):
        raise ValueError('runner rnr saw a different packet for SECRET-CAPTURE-CONTENT')

    if stage == 'runner setup':
        monkeypatch.setattr(cli, 'LlamaGenerator', boom)
        argv = ['--runner', 'model', '--model-url', 'http://127.0.0.1:9', '--model', 'qwen-fake']
    else:
        monkeypatch.setattr(cli, 'run_evaluation', boom)
        argv = ['--runner', 'template']
    out = tmp_path / 'out'
    assert cli.main(argv + ['--out-dir', str(out), '--quiet']) == 3
    err = capsys.readouterr().err
    assert 'dataset rejected' not in err and stage in err and 'ValueError' in err
    assert 'SECRET-CAPTURE-CONTENT' not in err and not out.exists()


def test_model_runner_without_url_is_a_usage_error(tmp_path):
    r = evaluate('--runner', 'model', '--model', 'qwen-fake', '--out-dir', str(tmp_path / 'out'))
    assert r.returncode == 2 and not (tmp_path / 'out').exists()


def test_identity_records_entity_catalog_source_and_hash(tmp_path):
    """Kit alias edits change name checks, so they must change the run identity (code_revision misses them)."""
    import shutil
    from analyst import evaluation as ev
    from analyst.grounding import load_entity_catalog
    run = ev.run_evaluation(ev.load_eval_dataset(DATA), [ev.TemplateRunner()], ev.EvalConfig(limit=1))
    cat = run['identity']['catalog']
    assert cat['source'] == 'amd/system1/data/kit' and cat['entity_count'] > 0 and len(cat['content_hash']) == 64
    kit = tmp_path / 'kit'
    shutil.copytree(ROOT / 'amd/system1/data/kit', kit)
    assets = kit / 'assets.csv'
    lines = assets.read_text(encoding='utf-8').splitlines(keepends=True)
    head, first = lines[0].rstrip('\n').split(','), lines[1].split(',')
    first[head.index('asset_name')] = 'Renamed Pump'
    assets.write_text(lines[0] + ','.join(first) + ''.join(lines[2:]), encoding='utf-8')
    edited = ev.catalog_identity(load_entity_catalog(kit))
    assert edited['content_hash'] != cat['content_hash'] and edited['source'] == str(kit.resolve())


def test_out_of_bounds_model_output_is_not_schema_valid_or_accepted():
    """Strict generation bounds count in the harness: 6 duplicate claims are a schema failure, never model success."""
    from analyst import evaluation as ev
    from analyst.pipeline import Generation

    def bloated(packet, errors=(), timeout_s=None):
        out = copy.deepcopy(ANSWERS[packet.question])
        out['evidence'] = out['evidence'][:1] * 6
        out['answer'] = out['evidence'][0]['claim']
        return Generation(json.dumps(out), 'qwen-fake', 'stop')

    cases = tuple(c for c in CASES if c.case_id == 'kit-equipment')
    result = ev.evaluate_cases(cases, [ev.ModelRunner(bloated)], ev.EvalConfig())
    row = result['records'][0]
    assert row['first_attempt']['parsed'] and not row['first_attempt']['schema_valid']
    assert row['path'] == 'fallback' and row['fallback']
    agg = result['runners']['model']
    assert agg['first_attempt']['schema_valid'] == rate(0, 1)
    assert agg['generation']['model_accepted'] == rate(0, 1)


def test_evaluate_cases_rejects_runners_that_see_different_packets():
    assert importlib.util.find_spec('analyst.evaluation'), 'analyst/evaluation.py must exist'
    from analyst import evaluation as ev

    class Tampering(ev.TemplateRunner):
        name = 'tampering'

        def run(self, request, config):
            request = copy.deepcopy(request)
            request['context']['facts'] = request['context']['facts'][:1]
            return super().run(request, config)

    cases = tuple(c for c in CASES if c.case_id == 'kit-equipment')
    with pytest.raises(ValueError, match='packet'):
        ev.evaluate_cases(cases, [ev.TemplateRunner(), Tampering()], ev.EvalConfig())


# ── documented offline workflow and phase invariants (task 2) ────────────────────────────────

README = SYS2 / 'README.md'
UV = 'uv run --no-project --with-requirements requirements.txt python '
PHASE_INVARIANTS = {   # TEST-01: grounding, output schema, packet leakage, no holdout few-shots, eval harness
    'test_dataset_template.py': ('test_recursive_leakage_rejected', 'test_bad_provenance_and_future_facts_rejected',
                                 'test_holdouts_match_system1_default_fold'),
    'test_save_packets.py': ('test_no_holdout_override_and_seed_import_exclusive',),
    'test_grounding.py': ('test_saved_request_template_passes_and_mutation_fails',
                          'test_schema_ids_and_actions_fail_deterministically'),
    'test_pipeline.py': ('test_two_failures_return_checked_template_fallback',
                         'test_insufficient_packet_short_circuits_without_generator_calls'),
    'test_generation.py': ('test_fewshot_loader_rejects_holdout_dataset',
                           'test_current_dev_case_never_appears_as_its_own_fewshot',
                           'test_schema_enums_are_current_packet_only_never_fewshot_ids'),
    'test_generation_contract.py': ('test_inconsistent_path_or_insufficiency_rejected_by_model_and_schema',
                                    'test_schema_drift_check_passes'),
    'test_evaluation.py': ('test_both_runners_score_identical_six_cases_over_http',
                           'test_tampered_or_leaking_dataset_rejected_before_any_call'),
}


def documented(script: str) -> list:
    """Every README command line of the form `uv run ... python scripts/<script> ...`, as argument lists."""
    import shlex
    found = []
    for line in README.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line.startswith(UV + 'scripts/' + script):
            found.append(shlex.split(line[len(UV):])[1:])
    assert found, 'README must document `%sscripts/%s ...`' % (UV, script)
    return found


def override(args: list, flag: str, value: str) -> list:
    out, skip = [], False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == flag:
            skip = True
            continue
        out.append(a)
    return out + [flag, value]


def test_phase_invariant_suites_collect():
    files = [str(SYS2 / 'tests' / name) for name in PHASE_INVARIANTS]
    r = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '--collect-only', '-q', *files],
                       capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    nodes = [line.split('::', 1) for line in r.stdout.splitlines() if '::' in line]
    for name, required in PHASE_INVARIANTS.items():
        tests = {t.split('[')[0] for f, t in nodes if f.endswith('/' + name)}
        assert tests, '%s collected no tests' % name
        missing = [t for t in required if t not in tests]
        assert not missing, '%s lost invariant tests %s' % (name, missing)


def test_documented_seed_command_reproduces_checked_in_packets(tmp_path):
    seeds = [a for a in documented('save_packets.py') if '--seed' in a]
    assert seeds, 'README must document save_packets.py --seed'
    out = tmp_path / 'dev_packets.json'
    script = str(SYS2 / 'scripts/save_packets.py')
    r = subprocess.run([sys.executable, script, *override(seeds[0], '--output', str(out))],
                       capture_output=True, text=True, timeout=120, cwd=str(SYS2))
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() == DATA.read_bytes()


def test_documented_template_command_runs_offline(tmp_path):
    cmds = [a for a in documented('evaluate.py') if a[a.index('--runner') + 1] == 'template']
    assert cmds, 'README must document the template-only evaluate.py command'
    args = cmds[0]
    assert '--model-url' not in args and '--dataset' not in args      # default approved data, no network
    out = tmp_path / 'template'
    r = evaluate(*override(args, '--out-dir', str(out)))
    assert r.returncode == 0, r.stderr
    m = metrics(out)
    assert list(m['runners']) == ['template'] and m['measurement']['endpoint_loopback'] is None
    assert m['identity']['dataset']['content_hash'] == raw_dataset()['manifest']['content_hash']


def test_documented_model_command_uses_approved_data_and_common_packets(server, tmp_path):
    http, url = server
    cmds = [a for a in documented('evaluate.py') if '--model-url' in a]
    assert cmds, 'README must document an evaluate.py command with --model-url'
    args = cmds[0]
    assert args[args.index('--runner') + 1] == 'both'
    assert '--dataset' not in args or args[args.index('--dataset') + 1] == 'data/dev_packets.json'
    out = tmp_path / 'model'
    r = evaluate(*override(override(args, '--model-url', url), '--out-dir', str(out)))   # fake HTTP, not a measurement
    assert r.returncode == 0, r.stderr
    m = metrics(out)
    assert m['comparison']['packet_hashes_match'] is True and m['comparison']['case_ids'] == CASE_IDS
    by = by_key(records(out))
    assert all(by[('template', c)]['packet_hash'] == by[('model', c)]['packet_hash'] for c in CASE_IDS)
    assert len(http.requests) == len(SUFFICIENT)
