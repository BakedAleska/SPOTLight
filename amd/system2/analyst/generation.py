"""Packet-scoped JSON schema and the llama.cpp generator for run_pipeline (stdlib HTTP, no new packages).

GeneratedAssessment is the internal generation model: exactly the six GEN-01 fields, extras forbidden at every
object, nonblank claims and answer, at least one cited ID per claim, confidence high|medium|low, and insufficiency
consistent with the packet. response_schema(packet) derives llama.cpp's grammar schema from that model, then
narrows it to the current packet: cited IDs enum = the packet's nonblank evidence IDs, recommended_action enum =
its allowed actions (MANUAL_REVIEW included), question = the packet question. Few-shot IDs never enter the enum.
Only keywords in SCHEMA_KEYWORDS are emitted (the subset scripts/smoke_serve.py used on the AMD box, plus string
lengths). Property order follows GEN-01 for generation; validation never depends on key order.

LlamaGenerator(url) implements the pipeline generator protocol over llama-server's OpenAI-compatible
/v1/chat/completions: thinking off, temperature 0, max_tokens 350. Every transport or envelope problem (HTTP
status, timeout, oversized body, non-JSON, NaN/Infinity, wrong types, fewer than three few-shots) raises
GenerationError, so run_pipeline counts it as an attempt and retries once or falls back to the checked template.
The time budget covers the whole call: a socket timeout only bounds each recv, so a watchdog shuts the socket at
the deadline and a server or tunnel that trickles headers or body cannot hold the call open past it.
The completion text itself is judged only by run_pipeline: finish_reason 'length' is truncation even when the
text parses, and token counts are recorded as metadata, never used to infer validity. The recorded SRV-00 smoke
needed 439 output tokens; the budget stays at 350 and long replies fall back rather than being assumed valid.
No model is loaded here and nothing is wired into the live API in this phase.

Images (contract backend.2): when the packet carries an image, the request goes to `vision_model` (default: the
text model) with the OpenAI content array built by prompts.build_messages (image first, packet text last) and the
same json_schema response_format. If the server rejects image input (llama-server without --mmproj, or a gateway
without the vision alias: a 4xx/5xx whose body mentions images/mmproj/multimodal/vision, or a 400/404 for a
distinct vision model), the same attempt is re-sent once text-only to the text model inside the same time budget,
and Generation.image_used is False; an accepted image call sets it True; no image leaves it None.
A 4xx that is really a gateway auth failure (LiteLLM answers a wrong key with 400 "No connected db.") is never read
as an image rejection, so a bad key is not hidden behind a text-only answer.

Image latency (audit A-6): an image request may use up to `image_timeout_s` (SYSTEM2_IMAGE_DEADLINE_S, default 25 s,
instead of the text path's 15 s). The image call keeps TEXT_RESERVE_S back; when it times out (or there is less than
MIN_IMAGE_CALL_S left for it), the same attempt is re-sent text-only in the reserved time (image_used False).
Requests without an image behave exactly as before.
"""
from __future__ import annotations

import dataclasses
import http.client
import json
import math
import socket
import threading
import time
from typing import List, Literal, Optional, Sequence
import urllib.parse

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from .packet import PreparedPacket
from .pipeline import Generation, GenerationError
from .prompts import (ANSWER_CHARS, CLAIM_CHARS, FEWSHOT_COUNT, MAX_EVIDENCE, Example, build_messages,
                      load_examples, prompt_identity, select_examples)

MAX_TOKENS = 350            # GEN-03; see module docstring for the 439-token observation
TEMPERATURE = 0
TIMEOUT_S = 15.0            # per call; run_pipeline passes its remaining deadline, the smaller one wins
IMAGE_TIMEOUT_S = 25.0      # per call for packets with an image (SYSTEM2_IMAGE_DEADLINE_S)
TEXT_RESERVE_S = 8.0        # kept back from an image call for the text-only resend
# Below this an image call is not started; the attempt goes text-only directly, with the whole remaining budget.
# Measured on the W7900D Q8_0 (results/archive/untuned-q8-original-2026-10-10): image calls that returned took
# 9.3-14.5 s, so a call started with ~5 s left (the old 4 s floor let the retry do that) was always cancelled.
MIN_IMAGE_CALL_S = 9.0
MAX_RESPONSE_BYTES = 262144  # whole HTTP body; the pipeline separately caps completion text at 64 KiB
MAX_IDS = 8
CHUNK = 16384
ERROR_BODY_BYTES = 4096     # error excerpt read only to recognise an image rejection; never logged in full
IMAGE_REJECT_STATUS = frozenset({400, 404, 413, 415, 422, 500, 501})
IMAGE_REJECT_WORDS = ('image', 'mmproj', 'multimodal', 'multi-modal', 'vision', 'modalit')
# LiteLLM answers a wrong gateway key with 400 "No connected db." (no virtual-key DB): an auth failure, not an image one
AUTH_ERROR_WORDS = ('no connected db', 'no_db_connection', 'authentication', 'api key', 'api_key', 'unauthorized')
SCHEMA_KEYWORDS = frozenset({'type', 'properties', 'required', 'additionalProperties', 'items', 'enum',
                             'minItems', 'maxItems', 'minLength', 'maxLength'})


