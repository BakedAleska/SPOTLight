"""Live /v1/assess: the checked pipeline behind the HTTP API, plus the explanation cache (Phase 3, SRV-01..03).

The API stays a stub until SYSTEM2_LLAMA_URL is set (assessor_from_env returns None, so the stub path and its tests
are untouched). When it is set, LiveAssessor runs the same run_pipeline the offline eval harness scores: at most
two LlamaGenerator attempts inside one 15 s deadline (25 s, SYSTEM2_IMAGE_DEADLINE_S, for requests with an
image), then the checked template (path 'fallback', model 'template'), so the live endpoint can never return an
unchecked model answer. Insufficient packets short-circuit
before any generation. to_response() maps an AssessmentRun to the flat BackendAssessResponse exactly the way
evaluation._public does, so the numbers the backend logs to app.model_calls (model, latency_ms, tokens_per_sec)
are the same fields the eval measured.

Streaming drafts: assess_image(..., on_draft=cb) runs the same pipeline inside generator.drafts(cb) when the
generator has it (LlamaGenerator, also through RecordingGenerator), so cb(call, tokens, text) sees the model's text
while it streams. Nothing else changes: the checked result, cache, recorder and probe are those of a plain call, and
cache hits and insufficient packets never call the model, so they produce no drafts.

Cache (SRV-02): one JSON file per key (flag_id, packet_hash, model, prompt_ver). packet_hash is
evaluation.packet_hash, the same hash the eval harness gates on, so a changed fact regenerates; prompt_ver folds in
the system prompt + few-shot pool hash, so a prompt change regenerates. File names are the sha256 of the canonical
key, never caller strings (no path traversal). Only llm/retry results are stored (fallback and insufficient_context
stay non-final, like the backend's TEMPLATE rows), and a stored entry is served only after it re-validates and
re-passes check_assessment against the current packet; anything else is a miss and is overwritten. The API and
scripts/precompute.py share this one module, so they cannot disagree on the key. Cached explanations never feed
prompts or eval datasets (prompts.py and evaluation.py do not read the cache).

The entity catalog is loaded once at construction (PipelineConfig.catalog), never per request. Configuration
errors become RuntimeError with the URL shown only through evaluation.endpoint() (no credentials or query); no
message here ever contains the X-API-Key. This module does not print.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable, Mapping, Optional, Tuple

from pydantic import ValidationError

from .backend_contract import MODEL_PATHS, BackendAssessRequest, BackendAssessResponse
from .evaluation import endpoint, packet_hash
from .grounding import EntityCatalog, GENERATION_FIELDS, check_assessment, default_catalog
from .packet import PreparedPacket, normalize_packet
from .paths import repo_root
from .pipeline import AssessmentRun, GenerationError, PipelineConfig, run_pipeline
from .prompts import PROMPT_VERSION, prompt_identity

DEADLINE_S = 15.0   # live-explain p95 target (PROJECT constraint); shared by both attempts
IMAGE_DEADLINE_S = 25.0   # requests with image_b64 (SYSTEM2_IMAGE_DEADLINE_S); text-only resend inside it (A-6)
IMAGE_DEADLINE_ENV = 'SYSTEM2_IMAGE_DEADLINE_S'
DEADLINE_ENV = 'SYSTEM2_DEADLINE_S'   # text requests; default DEADLINE_S (raise only for slow local models)
URL_ENV, MODEL_ENV, CACHE_ENV = 'SYSTEM2_LLAMA_URL', 'SYSTEM2_MODEL', 'SYSTEM2_CACHE_DIR'
RECORD_ENV = 'SYSTEM2_RECORD_DIR'   # opt-in: one JSON line per request with every attempt's raw text (measurement runs)
RECORD_FILE = 'system2-records.jsonl'
LLAMA_KEY_ENV = 'SYSTEM2_LLAMA_API_KEY'   # Bearer key for a gateway in front of llama-server (never logged)
VISION_MODEL_ENV = 'SYSTEM2_VISION_MODEL'   # model for requests with an image; default SYSTEM2_MODEL (backend.2)
IMAGE_USED, IMAGE_TEXT_ONLY, IMAGE_NOT_USED = 'used', 'text_only', 'not_used'   # X-System2-Image values
KEY_FIELDS = ('flag_id', 'packet_hash', 'model', 'prompt_ver')
PROBE_ENV, PROBE_TIMEOUT_ENV = 'SYSTEM2_MODEL_PROBE_S', 'SYSTEM2_MODEL_PROBE_TIMEOUT_S'
PROBE_S, PROBE_TIMEOUT_S = 30.0, 15.0   # /health model_status: check every 30 s, give up on one check after 15 s
ANSWERED = frozenset({'accepted', 'rejected', 'parse_error', 'truncated', 'oversized'})   # the model replied
LOG = logging.getLogger('system2.live')


def to_response(run: AssessmentRun, flag_id: int) -> BackendAssessResponse:
    """Flat public response for one pipeline run; a validation failure here is a bug and raises."""
    public = dict(run.assessment, flag_id=flag_id, model=run.model, latency_ms=run.latency_ms,
                  tokens_per_sec=run.tokens_per_sec, path=run.path)
    return BackendAssessResponse.model_validate(public)


# ── cache ────────────────────────────────────────────────────────────────────────────────────

def cache_key(flag_id: int, packet_hash: str, model: str, prompt_ver: str) -> str:
    """sha256 hex of the canonical key JSON; also the cache file stem."""
    fields = {'flag_id': flag_id, 'packet_hash': packet_hash, 'model': model, 'prompt_ver': prompt_ver}
    raw = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def prompt_version(generator) -> str:
    """PROMPT_VERSION plus the first 16 hex of the system prompt + few-shot pool hash when the generator has one."""
    examples = getattr(generator, 'examples', None)
    if examples is None:
        return PROMPT_VERSION
    return '%s:%s' % (PROMPT_VERSION, prompt_identity(examples)['prompt_sha256'][:16])


def cache_dir_from_env(environ: Mapping[str, str] = os.environ) -> Optional[Path]:
    """SYSTEM2_CACHE_DIR ('off' disables the cache), else amd/system2/cache in a checkout, else None (the image has
    no writable default). Measurement runs set 'off' so every request is a genuine generation."""
    env = (environ.get(CACHE_ENV) or '').strip()
    if env.lower() == 'off':
        return None
    if env:
        return Path(env)
    root = repo_root()
    return root / 'amd' / 'system2' / 'cache' if root is not None else None


class ExplanationCache:
    """File cache of checked llm/retry responses; see the module docstring for what is stored and served."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, key_fields: dict) -> Path:
        return self.root / ('%s.json' % cache_key(*(key_fields[k] for k in KEY_FIELDS)))

    def get(self, key_fields: dict, prepared: PreparedPacket,
            catalog: Optional[EntityCatalog] = None) -> Optional[BackendAssessResponse]:
        """The stored response, or None for missing, unreadable, mismatched, invalid or no-longer-grounded entries."""
        try:
            with open(self.path(key_fields), encoding='utf-8') as f:
                entry = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or entry.get('key') != {k: key_fields[k] for k in KEY_FIELDS}:
            return None
        try:
            resp = BackendAssessResponse.model_validate(entry.get('response'))
        except ValidationError:
            return None
        if resp.flag_id != prepared.flag_id or resp.path not in MODEL_PATHS:
            return None
        assessment = {k: v for k, v in resp.model_dump(mode='json').items() if k in GENERATION_FIELDS}
        if check_assessment(assessment, prepared, catalog):
            return None
        return resp

    def put(self, key_fields: dict, response: BackendAssessResponse) -> bool:
        """Atomically store an llm/retry response; False (never raises) for other paths or an unwritable dir."""
        if response.path not in MODEL_PATHS:
            return False
        entry = {'key': {k: key_fields[k] for k in KEY_FIELDS}, 'response': response.model_dump(mode='json'),
                 'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
        target, tmp = self.path(key_fields), None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix='.tmp-', suffix='.json', dir=self.root)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(entry, f, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
            return True
        except OSError:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False


# ── model probe (GET /health model_status) ───────────────────────────────────────────────────

def probe_error(exc: BaseException) -> str:
    """A short, secret-free reason: the error kind (plus the HTTP status), never a URL, host, port or key."""
    status = getattr(exc, 'status', None)
    if isinstance(status, int):
        return 'http %d' % status
    return exc.kind if isinstance(exc, GenerationError) else type(exc).__name__


class ModelProbe:
    """Background check that the model behind System 2 answers, so /health can say so without waiting on it.

    A daemon thread calls probe_fn(timeout_s) every interval_s. A check is skipped while an assessment runs (one
    llama-server slot: the probe would queue behind it and time out), and an assessment the model answered counts
    as a successful check (mark_up)."""

    def __init__(self, probe_fn: Callable[[float], None], interval_s: float = PROBE_S,
                 timeout_s: float = PROBE_TIMEOUT_S, clock: Callable[[], float] = time.monotonic):
        self._probe, self.interval_s, self.timeout_s, self._clock = probe_fn, interval_s, timeout_s, clock
        self._lock, self._stop, self._thread = threading.Lock(), threading.Event(), None
        self._status, self._checked, self._ms, self._error, self._busy = 'unknown', None, None, None, 0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name='system2-model-probe', daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval_s)

    def run_once(self) -> bool:
        """One check; False when skipped because an assessment is running."""
        with self._lock:
            if self._busy:
                return False
        t0, error = self._clock(), None
        try:
            self._probe(self.timeout_s)
        except Exception as exc:   # any failure means "not answering"; the reason is reduced to a short kind
            error = probe_error(exc)
        with self._lock:
            self._status, self._error = ('down', error) if error else ('up', None)
            self._ms, self._checked = int((self._clock() - t0) * 1000), self._clock()
        return True

    def begin(self) -> None:
        with self._lock:
            self._busy += 1

    def end(self, answered: bool) -> None:
        with self._lock:
            self._busy -= 1
            if answered:
                self._status, self._error, self._checked = 'up', None, self._clock()

    def snapshot(self) -> dict:
        with self._lock:
            ago = None if self._checked is None else int(self._clock() - self._checked)
            out = {'model_status': self._status, 'model_checked_s_ago': ago, 'model_probe_ms': self._ms}
            if self._status == 'down':
                out['model_error'] = self._error
            return out


