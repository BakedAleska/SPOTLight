// ─────────────────────────────────────────────────────────────────────────────
// The backend API client.
//
// The console reads everything through the FastAPI backend (`docs/backend-
// services-contract.md`), never straight from Postgres. The backend is what
// owns triage, the System 2 explanation, the operator acknowledgements and the
// replay, so a second SQL client here would be a second source of truth for
// the same rows — and would have no way to reach System 1 or System 2 at all.
// Neon sits behind the backend; this file only ever speaks HTTP.
//
// SERVER ONLY. BACKEND_URL is an internal address (http://backend:8000 inside
// compose) and BACKEND_ADMIN_TOKEN is a secret, so neither may be read from a
// Client Component. Nothing here is prefixed NEXT_PUBLIC_ on purpose.
// ─────────────────────────────────────────────────────────────────────────────

/** Inside docker-compose services reach each other by name; `make dev` outside
    it talks to the published port. Both end up at the same `/api/v1`. */
const BASE = (process.env.BACKEND_URL ?? "http://localhost:8000").replace(/\/+$/, "");

/** `/ops/*`, `/replay/*` and `POST /events*` need this header (contract,
    "Admin endpoints"). The presenter supplies it; it is never in the bundle. */
const ADMIN_TOKEN = process.env.BACKEND_ADMIN_TOKEN;

/** Default per-call cap. Generous enough for Neon's cold start on the overview
    query, short enough that a dead backend falls back rather than hanging the
    page. `/explain` passes its own, much longer, value. */
const TIMEOUT_MS = Number(process.env.BACKEND_TIMEOUT_MS ?? 10_000);

import { unstable_rethrow } from "next/navigation";

export class BackendError extends Error {
  constructor(
    readonly path: string,
    /** null when the request never got an answer (DNS, refused, timeout). */
    readonly status: number | null,
    message: string,
    options?: { cause?: unknown },
  ) {
    super(message, options);
    this.name = "BackendError";
  }
}

type Query = Record<string, string | number | boolean | undefined>;

interface CallOptions {
  query?: Query;
  timeoutMs?: number;
  /** Send X-Admin-Token. Throws if BACKEND_ADMIN_TOKEN is unset, rather than
      sending an unauthenticated request the backend will answer 401/503. */
  admin?: boolean;
  /** Hit the server root instead of `/api/v1` — only `/health` lives there. */
  root?: boolean;
}

/** `/health` is mounted at the root; everything else under `/api/v1`
    (`backend/app/main.py` include_router). */
function url(path: string, query?: Query, root = false) {
  const u = new URL(`${BASE}${root ? "" : "/api/v1"}${path}`);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) u.searchParams.set(key, String(value));
  }
  return u;
}

async function call<T>(method: "GET" | "POST", path: string, body?: unknown, options: CallOptions = {}) {
  const headers: Record<string, string> = { accept: "application/json" };
  if (body !== undefined) headers["content-type"] = "application/json";
  if (options.admin) {
    if (!ADMIN_TOKEN) {
      throw new BackendError(path, null, `${path} needs BACKEND_ADMIN_TOKEN, which is not set.`);
    }
    headers["X-Admin-Token"] = ADMIN_TOKEN;
  }

  // AbortSignal.timeout rather than a manual controller: the timer is cleared
  // with the signal, so a slow route can't leak a pending timeout per request.
  let response: Response;
  try {
    response = await fetch(url(path, options.query, options.root), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(options.timeoutMs ?? TIMEOUT_MS),
      // An operator console shows live flags: every read is request-time.
      // Under `cacheComponents` that is already the default, but saying it
      // here means the behaviour doesn't change if the flag ever moves.
      cache: "no-store",
    });
  } catch (cause) {
    // `cache: "no-store"` is a request-time API, so during the static-shell
    // prerender React aborts this fetch with a framework-controlled error.
    // Wrapping that in a BackendError would make the prerender think the
    // backend was down and bake a fallback into the shell.
    unstable_rethrow(cause);
    const why = cause instanceof Error && cause.name === "TimeoutError" ? "timed out" : "unreachable";
    throw new BackendError(path, null, `Backend ${why} at ${BASE}${path}`, { cause });
  }

  if (!response.ok) {
    // FastAPI puts the reason in `detail` or, for the handled cases in the
    // contract, in `error`. Keep whichever is there: it is the only thing that
    // distinguishes "no such flag" from "admin token unset".
    const detail = await response
      .json()
      .then((b) => b?.error ?? b?.detail ?? null)
      .catch(() => null);
    throw new BackendError(
      path,
      response.status,
      `Backend ${response.status} on ${path}${detail ? `: ${JSON.stringify(detail)}` : ""}`,
    );
  }

  return (await response.json()) as T;
}

