"""Settings, read once from the environment (names follow docs/backend-services-contract.md and
docs/system2-rag-contract.md). Every service URL is optional at runtime: when one is unset or down the
backend degrades (see README "Resilience") instead of failing."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# backend/app/config.py -> repo root is two levels above backend/
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent
# Vercel bundles only backend/ (its Root Directory): `vercel_build.py` copies the shared triage code and the kit
# here, mirroring the repo layout. A repo checkout (local, Docker, CI) always wins, so nothing reads the copy there.
_BUNDLED_ROOT = _BACKEND_DIR / "_shared"
TRIAGE_DIR_DEFAULT = Path("amd") / "system1"              # folder holding the `triage` package
KIT_DIR_DEFAULT = Path("amd") / "system1" / "data" / "kit"

DEFAULT_QUERY_PREFIX = ("Instruct: Given an operator question about a SPOT robot inspection, retrieve relevant "
                        "inspection events, anomaly notes, operator notes, and Spot manual passages\nQuery:")


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _unescape(s: str) -> str:
    # .env files often carry "\n" literally; the contract wants a real newline.
    return s.replace("\\n", "\n")


def _rooted(rel: Path) -> Path:
    """A repo-relative path: the checkout's copy when it exists, else the copy bundled by vercel_build.py."""
    repo, bundled = _REPO_ROOT / rel, _BUNDLED_ROOT / rel
    return bundled if not repo.exists() and bundled.exists() else repo


def _path(name: str, default: Path) -> Path:
    v = _env(name)
    if not v:
        return _rooted(default)
    p = Path(v)
    return p if p.is_absolute() else _rooted(p)


def triage_dir_from_env() -> Path:
    """The folder holding the `triage` package (`TRIAGE_DIR`); services/triage.py imports it before Settings exist."""
    return _path("TRIAGE_DIR", TRIAGE_DIR_DEFAULT)


def _truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() not in ("", "0", "false", "no", "off")


def serverless_from_env() -> bool:
    """SERVERLESS=1/0 wins; unset -> on when Vercel's runtime env (VERCEL) is present."""
    v = _env("SERVERLESS")
    return _truthy(v) if v is not None else _truthy(os.environ.get("VERCEL"))


