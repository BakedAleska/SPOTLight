"""The SPOT context MCP server (MCP Python SDK 2.x `MCPServer`, formerly `FastMCP`).

Run:  python -m spot_mcp                      (stdio, for local MCP clients)
      python -m spot_mcp --http --port 8090    (streamable HTTP at /mcp, for the System 2 worker)
      asgi.py                                  (the same HTTP app, as a Vercel Function; see README)
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import sys
from typing import Annotated, Any, Awaitable, Callable, Dict, Literal, Optional

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import domain as D
from .config import Settings
from .db import NullLogSink, PgDatabase, PgLogSink
from .embed import EmbeddingError, HttpEmbedder
from .repo import PgRepo
from .service import ContextService

log = logging.getLogger("spot_mcp")

INSTRUCTIONS = """Read-only context for explaining SPOT robot inspection flags to plant operators.
Start with get_flag_context(flag_id): it returns one packet with the flag, the event, derived facts (F1..),
knowledge-graph facts, similar incidents, manual passages and policy rules, the allowed actions, and the question.
Usually that is enough. Only call another tool when the packet's `gaps` say something is missing; graph_neighborhood
and search_graph walk the knowledge graph (assets, waypoints, failure types, manual topics, failure statistics).
Cite the `id` of every item you use.
Never invent ids or numbers. If a search says no_relevant_results, don't cite it. If insufficient_context is true,
say what is missing instead of guessing. recommended_action must be one of allowed_actions."""

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

FlagId = Annotated[int, Field(ge=1, description="Numeric flag id, e.g. 42")]
K = Annotated[int, Field(ge=1, le=10, description="Max results (1-10)")]
Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$", max_length=64)]
Query = Annotated[str, Field(min_length=2, max_length=500, description="Natural-language search text")]
ActionType = Literal["ACOUSTIC_INSPECTION", "GAUGE_READING", "NAVIGATION", "THERMAL_INSPECTION", "VISUAL_INSPECTION"]
Category = Literal["equipment", "data_quality", "navigation", "robot_health", "mission", "none", "all"]
Band = Literal["<=25", "26-40", "41-60", "61-80", "81+"]
GroupBy = Literal["action_type", "location_id", "configuration_version", "battery_band"]
Hops = Annotated[int, Field(ge=1, le=3, description="How many edges to walk out from the entry node(s) (1-3)")]
Entity = Annotated[str, Field(min_length=1, max_length=120, description=(
    "A node_id ('asset:HX-204', 'location:WP09', 'failure_type:LOCALIZATION_FAILURE', 'topic:docking') "
    "or a name/id ('HX-204', 'WP09', 'Turbine Hall South')"))]


def _request_id(ctx: Optional[Context]) -> Optional[str]:
    """request_id from the call's _meta, or the X-Request-Id header over HTTP."""
    if ctx is None:
        return None
    try:
        meta = ctx.request_context.meta or {}
        rid = meta.get("request_id")
        if rid:
            return str(rid)[:128]
        hdrs = ctx.headers or {}
        rid = hdrs.get("x-request-id") or hdrs.get("X-Request-Id")
        return str(rid)[:128] if rid else None
    except Exception:  # noqa: BLE001
        return None


def _result(d: Dict[str, Any]) -> CallToolResult:
    # Compact JSON text for small models; the same dict as structured content for programmatic clients.
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(d, separators=(",", ":"),
                                                                             ensure_ascii=False))],
                          structured_content=d)