# ── assessor ─────────────────────────────────────────────────────────────────────────────────

class RecordingGenerator:
    """Wraps a generator and keeps every call's raw completion (or error) for the current request's thread.

    Used only when SYSTEM2_RECORD_DIR is set. The pipeline's Attempt records keep statuses, errors and timings but
    not the model text; measurement runs need the text to audit what the model actually said."""

    def __init__(self, inner):
        self.inner = inner
        self._local = threading.local()

    def __getattr__(self, name):   # model, vision_model, examples, ... come from the wrapped generator
        return getattr(self.inner, name)

    def __repr__(self):
        return 'RecordingGenerator(%r)' % (self.inner,)

    def start(self) -> None:
        self._local.calls = []

    def calls(self) -> list:
        return list(getattr(self._local, 'calls', []))

    def __call__(self, packet, errors=(), timeout_s=None):
        t0 = time.monotonic()
        call = {'retry_errors': list(errors), 'timeout_s': timeout_s, 'packet_has_image': packet.image is not None}
        try:
            g = self.inner(packet, errors, timeout_s=timeout_s)
        except Exception as exc:
            call.update(error='%s: %s' % (type(exc).__name__, str(exc)[:300]),
                        wall_ms=int((time.monotonic() - t0) * 1000))
            getattr(self._local, 'calls', []).append(call)
            raise
        call.update(text=g.text, model=g.model, finish_reason=g.finish_reason, prompt_tokens=g.prompt_tokens,
                    completion_tokens=g.completion_tokens, tokens_per_sec=g.tokens_per_sec, image_used=g.image_used,
                    wall_ms=int((time.monotonic() - t0) * 1000))
        getattr(self._local, 'calls', []).append(call)
        return g


