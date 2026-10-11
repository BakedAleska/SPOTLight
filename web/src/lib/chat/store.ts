"use client";

/* The chat's live state, kept in a module-level store instead of React state.

   Why module scope: the first message of a new chat is sent from /assistant,
   and the URL must then become /assistant/<id> while the answer is still
   streaming. A navigation may remount any component under the route; this
   module is loaded once per tab and is never remounted, so the fetch, its
   AbortController and every streamed token live here and the UI simply
   re-subscribes. Streams for other chats keep running in the background too,
   so switching chats mid-answer doesn't lose that answer.

   Components read it with useChatState(selector); all writes go through the
   exported actions. State is immutable: each update replaces the touched
   thread object, so selectors can return thread objects directly. */

import { useSyncExternalStore } from "react";
import { ChatHttpError, chatFetch, createConversation, streamTurn } from "./api";
import type { ChatErrorCode, ChatEvent, ChatMode, Conversation, ConversationDetail, Message, RetrievalMeta, SendBody, Source } from "./types";

export type StreamPhase = "connecting" | "retrieving" | "thinking" | "writing";

export interface LiveStream {
  model: string;
  mode: ChatMode;
  phase: StreamPhase;
  requestId: string | null;
  sources: Source[];
  retrieval: RetrievalMeta | null;
  thinking: string;
  content: string;
  startedAt: number;
  thinkingStartedAt: number | null;
  thinkingEndedAt: number | null;
}

export interface TurnError {
  code: ChatErrorCode;
  detail: string;
  retryAfterS: number | null;
  /** Date.now() when the error arrived; the countdown runs from here. */
  at: number;
  model: string;
  /** The stored assistant message carrying this error, when there is one. */
  messageId: string | null;
  /** What Retry sends. */
  retry: SendBody;
}

export interface Thread {
  id: string;
  conversation: Conversation | null;
  messages: Message[];
  load: "idle" | "loading" | "ready" | "missing" | "error";
  loadError: string | null;
  stream: LiveStream | null;
  error: TurnError | null;
}

export interface ChatState {
  threads: Record<string, Thread>;
  /** The chat started from /assistant whose URL change may not have landed
      yet. DRAFT while the conversation itself is still being created. */
  draftId: string | null;
}

export const DRAFT = "draft";

let state: ChatState = { threads: {}, draftId: null };
const SERVER_STATE: ChatState = state;
const listeners = new Set<() => void>();
const listListeners = new Set<() => void>();
let frame: number | null = null;

/* One run per POST. A run is `answered` once done/error arrives: the thread
   is ready for the next turn right then, even though the server keeps the
   connection open for the optional `title` (contract §6, chat.2). The next
   turn waits for `released` (the connection closed) before it POSTs, so it
   doesn't race the server's per-chat lock. */
interface Run {
  controller: AbortController;
  answered: boolean;
  released: Promise<void>;
  release: () => void;
}
const runs = new Map<string, Run>();
/** Bumped per turn, so work left over from an older turn (the post-Stop
    resync) can tell it has been overtaken. */
const generations = new Map<string, number>();
/** `sources.retrieval` of answers streamed in this tab, by stored message id.
    It isn't stored with the message, so a reloaded answer shows only its mode. */
const retrievals = new Map<string, RetrievalMeta>();

/** Contract §6: wait at most 8 s for the previous stream to release; retry a
    409 once after ~600 ms. Mutable for tests only. */
export const timing = { releaseWaitMs: 8000, busyRetryMs: 600 };

function emit() {
  if (frame !== null) {
    cancelAnimationFrame(frame);
    frame = null;
  }
  listeners.forEach((listener) => listener());
}

/** Token deltas arrive far faster than the screen refreshes; coalesce them
    to one render per frame. Everything else renders immediately. */
function emitSoon() {
  if (frame !== null) return;
  if (typeof requestAnimationFrame !== "function") return emit();
  frame = requestAnimationFrame(() => {
    frame = null;
    emit();
  });
}

function emitListChange() {
  listListeners.forEach((listener) => listener());
}