def build_server(service: ContextService) -> MCPServer:
    mcp = MCPServer("spot-context", instructions=INSTRUCTIONS, version="1.0.0")

    async def run(tool: str, args: Dict[str, Any], fn: Callable[[], Awaitable[Dict[str, Any]]],
                  ctx: Optional[Context], request_id: Optional[str] = None) -> CallToolResult:
        try:
            return _result(await service.logged(tool, args, fn, request_id=request_id or _request_id(ctx)))
        except (ValueError, EmbeddingError) as exc:
            raise ToolError(str(exc)) from exc  # the model sees the message and can fix its call
        except Exception as exc:  # DB errors etc.: short and safe, no stack trace or connection string
            log.exception("tool %s failed", tool)
            raise ToolError("%s failed (%s); try get_flag_context or another tool" % (tool, type(exc).__name__)) from exc

    # ── Tier 1 ──────────────────────────────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO)
    async def get_flag_context(flag_id: FlagId, ctx: Context) -> CallToolResult:
        """START HERE. Everything needed to explain one flag, in one compact packet (under ~1,500 tokens).

        Contains, in order: the flag and the rules that fired; the event (action with reading, safe band,
        anomaly, asset, waypoint, and the validator's priority/category) or the mission; System 1 (Laya)
        predictions; derived facts F1..Fn (battery risk, waypoint reliability, asset history, mission health);
        graph facts (triples like "LOCALIZATION_FAILURE —common_at→ Turbine Hall South (WP09) (n=6, rate 50%)");
        similar past incidents, asset history, Spot manual passages and policy rules (each with a similarity);
        allowed_actions and suggested_action; citation_ids; insufficient_context + missing; gaps; the question last.
        Cite items by their `id`. If insufficient_context is true, explain what is missing instead of guessing."""
        rid = _request_id(ctx)
        return await run("get_flag_context", {"flag_id": flag_id},
                         lambda: service.get_flag_context(flag_id, request_id=rid), ctx, rid)

    # ── RAG tools ───────────────────────────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO)
    async def search_similar_incidents(ctx: Context, flag_id: Optional[FlagId] = None,
                                       text: Optional[Query] = None, asset_id: Optional[Id] = None,
                                       location_id: Optional[Id] = None, k: K = 3) -> CallToolResult:
        """Past incidents (anomalies and resolved flags with their outcomes) similar to a flag or a description.

        Give flag_id (preferred; the flag itself is excluded) or text. Optional asset_id / location_id
        (e.g. 'HX-204', 'WP07') restrict to that asset or waypoint. Returns only matches above the similarity
        cutoff; otherwise no_relevant_results=true."""
        rid = _request_id(ctx)
        args = {"flag_id": flag_id, "text": text, "asset_id": asset_id, "location_id": location_id, "k": k}
        return await run("search_similar_incidents", args, lambda: service.search_similar_incidents(
            flag_id, text, asset_id, location_id, k, request_id=rid), ctx)

    @mcp.tool(annotations=RO)
    async def search_asset_history(asset_id: Id, ctx: Context, query: Optional[Query] = None,
                                   k: K = 5) -> CallToolResult:
        """Past inspection events, incidents and operator notes for ONE asset (e.g. 'HX-204', 'PANEL-212').

        Without query: the asset's most relevant history. With query: history matching that text."""
        rid = _request_id(ctx)
        return await run("search_asset_history", {"asset_id": asset_id, "query": query, "k": k},
                         lambda: service.search_asset_history(asset_id, query, k, request_id=rid), ctx)

    @mcp.tool(annotations=RO)
    async def search_spot_manual(query: Query, ctx: Context, k: K = 3) -> CallToolResult:
        """Passages from the Boston Dynamics Spot User Guide (operation, docking/battery, Autowalk,
        localization, payloads, safety). Use for 'how should the robot/operator handle X' questions."""
        rid = _request_id(ctx)
        return await run("search_spot_manual", {"query": query, "k": k},
                         lambda: service.search_spot_manual(query, k, request_id=rid), ctx)

    @mcp.tool(annotations=RO)
    async def search_operator_notes(query: Query, ctx: Context, asset_id: Optional[Id] = None,
                                    k: K = 5) -> CallToolResult:
        """Notes operators wrote when acknowledging or resolving earlier flags. Optional asset_id filter."""
        rid = _request_id(ctx)
        return await run("search_operator_notes", {"query": query, "asset_id": asset_id, "k": k},
                         lambda: service.search_operator_notes(query, asset_id, k, request_id=rid), ctx)

    @mcp.tool(annotations=RO)
    async def search_past_explanations(query: Query, ctx: Context, k: K = 3) -> CallToolResult:
        """Earlier System 2 explanations of similar flags (for consistency). Not evidence by themselves."""
        rid = _request_id(ctx)
        return await run("search_past_explanations", {"query": query, "k": k},
                         lambda: service.search_past_explanations(query, k, request_id=rid), ctx)

    @mcp.tool(annotations=RO)
    async def search_policy(query: Query, ctx: Context, k: K = 3) -> CallToolResult:
        """The triage policy rules the validator applies (priority, category, battery and reading thresholds)."""
        rid = _request_id(ctx)
        return await run("search_policy", {"query": query, "k": k},
                         lambda: service.search_policy(query, k, request_id=rid), ctx)

    # ── Graph RAG ───────────────────────────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO)
    async def graph_neighborhood(entity: Entity, ctx: Context, hops: Hops = 2,
                                 k: Annotated[int, Field(ge=1, le=30, description="Max facts (1-30)")] = 20
                                 ) -> CallToolResult:
        """Knowledge-graph facts around ONE thing: an asset, waypoint, environment, action type, failure or
        anomaly type, policy rule, manual topic or manual page. Each fact is a triple such as
        "Turbine Hall South (WP09) —has_environment→ HIGH_NOISE" or
        "NAVIGATION —fails_more_in→ HIGH_NOISE (n=20, success 45%)", with an `id` to cite.
        Statistics and manual links come first. `entity` is a node_id or a name/id; names are matched by text,
        then by meaning. `resolved_by` says how, and `alternatives` lists other candidates."""
        rid = _request_id(ctx)
        return await run("graph_neighborhood", {"entity": entity, "hops": hops, "k": k},
                         lambda: service.graph_neighborhood(entity, hops, k, request_id=rid), ctx)

    @mcp.tool(annotations=RO)
    async def search_graph(query: Query, ctx: Context, k: K = 5, hops: Hops = 1) -> CallToolResult:
        """Find knowledge-graph nodes for a question (by name/id and by meaning), then their facts within `hops`
        edges. Use for questions that connect things: "which waypoints have localization failures?", "what does
        the manual say about docking at low battery?". Returns entry_nodes (with node_id for graph_neighborhood)
        and facts (triples with an `id` to cite)."""
        rid = _request_id(ctx)
        return await run("search_graph", {"query": query, "k": k, "hops": hops},
                         lambda: service.search_graph(query, k, hops, request_id=rid), ctx)

    # ── SQL tools ───────────────────────────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO)
    async def get_flag(flag_id: FlagId, ctx: Context) -> CallToolResult:
        """One flag: title, severity, score, status, the rules that fired, operator acks/notes, and any
        earlier System 2 explanation."""
        return await run("get_flag", {"flag_id": flag_id}, lambda: service.get_flag(flag_id), ctx)

    @mcp.tool(annotations=RO)
    async def get_system1_forecast(action_id: Id, ctx: Context) -> CallToolResult:
        """System 1 (Laya) predictions for an action (e.g. 'A0136'): pre-action will_fail / failure_mode /
        anomaly_expected compared with what actually happened, and post-action triage answers compared
        with the validator."""
        return await run("get_system1_forecast", {"action_id": action_id},
                         lambda: service.get_system1_forecast(action_id), ctx)

    @mcp.tool(annotations=RO)
    async def get_action(action_id: Id, ctx: Context) -> CallToolResult:
        """One robot action (e.g. 'A0136'): type, status, failure type, battery, asset, waypoint, the reading
        with its safe band and trust, anomalies, and the validator's priority/category/suggested action."""
        return await run("get_action", {"action_id": action_id}, lambda: service.get_action(action_id), ctx)

    @mcp.tool(annotations=RO)
    async def get_mission_summary(mission_id: Id, ctx: Context) -> CallToolResult:
        """One mission (e.g. 'M014'): reported status vs true health (failed actions, HIGH/CRITICAL anomalies),
        battery start/end, failure types, worst anomaly, and whether the reported status is misleading."""
        return await run("get_mission_summary", {"mission_id": mission_id},
                         lambda: service.get_mission_summary(mission_id), ctx)

    @mcp.tool(annotations=RO)
    async def get_reliability_stats(ctx: Context, action_type: Optional[ActionType] = None,
                                    location_id: Optional[Id] = None,
                                    configuration_version: Optional[Literal["V1", "V2"]] = None,
                                    battery_band: Optional[Band] = None,
                                    group_by: Optional[GroupBy] = None) -> CallToolResult:
        """Action success rate with sample size n and a 95% interval, optionally filtered and grouped.
        Rows with n < 20 carry a small-sample warning: say so if you use them."""
        args = {"action_type": action_type, "location_id": location_id,
                "configuration_version": configuration_version, "battery_band": battery_band, "group_by": group_by}
        return await run("get_reliability_stats", args, lambda: service.get_reliability_stats(**args), ctx)

    @mcp.tool(annotations=RO)
    async def get_battery_risk(battery_pct: Annotated[float, Field(ge=0, le=100, description="Battery %")],
                               ctx: Context,
                               configuration_version: Optional[Literal["V1", "V2"]] = None) -> CallToolResult:
        """Risk at a battery level: the policy status (CRITICAL <=15, LOW <=25, WATCH <=35, OK) and the kit's
        action success rate in that battery band versus the other bands."""
        args = {"battery_pct": battery_pct, "configuration_version": configuration_version}
        return await run("get_battery_risk", args,
                         lambda: service.get_battery_risk(battery_pct, configuration_version), ctx)

    @mcp.tool(annotations=RO)
    async def list_allowed_actions(category: Category, ctx: Context) -> CallToolResult:
        """The fixed set of recommended actions for an issue category. recommended_action must be one of them."""
        return await run("list_allowed_actions", {"category": category},
                         lambda: service.list_allowed_actions(category), ctx)

    @mcp.tool(annotations=RO)
    async def get_raw_event_log(mission_id: Id, ctx: Context,
                                around_ts: Optional[Annotated[str, Field(max_length=40, description=
                                    "ISO time, e.g. 2026-09-06T02:15:36; returns the events nearest it")]] = None,
                                limit: Annotated[int, Field(ge=1, le=50)] = 20) -> CallToolResult:
        """The verbatim SPOT events for a mission, in time order, exactly as the robot reported them
        (id log:<log_id>). Use to quote what the robot said."""
        args = {"mission_id": mission_id, "around_ts": around_ts, "limit": limit}
        return await run("get_raw_event_log", args,
                         lambda: service.get_raw_event_log(mission_id, around_ts, limit), ctx)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request):  # noqa: ARG001
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True, "server": "spot-context", "policy": D.POLICY_SOURCE})

    return mcp