class RunRecorder:
    """Appends one JSON line per /v1/assess request to <dir>/system2-records.jsonl (never the API key)."""

    def __init__(self, directory: Path):
        self.path = Path(directory) / RECORD_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock, open(self.path, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')


class LiveAssessor:
    """assess(body) -> (response, cache_status) through run_pipeline with `generator` (e.g. LlamaGenerator)."""

    def __init__(self, generator, model: str, config: Optional[PipelineConfig] = None,
                 cache: Optional[ExplanationCache] = None, image_deadline_s: float = IMAGE_DEADLINE_S,
                 recorder: Optional[RunRecorder] = None, probe: Optional[ModelProbe] = None):
        self.generator, self.model, self.cache, self.recorder = generator, model, cache, recorder
        self.probe = probe
        self.vision_model = getattr(generator, 'vision_model', None) or model
        self.config = config if config is not None else PipelineConfig(deadline_s=DEADLINE_S, catalog=default_catalog())
        self.image_config = replace(self.config, deadline_s=float(image_deadline_s))   # only requests with an image
        self.prompt_ver = prompt_version(generator)

    def __repr__(self):  # never shows a key: LlamaGenerator's repr is endpoint + model only
        return 'LiveAssessor(%r, model=%r)' % (self.generator, self.model)

    def model_health(self) -> dict:
        """The /health model_* keys ({} without a probe)."""
        return self.probe.snapshot() if self.probe is not None else {}

    def key_fields(self, prepared: PreparedPacket) -> dict:
        model = self.vision_model if prepared.image is not None else self.model
        return {'flag_id': prepared.flag_id, 'packet_hash': packet_hash(prepared), 'model': model,
                'prompt_ver': self.prompt_ver}

    @property
    def streams_drafts(self) -> bool:
        """True when on_draft can receive the model's text while it streams (the generator has drafts())."""
        return callable(getattr(self.generator, 'drafts', None))

    def assess_image(self, body, refresh: bool = False, on_draft: Optional[Callable[[int, int, str], None]] = None
                     ) -> Tuple[BackendAssessResponse, str, bool, Optional[str]]:
        """(response, 'hit'|'miss'|'off', stored, image status or None when the request had no image).

        on_draft(call, tokens, text), when given and the generator streams, receives the accumulated completion text
        after each streamed chunk (display only; the result is checked exactly as without it)."""
        body = body if isinstance(body, BackendAssessRequest) else BackendAssessRequest.model_validate(body)
        prepared = normalize_packet(body)
        has_image = prepared.image is not None
        key = self.key_fields(prepared) if self.cache is not None else None
        if key is not None and not refresh:
            cached = self.cache.get(key, prepared, self.config.catalog)
            if cached is not None:   # image requests only ever store answers that saw the image
                image = IMAGE_USED if has_image else None
                self._record(body, prepared, 'hit', False, image, cached, None, [])
                return cached, 'hit', True, image
        recording = self.recorder is not None and isinstance(self.generator, RecordingGenerator)
        if recording:
            self.generator.start()
        config = self.image_config if has_image else self.config
        if self.probe is not None:
            self.probe.begin()
        answered = False
        drafts = getattr(self.generator, 'drafts', None) if on_draft is not None else None
        try:
            with drafts(on_draft) if callable(drafts) else nullcontext():
                run = run_pipeline(body, self.generator, config)
            answered = any(a.status in ANSWERED for a in run.attempts)
        finally:
            if self.probe is not None:
                self.probe.end(answered)
        response = to_response(run, prepared.flag_id)
        image = None
        if has_image:
            image = {True: IMAGE_USED, False: IMAGE_TEXT_ONLY}.get(run.image_used, IMAGE_NOT_USED)
            if run.image_used is False:
                LOG.warning('system2 flag_id=%d: image not used in the accepted answer; answered text-only '
                            '(image_used=false)', prepared.flag_id)
        if key is None:
            status, stored = 'off', False
        elif has_image and run.image_used is not True:
            status, stored = 'miss', False   # never cache a text-only answer under the image key
        else:
            status, stored = 'miss', self.cache.put(key, response)
        self._record(body, prepared, status, stored, image, response, run, self.generator.calls() if recording else [],
                     config.deadline_s)
        return response, status, stored, image

    def _record(self, body, prepared, status, stored, image, response, run, calls, deadline_s=None) -> None:
        if self.recorder is None:
            return
        self.recorder.write({
            'recorded_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            'request_id': getattr(body, 'request_id', None), 'flag_id': prepared.flag_id,
            'packet_hash': packet_hash(prepared), 'prompt_ver': self.prompt_ver, 'model': self.model,
            'vision_model': self.vision_model,
            'image_sha256': prepared.image.sha256 if prepared.image is not None else None,
            'deadline_s': deadline_s, 'cache': status, 'stored': stored, 'image_status': image,
            'response': response.model_dump(mode='json'), 'run': run.to_dict() if run is not None else None,
            'calls': calls})

    def assess_detail(self, body, refresh: bool = False) -> Tuple[BackendAssessResponse, str, bool]:
        """(response, 'hit'|'miss'|'off', stored); ValueError for malformed consumed values before any generation."""
        response, status, stored, _ = self.assess_image(body, refresh)
        return response, status, stored

    def assess(self, body, refresh: bool = False) -> Tuple[BackendAssessResponse, str]:
        response, status, _ = self.assess_detail(body, refresh)
        return response, status


def _seconds_env(environ: Mapping[str, str], name: str, default: float, high: float = 120.0,
                 low: Optional[float] = None) -> float:
    """Seconds from env: in (0, high], or [low, high] when low is given."""
    raw = (environ.get(name) or '').strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = -1.0
    if low is None and not 0 < value <= high:
        raise RuntimeError('%s must be a number of seconds in (0, %g], got %r' % (name, high, raw))
    if low is not None and not low <= value <= high:
        raise RuntimeError('%s must be a number of seconds in [%g, %g], got %r' % (name, low, high, raw))
    return value


def assessor_from_env(environ: Mapping[str, str] = os.environ) -> Optional[LiveAssessor]:
    """None (stub) when SYSTEM2_LLAMA_URL is unset or blank; RuntimeError on an incomplete or unusable live config."""
    url = (environ.get(URL_ENV) or '').strip()
    if not url:
        return None
    model = (environ.get(MODEL_ENV) or '').strip()
    vision_model = (environ.get(VISION_MODEL_ENV) or '').strip() or None
    deadline = _seconds_env(environ, DEADLINE_ENV, DEADLINE_S)
    image_deadline = _seconds_env(environ, IMAGE_DEADLINE_ENV, IMAGE_DEADLINE_S)
    probe_s = _seconds_env(environ, PROBE_ENV, PROBE_S, high=3600.0, low=5.0)
    probe_timeout = _seconds_env(environ, PROBE_TIMEOUT_ENV, PROBE_TIMEOUT_S)
    if not model:
        raise RuntimeError('%s is set but %s is empty; set the served model name (e.g. qwen3.5-9b-q4km)'
                           % (URL_ENV, MODEL_ENV))
    from .generation import LlamaGenerator   # deferred: stub mode never loads the few-shots
    cache_dir = cache_dir_from_env(environ)
    try:
        generator = LlamaGenerator(url, model=model, vision_model=vision_model,
                                   api_key=(environ.get(LLAMA_KEY_ENV) or '').strip() or None,
                                   timeout_s=deadline, image_timeout_s=image_deadline)
        record_dir = (environ.get(RECORD_ENV) or '').strip()
        recorder = RunRecorder(Path(record_dir)) if record_dir else None
        return LiveAssessor(RecordingGenerator(generator) if recorder else generator, model,
                            cache=ExplanationCache(cache_dir) if cache_dir is not None else None,
                            config=PipelineConfig(deadline_s=deadline, catalog=default_catalog()),
                            image_deadline_s=image_deadline, recorder=recorder,
                            probe=ModelProbe(generator.probe, interval_s=probe_s, timeout_s=probe_timeout))
    except (ValueError, OSError) as exc:
        raise RuntimeError('live System 2 config unusable (%s %s): %s'
                           % (URL_ENV, endpoint(url), str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
                           ) from None
