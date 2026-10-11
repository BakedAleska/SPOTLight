"""Backend packet assessment API; legacy fixture loaders remain available offline.

Stub by default. create_app(assessor=...) switches /v1/assess to live mode: any object with .assess(body) ->
(BackendAssessResponse, cache_status) and .model (analyst.live.LiveAssessor) is called after the X-API-Key middleware
and normalize_packet, and the cache status goes out as X-System2-Cache. This module never imports the generator.
"""
from contextlib import asynccontextmanager
import hmac
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .contract import AssessRequest, AssessResponse
from .backend_contract import BACKEND_CONTRACT_VERSION, BackendAssessRequest, BackendAssessResponse
from .packet import normalize_packet, assess_prepared_packet

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
MIN_KEY_LEN = 16
KEY_HEADER = "X-API-Key"
OPEN_PATHS = frozenset({"/health"})
CACHE_HEADER = "X-System2-Cache"
IMAGE_HEADER = "X-System2-Image"   # used | text_only | not_used; only when the request carried an image (backend.2)
FIXTURE_KEYS = {"name", "description", "request", "response"}

Fixture = Tuple[str, AssessRequest, AssessResponse]


def load_fixtures(fixtures_dir: Path = FIXTURES) -> List[Fixture]:
    """[(name, request, response)] in sorted filename order; ValueError on any malformed fixture."""
    paths = sorted(Path(fixtures_dir).glob("*.json"))
    if not paths:
        raise ValueError("no *.json fixtures in %r" % str(fixtures_dir))
    out = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or set(d) != FIXTURE_KEYS:
            raise ValueError("fixture %r: top-level keys must be %r, got %r"
                             % (p.name, sorted(FIXTURE_KEYS), sorted(d) if isinstance(d, dict) else type(d)))
        if d["name"] != p.stem:
            raise ValueError("fixture %r: name %r differs from file stem %r" % (p.name, d["name"], p.stem))
        req = AssessRequest.model_validate(d["request"])
        resp = AssessResponse.model_validate(d["response"])
        if resp.flag_id != req.flag.flag_id:
            raise ValueError("fixture %r: response.flag_id %r != request flag_id %r"
                             % (p.name, resp.flag_id, req.flag.flag_id))
        out.append((d["name"], req, resp))
    return out


def key_problem(key: str) -> Optional[str]:
    """Why `key` cannot be the shared secret, or None. Never includes the key itself.

    Header values reach the app as latin-1 text with surrounding whitespace stripped (h11), so a key with padding,
    non-ASCII or control characters would start fine and then fail every real request.
    """
    if len(key) < MIN_KEY_LEN:
        return "it has fewer than %d characters" % MIN_KEY_LEN
    if not key.strip():
        return "it is only whitespace"
    if key != key.strip():
        return "it has leading or trailing whitespace"
    if not key.isascii():
        return "it has non-ASCII characters"
    if not key.isprintable():
        return "it has non-printable characters"
    return None


def check_key(key: str) -> None:
    """RuntimeError unless `key` is a usable shared secret; the message never contains the key."""
    why = key_problem(key)
    if why is not None:
        raise RuntimeError("AMD_API_KEY must be a secret of at least %d printable ASCII characters with no surrounding "
                           "whitespace; %s (the key is never logged)" % (MIN_KEY_LEN, why))


def resolve_api_key(api_key: Optional[str] = None) -> str:
    """Resolve the shared factory/launcher environment contract or explicit injection."""
    if api_key is None:
        canonical = os.environ.get("SYSTEM2_API_KEY")
        alias = os.environ.get("AMD_API_KEY")
        if canonical is not None and alias is not None and canonical != alias:
            raise RuntimeError("SYSTEM2_API_KEY and AMD_API_KEY disagree (keys are never logged)")
        api_key = canonical if canonical is not None else (alias or "")
    check_key(api_key)
    return api_key


def create_app(api_key: Optional[str] = None, fixtures_dir: Path = FIXTURES, assessor=None) -> FastAPI:
    key = resolve_api_key(api_key)
    expected = key.encode("ascii")

    # No /docs, /redoc or /openapi.json: the committed schema/*.json files are the published contract, and the
    # interactive docs would otherwise hand the contract and a request form to anyone who can reach the port.
    probe = getattr(assessor, "probe", None)

    @asynccontextmanager
    async def lifespan(_app):
        # live mode: the model probe thread feeds /health model_status (docs/backend-services-contract.md)
        if probe is not None:
            probe.start()
        yield
        if probe is not None:
            probe.stop()

    app = FastAPI(title="SPOTlight System 2 Analyst (stub)", version=BACKEND_CONTRACT_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def key_first(request: Request, call_next):
        if request.url.path not in OPEN_PATHS:
            given = request.headers.get(KEY_HEADER)       # Starlette decodes header bytes as latin-1
            if given is None or not hmac.compare_digest(given.encode("latin-1"), expected):
                return JSONResponse({"detail": "missing or invalid X-API-Key"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        if assessor is None:
            return {"status": "ok", "mode": "stub", "contract_version": BACKEND_CONTRACT_VERSION}
        out = {"status": "ok", "mode": "live", "contract_version": BACKEND_CONTRACT_VERSION, "model": assessor.model}
        if hasattr(assessor, "model_health"):
            out.update(assessor.model_health())
        return out

    @app.post("/v1/assess", response_model=BackendAssessResponse)
    def assess(body: BackendAssessRequest, response: Response) -> BackendAssessResponse:
        t0 = time.perf_counter()
        try:
            prepared = normalize_packet(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if assessor is not None:
            image = None
            try:
                if hasattr(assessor, "assess_image"):
                    result, status, _, image = assessor.assess_image(body)
                else:
                    result, status = assessor.assess(body)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            response.headers[CACHE_HEADER] = status
            if prepared.image is not None:
                response.headers[IMAGE_HEADER] = image or "not_used"
            return result
        if prepared.image is not None:     # the stub never sends anything to a model
            response.headers[IMAGE_HEADER] = "not_used"
        result = assess_prepared_packet(prepared)
        return result.model_copy(update={"latency_ms": int((time.perf_counter() - t0) * 1000)})

    return app