def service_from_env(settings: Optional[Settings] = None) -> ContextService:
    s = settings or Settings.from_env()
    if not s.database_url:
        raise SystemExit("DATABASE_URL is not set (see amd/mcp/.env.example)")
    sink = PgLogSink(s.logs_database_url) if (s.log_calls and s.logs_database_url) else NullLogSink()
    embedder = HttpEmbedder(s.embed_url, s.embed_model, s.embed_dim, s.embed_query_prefix, s.embed_timeout_s, sink)
    db = PgDatabase(s.database_url, max_size=s.pool_max, statement_timeout_ms=s.statement_timeout_ms)
    return ContextService(PgRepo(db), embedder, s, sink)


class BearerAuth:
    """Optional shared-secret check for the HTTP transport (MCP_AUTH_TOKEN). /healthz stays open.
    Other scope types (lifespan) pass straight through, so the wrapped app's lifespan still runs."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, ("Bearer " + token).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") != "/healthz":
            hdrs = dict(scope.get("headers") or [])
            if not hmac.compare_digest(hdrs.get(b"authorization", b""), self.expected):
                from starlette.responses import JSONResponse
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def create_app(service: Optional[ContextService] = None, *, host: str = "0.0.0.0", token: Optional[str] = None):
    """The streamable-HTTP ASGI app (/mcp plus an open /healthz), shared by `main()` and `asgi.py` (Vercel).

    Stateless + JSON responses: each POST stands alone, so any instance can answer it and nothing streams.
    The app's ASGI lifespan starts the SDK's session manager (requests fail without it); uvicorn and Vercel's
    Python runtime both run it. `host` only decides the SDK's DNS-rebinding guard, which is on for loopback hosts.
    """
    app = build_server(service or service_from_env()).streamable_http_app(
        stateless_http=True, json_response=True, host=host)
    if token:
        return BearerAuth(app, token)
    if host not in LOOPBACK:
        log.warning("serving on %s without MCP_AUTH_TOKEN: anyone who can reach the port can read the data", host)
    return app


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="SPOT context MCP server")
    p.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    p.add_argument("--stdio", action="store_true", help="force stdio (overrides MCP_TRANSPORT=http)")
    p.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8090")))
    a = p.parse_args(argv)
    http = not a.stdio and (a.http or os.environ.get("MCP_TRANSPORT", "stdio") in ("http", "streamable-http"))
    # stdio: stdout is the protocol channel, so logs must go to stderr.
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = service_from_env()
    if not http:
        build_server(service).run("stdio")
        return
    import uvicorn
    app = create_app(service, host=a.host, token=os.environ.get("MCP_AUTH_TOKEN"))
    log.info("spot-context MCP on http://%s:%d/mcp (policy %s)", a.host, a.port, D.POLICY_SOURCE)
    uvicorn.run(app, host=a.host, port=a.port, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