export function get<T>(path: string, options?: CallOptions) {
  return call<T>("GET", path, undefined, options);
}

export function post<T>(path: string, body?: unknown, options?: CallOptions) {
  return call<T>("POST", path, body ?? {}, options);
}

// ── Wire types ───────────────────────────────────────────────────────────────
//
// Only the fields the console reads. The backend's response models are
// deliberately `Loose` (extra keys pass through), so these are a subset by
// design — adding a column upstream never breaks this file.

/** `flags.reasons`: both verdicts first, then one entry per fired policy rule
    (contract, "Backend details"). Only the policy rules carry a `weight`. */
export interface WireReason {
  rule: string;
  detail?: string;
  weight?: number;
  priority?: string;
  /** rules_verdict / laya_verdict only. */
  verdict?: string;
  confidence?: number;
  agreement?: string;
  model?: string;
}

export interface WireFlag {
  flag_id: number;
  event_id: string | null;
  mission_id: string | null;
  action_id: string | null;
  location_id: string | null;
  asset_id: string | null;
  score: number;
  severity: string;
  title: string;
  reasons: WireReason[];
  escalated: boolean;
  status: string;
  created_at: string;
  priority: string | null;
  category: string | null;
  assessment_state?: string | null;
}

export interface WireFlagList {
  items: WireFlag[];
  page: { total: number; limit: number; offset: number };
}

/** One card from `/insights/overview`, keyed by the stable `key` the backend
    assigns (`green_but_broken`, `battery_cliff`, …). `detail` is per-card. */
export interface WireInsight {
  key: string;
  label: string;
  value: number | null;
  unit: string;
  pitch: string;
  detail: Record<string, unknown>;
}

export interface WireOverview {
  insights: WireInsight[];
  stored_insights: unknown[];
  generated_at: string;
}

export interface WireMission {
  mission_id: string;
  mission_name?: string | null;
  mission_status?: string | null;
  configuration_version?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  status_misleading: boolean;
  actions_total: number;
  actions_failed: number;
  serious_anomalies: number;
}

export interface WireMissionList {
  items: WireMission[];
  totals: Record<string, number>;
}

export interface WireAsset {
  asset_id: string;
  asset_name: string;
  criticality?: string | null;
}

export interface WireLocation {
  location_id: string;
  location_name: string;
  environment?: string | null;
}

/** `GET /health` (root, not `/api/v1`). Lets the console tell "the backend is
    up but its database is empty" from "the backend is down" — very different
    things on stage. Each dependency is `{status: up | down | unset, …}`. */
export interface WireServiceHealth {
  status: string;
  url?: string;
  error?: string;
  breaker?: string;
  model_loaded?: boolean;
}

export interface WireHealth {
  ok: boolean;
  service: string;
  version: string;
  triage_mode: string;
  database?: WireServiceHealth;
  system1?: WireServiceHealth;
  embed?: WireServiceHealth;
  mcp?: WireServiceHealth;
  system2?: WireServiceHealth;
  orchestrator?: WireServiceHealth;
  replay: Record<string, unknown>;
  kit: Record<string, unknown>;
}

/** Shallow by default: `deep=true` probes every dependency with a ~2 s timeout
    each, which is a slow thing to put in front of a page render. */
export function health(options?: { deep?: boolean; timeoutMs?: number }) {
  return get<WireHealth>("/health", {
    root: true,
    query: { deep: options?.deep ?? false },
    timeoutMs: options?.timeoutMs,
  });
}