export function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Fires when the history list may be stale (a turn finished, a title came). */
export function onConversationsChanged(listener: () => void) {
  listListeners.add(listener);
  return () => {
    listListeners.delete(listener);
  };
}

export function getChatState() {
  return state;
}

export function useChatState<T>(selector: (state: ChatState) => T): T {
  return useSyncExternalStore(subscribe, () => selector(state), () => selector(SERVER_STATE));
}

function emptyThread(id: string): Thread {
  return { id, conversation: null, messages: [], load: "idle", loadError: null, stream: null, error: null };
}

function patchThread(id: string, patch: Partial<Thread> | ((thread: Thread) => Partial<Thread>), soon = false) {
  const current = state.threads[id] ?? emptyThread(id);
  const next = { ...current, ...(typeof patch === "function" ? patch(current) : patch) };
  state = { ...state, threads: { ...state.threads, [id]: next } };
  if (soon) emitSoon();
  else emit();
}

function patchStream(id: string, patch: (stream: LiveStream) => Partial<LiveStream>, soon = false) {
  patchThread(id, (thread) => (thread.stream ? { stream: { ...thread.stream, ...patch(thread.stream) } } : {}), soon);
}

let localSeq = 0;
function localId(prefix: string) {
  localSeq += 1;
  return `local-${prefix}-${Date.now().toString(36)}-${localSeq}`;
}

export function isLocal(message: Message) {
  return message.message_id.startsWith("local-");
}

function optimisticUser(conversationId: string, content: string, after: Message[]): Message {
  return {
    message_id: localId("user"),
    conversation_id: conversationId,
    seq: (after[after.length - 1]?.seq ?? 0) + 1,
    role: "user",
    content,
    thinking: null,
    thinking_ms: null,
    model: null,
    mode: null,
    status: "complete",
    sources: [],
    citations: [],
    grounding: null,
    usage: null,
    latency_ms: null,
    error: null,
    error_code: null,
    created_at: new Date().toISOString(),
  };
}

function newStream(model: string, mode: ChatMode): LiveStream {
  return {
    model, mode, phase: "connecting", requestId: null, sources: [], retrieval: null, thinking: "", content: "",
    startedAt: Date.now(), thinkingStartedAt: null, thinkingEndedAt: null,
  };
}

/* ── loading ──────────────────────────────────────────────────────────────── */

/**
 * Loads a conversation from the backend. A thread with a live stream is
 * never overwritten — the stream is newer than anything stored. `quiet`
 * refreshes in the background without showing a loading state.
 */
export async function loadThread(id: string, { quiet = false } = {}) {
  if (id === DRAFT) return;
  const existing = state.threads[id];
  if (existing?.stream) return;
  if (!quiet || !existing || existing.load !== "ready") patchThread(id, { load: "loading", loadError: null });
  try {
    const detail = await chatFetch<ConversationDetail>(`/api/chat/conversations/${id}`);
    if (state.threads[id]?.stream) return;
    patchThread(id, (thread) => ({
      conversation: detail.conversation,
      messages: detail.messages,
      load: "ready",
      loadError: null,
      // Keep a live error only while its stored message is still on screen.
      error: thread.error?.messageId && detail.messages.some((m) => m.message_id === thread.error?.messageId) ? thread.error : null,
    }));
  } catch (error) {
    if (state.threads[id]?.stream) return;
    if (error instanceof ChatHttpError && error.status === 404) patchThread(id, { load: "missing", loadError: null });
    else patchThread(id, { load: quiet && existing?.load === "ready" ? "ready" : "error", loadError: error instanceof Error ? error.message : "Cannot load this chat." });
  }
}

/* ── sending ──────────────────────────────────────────────────────────────── */

interface TurnPlan {
  body: SendBody;
  /** Applied to the visible messages before the request goes out. */
  optimistic: (messages: Message[]) => Message[];
}

interface TurnFlags {
  gotMeta: boolean;
  finished: boolean;
}

