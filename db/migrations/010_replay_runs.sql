-- target: main
-- Stepped replay state (docs/backend-services-contract.md "Serverless deployment and stepped replay", added
-- 2026-10-10 by the backend agent). On Vercel the backend keeps no process between requests, so a replay run lives
-- here and each POST /api/v1/replay/step advances it (backend/app/services/replay.py SteppedReplay,
-- backend/app/db/queries.py replay_*). Docker's background replay never touches this table. Additive, idempotent.
CREATE TABLE IF NOT EXISTS replay_runs (
  run_id      bigserial PRIMARY KEY,                 -- request ids are rp<run_id>-<event_id>
  state       text NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'stopped', 'finished', 'error')),
  params      jsonb NOT NULL DEFAULT '{}',           -- speed, max_gap_s, missions, start_at, limit, triage_mode
  event_ids   text[] NOT NULL,                       -- the selected replay.ndjson events, in order, fixed at start
  next_index  int NOT NULL DEFAULT 0,                -- index into event_ids of the next event to ingest
  counts      jsonb NOT NULL DEFAULT '{}',           -- sent, escalated, flags_created, laya_unavailable, ...
  last_event  jsonb,
  job_run_id  bigint,                                -- app.job_runs row (may live in the logs DB)
  lease_token text,                                  -- the step currently ingesting; only it may write progress
  lease_until timestamptz,                           -- a killed/frozen step's lease expires here
  error       text,
  started_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
-- At most one running run, whichever instance starts it (a second start gets ON CONFLICT DO NOTHING -> 409).
CREATE UNIQUE INDEX IF NOT EXISTS replay_runs_one_running ON replay_runs ((true)) WHERE state = 'running';

-- 007_roles.sql ran before this table existed on a first migrate; grant explicitly (guarded like 007).
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spot_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON replay_runs TO spot_app;
    GRANT USAGE, SELECT, UPDATE ON SEQUENCE replay_runs_run_id_seq TO spot_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spot_mcp_reader') THEN
    GRANT SELECT ON replay_runs TO spot_mcp_reader;
  END IF;
END
$grants$;
