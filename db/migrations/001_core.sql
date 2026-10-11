-- target: main
-- Core relational tables. Names and columns come from the original draft (git show 19dff30:web/db/schema.sql),
-- minus its leading DROP (that lives in db/reset.sql). Idempotent: safe to re-run.
-- Three layers: raw kit tables (seeded from CSV), the live pipeline (replay -> System 1 -> System 2 -> operator),
-- and analytics tables filled by db/setup.py seed (seed_analytics). Databricks was dropped (research/decisions.md).

-- ── Raw kit tables (NextEra SPOT challenge data) ─────────────────────────────
CREATE TABLE IF NOT EXISTS locations (
  location_id   text PRIMARY KEY,
  location_name text NOT NULL,
  zone_type     text,
  floor         int,
  x_coord       real,
  y_coord       real,
  environment   text
);

CREATE TABLE IF NOT EXISTS assets (
  asset_id    text PRIMARY KEY,
  asset_name  text NOT NULL,
  asset_type  text,
  location_id text REFERENCES locations,
  criticality text            -- LOW | MEDIUM | HIGH | CRITICAL
);

CREATE TABLE IF NOT EXISTS missions (
  mission_id            text PRIMARY KEY,
  mission_name          text,
  scenario              text,  -- BASELINE | IMPROVED
  configuration_version text,  -- V1 | V2
  start_time            timestamp,
  end_time              timestamp,
  duration_min          int,
  start_battery_pct     int,
  end_battery_pct       int,
  mission_status        text,  -- reported status: COMPLETED | FAILED
  operator_mode         text
);

CREATE TABLE IF NOT EXISTS actions (
  action_id             text PRIMARY KEY,
  mission_id            text REFERENCES missions,
  sequence              int,
  timestamp             timestamp,
  location_id           text REFERENCES locations,
  asset_id              text REFERENCES assets,
  action_type           text,
  battery_pct           int,
  status                text,  -- SUCCESS | FAILED
  failure_type          text,
  duration_sec          int,
  scenario              text,
  configuration_version text
);

CREATE TABLE IF NOT EXISTS readings (
  reading_id       text PRIMARY KEY,
  action_id        text REFERENCES actions,
  mission_id       text REFERENCES missions,
  asset_id         text REFERENCES assets,
  location_id      text REFERENCES locations,
  timestamp        timestamp,
  measurement_type text,
  value            real,
  unit             text,
  quality          text   -- GOOD | QUESTIONABLE
);

CREATE TABLE IF NOT EXISTS anomalies (
  anomaly_id               text PRIMARY KEY,
  action_id                text REFERENCES actions,
  mission_id               text REFERENCES missions,
  location_id              text REFERENCES locations,
  asset_id                 text REFERENCES assets,
  timestamp                timestamp,
  anomaly_type             text,
  severity                 text,   -- LOW | MEDIUM | HIGH | CRITICAL
  operator_review_required boolean,
  description              text
);