function handleEvent(id: string, run: Run, event: ChatEvent, flags: TurnFlags, body: SendBody) {
  // A deleted chat ignores whatever its stream still sends.
  if (!state.threads[id]) return;
  // After done/error only a late title is meaningful; a newer turn may own
  // the thread's live stream by now, so nothing else may touch it.
  if (flags.finished && event.event !== "title") return;
  switch (event.event) {
    case "meta": {
      flags.gotMeta = true;
      const stored = event.data.user_message;
      patchThread(id, (thread) => {
        const messages = [...thread.messages];
        // Swap the optimistic user turn for the stored one (it carries the
        // real message_id that edit needs). Regenerate re-sends the stored
        // last user message, which then simply replaces itself.
        let index = -1;
        for (let i = messages.length - 1; i >= 0; i--) {
          if (messages[i].role === "user") { index = i; break; }
        }
        if (stored) {
          if (index >= 0 && (isLocal(messages[index]) || messages[index].message_id === stored.message_id)) messages[index] = stored;
          else messages.push(stored);
        }
        return {
          messages,
          stream: thread.stream ? { ...thread.stream, phase: "retrieving", requestId: event.data.request_id, model: event.data.model, mode: event.data.mode } : thread.stream,
        };
      });
      // The user turn is stored, so a brand-new chat now has a message and
      // shows up in the history (empty chats are not listed, chat.2).
      emitListChange();
      break;
    }
    case "sources":
      patchStream(id, (stream) => ({
        sources: event.data.sources ?? [],
        retrieval: event.data.retrieval ?? null,
        phase: stream.mode === "thinking" ? "thinking" : "writing",
      }));
      break;
    case "thinking":
      patchStream(id, (stream) => ({
        thinking: stream.thinking + (event.data.delta ?? ""),
        phase: stream.content ? stream.phase : "thinking",
        thinkingStartedAt: stream.thinkingStartedAt ?? Date.now(),
      }), true);
      break;
    case "delta":
      patchStream(id, (stream) => ({
        content: stream.content + (event.data.delta ?? ""),
        phase: "writing",
        thinkingEndedAt: stream.thinkingStartedAt && !stream.thinkingEndedAt ? Date.now() : stream.thinkingEndedAt,
      }), true);
      break;
    case "done": {
      flags.finished = true;
      run.answered = true;
      const message = event.data.message;
      patchThread(id, (thread) => {
        if (thread.stream?.retrieval) retrievals.set(message.message_id, thread.stream.retrieval);
        return {
          messages: [...thread.messages.filter((m) => m.message_id !== message.message_id), message],
          conversation: event.data.conversation ?? thread.conversation,
          stream: null,
          error: null,
        };
      });
      emitListChange();
      break;
    }
    case "title":
      // A rename that landed while the model title was being generated wins
      // (contract §4: a user title is never overwritten); the backend refuses
      // the write too, so this keeps the screen in line with what is stored.
      patchThread(id, (thread) => ({
        conversation: thread.conversation && thread.conversation.title_source !== "user"
          ? { ...thread.conversation, title: event.data.title, title_source: "model" }
          : thread.conversation,
      }));
      emitListChange();
      break;
    case "error": {
      flags.finished = true;
      run.answered = true;
      const stored = event.data.message;
      patchThread(id, (thread) => ({
        messages: stored ? [...thread.messages.filter((m) => m.message_id !== stored.message_id), stored] : thread.messages,
        stream: null,
        error: {
          code: event.data.code,
          detail: event.data.detail ?? "",
          retryAfterS: typeof event.data.retry_after_s === "number" ? event.data.retry_after_s : null,
          at: Date.now(),
          model: thread.stream?.model ?? body.model,
          messageId: stored?.message_id ?? null,
          retry: retryBody(thread, body, flags.gotMeta),
        },
      }));
      emitListChange();
      break;
    }
  }
}

/** After the user turn is stored, retrying means regenerating its answer;
    before that, it means sending the same request again. */
function retryBody(thread: Thread, body: SendBody, userStored: boolean): SendBody {
  if (!userStored) return body;
  const lastUser = [...thread.messages].reverse().find((m) => m.role === "user");
  return { content: lastUser?.content ?? body.content, model: body.model, mode: body.mode, edit_message_id: null, regenerate: true };
}

