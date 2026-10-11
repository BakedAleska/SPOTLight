-- target: main
-- AI Assistance chat history (docs/chat-contract.md §1). Idempotent: safe to re-run.
-- Read and written only by the backend's ChatRepo (backend/app/db/chat.py). Chats are NOT evidence: they are never
-- embedded into `documents`, never graph nodes, and spot_mcp_reader cannot read them (same leakage rule as
-- System 2: previously generated text is never retrieved as a source). `/ops/reset-demo` leaves them alone;
-- db/reset.sql drops them.
CREATE EXTENSION IF NOT EXISTS pg_trgm;          -- also created by 009; repeated so this file stands alone
-- gen_random_uuid() is built in since PostgreSQL 13 (Neon and the pgvector image are 16/17).

CREATE TABLE IF NOT EXISTS chat_conversations (
  conversation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title        text NOT NULL DEFAULT 'New chat' CHECK (char_length(title) BETWEEN 1 AND 200),
  title_source text NOT NULL DEFAULT 'default' CHECK (title_source IN ('default','auto','model','user')),
  model        text NOT NULL,
  mode         text NOT NULL DEFAULT 'fast' CHECK (mode IN ('fast','thinking')),
  pinned       boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_messages (
  message_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES chat_conversations ON DELETE CASCADE,
  seq             int  NOT NULL,                 -- 1, 2, 3 ... per conversation; edit/regenerate delete the tail
  role            text NOT NULL CHECK (role IN ('user','assistant')),
  content         text NOT NULL DEFAULT '',
  thinking        text,                          -- thought summary (assistant, thinking mode), NULL otherwise
  thinking_ms     int,
  model           text,
  mode            text CHECK (mode IN ('fast','thinking')),
  status          text NOT NULL DEFAULT 'complete' CHECK (status IN ('complete','error','stopped')),
  sources         jsonb NOT NULL DEFAULT '[]',   -- Source[] shown to the model (contract §3)
  citations       jsonb NOT NULL DEFAULT '[]',   -- source ids the answer actually cited, in order of first use
  grounding       jsonb,                         -- Grounding (contract §3), assistant only
  usage           jsonb,                         -- {prompt_tokens, output_tokens, thoughts_tokens}
  latency_ms      int,
  error           text,
  error_code      text,                          -- chat.2: §4 error.code ('rate_limited', ...), NULL unless status='error'
  request_id      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conversation_id, seq)                  -- also the index for "messages of a conversation, by seq"
);

-- chat.2: databases created before error_code existed get the column here (CREATE TABLE IF NOT EXISTS skips them).
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS error_code text;

-- Sidebar: pinned first, then most recently updated (ChatRepo.list_conversations orders exactly like this).
CREATE INDEX IF NOT EXISTS chat_conversations_sidebar    ON chat_conversations (pinned DESC, updated_at DESC);
-- Sidebar search: `ILIKE '%q%'` on titles and on any message's content (pg_trgm GIN serves ILIKE).
CREATE INDEX IF NOT EXISTS chat_conversations_title_trgm ON chat_conversations USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS chat_messages_content_trgm    ON chat_messages USING gin (content gin_trgm_ops);

-- ── Grants (guarded like 007_roles.sql / 009_graph.sql: warn instead of failing) ──────────────────
-- spot_app: CRUD on both tables (the backend). spot_mcp_reader: NOTHING. 007 runs before this file on every
-- `migrate` and grants spot_mcp_reader SELECT on ALL tables in public (plus default privileges for new ones), so
-- the REVOKE here must run every time, after 007; it does, because migrations apply in file-name order.
-- PUBLIC never had table privileges; the REVOKE FROM PUBLIC just keeps it that way explicitly.
DO $grants$
BEGIN
  REVOKE ALL ON chat_conversations, chat_messages FROM PUBLIC;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spot_app') THEN
    GRANT USAGE ON SCHEMA public TO spot_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON chat_conversations, chat_messages TO spot_app;
  ELSE
    RAISE WARNING 'role spot_app missing; run migrate again after creating it (007_roles.sql)';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spot_mcp_reader') THEN
    REVOKE ALL ON chat_conversations, chat_messages FROM spot_mcp_reader;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE WARNING 'could not apply chat grants (%); apply them as the database owner', SQLERRM;
END
$grants$;
