from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from ..deps import Services, get_services
from ..schemas.common import Health, ServiceHealth

router = APIRouter(tags=["health"])


@router.get("/health", response_model=Health, response_model_exclude_none=True)
async def health(deep: bool = Query(True, description="Probe every dependency (each with a ~2 s timeout). "
                                                     "`deep=false` answers instantly (Docker HEALTHCHECK)."),
                 svc: Services = Depends(get_services)) -> Health:
    """Liveness plus the status of every dependency: database, System 1 (Laya), embedding service, MCP server,
    System 2 and the orchestrator. `ok` is true whenever the API itself is serving; a down dependency only switches
    the affected endpoints to their fallbacks."""
    from ..main import VERSION

    s = svc.settings
    out = Health(ok=True, version=VERSION, triage_mode=s.triage_mode,  # type: ignore[arg-type]
                 replay=svc.replay.status(),
                 kit={"dir": str(svc.ref.kit_dir), "missions": len(svc.ref.missions), "actions": len(svc.ref.actions),
                      "replay_file": svc.ref.replay_path.exists()})
    if not deep:
        return out

    async def db_status(db):
        if not db.configured:
            return {"status": "unset"}
        return {"status": "up" if await db.ping() else "down"}

    async def svc_status(client, url):
        res = await client.health()
        res.setdefault("url", url)
        if getattr(client, "breaker", None) is not None:
            res["breaker"] = client.breaker.state
        return res

    results = await asyncio.gather(
        db_status(svc.db), db_status(svc.logs_db), svc_status(svc.system1, s.system1_url),
        svc_status(svc.embed, s.embed_url), svc_status(svc.mcp, s.mcp_url),
        svc_status(svc.system2, s.system2_url), svc_status(svc.orchestrator, s.orchestrator_url),
        return_exceptions=True)
    names = ["database", "logs_database", "system1", "embed", "mcp", "system2", "orchestrator"]
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            res = {"status": "down", "error": str(res)}
        res = {k: v for k, v in res.items() if k in ("status", "url", "error", "breaker", "http", "model_loaded",
                                                     "model_status")}
        setattr(out, name, ServiceHealth(**res))
    return out
