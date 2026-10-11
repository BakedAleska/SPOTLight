"""System 1 (Laya) inference service: FastAPI app, port 8082.

    POST /v1/predict    canonical events -> calibrated answers to their typed questions
    GET  /v1/questions  the question bank and per-kind question sets
    GET  /v1/model      checkpoint, fine-tuned flag, temperatures, training summary, device
    GET  /health        {"ok": true, "model_loaded": true}

Contract: docs/backend-services-contract.md ("System 1 service"). Config: service/config.py (env vars).
"""
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import List, Optional

os.environ.setdefault("TORCH_COMPILE_DISABLE", "1")   # eager mode everywhere (ROCm-safe), before torch loads

from fastapi import FastAPI, Request                                    # noqa: E402
from fastapi.encoders import jsonable_encoder                           # noqa: E402
from fastapi.exceptions import RequestValidationError                   # noqa: E402
from fastapi.responses import JSONResponse                              # noqa: E402
from pydantic import ValidationError                                    # noqa: E402

from triage.questions import QUESTION_SETS, QUESTIONS                   # noqa: E402

from .calllog import CallLogger                                         # noqa: E402
from .config import Settings                                            # noqa: E402
from .schemas import EVENT_MODELS, PredictRequest, PredictResponse      # noqa: E402

log = logging.getLogger("system1")


def parse_jobs(req: PredictRequest) -> list:
    """Validate every item's event against its kind and its question subset. Collects all errors and
    raises one RequestValidationError whose `loc`s name the exact field (e.g. items.0.event.asset.criticality)."""
    errors, jobs = [], []
    for i, item in enumerate(req.items):
        base = ("body", "items", i)
        allowed = QUESTION_SETS[item.kind]
        ev_kind = item.event.get("kind")
        if ev_kind is not None and ev_kind != item.kind:
            errors.append({"type": "value_error", "loc": base + ("event", "kind"),
                           "msg": "event.kind %r does not match item kind %r" % (ev_kind, item.kind),
                           "input": ev_kind})
        try:
            event = EVENT_MODELS[item.kind].model_validate(item.event).model_dump()
        except ValidationError as err:
            for e in err.errors(include_url=False, include_context=False):
                errors.append(dict(e, loc=base + ("event",) + tuple(e["loc"])))
            event = None
        qids = list(allowed)
        if item.questions is not None:
            if not item.questions:
                errors.append({"type": "value_error", "loc": base + ("questions",),
                               "msg": "questions must be omitted or non-empty", "input": item.questions})
            for j, q in enumerate(item.questions):
                if q not in allowed:
                    errors.append({"type": "value_error", "loc": base + ("questions", j),
                                   "msg": "%r is not a %s question; allowed: %s" % (q, item.kind, ", ".join(allowed)),
                                   "input": q})
            qids = list(dict.fromkeys(item.questions))       # requested order, de-duplicated
        if event is not None:
            jobs.append((item.kind, event, qids))
    if errors:
        raise RequestValidationError(errors)
    return jobs


def load_engine(engine_cls, settings: Settings):
    """The configured checkpoint, else LAYA_FALLBACK_CHECKPOINT (e.g. a private fine-tune with no HF_TOKEN)."""
    name = settings.checkpoint + ("/" + settings.checkpoint_subdir if settings.checkpoint_subdir else "")
    try:
        eng = engine_cls.load(settings.checkpoint, settings.device, settings.amp, settings.max_batch,
                              subdir=settings.checkpoint_subdir)
        eng.checkpoint = name
        return eng
    except Exception as exc:
        fallback = settings.fallback_checkpoint
        if not fallback or fallback == settings.checkpoint:
            raise
        log.warning("could not load %s (%s: %s); falling back to %s", name, type(exc).__name__,
                    str(exc).splitlines()[0][:200] if str(exc) else "", fallback)
        return engine_cls.load(fallback, settings.device, settings.amp, settings.max_batch)


def create_app(settings: Optional[Settings] = None, engine=None) -> FastAPI:
    """`engine` is injectable for tests; otherwise the checkpoint is loaded once at startup."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        eng = engine
        if eng is None:
            from .engine import Engine
            eng = load_engine(Engine, settings)
            if settings.warmup:
                log.info("warmup: %.2fs", eng.warmup())
        app.state.engine = eng
        app.state.calllog = CallLogger(settings.logs_database_url)
        log.info("System 1 ready: %s on %s (model_calls logging %s)", eng.checkpoint, eng.device,
                 "on" if app.state.calllog.enabled else "off")
        yield
        app.state.calllog.close()

    app = FastAPI(title="SPOT System 1 (Laya)", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def on_invalid(_: Request, exc: RequestValidationError):
        errs = exc.errors()
        first = errs[0] if errs else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return JSONResponse(status_code=422, content=jsonable_encoder(
            {"error": "%s: %s" % (where, first.get("msg", "invalid request")), "detail": errs}))

    @app.get("/health")
    def health(request: Request):
        eng = getattr(request.app.state, "engine", None)
        return {"ok": eng is not None, "model_loaded": eng is not None,
                "device": eng.device if eng is not None else None}

    @app.get("/v1/questions")
    def questions():
        return {"questions": QUESTIONS, "question_sets": QUESTION_SETS}

    @app.get("/v1/model")
    def model(request: Request):
        return request.app.state.engine.model_info()

    @app.post("/v1/predict", response_model=PredictResponse)
    def predict(req: PredictRequest, request: Request):
        jobs = parse_jobs(req)
        eng = request.app.state.engine
        calllog: CallLogger = request.app.state.calllog
        single_action: List[str] = [j[1].get("action_id") for j in jobs if j[1].get("action_id")]
        meta = {"items": len(jobs), "kinds": sorted({j[0] for j in jobs}), "ids": [it.id for it in req.items][:64],
                "fine_tuned": eng.fine_tuned}
        t0 = time.perf_counter()
        try:
            answers, stats = eng.predict(jobs)
        except Exception as err:
            ms = int(round(1000 * (time.perf_counter() - t0)))
            log.exception("predict failed")
            calllog.log(request_id=req.request_id, model=eng.checkpoint, endpoint="/v1/predict", latency_ms=ms,
                        device=eng.device, ok=False, error="%s: %s" % (type(err).__name__, err), metadata=meta)
            return JSONResponse(status_code=500, content={"error": "inference failed: %s" % type(err).__name__})
        ms = int(round(1000 * (time.perf_counter() - t0)))
        calllog.log(request_id=req.request_id, model=eng.checkpoint, endpoint="/v1/predict", latency_ms=ms,
                    device=eng.device, ok=True, action_id=single_action[0] if len(jobs) == 1 and single_action else None,
                    metadata=dict(meta, decisions=stats["decisions"], forward_ms=stats["forward_ms"]))
        return {"request_id": req.request_id, "model": eng.checkpoint, "fine_tuned": eng.fine_tuned,
                "device": eng.device, "latency_ms": ms,
                "items": [{"id": it.id, "kind": it.kind, "answers": a} for it, a in zip(req.items, answers)]}

    return app


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()     # cheap: the model loads in the lifespan, when the server starts