# ── internal generation model ───────────────────────────────────────────────────────────────

class GeneratedEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    claim: str = Field(min_length=1, max_length=CLAIM_CHARS)
    ids: List[str] = Field(min_length=1, max_length=MAX_IDS)


class GeneratedAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    evidence: List[GeneratedEvidence] = Field(max_length=MAX_EVIDENCE)
    answer: str = Field(min_length=1, max_length=ANSWER_CHARS)
    question: str
    recommended_action: str
    confidence: Literal['high', 'medium', 'low']
    insufficient_context: bool

    @model_validator(mode='after')
    def _packet_scoped(self, info: ValidationInfo):
        p = (info.context or {}).get('prepared')
        if not isinstance(p, PreparedPacket):
            raise ValueError('validation needs the prepared packet as context')
        if not self.answer.strip() or any(not e.claim.strip() for e in self.evidence):
            raise ValueError('blank claim or answer')
        for e in self.evidence:
            if len(set(e.ids)) != len(e.ids):
                raise ValueError('duplicate ID within a claim')
            if any(i not in p.content for i in e.ids):
                raise ValueError('cited ID not in this packet')
        if self.recommended_action not in p.allowed_actions:
            raise ValueError('recommended_action not allowed by this packet')
        if self.question != p.question:
            raise ValueError('question must repeat the packet question')
        if self.insufficient_context != p.insufficient_context:
            raise ValueError('insufficient_context must match the packet')
        if self.insufficient_context:
            if self.evidence or self.confidence != 'low' or self.recommended_action != 'MANUAL_REVIEW':
                raise ValueError('insufficient context needs no evidence, low confidence, MANUAL_REVIEW')
        elif not self.evidence:
            raise ValueError('a complete assessment needs evidence')
        return self


def validate_generated(candidate, prepared: PreparedPacket) -> GeneratedAssessment:
    """Strict packet-scoped validation; raises ValueError (pydantic ValidationError is one)."""
    return GeneratedAssessment.model_validate(candidate, context={'prepared': prepared})


def _inline(node, defs):
    if isinstance(node, dict):
        if '$ref' in node:
            return _inline(defs[node['$ref'].rsplit('/', 1)[1]], defs)
        return {k: _inline(v, defs) for k, v in node.items() if k not in ('title', 'description', '$defs')}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def response_schema(prepared: PreparedPacket) -> dict:
    """GeneratedAssessment's schema with refs inlined and enums narrowed to the current packet only."""
    raw = GeneratedAssessment.model_json_schema()
    s = _inline(raw, raw.get('$defs', {}))
    props = s['properties']
    ids = [i for i in prepared.content if i.strip()]
    ids_schema = props['evidence']['items']['properties']['ids']['items']
    if ids:
        ids_schema['enum'] = ids
    props['question']['enum'] = [prepared.question]
    props['insufficient_context']['enum'] = [prepared.insufficient_context]
    if prepared.insufficient_context or not ids:
        props['evidence']['maxItems'] = 0
        props['confidence']['enum'] = ['low']
        props['recommended_action']['enum'] = ['MANUAL_REVIEW']
    else:
        props['evidence']['minItems'] = 1
        props['recommended_action']['enum'] = list(prepared.allowed_actions)
    return s