/** Resolves "released", "timeout" or "aborted", whichever comes first. */
function waitForRelease(released: Promise<void>, ms: number, signal: AbortSignal): Promise<"released" | "timeout" | "aborted"> {
  if (signal.aborted) return Promise.resolve("aborted");
  return new Promise((resolve) => {
    const timer = setTimeout(() => finish("timeout"), ms);
    const onAbort = () => finish("aborted");
    signal.addEventListener("abort", onAbort);
    void released.then(() => finish("released"));
    function finish(result: "released" | "timeout" | "aborted") {
      clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
      resolve(result);
    }
  });
}

/** Sleeps `ms`; false when the signal aborts first. */
function sleep(ms: number, signal: AbortSignal): Promise<boolean> {
  return waitForRelease(new Promise(() => {}), ms, signal).then((result) => result !== "aborted");
}

/**
 * Runs one turn. Resolves true when the turn went ahead (it streams, or it
 * failed with a visible error card), and false when nothing was sent and the
 * thread is as it was: then the caller hands the text back to the composer.
 * A turn is never dropped silently (contract §6, chat.2).
 */
async function runTurn(id: string, plan: TurnPlan): Promise<boolean> {
  const previous = runs.get(id);
  // An answer still streaming here: the UI blocks sending, so this is a race
  // (a double click). Give the text back rather than clobber the live answer.
  if (previous && !previous.answered) return false;

  let release = () => {};
  const released = new Promise<void>((resolve) => { release = resolve; });
  const run: Run = { controller: new AbortController(), answered: false, released, release };
  const { signal } = run.controller;
  runs.set(id, run);
  const generation = (generations.get(id) ?? 0) + 1;
  generations.set(id, generation);
  const flags: TurnFlags = { gotMeta: false, finished: false };
  const before = state.threads[id]?.messages ?? [];
  let posted = false;

  // The turn shows at once, even while it waits for the previous stream.
  patchThread(id, (thread) => ({
    messages: plan.optimistic(thread.messages),
    stream: newStream(plan.body.model, plan.body.mode),
    error: null,
    load: "ready",
  }));
  try {
    if (previous) {
      const waited = await waitForRelease(previous.released, timing.releaseWaitMs, signal);
      if (waited === "aborted") {
        patchThread(id, { messages: before, stream: null });
        return false;
      }
      if (waited === "timeout") {
        failTurn(id, "conversation_busy", "The previous answer is still finishing. Retry in a moment.", plan.body, false);
        return true;
      }
    }
    for (let attempt = 0; ; attempt++) {
      try {
        posted = true;
        await streamTurn(id, plan.body, signal, (event) => handleEvent(id, run, event, flags, plan.body));
        break;
      } catch (error) {
        // Right after a Stop the server may still be saving the stopped
        // answer and hold the chat's lock: try once more shortly.
        const busy = error instanceof ChatHttpError && error.status === 409;
        if (busy && attempt === 0 && !signal.aborted) {
          posted = false;
          if (await sleep(timing.busyRetryMs, signal)) continue;
        }
        throw error;
      }
    }
    if (!flags.finished) {
      // The server closed without done/error: treat as a dropped connection
      // and resync from what the backend stored.
      failTurn(id, "connection_lost", "The stream closed before the answer finished.", plan.body, flags.gotMeta);
      void settleAfterInterruption(id, generation);
    }
  } catch (error) {
    if (flags.finished) {
      // The answer is in; losing the connection while a title is pending
      // changes nothing.
    } else if (signal.aborted && !posted) {
      // Stopped while waiting to retry: nothing reached the server.
      patchThread(id, { messages: before, stream: null });
      return false;
    } else if (signal.aborted) {
      keepStoppedPartial(id);
      void settleAfterInterruption(id, generation);
    } else if (error instanceof ChatHttpError) {
      failTurn(id, error.code, error.message, plan.body, flags.gotMeta);
    } else {
      failTurn(id, "connection_lost", error instanceof Error ? error.message : "", plan.body, flags.gotMeta);
      void settleAfterInterruption(id, generation);
    }
  } finally {
    // A newer turn may already own this thread's slot: leave it alone.
    if (runs.get(id) === run) runs.delete(id);
    run.release();
    if (frame !== null) emit();
  }
  return true;
}

