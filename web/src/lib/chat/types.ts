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
  retrieval_mode: "hybrid" | "fulltext";
  graph_seeds: string[];
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
  mode: "hybrid" | "fulltext" | (string & {});
  graph_seeds?: string[];
  latency_ms?: number;
  counts?: { rag: number; graph: number; sql: number };
  /** chat.2: every degraded retrieval step, as a short string. */
  gaps?: string[];
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
