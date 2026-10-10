"""SPOT operator dashboard backend (FastAPI), port 8000.

Route tree (docs/backend-services-contract.md): /health and /api/v1/{events, replay, flags, missions, system1,
assets, locations, insights, search, ops}. Run: `uvicorn app.main:app --app-dir backend --port 8000`.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import psycopg
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings
from .db.pool import DatabaseUnavailable
from .deps import AdminAuthError, Services, build_services
from .routers import (ask, assets, events, flags, health, insights, locations, missions, ops, replay, search,
                      system1)
from .services.calllog import request_id_var
from .services.flags import FlagNotFound, RequestDeadlineExceeded

VERSION = "1.0.0"
SERVERLESS_LOG_DRAIN_S = 2.0
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
]


def _err(status: int, code: str, detail: str) -> JSONResponse:
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
        t0 = time.perf_counter()
        status, error = 500, None
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        except Exception as exc:  # pragma: no cover - handled by the exception handlers below
            error = str(exc)
            raise
        finally:
            path = request.url.path
            if settings.log_requests and not (path == "/health" or path.startswith(("/docs", "/openapi", "/redoc"))):
                route = request.scope.get("route")
                app.state.svc.sink.request(request_id=rid, route=getattr(route, "path", path),
                                           method=request.method, status=status,
                                           latency_ms=int((time.perf_counter() - t0) * 1000), error=error)
            if settings.serverless:
                # The instance may freeze once the response is sent: finish this request's log writes first.
                await app.state.svc.sink.drain(timeout_s=SERVERLESS_LOG_DRAIN_S)
            request_id_var.reset(token)

    @app.exception_handler(AdminAuthError)
    async def _admin(request: Request, exc: AdminAuthError):
        return _err(exc.status, exc.code, exc.detail)

    @app.exception_handler(RequestDeadlineExceeded)
    async def _deadline(request: Request, exc: RequestDeadlineExceeded):
        return _err(504, "deadline_exceeded", str(exc))

    @app.exception_handler(DatabaseUnavailable)
    async def _db_down(request: Request, exc: DatabaseUnavailable):
        return _err(503, "database_unavailable", str(exc))

    @app.exception_handler(psycopg.Error)
    async def _db_error(request: Request, exc: psycopg.Error):
        log.warning("database error on %s: %s", request.url.path, exc)
        return _err(503, "database_error", "%s: %s" % (type(exc).__name__, str(exc).strip()[:300]))

    @app.exception_handler(FlagNotFound)
    async def _no_flag(request: Request, exc: FlagNotFound):
        return _err(404, "not_found", "no flag with flag_id=%s" % exc.args[0])

    @app.exception_handler(ValueError)
    async def _bad_value(request: Request, exc: ValueError):
        return _err(422, "invalid_request", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        # custom_encoder: a field_validator's ValueError sits in ctx.error and is not JSON serializable.
        detail = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
        return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": detail,
                                                      "request_id": request_id_var.get()})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return _err(500, "internal_error", "%s: %s" % (type(exc).__name__, exc))

    app.include_router(health.router)
    for r in (events, replay, flags, missions, system1, assets, locations, insights, search, ops, ask):
        app.include_router(r.router, prefix="/api/v1")
    return app


def _default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


app = _default_app()