@dataclass
class Settings:
    database_url: Optional[str] = None
    logs_database_url: Optional[str] = None
    system1_url: str = "http://localhost:8082"
    system1_timeout_s: float = 5.0
    system1_ingest_timeout_s: Optional[float] = None   # per-event ingest/replay; None -> system1_timeout_s
    embed_url: str = "http://localhost:8081"
    embed_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embed_dim: int = 1024
    embed_query_prefix: str = DEFAULT_QUERY_PREFIX
    embed_doc_prefix: str = ""
    embed_timeout_s: float = 10.0
    search_embed_timeout_s: float = 4.0
    mcp_url: str = "http://localhost:8090/mcp"
    mcp_timeout_s: float = 15.0
    mcp_auth_token: Optional[str] = None
    system2_url: Optional[str] = None
    system2_api_key: Optional[str] = None
    system2_timeout_s: float = 60.0
    orchestrator_url: Optional[str] = None     # set -> /explain asks the LangGraph orchestrator first; unset -> unchanged
    orchestrator_api_key: Optional[str] = None
    orchestrator_timeout_s: float = 42.0       # REQUEST_DEADLINE_S (45) minus the 3 s reserve; ORCH_DEADLINE_S is 40
    kit_dir: Path = field(default_factory=lambda: _rooted(KIT_DIR_DEFAULT))
    triage_dir: Path = field(default_factory=lambda: _rooted(TRIAGE_DIR_DEFAULT))
    triage_mode: str = "serial"
    replay_speed: float = 60.0
    replay_max_gap_s: float = 2.0
    replay_mode: str = "background"        # background (asyncio task) | stepped (replay_runs + POST /replay/step)
    replay_step_budget_s: float = 10.0
    serverless: bool = False               # Vercel: no state kept between requests (contract "Serverless deployment")
    breaker_cooldown_s: float = 10.0
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_connect_timeout_s: float = 3.0
    cors_origins: List[str] = field(default_factory=lambda: ["http://localhost:3000"])
    admin_token: Optional[str] = None      # X-Admin-Token for /ops, /replay, POST /events (audit H3); unset -> 503
    request_deadline_s: float = 45.0       # overall deadline for /flags/{id}/context and /explain (audit M4)
    log_requests: bool = True
    service_name: str = "backend"

    @classmethod
    def from_env(cls) -> "Settings":
        db = _env("DATABASE_URL")
        prefix = os.environ.get("EMBED_QUERY_PREFIX")
        mode = (_env("TRIAGE_MODE", "serial") or "serial").lower()
        if mode not in ("serial", "parallel"):
            raise ValueError("TRIAGE_MODE must be 'serial' or 'parallel', got %r" % mode)
        serverless = serverless_from_env()
        replay_mode = (_env("REPLAY_MODE", "stepped" if serverless else "background") or "").lower()
        if replay_mode not in ("background", "stepped"):
            raise ValueError("REPLAY_MODE must be 'background' or 'stepped', got %r" % replay_mode)
        return cls(
            database_url=db,
            logs_database_url=_env("LOGS_DATABASE_URL", db),
            system1_url=(_env("SYSTEM1_URL", cls.system1_url) or "").rstrip("/"),
            system1_timeout_s=float(_env("SYSTEM1_TIMEOUT_S", "5") or 5),
            # Ingest/replay fails fast to rules-only; explicit batch calls (forecast, predict) keep SYSTEM1_TIMEOUT_S.
            system1_ingest_timeout_s=float(_env("SYSTEM1_INGEST_TIMEOUT_S")) if _env("SYSTEM1_INGEST_TIMEOUT_S")
            else None,
            embed_url=(_env("EMBED_URL", cls.embed_url) or "").rstrip("/"),
            embed_model=_env("EMBED_MODEL", cls.embed_model) or cls.embed_model,
            embed_dim=int(_env("EMBED_DIM", "1024") or 1024),
            # Unset -> the Qwen3 instruction; set-but-empty -> no prefix (other models, e.g. arctic uses "query: ").
            embed_query_prefix=_unescape(prefix) if prefix is not None else DEFAULT_QUERY_PREFIX,
            embed_doc_prefix=_unescape(os.environ.get("EMBED_DOC_PREFIX", "")),
            # The CPU embed container takes ~2.3 s per query: notes may wait longer, /search falls back after 4 s.
            embed_timeout_s=float(_env("EMBED_TIMEOUT_S", "10") or 10),
            search_embed_timeout_s=float(_env("SEARCH_EMBED_TIMEOUT_S", "4") or 4),
            mcp_url=(_env("MCP_URL", cls.mcp_url) or "").rstrip("/"),
            mcp_timeout_s=float(_env("MCP_TIMEOUT_S", "15") or 15),
            mcp_auth_token=_env("MCP_AUTH_TOKEN"),
            system2_url=(_env("SYSTEM2_URL") or "").rstrip("/") or None,
            system2_api_key=_env("SYSTEM2_API_KEY"),
            system2_timeout_s=float(_env("SYSTEM2_TIMEOUT_S", "60") or 60),
            orchestrator_url=(_env("ORCHESTRATOR_URL") or "").rstrip("/") or None,
            orchestrator_api_key=_env("ORCHESTRATOR_API_KEY"),
            orchestrator_timeout_s=float(_env("ORCHESTRATOR_TIMEOUT_S", "42") or 42),
            kit_dir=_path("KIT_DIR", KIT_DIR_DEFAULT),
            triage_dir=triage_dir_from_env(),
            triage_mode=mode,
            replay_speed=float(_env("REPLAY_SPEED", "60") or 60),
            replay_max_gap_s=float(_env("REPLAY_MAX_GAP_S", "2") or 2),
            replay_mode=replay_mode,
            replay_step_budget_s=float(_env("REPLAY_STEP_BUDGET_S", "10") or 10),
            serverless=serverless,
            breaker_cooldown_s=float(_env("BREAKER_COOLDOWN_S", "10") or 10),
            # Serverless: no idle connection survives a frozen instance, so keep none open between requests.
            db_pool_min=int(_env("DB_POOL_MIN", "0" if serverless else "1") or 0),
            db_pool_max=int(_env("DB_POOL_MAX", "10") or 10),
            db_connect_timeout_s=float(_env("DB_CONNECT_TIMEOUT_S", "3") or 3),
            cors_origins=[o.strip() for o in (_env("CORS_ORIGINS", "http://localhost:3000") or "").split(",")
                          if o.strip()],
            admin_token=_env("ADMIN_TOKEN"),
            request_deadline_s=float(_env("REQUEST_DEADLINE_S", "45") or 45),
            log_requests=(_env("LOG_REQUESTS", "1") or "1") not in ("0", "false", "no"),
        )
