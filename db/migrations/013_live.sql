-- target: main
-- Live simulation runs (docs/backend-services-contract.md "Live simulation", docs/LIVE-SIM-PLAN.md). Idempotent.
-- An operator queues a run from the web console (POST /api/v1/live/runs); the live runner on the presenter laptop
-- claims it, walks the route's 8 stops and posts each stop's Laya answers, vision readings and Qwen output back.
-- The backend writes the usual pipeline rows (raw.spot_event_log, events, system1_predictions, flags,
-- assessments, documents) plus one live_captures row per stop and live knowledge-graph rows (props.source='live').
-- Every run uses fresh action ids (LIVE-<run_id>-<stop>), so re-running a route never overwrites earlier rows.

CREATE TABLE IF NOT EXISTS live_runs (
  run_id       bigserial PRIMARY KEY,
  route_id     text NOT NULL,                       -- R1..R5 (backend/app/data/live_routes.json)
  mission_id   text,                                -- 'LIVE-<run_id>', set when the run is created
  status       text NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'running', 'done', 'error', 'cancelled')),
  current_stop int NOT NULL DEFAULT 0,              -- stops the robot has reached (0..total_stops)
  total_stops  int NOT NULL,
  runner       text,                                -- who claimed it (runner host label, never an address)
  requested_by text,
  error        text,
  counts       jsonb NOT NULL DEFAULT '{}',          -- red, green, qwen, template, ...
  created_at   timestamptz NOT NULL DEFAULT now(),
  claimed_at   timestamptz,
  finished_at  timestamptz,
  updated_at   timestamptz NOT NULL DEFAULT now()
);
-- At most one queued or running run at a time.
CREATE UNIQUE INDEX IF NOT EXISTS live_runs_one_active ON live_runs ((true)) WHERE status IN ('pending', 'running');

CREATE TABLE IF NOT EXISTS live_captures (
  capture_id       bigserial PRIMARY KEY,
  run_id           bigint NOT NULL REFERENCES live_runs ON DELETE CASCADE,
  stop_index       int NOT NULL,                    -- 0..7
  stop             text NOT NULL,                   -- 'A'..'H'
  action_id        text NOT NULL,                   -- fresh live id, e.g. LIVE-12-C
  source_action_id text NOT NULL,                   -- the kit row the robot's log is taken from (A0194)
  location_id      text,
  asset_id         text,
  action_type      text,
  battery_pct      int,
  status           text,                            -- SUCCESS | FAILED (kit row)
  failure_type     text,
  reading          jsonb,                           -- kit reading row (reading_id, value, unit, quality)
  anomaly          jsonb,                           -- kit anomaly row
  phase            text NOT NULL DEFAULT 'arrived'
                   CHECK (phase IN ('arrived', 'triaged', 'analysing', 'done', 'error')),
  event_id         text,                            -- the ACTION_COMPLETED/FAILED event
  flag_id          bigint REFERENCES flags ON DELETE SET NULL,
  verdict          text CHECK (verdict IN ('red', 'green')),
  priority         text,
  severity         text,
  category         text,
  laya             jsonb,                           -- status, model, answers summary, p_escalate, latency
  rules            jsonb,                           -- escalate, priority, category, fired rules, policy_version
  decision         jsonb,                           -- combined decision (TRIAGE_MODE)
  photo            jsonb,                           -- path, sha256, label ('simulated capture'), credit
  vision           jsonb,                           -- V# rows, OCR, kind, pairing, latency
  qwen             jsonb,                           -- question/answer/evidence/recommended_action/model/path/latency
  qwen_source      text CHECK (qwen_source IN ('pending', 'system2', 'template', 'error')),
  doc_id           text,                            -- the explanation document (embedded)
  kg               jsonb NOT NULL DEFAULT '{}',     -- node/edge ids this stop added
  timings          jsonb NOT NULL DEFAULT '{}',     -- ms per step
  arrived_at       timestamptz NOT NULL DEFAULT now(),
  triaged_at       timestamptz,
  analysed_at      timestamptz,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, stop_index)
);
CREATE INDEX IF NOT EXISTS live_captures_updated ON live_captures (updated_at);

-- 007_roles.sql ran before these tables existed on a first migrate; grant explicitly (guarded like 007/010).
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spot_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON live_runs, live_captures TO spot_app;
    GRANT USAGE, SELECT, UPDATE ON SEQUENCE live_runs_run_id_seq, live_captures_capture_id_seq TO spot_app;
    -- live knowledge-graph rows (props.source = 'live'); db/graph_build.py leaves them alone
    GRANT SELECT, INSERT, UPDATE ON kg_nodes, kg_edges TO spot_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spot_mcp_reader') THEN
    GRANT SELECT ON live_runs, live_captures TO spot_mcp_reader;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE WARNING 'could not apply live grants (%); apply them as the database owner', SQLERRM;
END
$grants$;