-- ── Live pipeline ─────────────────────────────────────────────────────────────
-- Every replay event as ingested ("live" view of SPOT). The enriched payload carries joined readings/anomalies.
CREATE TABLE IF NOT EXISTS events (
  event_id    text PRIMARY KEY,
  mission_id  text REFERENCES missions,
  ts          timestamp NOT NULL,        -- event time from the robot
  event_type  text NOT NULL,             -- MISSION_STARTED | ACTION_STARTED | ACTION_COMPLETED | ACTION_FAILED | MISSION_COMPLETED
  action_id   text,
  location_id text,
  asset_id    text,
  battery_pct int,
  payload     jsonb NOT NULL DEFAULT '{}',
  ingested_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_ingested_idx ON events (ingested_at DESC);
CREATE INDEX IF NOT EXISTS events_mission_idx  ON events (mission_id, ts);

-- System 1 "Triage" output: one row per event that tripped at least one rule.
CREATE TABLE IF NOT EXISTS flags (
  flag_id      bigserial PRIMARY KEY,
  event_id     text REFERENCES events ON DELETE CASCADE,
  mission_id   text,
  action_id    text,
  location_id  text,
  asset_id     text,
  score        real NOT NULL,            -- 0..100 priority
  severity     text NOT NULL,            -- INFO | LOW | MEDIUM | HIGH | CRITICAL
  title        text NOT NULL,            -- one-line, operator-readable
  reasons      jsonb NOT NULL,           -- [{rule, detail, weight}]  rule = policy rule name (doc_id 'policy:<rule>')
  escalated    boolean NOT NULL DEFAULT false,
  status       text NOT NULL DEFAULT 'OPEN',   -- OPEN | ACKNOWLEDGED | RESOLVED
  created_at   timestamptz NOT NULL DEFAULT now()
);
-- Added: the System 1 triage event id that produced the flag ('act-A0001' | 'mis-M001').
-- Unique, so re-seeding upserts and flag_id stays stable. NULL is allowed for flags from other sources.
ALTER TABLE flags ADD COLUMN IF NOT EXISTS triage_event_id text;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS priority text;      -- P1 | P2 | P3 | P4 (policy.py)
ALTER TABLE flags ADD COLUMN IF NOT EXISTS category text;      -- equipment | navigation | data_quality | robot_health | mission
CREATE UNIQUE INDEX IF NOT EXISTS flags_triage_event_uidx ON flags (triage_event_id);
CREATE INDEX IF NOT EXISTS flags_open_idx ON flags (status, score DESC);
CREATE INDEX IF NOT EXISTS flags_escalation_idx ON flags (escalated, created_at);
CREATE INDEX IF NOT EXISTS flags_action_idx ON flags (action_id);
CREATE INDEX IF NOT EXISTS flags_asset_idx ON flags (asset_id);

-- System 2 "Analyst" output (AMD-hosted Hugging Face model). Mirrors Orbit's question/answer/reasoning format.
CREATE TABLE IF NOT EXISTS assessments (
  assessment_id      bigserial PRIMARY KEY,
  flag_id            bigint UNIQUE REFERENCES flags ON DELETE CASCADE,
  state              text NOT NULL DEFAULT 'PENDING',  -- PENDING | DONE | ERROR | TEMPLATE
  question           text,
  answer             text,
  reasoning          text,
  recommended_action text,
  severity_assessment text,
  confidence         real,
  model              text,
  latency_ms         int,
  tokens_per_sec     real,
  error              text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  completed_at       timestamptz
);

-- Operator actions: closes the loop ("actionable").
CREATE TABLE IF NOT EXISTS acknowledgements (
  ack_id     bigserial PRIMARY KEY,
  flag_id    bigint REFERENCES flags ON DELETE CASCADE,
  action     text NOT NULL,     -- ACKNOWLEDGE | RESOLVE | ESCALATE | NOTE
  note       text,
  operator   text NOT NULL DEFAULT 'operator',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS acknowledgements_flag_idx ON acknowledgements (flag_id);

-- ── Analytics (filled by db/setup.py seed (seed_analytics)) ─────────────────────
CREATE TABLE IF NOT EXISTS risk_scores (    -- P(action fails | waypoint, action type, battery band, config)
  location_id    text,
  action_type    text,
  battery_band   text,               -- e.g. '<=25', '26-40', '41-60', '61-80', '81+'
  configuration_version text,
  risk           real NOT NULL,
  n_observed     int,
  PRIMARY KEY (location_id, action_type, battery_band, configuration_version)
);

CREATE TABLE IF NOT EXISTS mission_kpis (
  mission_id        text PRIMARY KEY,
  reported_status   text,
  true_health       real,    -- 0..100
  failed_actions    int,
  total_actions     int,
  high_anomalies    int,
  battery_drop      int,
  synced_at         timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS insights (        -- named headline metrics shown on the Overview page
  key        text PRIMARY KEY,
  label      text NOT NULL,
  value      real,
  detail     jsonb,
  source     text NOT NULL DEFAULT 'local',
  synced_at  timestamptz DEFAULT now()
);
