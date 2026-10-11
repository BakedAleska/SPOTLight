"""SPOT operator dashboard backend (FastAPI), port 8000.

Route tree (docs/backend-services-contract.md): /health and /api/v1/{events, replay, flags, missions, system1,
assets, locations, insights, search, ops, ask, chat, live}; `chat` (docs/chat-contract.md) streams answers as SSE.
Run: `uvicorn app.main:app --app-dir backend --port 8000`.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import psycopg
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings
from .db.pool import DatabaseUnavailable
from .deps import AdminAuthError, Services, build_services
from .routers import (ask, assets, chat, events, flags, health, insights, live, locations, missions, ops,
                      replay, search, system1)
from .services.calllog import redact, request_id_var
from .services.flags import FlagNotFound, RequestDeadlineExceeded
from .services.live import LiveError

VERSION = "1.0.0"
SERVERLESS_LOG_DRAIN_S = 2.0
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
UNLOGGED_PREFIXES = ("/docs", "/openapi", "/redoc")
HTTP_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
              409: "conflict", 422: "invalid_request", 500: "internal_error", 503: "unavailable"}
log = logging.getLogger("backend")

TAGS = [
    {"name": "health", "description": "Liveness and dependency status."},
    {"name": "events", "description": "Ingest SPOT events through the 7-step pipeline; read the live feed."},
    {"name": "replay", "description": "Stream the kit's replay.ndjson through the pipeline (background task, or "
                                      "stepped by POST /replay/step on serverless)."},
    {"name": "flags", "description": "Escalated events: list, detail, System 2 context, explanation, operator ack."},
    {"name": "missions", "description": "Missions with True Mission Health, timelines and debriefs."},
    {"name": "system1", "description": "Laya (System 1) proxy and pre-action failure forecasts."},
    {"name": "assets", "description": "Inspected equipment."},
    {"name": "locations", "description": "Waypoints with reliability."},
    {"name": "insights", "description": "Headline insights and reliability breakdowns."},
    {"name": "search", "description": "RAG search over documents (hybrid vector + full text)."},
    {"name": "ops", "description": "Model-call latency/throughput, jobs, demo reset."},
    {"name": "live", "description": "Live simulation: an operator queues a route, the live runner walks it and every "
                                    "stop is triaged (Laya + rules), photographed (simulated) and explained (Qwen)."},
    {"name": "chat", "description": "AI Assistance: stored chats answered by Gemini from the project database "
                                    "(RAG documents, knowledge graph, SQL), streamed as server-sent events."},
]


DRAFT_SUFFIX = "/draft"      # POST /api/v1/live/runs/{id}/stops/{i}/draft


def should_log_request(mode: str, path: str, method: str, status: int, latency_ms: int, matched: bool,
                       slow_ms: int) -> bool:
    """Whether app.request_log gets this request (LOG_REQUESTS, docs/backend-services-contract.md). `writes` keeps
    writes, errors and slow reads, and drops the dashboard's 2-2.5 s polls (97% of the table, fix plan DP-08) and
    404s that matched no route (`/`, `/favicon.ico`, scanner probes). /health and the API docs are never logged."""
    if mode == "off" or path == "/health" or path.startswith(UNLOGGED_PREFIXES):
        return False
    if mode == "all":
        return True
    if status == 404 and not matched:
        return False
    if path.endswith(DRAFT_SUFFIX) and status < 400 and latency_ms < slow_ms:
        return False    # the live runner's Qwen draft relay: a POST every LIVE_DRAFT_INTERVAL_S per stop, poll-like
    return method not in READ_METHODS or status >= 400 or latency_ms >= slow_ms


def _log_text(code: str, detail: object) -> str:
    """`<code>: <detail[:200]>` for app.request_log.error. Redacted before the cut: a key cut in half by the
    200-character cap no longer matches a pattern, so redacting afterwards (in LogSink) could store half a key."""
    text = detail if isinstance(detail, str) else json.dumps(detail, separators=(",", ":"), default=str)
    return "%s: %s" % (code, (redact(text) or "")[:200])


def _note_error(request: Request, code: str, detail: object) -> None:
    """What the request-log middleware stores in app.request_log.error for a handled 4xx/5xx."""
    request.state.log_error = _log_text(code, detail)


def _err(request: Request, status: int, code: str, detail: str) -> JSONResponse:
    _note_error(request, code, detail)
    return JSONResponse(status_code=status, content={"error": code, "detail": detail,
                                                     "request_id": request_id_var.get()})


def create_app(settings: Optional[Settings] = None, services: Optional[Services] = None) -> FastAPI:
    settings = settings or (services.settings if services else Settings.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc: Services = app.state.svc
        await svc.open()
        log.info("backend up: triage_mode=%s replay_mode=%s serverless=%s system1=%s db=%s", settings.triage_mode,
                 settings.replay_mode, settings.serverless, settings.system1_url,
                 "set" if settings.database_url else "unset")
        try:
            yield
        finally:
            await svc.close()

    app = FastAPI(
        title="SPOT Operator Dashboard API", version=VERSION, lifespan=lifespan, openapi_tags=TAGS,
        description="Backend for the NextEra SPOT challenge dashboard. Every SPOT event runs through System 1 "
                    "(Laya) and the deterministic validator; escalated events become flags that System 2 explains "
                    "and operators acknowledge. Every external dependency has a fallback, so the demo path "
                    "never returns a 500.")
    app.state.svc = services or build_services(settings)
    # Audit H3: only the dashboard's origin(s) (CORS_ORIGINS), never "*" by default.
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["X-Request-ID"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or "r-" + uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        # Created before call_next so the exception handlers below write into this same state (scope["state"]).
        request.state.log_error = None
        t0 = time.perf_counter()
        status, error = 500, None
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        except Exception as exc:  # pragma: no cover - handled by the exception handlers below
            error = _log_text("internal_error", "%s: %s" % (type(exc).__name__, exc))
            raise
        finally:
            ms = int((time.perf_counter() - t0) * 1000)
            route = request.scope.get("route")
            if should_log_request(settings.log_requests_mode, request.url.path, request.method, status, ms,
                                  route is not None, settings.log_slow_ms):
                if status >= 400:
                    # A response that bypassed the handlers (routers/chat.py's own _err, CORS's 400 for a rejected
                    # preflight) still gets its HTTP code, so a 4xx/5xx row never has error NULL.
                    error = (error or getattr(request.state, "log_error", None)
                             or "%s: HTTP %d" % (HTTP_CODES.get(status, "http_error"), status))
                app.state.svc.sink.request(request_id=rid, route=getattr(route, "path", request.url.path),
                                           method=request.method, status=status, latency_ms=ms,
                                           error=error if status >= 400 else None)
            if settings.serverless:
                # The instance may freeze once the response is sent: finish this request's log writes first.
                await app.state.svc.sink.drain(timeout_s=SERVERLESS_LOG_DRAIN_S)
            request_id_var.reset(token)

    @app.exception_handler(AdminAuthError)
    async def _admin(request: Request, exc: AdminAuthError):
        return _err(request, exc.status, exc.code, exc.detail)

    @app.exception_handler(RequestDeadlineExceeded)
    async def _deadline(request: Request, exc: RequestDeadlineExceeded):
        return _err(request, 504, "deadline_exceeded", str(exc))

    @app.exception_handler(DatabaseUnavailable)
    async def _db_down(request: Request, exc: DatabaseUnavailable):
        return _err(request, 503, "database_unavailable", str(exc))

    @app.exception_handler(psycopg.Error)
    async def _db_error(request: Request, exc: psycopg.Error):
        log.warning("database error on %s: %s", request.url.path, exc)
        return _err(request, 503, "database_error", "%s: %s" % (type(exc).__name__, str(exc).strip()[:300]))

    @app.exception_handler(LiveError)
    async def _live(request: Request, exc: LiveError):
        code = {404: "not_found", 409: "conflict", 413: "too_large"}.get(exc.status, "error")
        return _err(request, exc.status, "live_" + code, exc.detail)

    @app.exception_handler(FlagNotFound)
    async def _no_flag(request: Request, exc: FlagNotFound):
        return _err(request, 404, "not_found", "no flag with flag_id=%s" % exc.args[0])

    @app.exception_handler(ValueError)
    async def _bad_value(request: Request, exc: ValueError):
        return _err(request, 422, "invalid_request", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        # custom_encoder: a field_validator's ValueError sits in ctx.error and is not JSON serializable.
        detail = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
        _note_error(request, "invalid_request", detail)
        return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": detail,
                                                      "request_id": request_id_var.get()})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        # The routers' HTTPException(404, "no mission X"): same response as FastAPI's default, plus the log error.
        _note_error(request, HTTP_CODES.get(exc.status_code, "http_%d" % exc.status_code), exc.detail)
        return await http_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # Runs outside the request-log middleware, which has already logged this 500 with the exception text.
        log.exception("unhandled error on %s", request.url.path)
        return _err(request, 500, "internal_error", "%s: %s" % (type(exc).__name__, exc))

    app.include_router(health.router)
    for r in (events, replay, flags, missions, system1, assets, locations, insights, search, ops, ask, chat,
              live):
        app.include_router(r.router, prefix="/api/v1")
    return app


def _default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


app = _default_app()
