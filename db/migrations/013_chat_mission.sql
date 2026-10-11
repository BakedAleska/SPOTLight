-- target: main
-- Mission chats (docs/chat-contract.md §9, chat.3). Idempotent: safe to re-run. Additive only.
-- A conversation can be about one mission: chat_conversations.mission_id -> missions, ON DELETE SET NULL (a mission
-- that disappears turns its chats into ordinary ones; it never deletes history). The mission is set when the chat is
-- created and never changed by PATCH. Grants are unchanged: spot_app already has CRUD on chat_conversations (011),
-- and spot_mcp_reader still has nothing (011 revokes it on every `migrate`).

ALTER TABLE chat_conversations ADD COLUMN IF NOT EXISTS mission_id text;

-- The FK as its own named constraint, so a column that already exists without it (a hand-made column, or an
-- interrupted run) still gets it. Guarded like 011's grants: warn instead of failing on a non-owner.
DO $fk$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conrelid = 'chat_conversations'::regclass AND conname = 'chat_conversations_mission_id_fkey') THEN
    -- Rows pointing at a mission that no longer exists would block the constraint: detach them first.
    UPDATE chat_conversations c SET mission_id = NULL
     WHERE c.mission_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM missions m WHERE m.mission_id = c.mission_id);
    ALTER TABLE chat_conversations ADD CONSTRAINT chat_conversations_mission_id_fkey
      FOREIGN KEY (mission_id) REFERENCES missions (mission_id) ON DELETE SET NULL;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE WARNING 'could not add the chat mission FK (%); apply 013 as the database owner', SQLERRM;
END
$fk$;

-- "Chats about this mission", most recent first (list_conversations(mission_id=...), list_missions' chat count).
CREATE INDEX IF NOT EXISTS chat_conversations_mission ON chat_conversations (mission_id, updated_at DESC);