function failTurn(id: string, code: ChatErrorCode, detail: string, body: SendBody, userStored: boolean) {
  patchThread(id, (thread) => ({
    stream: null,
    error: { code, detail, retryAfterS: null, at: Date.now(), model: thread.stream?.model ?? body.model, messageId: null, retry: retryBody(thread, body, userStored) },
  }));
  emitListChange();
}

/** Stop: show what streamed so far as a stopped answer right away. The
    backend stores its own copy; settleAfterInterruption swaps that in. */
function keepStoppedPartial(id: string) {
  patchThread(id, (thread) => {
    const stream = thread.stream;
    if (!stream) return {};
    const last = thread.messages[thread.messages.length - 1];
    const partial: Message = {
      ...optimisticUser(id, stream.content, thread.messages),
      message_id: localId("stopped"),
      role: "assistant",
      thinking: stream.thinking || null,
      thinking_ms: stream.thinkingStartedAt ? (stream.thinkingEndedAt ?? Date.now()) - stream.thinkingStartedAt : null,
      model: stream.model,
      mode: stream.mode,
      status: "stopped",
      sources: stream.sources,
      seq: (last?.seq ?? 0) + 1,
    };
    return { stream: null, messages: [...thread.messages, partial] };
  });
  emitListChange();
}

/** The backend saves a stopped/failed partial asynchronously. Re-read the
    thread shortly after, keeping our local partial until the stored one
    shows up (a second read covers a slow save). */
async function settleAfterInterruption(id: string, generation: number) {
  const overtaken = () => generations.get(id) !== generation;
  for (const wait of [700, 2500]) {
    await new Promise((resolve) => setTimeout(resolve, wait));
    if (overtaken() || state.threads[id]?.stream) return;
    try {
      const detail = await chatFetch<ConversationDetail>(`/api/chat/conversations/${id}`);
      const thread = state.threads[id];
      if (!thread || thread.stream || overtaken()) return;
      const storedLast = detail.messages[detail.messages.length - 1];
      const localPartial = thread.messages.find((m) => isLocal(m) && m.role === "assistant");
      if (storedLast?.role === "assistant" || !localPartial) {
        // A stored answer (stopped, failed or even complete) speaks for
        // itself; drop any "connection dropped" card that would duplicate it.
        patchThread(id, { messages: detail.messages, conversation: detail.conversation, ...(storedLast?.role === "assistant" ? { error: null } : {}) });
        emitListChange();
        return;
      }
      // Not saved yet: keep the stored history plus our partial.
      patchThread(id, { messages: [...detail.messages, localPartial], conversation: detail.conversation });
    } catch {
      return;
    }
  }
}

/** A retry after a pre-stream failure re-sends the same text; the unsent
    optimistic copy is dropped first so it isn't shown twice. */
function withoutUnsent(messages: Message[]) {
  return messages.filter((m) => !(isLocal(m) && m.role === "user"));
}

/** Sends a new user message in an existing conversation. */
export function sendMessage(id: string, content: string, model: string, mode: ChatMode): Promise<boolean> {
  const body: SendBody = { content, model, mode, edit_message_id: null, regenerate: false };
  return runTurn(id, {
    body,
    optimistic: (messages) => {
      const kept = withoutUnsent(messages);
      return [...kept, optimisticUser(id, content, kept)];
    },
  });
}

/** Re-answers the last user message, optionally with another model. */
export function regenerate(id: string, model: string, mode: ChatMode): Promise<boolean> {
  const thread = state.threads[id];
  const lastUser = thread ? [...thread.messages].reverse().find((m) => m.role === "user") : undefined;
  if (!thread || !lastUser) return Promise.resolve(false);
  return runTurn(id, {
    body: { content: lastUser.content, model, mode, edit_message_id: null, regenerate: true },
    optimistic: (messages) => {
      const index = messages.lastIndexOf(lastUser);
      return index >= 0 ? messages.slice(0, index + 1) : messages;
    },
  });
}