def request_body(prepared: PreparedPacket, examples: Sequence[Example], errors: Sequence[str] = (),
                 model: Optional[str] = None, max_tokens: int = MAX_TOKENS, include_image: bool = True) -> dict:
    body = {}
    if model:
        body['model'] = model
    body.update({'messages': build_messages(prepared, examples, errors, include_image), 'temperature': TEMPERATURE,
                 'max_tokens': max_tokens, 'stream': False,
                 'response_format': {'type': 'json_schema',
                                     'json_schema': {'name': 'assessment', 'schema': response_schema(prepared)}},
                 'chat_template_kwargs': {'enable_thinking': False}})
    return body


# ── llama-server client ─────────────────────────────────────────────────────────────────────

class HTTPStatusError(GenerationError):
    """Non-2xx reply: GenerationError('http', 'status N') plus the status and a short body excerpt."""

    def __init__(self, status: int, excerpt: str = ''):
        super().__init__('http', 'status %d' % status)
        self.status, self.excerpt = status, excerpt


def image_rejected(exc: GenerationError, distinct_vision_model: bool) -> bool:
    """True when an image request failed because the server/gateway cannot take images (resend text-only)."""
    if not isinstance(exc, HTTPStatusError) or exc.status not in IMAGE_REJECT_STATUS:
        return False
    text = exc.excerpt.lower()
    if any(w in text for w in AUTH_ERROR_WORDS):
        return False
    return any(w in text for w in IMAGE_REJECT_WORDS) or (distinct_vision_model and exc.status in (400, 404))


def _reject_constant(name):
    raise ValueError('non-finite JSON constant %s' % name)


def _count(v) -> Optional[int]:
    return v if type(v) is int and v >= 0 else None


def _rate(v) -> Optional[float]:
    return float(v) if type(v) in (int, float) and math.isfinite(v) and v >= 0 else None


def _first(*values):
    return next((v for v in values if v is not None), None)


