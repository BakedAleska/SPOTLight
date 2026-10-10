"""Live /v1/assess: the checked pipeline behind the HTTP API, plus the explanation cache (Phase 3, SRV-01..03).

The API stays a stub until SYSTEM2_LLAMA_URL is set (assessor_from_env returns None, so the stub path and its tests
are untouched). When it is set, LiveAssessor runs the same run_pipeline the offline eval harness scores: at most
two LlamaGenerator attempts inside one 15 s deadline (25 s, SYSTEM2_IMAGE_DEADLINE_S, for requests with an
image), then the checked template (path 'fallback', model 'template'), so the live endpoint can never return an
unchecked model answer. Insufficient packets short-circuit
before any generation. to_response() maps an AssessmentRun to the flat BackendAssessResponse exactly the way
evaluation._public does, so the numbers the backend logs to app.model_calls (model, latency_ms, tokens_per_sec)
are the same fields the eval measured.

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

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Mapping, Optional, Tuple

from pydantic import ValidationError

from .backend_contract import MODEL_PATHS, BackendAssessRequest, BackendAssessResponse
from .evaluation import endpoint, packet_hash
from .grounding import EntityCatalog, GENERATION_FIELDS, check_assessment, default_catalog
from .packet import PreparedPacket, normalize_packet
from .paths import repo_root
from .pipeline import AssessmentRun, PipelineConfig, run_pipeline
from .prompts import PROMPT_VERSION, prompt_identity

DEADLINE_S = 15.0   # live-explain p95 target (PROJECT constraint); shared by both attempts
IMAGE_DEADLINE_S = 25.0   # requests with image_b64 (SYSTEM2_IMAGE_DEADLINE_S); text-only resend inside it (A-6)
IMAGE_DEADLINE_ENV = 'SYSTEM2_IMAGE_DEADLINE_S'
DEADLINE_ENV = 'SYSTEM2_DEADLINE_S'   # text requests; default DEADLINE_S (raise only for slow local models)
URL_ENV, MODEL_ENV, CACHE_ENV = 'SYSTEM2_LLAMA_URL', 'SYSTEM2_MODEL', 'SYSTEM2_CACHE_DIR'
LLAMA_KEY_ENV = 'SYSTEM2_LLAMA_API_KEY'   # Bearer key for a gateway in front of llama-server (never logged)
VISION_MODEL_ENV = 'SYSTEM2_VISION_MODEL'   # model for requests with an image; default SYSTEM2_MODEL (backend.2)
IMAGE_USED, IMAGE_TEXT_ONLY, IMAGE_NOT_USED = 'used', 'text_only', 'not_used'   # X-System2-Image values
KEY_FIELDS = ('flag_id', 'packet_hash', 'model', 'prompt_ver')
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
    """SYSTEM2_CACHE_DIR, else amd/system2/cache in a checkout, else None (the image has no writable default)."""
    env = (environ.get(CACHE_ENV) or '').strip()
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


# ── assessor ─────────────────────────────────────────────────────────────────────────────────

class LiveAssessor:
    """assess(body) -> (response, cache_status) through run_pipeline with `generator` (e.g. LlamaGenerator)."""

    def __init__(self, generator, model: str, config: Optional[PipelineConfig] = None,
                 cache: Optional[ExplanationCache] = None, image_deadline_s: float = IMAGE_DEADLINE_S):
        self.generator, self.model, self.cache = generator, model, cache
        self.vision_model = getattr(generator, 'vision_model', None) or model
        self.config = config if config is not None else PipelineConfig(deadline_s=DEADLINE_S, catalog=default_catalog())
        self.image_config = replace(self.config, deadline_s=float(image_deadline_s))   # only requests with an image
        self.prompt_ver = prompt_version(generator)

    def __repr__(self):  # never shows a key: LlamaGenerator's repr is endpoint + model only
        return 'LiveAssessor(%r, model=%r)' % (self.generator, self.model)

    def key_fields(self, prepared: PreparedPacket) -> dict:
        model = self.vision_model if prepared.image is not None else self.model
        return {'flag_id': prepared.flag_id, 'packet_hash': packet_hash(prepared), 'model': model,
                'prompt_ver': self.prompt_ver}

    def assess_image(self, body, refresh: bool = False) -> Tuple[BackendAssessResponse, str, bool, Optional[str]]:
        """(response, 'hit'|'miss'|'off', stored, image status or None when the request had no image)."""
        body = body if isinstance(body, BackendAssessRequest) else BackendAssessRequest.model_validate(body)
        prepared = normalize_packet(body)
        has_image = prepared.image is not None
        key = self.key_fields(prepared) if self.cache is not None else None
        if key is not None and not refresh:
            cached = self.cache.get(key, prepared, self.config.catalog)
            if cached is not None:   # image requests only ever store answers that saw the image
                return cached, 'hit', True, IMAGE_USED if has_image else None
        run = run_pipeline(body, self.generator, self.image_config if has_image else self.config)
        response = to_response(run, prepared.flag_id)
        image = None
        if has_image:
            image = {True: IMAGE_USED, False: IMAGE_TEXT_ONLY}.get(run.image_used, IMAGE_NOT_USED)
            if run.image_used is False:
                LOG.warning('system2 flag_id=%d: image rejected by the model server; answered text-only '
                            '(image_used=false)', prepared.flag_id)
        if key is None:
            return response, 'off', False, image
        if has_image and run.image_used is not True:
            return response, 'miss', False, image   # never cache a text-only answer under the image key
        return response, 'miss', self.cache.put(key, response), image

    def assess_detail(self, body, refresh: bool = False) -> Tuple[BackendAssessResponse, str, bool]:
        """(response, 'hit'|'miss'|'off', stored); ValueError for malformed consumed values before any generation."""
        response, status, stored, _ = self.assess_image(body, refresh)
        return response, status, stored

    def assess(self, body, refresh: bool = False) -> Tuple[BackendAssessResponse, str]:
        response, status, _ = self.assess_detail(body, refresh)
        return response, status


def _seconds_env(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = (environ.get(name) or '').strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = -1.0
    if not 0 < value <= 120:
        raise RuntimeError('%s must be a number of seconds in (0, 120], got %r' % (name, raw))
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
    if not model:
        raise RuntimeError('%s is set but %s is empty; set the served model name (e.g. qwen3.5-9b-q4km)'
                           % (URL_ENV, MODEL_ENV))
    from .generation import LlamaGenerator   # deferred: stub mode never loads the few-shots
    cache_dir = cache_dir_from_env(environ)
    try:
        generator = LlamaGenerator(url, model=model, vision_model=vision_model,
                                   api_key=(environ.get(LLAMA_KEY_ENV) or '').strip() or None,
                                   timeout_s=deadline, image_timeout_s=image_deadline)
        return LiveAssessor(generator, model, cache=ExplanationCache(cache_dir) if cache_dir is not None else None,
                            config=PipelineConfig(deadline_s=deadline, catalog=default_catalog()),
                            image_deadline_s=image_deadline)
    except (ValueError, OSError) as exc:
        raise RuntimeError('live System 2 config unusable (%s %s): %s'
                           % (URL_ENV, endpoint(url), str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
                           ) from None
