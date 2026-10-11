-- target: logs
-- Logs and raw SPOT data (contract §6). Runs against LOGS_DATABASE_URL (defaults to DATABASE_URL).
-- Both schemas are append-only: nothing UPDATEs or DELETEs them (enforced for the app roles in 007_roles.sql).

-- ── raw: SPOT data exactly as received (audit, replay, re-derivation) ──────────
CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE IF NOT EXISTS raw.spot_event_log (   -- every robot/replay event, verbatim
  log_id      bigserial PRIMARY KEY,
  source      text NOT NULL,               -- 'replay.ndjson' | 'live' | ...
  event_id    text, mission_id text, event_type text,
  event_ts    timestamp,                   -- time from the robot
  payload     jsonb NOT NULL,              -- the event exactly as received
  received_at timestamptz NOT NULL DEFAULT now(),
  sha256      text NOT NULL,               -- of the raw payload; UNIQUE (source, sha256) makes re-imports idempotent
  UNIQUE (source, sha256)
);
CREATE TABLE IF NOT EXISTS raw.kit_files (        -- provenance of every imported kit file
  file_id bigserial PRIMARY KEY, file_name text NOT NULL, sha256 text NOT NULL, row_count int,
  imported_at timestamptz NOT NULL DEFAULT now(), UNIQUE (file_name, sha256));
CREATE TABLE IF NOT EXISTS raw.kit_rows (         -- every CSV row verbatim, before typing/cleaning
  file_id bigint REFERENCES raw.kit_files, row_num int, data jsonb NOT NULL, PRIMARY KEY (file_id, row_num));
CREATE TABLE IF NOT EXISTS raw.inspection_media ( -- metadata + pointer for images/audio; blobs live in object storage or the repo
  media_id text PRIMARY KEY, action_id text, mission_id text, media_type text, uri text NOT NULL,
  sha256 text, bytes int, captured_at timestamp, metadata jsonb NOT NULL DEFAULT '{}');

CREATE INDEX IF NOT EXISTS spot_event_log_mission_ts ON raw.spot_event_log (mission_id, event_ts);
CREATE INDEX IF NOT EXISTS spot_event_log_type_ts    ON raw.spot_event_log (event_type, event_ts);
CREATE INDEX IF NOT EXISTS spot_event_log_payload    ON raw.spot_event_log USING gin (payload);

-- ── app: application logs (append-only) ──────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.model_calls (      -- every System 1 / System 2 / embedding call (the AMD latency evidence)
  call_id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), request_id text,
  system text NOT NULL CHECK (system IN ('system1','system2','embed')), model text, endpoint text,
  flag_id bigint, action_id text, input_tokens int, output_tokens int, latency_ms int, tokens_per_sec real,
  device text, ok boolean NOT NULL, error text, metadata jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS app.mcp_tool_calls (   -- audit of every MCP tool call System 2 makes
  call_id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), request_id text, flag_id bigint,
  tool text NOT NULL, args jsonb NOT NULL, result_count int, result_bytes int, top_similarity real,
  latency_ms int, ok boolean NOT NULL, error text);
CREATE TABLE IF NOT EXISTS app.request_log (      -- web/API requests
  req_id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), request_id text, service text,
  route text, method text, status int, latency_ms int, error text);
CREATE TABLE IF NOT EXISTS app.job_runs (         -- seed / ingest / replay / fine-tune sync jobs
  run_id bigserial PRIMARY KEY, job text NOT NULL, started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz, status text NOT NULL DEFAULT 'running', counts jsonb NOT NULL DEFAULT '{}',
  error text, metadata jsonb NOT NULL DEFAULT '{}');

CREATE INDEX IF NOT EXISTS model_calls_ts        ON app.model_calls (ts DESC);
CREATE INDEX IF NOT EXISTS model_calls_system_ts ON app.model_calls (system, ts);
CREATE INDEX IF NOT EXISTS model_calls_request   ON app.model_calls (request_id);
CREATE INDEX IF NOT EXISTS mcp_tool_calls_ts      ON app.mcp_tool_calls (ts DESC);
CREATE INDEX IF NOT EXISTS mcp_tool_calls_tool_ts ON app.mcp_tool_calls (tool, ts);
CREATE INDEX IF NOT EXISTS mcp_tool_calls_request ON app.mcp_tool_calls (request_id);
CREATE INDEX IF NOT EXISTS request_log_ts         ON app.request_log (ts DESC);
CREATE INDEX IF NOT EXISTS request_log_request    ON app.request_log (request_id);
CREATE INDEX IF NOT EXISTS job_runs_ts            ON app.job_runs (started_at DESC);
