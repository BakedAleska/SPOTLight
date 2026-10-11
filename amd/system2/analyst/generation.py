"""Packet-scoped JSON schema and the llama.cpp generator for run_pipeline (stdlib HTTP, no new packages).

GeneratedAssessment is the internal generation model: exactly the six GEN-01 fields, extras forbidden at every
object, nonblank claims and answer, at least one cited ID per claim, confidence high|medium|low, and insufficiency
consistent with the packet. response_schema(packet) derives llama.cpp's grammar schema from that model, then
narrows it to the current packet: cited IDs enum = the packet's nonblank evidence IDs, recommended_action enum =
its allowed actions (MANUAL_REVIEW included), question = the packet question. Few-shot IDs never enter the enum.
Only keywords in SCHEMA_KEYWORDS are emitted (the subset scripts/smoke_serve.py used on the AMD box, plus string
lengths). Property order follows GEN-01 for generation; validation never depends on key order.

LlamaGenerator(url) implements the pipeline generator protocol over llama-server's OpenAI-compatible
/v1/chat/completions: thinking off, temperature 0, max_tokens 512. Every transport or envelope problem (HTTP
status, timeout, oversized body, non-JSON, NaN/Infinity, wrong types, fewer than three few-shots) raises
GenerationError, so run_pipeline counts it as an attempt and retries once or falls back to the checked template.
The time budget covers the whole call: a socket timeout only bounds each recv, so a watchdog shuts the socket at
the deadline and a server or tunnel that trickles headers or body cannot hold the call open past it.
The completion text itself is judged only by run_pipeline: finish_reason 'length' is truncation even when the
text parses, and token counts are recorded as metadata, never used to infer validity. The recorded SRV-00 smoke
needed 439 output tokens; the budget was 350 until 2026-10-11, when the 27B pretty-printed a valid reply past
it (M001 run). It is 512 now; a reply that still hits it is truncation and falls back, never assumed valid.
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

Streaming drafts (docs/backend-services-contract.md, "Streaming Qwen drafts"): inside `with generator.drafts(cb):`
(per thread, so concurrent requests never share it) every model call is sent with `stream: true` +
`stream_options.include_usage` and an otherwise identical body, and cb(call, tokens, text) receives the accumulated
content after each content chunk (`call` is 1-based across the block: the pipeline's retry and an image request's
text-only resend each start a new call). The stream has the same connect/watchdog deadline, status handling and size
limits as a plain call; its chunks are folded into a synthetic chat completion (content, last finish_reason, usage,
timings, model) and judged by the same parse_envelope, so a streamed Generation means exactly what a plain one does.
An upstream that ignores `stream: true` (a 2xx reply whose Content-Type is not text/event-stream) is read as a plain
completion under the plain size cap, with no drafts.
Outside the block the request body and behaviour are byte-identical to the non-streaming client.
"""
from __future__ import annotations

import contextlib
import dataclasses
import http.client
import json
import logging
import math
import socket
import threading
import time
from typing import Callable, List, Literal, Optional, Sequence
import urllib.parse

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from .packet import PreparedPacket
from .pipeline import Generation, GenerationError
from .prompts import (ANSWER_CHARS, CLAIM_CHARS, FEWSHOT_COUNT, MAX_EVIDENCE, Example, build_messages,
                      load_examples, prompt_identity, select_examples)

MAX_TOKENS = 512            # GEN-03; was 350 (module docstring: the 439-token smoke, the 2026-10-11 M001 run)
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
STREAM_RAW_FACTOR = 16     # a stream's raw SSE bytes may be this many times max_response_bytes (per-chunk framing)
LOG = logging.getLogger('system2.generation')
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
                 model: Optional[str] = None, max_tokens: int = MAX_TOKENS, include_image: bool = True,
                 stream: bool = False) -> dict:
    body = {}
    if model:
        body['model'] = model
    body.update({'messages': build_messages(prepared, examples, errors, include_image), 'temperature': TEMPERATURE,
                 'max_tokens': max_tokens, 'stream': False,
                 'response_format': {'type': 'json_schema',
                                     'json_schema': {'name': 'assessment', 'schema': response_schema(prepared)}},
                 'chat_template_kwargs': {'enable_thinking': False}})
    if stream:   # drafts only; the plain body above is unchanged
        body['stream'] = True
        body['stream_options'] = {'include_usage': True}
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