/** Replaces a user message (and everything after it) with new text. */
export function editAndResend(id: string, messageId: string, content: string, model: string, mode: ChatMode): Promise<boolean> {
  return runTurn(id, {
    body: { content, model, mode, edit_message_id: messageId, regenerate: false },
    optimistic: (messages) => {
      const sent = withoutUnsent(messages);
      const index = sent.findIndex((m) => m.message_id === messageId);
      const kept = index >= 0 ? sent.slice(0, index) : sent;
      return [...kept, optimisticUser(id, content, kept)];
    },
  });
}

/** Retries whatever the last failed turn asked for, optionally on another model. */
export function retryTurn(id: string, model?: string): Promise<boolean> {
  const error = state.threads[id]?.error;
  if (!error) return Promise.resolve(false);
  const body = { ...error.retry, model: model ?? error.retry.model };
  if (body.regenerate) return regenerate(id, body.model, body.mode);
  if (body.edit_message_id) return editAndResend(id, body.edit_message_id, body.content, body.model, body.mode);
  return sendMessage(id, body.content, body.model, body.mode);
}

export function stopTurn(id: string) {
  runs.get(id)?.controller.abort();
}

/** Retrieval meta of an answer streamed in this tab (null after a reload). */
export function retrievalFor(messageId: string): RetrievalMeta | null {
  return retrievals.get(messageId) ?? null;
}

/**
 * First message of a new chat. The user's turn appears at once under a
 * DRAFT thread; once the backend creates the conversation the thread moves
 * to its real id, `onCreated` changes the URL, and the answer streams into
 * the same store entry whatever the router does to the page.
 */
export async function startConversation(content: string, model: string, mode: ChatMode, onCreated: (id: string) => void): Promise<boolean> {
  const draft: Thread = {
    ...emptyThread(DRAFT),
    messages: [optimisticUser(DRAFT, content, [])],
    stream: newStream(model, mode),
    load: "ready",
  };
  state = { threads: { ...state.threads, [DRAFT]: draft }, draftId: DRAFT };
  emit();
  let conversation: Conversation;
  try {
    conversation = await createConversation({ model, mode });
  } catch (error) {
    const code = error instanceof ChatHttpError ? error.code : "backend_unreachable";
    patchThread(DRAFT, { stream: null, error: {
      code, detail: error instanceof Error ? error.message : "", retryAfterS: null, at: Date.now(), model, messageId: null,
      retry: { content, model, mode, edit_message_id: null, regenerate: false },
    } });
    return true;
  }
  if (state.draftId !== DRAFT) return true; // the user left with "New chat"
  const id = conversation.conversation_id;
  const threads = { ...state.threads };
  const pending = threads[DRAFT];
  delete threads[DRAFT];
  // Carry the visible turn and the "connecting" indicator over, so there is
  // no frame where the new thread renders empty. The history isn't refreshed
  // here: an empty chat isn't listed (chat.2); `meta` refreshes it once the
  // first message is stored.
  threads[id] = { ...emptyThread(id), conversation, load: "ready", messages: pending?.messages ?? [], stream: pending?.stream ?? null };
  state = { threads, draftId: id };
  emit();
  onCreated(id);
  return sendMessage(id, content, model, mode);
}

/** Retry for a draft whose conversation couldn't be created, optionally
    on another model ("Try with …"). */
export function retryDraft(onCreated: (id: string) => void, model?: string) {
  const retry = state.threads[DRAFT]?.error?.retry;
  if (retry) void startConversation(retry.content, model ?? retry.model, retry.mode, onCreated);
}

/** "New chat": forget the draft so /assistant shows the empty state. */
export function clearDraft() {
  if (!state.draftId) return;
  const threads = { ...state.threads };
  if (threads[DRAFT] && !threads[DRAFT].stream) delete threads[DRAFT];
  state = { threads, draftId: null };
  emit();
}

/** Applies a conversation change made elsewhere (rename, pin) to the open thread. */
export function applyConversation(conversation: Conversation) {
  if (!state.threads[conversation.conversation_id]) return;
  patchThread(conversation.conversation_id, { conversation });
}

export function forgetThread(id: string) {
  runs.get(id)?.controller.abort();
  const threads = { ...state.threads };
  delete threads[id];
  state = { threads, draftId: state.draftId === id ? null : state.draftId };
  emit();
}
