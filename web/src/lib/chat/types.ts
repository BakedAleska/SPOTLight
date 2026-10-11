/* Shapes from docs/chat-contract.md §3 and §4. The backend is the source of
   truth; these types only describe what the browser reads. */

export type ChatMode = "fast" | "thinking";
export type MessageStatus = "complete" | "error" | "stopped";
export type SourceOrigin = "rag" | "graph" | "sql";

export interface Source {
  id: string;
  /** 1-based display number within this answer. */
  n: number;
  origin: SourceOrigin;
  kind: string;
  title: string;
  snippet: string;
  score: number | null;
  refs?: {
    mission_id?: string | null;
    asset_id?: string | null;
    location_id?: string | null;
    action_id?: string | null;
    flag_id?: number | string | null;
  } | null;
  path?: string | null;
}

export interface Grounding {
  grounded: boolean;
  cited_ids: string[];
  unknown_ids: string[];
  unverified_numbers: string[];
  source_count: number;
  retrieval_mode: "hybrid" | "fulltext" | null;
  graph_seeds: string[];
  /** chat.4 (§10): absent on older rows, which are grounded. */
  mode?: "grounded" | "general";
  /** chat.4, general answers only: plant ids the answer named although no plant data was attached. */
  fleet_ids?: string[];
}

export interface Conversation {
  conversation_id: string;
  title: string;
  title_source: "default" | "auto" | "model" | "user";
  model: string;
  mode: ChatMode;
  pinned: boolean;
  created_at: string;
  updated_at: string;
  message_count: number;
  /** chat.3: the mission this chat is about (canonical id, "M007"), or null. */
  mission_id: string | null;
  mission: MissionRef | null;
}

/** chat.3: the short mission label carried on a conversation. */
export interface MissionRef {
  mission_id: string;
  mission_name: string;
  mission_status: string;
}

/** chat.3: one row of GET /chat/missions (ChatRepo.list_missions). */
export interface MissionSummary {
  mission_id: string;
  mission_name: string;
  scenario: string | null;
  configuration_version: string | null;
  start_time: string | null;
  duration_min: number | null;
  start_battery_pct: number | null;
  end_battery_pct: number | null;
  mission_status: string;
  actions: number;
  failed: number;
  anomalies: number;
  serious_anomalies: number;
  open_flags: number;
  /** Reported COMPLETED with at least one failed action. */
  hidden_failure: boolean;
  /** Conversations about this mission with at least one message. */
  chats: number;
}

export interface MissionList {
  items: MissionSummary[];
}

export interface ConversationSummary extends Conversation {
  last_message_preview: string | null;
}

export interface Message {
  message_id: string;
  conversation_id: string;
  seq: number;
  role: "user" | "assistant";
  content: string;
  thinking: string | null;
  thinking_ms: number | null;
  model: string | null;
  mode: ChatMode | null;
  status: MessageStatus;
  sources: Source[];
  citations: string[];
  grounding: Grounding | null;
  usage: { prompt_tokens: number; output_tokens: number; thoughts_tokens: number } | null;
  latency_ms: number | null;
  error: string | null;
  /** chat.2: the §4 error code behind status "error", so a reloaded failed
      turn shows the same card as a live one. Null otherwise. */
  error_code?: ChatErrorCode | null;
  created_at: string;
}

/** The `retrieval` object of the `sources` event. */
export interface RetrievalMeta {
  /** chat.4: null for a general answer (no retrieval). */
  mode: "hybrid" | "fulltext" | (string & {}) | null;
  graph_seeds?: string[];
  latency_ms?: number;
  counts?: { rag: number; graph: number; sql: number };
  /** chat.2: every degraded retrieval step, as a short string. */
  gaps?: string[];
  /** chat.3: the mission a mission chat retrieved for; absent or null otherwise. */
  mission_id?: string | null;
  /** chat.4 (§10): whether this answer is grounded in plant data or a general answer, and the rule that decided. */
  answer_mode?: "grounded" | "general";
  route?: string;
}

export interface ModelInfo {
  id: string;
  label: string;
  description: string;
  family: "gemini" | "gemma";
  thinking_summaries: boolean;
}

export interface ModelCatalog {
  configured: boolean;
  default_model: string;
  default_mode: ChatMode;
  models: ModelInfo[];
  modes: { id: ChatMode; label: string; description: string }[];
}

export interface Suggestion {
  title: string;
  prompt: string;
}

export interface ConversationList {
  items: ConversationSummary[];
  total: number;
}

export interface ConversationDetail {
  conversation: Conversation;
  messages: Message[];
}

export type ChatErrorCode =
  | "rate_limited"
  | "model_unavailable"
  | "gemini_auth"
  | "blocked"
  | "upstream_error"
  | "retrieval_failed"
  | "internal_error"
  // Pre-stream HTTP errors (JSON, not SSE).
  | "not_found"
  | "conversation_busy"
  | "invalid_request"
  | "gemini_unset"
  | "unknown_mission"
  // Raised by the web layer itself.
  | "disabled"
  | "backend_unreachable"
  | "connection_lost"
  | (string & {});

/** Body of POST /chat/conversations/{id}/messages. */
export interface SendBody {
  content: string;
  model: string;
  mode: ChatMode;
  edit_message_id: string | null;
  regenerate: boolean;
}

/** SSE events, in the order the backend sends them. */
export type ChatEvent =
  | { event: "meta"; data: { conversation_id: string; request_id: string; model: string; mode: ChatMode; user_message: Message } }
  | { event: "sources"; data: { sources: Source[]; retrieval?: RetrievalMeta | null } }
  | { event: "thinking"; data: { delta: string } }
  | { event: "delta"; data: { delta: string } }
  | { event: "done"; data: { message: Message; conversation: Conversation } }
  | { event: "title"; data: { conversation_id: string; title: string } }
  | { event: "error"; data: { code: ChatErrorCode; detail: string; retry_after_s?: number; message: Message | null } };