def parse_envelope(data: bytes, fallback_model: Optional[str]) -> Generation:
    """llama-server chat completion body -> Generation; GenerationError('protocol') on any structural problem."""
    try:
        env = json.loads(data.decode('utf-8'), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise GenerationError('protocol', 'response is not finite JSON: %s' % type(exc).__name__) from None
    if not isinstance(env, dict):
        raise GenerationError('protocol', 'response is not a JSON object')
    choices = env.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise GenerationError('protocol', 'response has no choices')
    message, finish = choices[0].get('message'), choices[0].get('finish_reason')
    if not isinstance(message, dict) or not isinstance(message.get('content'), str):
        raise GenerationError('protocol', 'choice message content is not a string')
    if finish is not None and not isinstance(finish, str):
        raise GenerationError('protocol', 'finish_reason is not a string')
    usage, timings, model = env.get('usage'), env.get('timings'), env.get('model')
    usage = usage if isinstance(usage, dict) else {}
    timings = timings if isinstance(timings, dict) else {}
    name = model if isinstance(model, str) and model.strip() else (fallback_model or 'unknown')
    return Generation(message['content'], name, finish,
                      _first(_count(usage.get('prompt_tokens')), _count(timings.get('prompt_n'))),
                      _first(_count(usage.get('completion_tokens')), _count(timings.get('predicted_n'))),
                      _rate(timings.get('predicted_per_second')))


class LlamaGenerator:
    """generator(packet, errors, timeout_s=...) -> Generation against llama-server; see module docstring."""

    def __init__(self, url: str, model: Optional[str] = None, examples: Optional[Sequence[Example]] = None,
                 max_tokens: int = MAX_TOKENS, timeout_s: float = TIMEOUT_S,
                 max_response_bytes: int = MAX_RESPONSE_BYTES, api_key: Optional[str] = None,
                 vision_model: Optional[str] = None, image_timeout_s: float = IMAGE_TIMEOUT_S):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError('llama-server URL must be http(s)://host[:port]')
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError('max_tokens must be a positive integer')
        self.endpoint = url.rstrip('/') + '/v1/chat/completions'
        # http.client directly: no environment proxies and no redirects (a 3xx is GenerationError('http'), so the
        # Authorization header never reaches another host); the call owns its socket, so the deadline can close it
        self._https, self._host, self._port = parts.scheme == 'https', parts.hostname, parts.port
        self._target = parts.path.rstrip('/') + '/v1/chat/completions'
        self.model, self.max_tokens, self.timeout_s = model, max_tokens, float(timeout_s)
        self.max_response_bytes, self._api_key = max_response_bytes, api_key
        self.vision_model = vision_model or model     # model used when the packet carries an image
        self.image_timeout_s = float(image_timeout_s)
        self.examples = tuple(examples) if examples is not None else load_examples()

    def __repr__(self):  # never shows the key
        return 'LlamaGenerator(%r, model=%r)' % (self.endpoint, self.model)

    def identity(self, prepared: PreparedPacket) -> dict:
        ident = prompt_identity(select_examples(self.examples, prepared, FEWSHOT_COUNT))
        ident.update({'model': self.model, 'max_tokens': self.max_tokens, 'temperature': TEMPERATURE,
                      'thinking': False})
        return ident

    def __call__(self, packet: PreparedPacket, errors: Sequence[str] = (), timeout_s: Optional[float] = None
                 ) -> Generation:
        try:
            chosen = select_examples(self.examples, packet, FEWSHOT_COUNT)
        except ValueError as exc:
            raise GenerationError('prompt', str(exc)) from None
        cap = self.timeout_s if packet.image is None else self.image_timeout_s
        budget = cap if timeout_s is None else min(cap, timeout_s)
        if budget <= 0:
            raise GenerationError('timeout', 'no time budget left')
        headers = {'Content-Type': 'application/json'}
        if self._api_key:
            headers['Authorization'] = 'Bearer %s' % self._api_key
        if packet.image is None:
            return parse_envelope(self._post(self._body(packet, chosen, errors, self.model, True), headers, budget),
                                  self.model)
        deadline = time.monotonic() + budget
        image_budget = budget - TEXT_RESERVE_S
        if image_budget >= MIN_IMAGE_CALL_S:
            try:
                data = self._post(self._body(packet, chosen, errors, self.vision_model, True), headers, image_budget)
                return dataclasses.replace(parse_envelope(data, self.vision_model), image_used=True)
            except GenerationError as exc:
                if exc.kind != 'timeout' and not image_rejected(exc, self.vision_model != self.model):
                    raise
        left = deadline - time.monotonic()      # one text-only resend inside the same budget
        if left <= 0:
            raise GenerationError('timeout', 'no time left for the text-only resend')
        data = self._post(self._body(packet, chosen, errors, self.model, False), headers, left)
        return dataclasses.replace(parse_envelope(data, self.model), image_used=False)

    def _body(self, packet, chosen, errors, model, include_image) -> bytes:
        return json.dumps(request_body(packet, chosen, errors, model, self.max_tokens, include_image),
                          ensure_ascii=False).encode('utf-8')

    def _post(self, body: bytes, headers: dict, budget: float) -> bytes:
        """POST and read the whole body within `budget` s; the watchdog shuts the socket at the deadline."""
        deadline = time.monotonic() + budget
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=budget)    # bounds connect; the watchdog bounds the rest
        expired, timer = threading.Event(), None
        late = 'response not complete within %.1f s' % budget
        try:
            conn.connect()
            left = deadline - time.monotonic()
            if left <= 0:
                raise GenerationError('timeout', late)
            timer = threading.Timer(left, _expire, (conn.sock, expired))
            timer.daemon = True
            timer.start()
            conn.request('POST', self._target, body=body, headers=dict(headers, Connection='close'))
            resp = conn.getresponse()
            if not 200 <= resp.status < 300:
                try:
                    excerpt = resp.read(ERROR_BODY_BYTES).decode('utf-8', 'replace')
                except (OSError, http.client.HTTPException):
                    excerpt = ''
                raise HTTPStatusError(resp.status, excerpt)
            chunks, size = [], 0
            while True:
                if expired.is_set() or time.monotonic() > deadline:
                    raise GenerationError('timeout', late)
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise GenerationError('oversized', 'response over %d bytes' % self.max_response_bytes)
                chunks.append(chunk)
            if expired.is_set():          # EOF caused by the watchdog is a timeout, not a complete body
                raise GenerationError('timeout', late)
            return b''.join(chunks)
        except TimeoutError:
            raise GenerationError('timeout', 'no response within %.1f s' % budget) from None
        except (OSError, http.client.HTTPException) as exc:
            if expired.is_set():
                raise GenerationError('timeout', late) from None
            raise GenerationError('transport', type(exc).__name__) from None
        finally:
            if timer is not None:
                timer.cancel()
            conn.close()


def _expire(sock, expired: threading.Event) -> None:
    """Watchdog: flag the deadline, then shut the socket so any blocked send/recv returns at once."""
    expired.set()
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)   # base method: also safe for an SSL socket mid-read
    except OSError:
        pass