def _stream_error(err) -> HTTPStatusError:
    """An `error` object sent inside a 200 stream, read like a non-2xx reply (so image rejection still applies)."""
    code = err.get('code') if isinstance(err, dict) else None
    status = code if type(code) is int and 400 <= code < 600 else 500
    try:
        excerpt = json.dumps(err, ensure_ascii=False)[:ERROR_BODY_BYTES]
    except (TypeError, ValueError):
        excerpt = ''
    return HTTPStatusError(status, excerpt)


class _DraftSink:
    """Per-thread draft state inside LlamaGenerator.drafts(): the callback and the 1-based model-call counter."""

    def __init__(self, callback: Callable[[int, int, str], None]):
        self.callback, self.call = callback, 0

    def notify(self, call: int, tokens: int, text: str) -> None:
        try:
            self.callback(call, tokens, text)
        except Exception:   # a draft consumer can never fail or slow-fail the generation
            LOG.debug('draft callback failed', exc_info=True)


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
        self._draft_local = threading.local()

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
            return self._complete(packet, chosen, errors, self.model, True, headers, budget)
        deadline = time.monotonic() + budget
        image_budget = budget - TEXT_RESERVE_S
        if image_budget >= MIN_IMAGE_CALL_S:
            try:
                g = self._complete(packet, chosen, errors, self.vision_model, True, headers, image_budget)
                return dataclasses.replace(g, image_used=True)
            except GenerationError as exc:
                if exc.kind != 'timeout' and not image_rejected(exc, self.vision_model != self.model):
                    raise
        left = deadline - time.monotonic()      # one text-only resend inside the same budget
        if left <= 0:
            raise GenerationError('timeout', 'no time left for the text-only resend')
        g = self._complete(packet, chosen, errors, self.model, False, headers, left)
        return dataclasses.replace(g, image_used=False)

    @contextlib.contextmanager
    def drafts(self, callback: Callable[[int, int, str], None]):
        """Within the block (this thread only) calls stream and callback(call, tokens, text) sees each content chunk."""
        previous = getattr(self._draft_local, 'sink', None)
        self._draft_local.sink = _DraftSink(callback)
        try:
            yield
        finally:
            self._draft_local.sink = previous

    def _complete(self, packet, chosen, errors, model, include_image, headers, budget) -> Generation:
        """One model call: the plain POST, or the streaming POST while drafts() is active on this thread."""
        sink = getattr(self._draft_local, 'sink', None)
        if sink is None:
            return parse_envelope(self._post(self._body(packet, chosen, errors, model, include_image), headers, budget),
                                  model)
        sink.call += 1
        body = self._body(packet, chosen, errors, model, include_image, stream=True)
        return parse_envelope(self._post_stream(body, headers, budget, sink, sink.call), model)

    def probe(self, timeout_s: float) -> None:
        """One 1-token completion on the text model (same URL, model and key as an assessment); raises
        GenerationError when the server does not answer with a JSON chat completion within `timeout_s`."""
        body = {'messages': [{'role': 'user', 'content': 'Reply with ok.'}], 'max_tokens': 1, 'temperature': 0,
                'stream': False, 'chat_template_kwargs': {'enable_thinking': False}}
        if self.model:
            body['model'] = self.model
        headers = {'Content-Type': 'application/json'}
        if self._api_key:
            headers['Authorization'] = 'Bearer %s' % self._api_key
        data = self._post(json.dumps(body).encode('utf-8'), headers, timeout_s)
        try:
            ok = isinstance(json.loads(data).get('choices'), list)
        except (ValueError, AttributeError):
            ok = False
        if not ok:
            raise GenerationError('envelope', 'not a chat completion')

    def _body(self, packet, chosen, errors, model, include_image, stream: bool = False) -> bytes:
        return json.dumps(request_body(packet, chosen, errors, model, self.max_tokens, include_image, stream),
                          ensure_ascii=False).encode('utf-8')

    def _read_body(self, resp, deadline: float, expired: threading.Event, late: str) -> bytes:
        """A whole non-stream body under the deadline and max_response_bytes (the caller maps OSError/timeouts)."""
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
            return self._read_body(resp, deadline, expired, late)
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


    def _post_stream(self, body: bytes, headers: dict, budget: float, sink: _DraftSink, call: int) -> bytes:
        """Streaming POST under _post's rules (deadline watchdog, non-2xx excerpt, size caps); reads the SSE chunks,
        reports the growing text to `sink`, and returns a synthetic non-stream envelope for parse_envelope."""
        deadline = time.monotonic() + budget
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=budget)    # bounds connect; the watchdog bounds the rest
        expired, timer = threading.Event(), None
        late = 'response not complete within %.1f s' % budget
        raw_cap = STREAM_RAW_FACTOR * self.max_response_bytes
        text, tokens, size, raw, buf = '', 0, 0, 0, b''
        finish = usage = timings = model = None
        saw_choices = done = False

        def handle(line: bytes) -> bool:
            """One SSE line; True at [DONE]."""
            nonlocal text, tokens, size, finish, usage, timings, model, saw_choices
            line = line.rstrip(b'\r')
            if not line.startswith(b'data:'):
                return False                   # blank separator, ': comment', event:/id:/retry: fields
            payload = line[5:].strip()
            if payload == b'[DONE]':
                return True
            if not payload:
                return False
            try:
                chunk = json.loads(payload.decode('utf-8'), parse_constant=_reject_constant)
            except (UnicodeDecodeError, ValueError) as exc:
                raise GenerationError('protocol', 'stream chunk is not finite JSON: %s' % type(exc).__name__) from None
            if not isinstance(chunk, dict):
                raise GenerationError('protocol', 'stream chunk is not a JSON object')
            if chunk.get('error') is not None:
                raise _stream_error(chunk['error'])
            choices = chunk.get('choices')
            if choices is not None and not isinstance(choices, list):
                raise GenerationError('protocol', 'stream chunk choices is not a list')
            if choices:
                first = choices[0]
                if not isinstance(first, dict):
                    raise GenerationError('protocol', 'stream chunk choice is not an object')
                saw_choices = True
                delta = first.get('delta')
                piece = delta.get('content') if isinstance(delta, dict) else None
                if piece is not None and not isinstance(piece, str):
                    raise GenerationError('protocol', 'choice delta content is not a string')
                if piece:
                    size += len(piece.encode('utf-8', 'surrogatepass'))
                    if size > self.max_response_bytes:
                        raise GenerationError('oversized', 'response over %d bytes' % self.max_response_bytes)
                    text, tokens = text + piece, tokens + 1
                    sink.notify(call, tokens, text)
                if first.get('finish_reason') is not None:
                    finish = first['finish_reason']
            if isinstance(chunk.get('usage'), dict):
                usage = chunk['usage']
            if isinstance(chunk.get('timings'), dict):
                timings = chunk['timings']
            if isinstance(chunk.get('model'), str) and chunk['model'].strip():
                model = chunk['model']
            return False

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
            ctype = (resp.getheader('Content-Type') or '').split(';', 1)[0].strip().lower()
            if ctype != 'text/event-stream':   # upstream ignored `stream: true`: a plain completion, no drafts
                LOG.info('upstream answered a streamed call with %r, read as a plain completion', ctype or 'no type')
                return self._read_body(resp, deadline, expired, late)
            while not done:
                if expired.is_set() or time.monotonic() > deadline:
                    raise GenerationError('timeout', late)
                data = resp.read1(CHUNK)       # returns as soon as some bytes arrive (chunked or not)
                if not data:
                    break
                raw += len(data)
                if raw > raw_cap:
                    raise GenerationError('oversized', 'stream over %d bytes' % raw_cap)
                buf += data
                while not done:
                    cut = buf.find(b'\n')
                    if cut < 0:
                        break
                    line, buf = buf[:cut], buf[cut + 1:]
                    done = handle(line)
            if expired.is_set():          # EOF caused by the watchdog is a timeout, not a complete stream
                raise GenerationError('timeout', late)
            if not done and buf.strip():
                done = handle(buf)        # last line without a trailing newline
            if not done and finish is None:
                raise GenerationError('protocol', 'stream ended before [DONE]')
            if not saw_choices:
                raise GenerationError('protocol', 'response has no choices')
            envelope = {'choices': [{'message': {'content': text}, 'finish_reason': finish}],
                        'usage': usage, 'timings': timings, 'model': model}
            return json.dumps(envelope).encode('ascii')   # ASCII escapes keep any lone surrogate intact
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
