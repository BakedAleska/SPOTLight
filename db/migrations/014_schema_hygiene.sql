-- target: main
-- Schema hygiene (data audit 2026-10-11). Additive and idempotent: re-run by `db/setup.py migrate` on every deploy.
-- 1. Indexes the code's lookups and FK actions use
CREATE INDEX IF NOT EXISTS flags_event_idx        ON flags (event_id);
CREATE INDEX IF NOT EXISTS flags_mission_idx      ON flags (mission_id);
CREATE INDEX IF NOT EXISTS live_captures_flag_idx ON live_captures (flag_id);

-- 2. Assessment provenance (the backend computed these but never stored them; cached /explain lost them)
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS fallback_reason text;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS source text;
COMMENT ON COLUMN assessments.state IS 'PENDING | DONE | ERROR | TEMPLATE (TEMPLATE = deterministic fallback, retried while System 2 is configured)';
COMMENT ON COLUMN assessments.source IS 'Explanation.source: system2 | system2_template | template | system1 (system1 = the orchestrator''s short System 1 answer); NULL on rows from before 014';
COMMENT ON COLUMN assessments.fallback_reason IS 'why a TEMPLATE was stored instead of a System 2 answer; NULL for System 2 answers';
COMMENT ON COLUMN flags.category IS 'equipment | navigation | data_quality | robot_health | mission (mission-level flags have NULL action/location/asset)';
COMMENT ON TABLE mission_kpis IS 'Per-mission KPIs computed from the kit by db/setup.py seed (seed_analytics)';
COMMENT ON TABLE risk_scores IS 'P(action fails | waypoint, action type, battery band, config) computed from kit actions by db/setup.py seed (seed_analytics)';
COMMENT ON TABLE insights IS 'Headline metrics; kit validation_results.json metrics loaded and SQL-verified by db/setup.py seed (seed_analytics)';

-- 3. Dead Databricks leftover: no reader or writer anywhere (contract updated in the same change)
DROP TABLE IF EXISTS anomaly_briefs;

-- 4. CHECKs for enumerations the code already writes. NOT VALID then VALIDATE; a violating row only warns.
DO $chk$
DECLARE
  c record;
BEGIN
  FOR c IN SELECT * FROM (VALUES
    ('flags', 'flags_status_check', $$CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED'))$$),
    ('flags', 'flags_severity_check', $$CHECK (severity IN ('INFO', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'))$$),
    ('flags', 'flags_priority_check', $$CHECK (priority IS NULL OR priority IN ('P1', 'P2', 'P3', 'P4'))$$),
    ('assessments', 'assessments_state_check', $$CHECK (state IN ('PENDING', 'DONE', 'ERROR', 'TEMPLATE'))$$),
    ('acknowledgements', 'acknowledgements_action_check', $$CHECK (action IN ('ACKNOWLEDGE', 'RESOLVE', 'ESCALATE', 'NOTE'))$$),
    ('system1_predictions', 'system1_predictions_event_kind_check', $$CHECK (event_kind IN ('pre_action', 'action', 'mission'))$$),
    ('actions', 'actions_failure_type_iff_failed', $$CHECK ((status = 'FAILED') = (failure_type IS NOT NULL))$$)
  ) AS t(tbl, con, def) LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = c.tbl::regclass AND conname = c.con) THEN
      EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s NOT VALID', c.tbl, c.con, c.def);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = c.tbl::regclass AND conname = c.con AND NOT convalidated) THEN
      BEGIN
        EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', c.tbl, c.con);
      EXCEPTION WHEN check_violation THEN
        RAISE WARNING '% has rows violating %; left NOT VALID (new writes are still checked)', c.tbl, c.con;
      END;
    END IF;
  END LOOP;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE WARNING 'could not add CHECK constraints (%); run migrate as the table owner', SQLERRM;
END
$chk$;
