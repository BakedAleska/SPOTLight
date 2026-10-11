-- target: logs
-- AI Assistance chat (docs/chat-contract.md §1): allow system = 'chat' in app.model_calls. Every Gemini call is
-- logged with system='chat', endpoint 'gemini:streamGenerateContent' (or 'gemini:generateContent' for titles),
-- metadata.provider = 'gemini'.
-- 005 declares the CHECK inline, so Postgres named it model_calls_system_check. This drops every CHECK on the
-- `system` column (whatever its name) and adds model_calls_system_check listing system1, system2, embed, chat.
-- Idempotent: once a model_calls_system_check that allows 'chat' exists, re-runs change nothing (no table lock,
-- no re-validation). 005 is `CREATE TABLE IF NOT EXISTS`, so it never brings the old CHECK back.
DO $chk$
DECLARE
  r      record;
  done   boolean := false;
BEGIN
  IF to_regclass('app.model_calls') IS NULL THEN
    RAISE WARNING 'app.model_calls missing (005_raw_and_app_logs.sql not applied on this database); skipping';
    RETURN;
  END IF;
  FOR r IN
    SELECT c.conname, pg_get_constraintdef(c.oid) AS def
    FROM pg_constraint c
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
    WHERE c.conrelid = 'app.model_calls'::regclass AND c.contype = 'c' AND a.attname = 'system'
  LOOP
    IF r.conname = 'model_calls_system_check' AND r.def LIKE '%''chat''%' THEN
      done := true;                                -- already migrated
    ELSE
      EXECUTE format('ALTER TABLE app.model_calls DROP CONSTRAINT %I', r.conname);
    END IF;
  END LOOP;
  IF NOT done THEN
    ALTER TABLE app.model_calls ADD CONSTRAINT model_calls_system_check
      CHECK (system IN ('system1', 'system2', 'embed', 'chat'));
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE WARNING 'could not update app.model_calls system CHECK (%); run migrate as the table owner', SQLERRM;
END
$chk$;
