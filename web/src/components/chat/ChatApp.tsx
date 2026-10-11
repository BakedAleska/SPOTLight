"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import useSWR, { useSWRConfig } from "swr";
import { KeyRound, MessageSquareOff, PanelLeftOpen, RotateCw, SquarePen, TriangleAlert } from "lucide-react";
import { ChatHttpError, chatFetcher, describeError } from "@/lib/chat/api";
import { modelLabel } from "@/lib/chat/grounding";
import { PREF_HISTORY, PREF_MODE, PREF_MODEL, setPref, usePref } from "@/lib/chat/prefs";
import {
  DRAFT,
  applyConversation,
  clearDraft,
  editAndResend,
  forgetThread,
  loadThread,
  onConversationsChanged,
  regenerate,
  retryDraft,
  retryTurn,
  sendMessage,
  startConversation,
  stopTurn,
  useChatState,
  type ChatState,
} from "@/lib/chat/store";
import { publishChatTitle } from "@/lib/chat/title-store";
import type { ChatMode, ModelCatalog } from "@/lib/chat/types";
import { Composer, Disclaimer, type ComposerHandle } from "./Composer";
import { EmptyState } from "./EmptyState";
import { HISTORY_KEY, HistoryPanel } from "./HistoryPanel";
import { ModelPicker } from "./ModelPicker";
import { SourcesPanel, type SourcesView } from "./SourcesPanel";
import { ThreadView } from "./ThreadView";
import "./chat.css";

const ICON_BUTTON =
  "grid size-9 shrink-0 cursor-pointer place-items-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-ink";

const selectDraft = (s: ChatState) => s.draftId;
const selectThreads = (s: ChatState) => s.threads;

function conversationIdFrom(pathname: string) {
  const match = /^\/assistant\/([^/]+)\/?$/.exec(pathname);
  return match ? decodeURIComponent(match[1]) : null;
}

function DisabledNotice() {
  return (
    <div className="grid h-full place-items-center p-6">
      <div className="max-w-md text-center">
        <MessageSquareOff size={28} aria-hidden="true" className="mx-auto text-muted" />
        <h1 className="mt-3 text-lg font-semibold text-ink">SPOTbot is turned off</h1>
        <p className="mt-2 text-sm text-muted">
          This deployment has <code className="rounded bg-surface-2 px-1 font-mono text-[0.9em]">FEATURE_AI_ASSISTANT=false</code>.
          Set it to <code className="rounded bg-surface-2 px-1 font-mono text-[0.9em]">true</code> and restart the web service to use the chat.
        </p>
      </div>
    </div>
  );
}

/**
 * The whole SPOTbot page. Rendered by app/assistant/layout.tsx, so it
 * stays mounted while the URL moves between /assistant and
 * /assistant/<id>; the conversation shown is derived from the pathname. The
 * live answer itself lives in lib/chat/store, outside React.
 */
export function ChatApp() {
  const pathname = usePathname();
  const router = useRouter();
  const { mutate } = useSWRConfig();
  const pathId = conversationIdFrom(pathname);
  const draftId = useChatState(selectDraft);
  const threads = useChatState(selectThreads);
  const activeId = pathId ?? draftId;
  const thread = activeId ? threads[activeId] ?? null : null;
  const streamingIds = useMemo(() => new Set(Object.values(threads).filter((t) => t.stream).map((t) => t.id)), [threads]);

  const catalogQuery = useSWR<ModelCatalog>("/api/chat/models", chatFetcher, { revalidateOnFocus: false, shouldRetryOnError: false });
  const catalog = catalogQuery.data;
  const disabled = catalogQuery.error instanceof ChatHttpError && catalogQuery.error.code === "disabled";
  const models = useMemo(() => catalog?.models ?? [], [catalog]);

  const storedModel = usePref(PREF_MODEL);
  const storedMode = usePref(PREF_MODE);
  const historyPref = usePref(PREF_HISTORY);
  const known = useCallback((id: string | null | undefined): id is string => !!id && models.some((m) => m.id === id), [models]);
  const mode: ChatMode = storedMode === "thinking" || storedMode === "fast" ? storedMode : catalog?.default_mode ?? "fast";
  const historyOpen = historyPref !== "closed";

  // The header shows the open chat's own model; a new chat uses the
  // remembered preference. Picking a model in an open chat applies to its
  // next send (the backend then stores it on the conversation).
  const [picked, setPicked] = useState<{ thread: string; model: string } | null>(null);
  const openId = thread && thread.id !== DRAFT ? thread.id : null;
  const preferred = known(storedModel) ? storedModel : catalog?.default_model ?? "";
  const pickedHere = openId && picked?.thread === openId ? picked.model : null;
  const chatModel = openId ? thread?.conversation?.model : null;
  const model = known(pickedHere) ? pickedHere : known(chatModel) ? chatModel : preferred;

  const [drawer, setDrawer] = useState(false);
  const [text, setText] = useState("");
  const [sources, setSources] = useState<(SourcesView & { thread: string | null }) | null>(null);
  const composerRef = useRef<ComposerHandle>(null);

  // /assistant?ask=… (from a flag in the Overview queue) starts a new chat with
  // the question written but not sent, so the operator can edit it first. The
  // parameter is dropped once read, so a reload doesn't write it in again.
  const ask = useSearchParams().get("ask");
  const [askTaken, setAskTaken] = useState<string | null>(null);
  if (ask !== askTaken) {
    setAskTaken(ask);
    if (ask) setText(ask);
  }
  useEffect(() => {
    if (!ask) return;
    clearDraft();
    router.replace("/assistant", { scroll: false });
    composerRef.current?.focus();
  }, [ask, router]);

  // The URL caught up with a chat started from /assistant: the draft pointer
  // has done its job, and /assistant should mean "new chat" again.
  useEffect(() => {
    if (pathId && draftId === pathId) clearDraft();
  }, [pathId, draftId]);

  // Opening a chat: load it, or refresh it quietly if we already have it.
  useEffect(() => {
    if (!pathId) return;
    void loadThread(pathId, { quiet: true });
  }, [pathId]);

  // History list stays in step with turns finishing and titles arriving.
  useEffect(
    () => onConversationsChanged(() => void mutate((key) => typeof key === "string" && key.startsWith(HISTORY_KEY))),
    [mutate],
  );

  const title = thread?.conversation?.title ?? null;
  useEffect(() => {
    if (pathId && title) publishChatTitle(pathId, title);
  }, [pathId, title]);

  const onCreated = useCallback((id: string) => router.replace(`/assistant/${id}`, { scroll: false }), [router]);

  const send = useCallback((value?: string) => {
    const content = (value ?? text).trim();
    if (!content || !model || !catalog?.configured) return;
    const fromComposer = value === undefined;
    if (fromComposer) setText("");
    const started = activeId && activeId !== DRAFT
      ? sendMessage(activeId, content, model, mode)
      : startConversation(content, model, mode, onCreated);
    // A turn that couldn't go ahead (Stop while it waited) hands its text
    // back, unless the operator has started typing something else.
    void started.then((ok) => { if (!ok && fromComposer) setText((current) => current || content); });
    composerRef.current?.focus();
  }, [text, model, mode, catalog, activeId, onCreated]);

  const newChat = useCallback(() => {
    clearDraft();
    setText("");
    setDrawer(false);
    router.push("/assistant");
    composerRef.current?.focus();
  }, [router]);

  const openSources = useCallback((view: SourcesView) => setSources({ ...view, thread: activeId }), [activeId]);
  const closeSources = useCallback(() => setSources(null), []);
  const chooseModel = useCallback((id: string) => {
    setPref(PREF_MODEL, id);
    if (openId) setPicked({ thread: openId, model: id });
  }, [openId]);
  const chooseMode = useCallback((next: ChatMode) => setPref(PREF_MODE, next), []);

  if (disabled) return <DisabledNotice />;

  const configured = catalog?.configured ?? true;
  const streaming = !!thread?.stream;
  const composerBlocked = !catalog || !configured;
  const blockedReason = catalogQuery.error
    ? "SPOTbot's backend is unreachable."
    : catalog && !configured
      ? "Gemini API key not configured."
      : undefined;
  const thinkingHint = catalog?.modes.find((m) => m.id === "thinking")?.description;

  const composer = (
    <>
      <Composer
        ref={composerRef}
        value={text}
        onChange={setText}
        onSend={() => send()}
        onStop={() => activeId && stopTurn(activeId)}
        streaming={streaming}
        disabled={composerBlocked}
        disabledReason={blockedReason}
        mode={mode}
        onModeChange={chooseMode}
        thinkingHint={thinkingHint}
      />
      <Disclaimer />
    </>
  );

  const phase = thread?.stream?.phase;
  const announcement =
    phase === "connecting" || phase === "retrieving" ? "Searching mission data."
    : phase === "thinking" ? "Thinking."
    : phase === "writing" ? "Writing the answer."
    : thread?.error ? `${describeError(thread.error.code, thread.error.detail, modelLabel(models, thread.error.model)).title}.`
    : "";

  const showSources = sources && sources.thread === activeId ? sources : null;
  const lastMessage = thread?.messages[thread.messages.length - 1];

  return (
    // overflow-clip: nothing in the chat may overflow into the shell's
    // <main>, or a wheel past the end of the thread scrolls the whole chat
    // column off-screen. The thread and the history scroll on their own.
    <div className="relative flex h-full min-h-0 overflow-clip">
      {/* Mobile: the history is a drawer over the chat. */}
      {drawer && (
        <button type="button" tabIndex={-1} aria-hidden="true" onClick={() => setDrawer(false)} className="fixed inset-0 z-30 cursor-default bg-black/50 md:hidden" />
      )}
      <div
        className={`fixed inset-y-0 left-0 z-40 md:static md:z-auto ${drawer ? "flex" : "hidden"} ${historyOpen ? "md:flex" : "md:hidden"}`}
        onKeyDown={(event) => { if (event.key === "Escape" && drawer) setDrawer(false); }}
      >
        <HistoryPanel
          activeId={activeId}
          streamingIds={streamingIds}
          onNewChat={newChat}
          onCollapse={() => {
            setDrawer(false);
            setPref(PREF_HISTORY, "closed");
          }}
          onNavigate={() => setDrawer(false)}
          onDeleted={(id) => {
            forgetThread(id);
            if (id === activeId) router.push("/assistant");
          }}
          onChanged={(conversation) => {
            applyConversation(conversation);
            publishChatTitle(conversation.conversation_id, conversation.title);
          }}
        />
      </div>

      <section aria-label="Chat" className="relative flex min-w-0 flex-1 flex-col bg-canvas">
        <header className="flex h-12 shrink-0 items-center gap-1 border-b border-line px-2 sm:px-3">
          <button type="button" onClick={() => setDrawer(true)} aria-label="Show chat history" className={`${ICON_BUTTON} md:hidden`}>
            <PanelLeftOpen size={17} aria-hidden="true" />
          </button>
          {!historyOpen && (
            <>
              <button type="button" onClick={() => setPref(PREF_HISTORY, "open")} aria-label="Show chat history" title="Show chat history" className={`${ICON_BUTTON} hidden md:grid`}>
                <PanelLeftOpen size={17} aria-hidden="true" />
              </button>
              <button type="button" onClick={newChat} aria-label="New chat" title="New chat" className={`${ICON_BUTTON} hidden md:grid`}>
                <SquarePen size={16} aria-hidden="true" />
              </button>
            </>
          )}
          <ModelPicker models={models} value={model} onChange={chooseModel} disabled={!catalog} />
          <button type="button" onClick={newChat} aria-label="New chat" className={`${ICON_BUTTON} ml-auto md:hidden`}>
            <SquarePen size={16} aria-hidden="true" />
          </button>
        </header>

        {catalog && !configured && (
          <div role="status" className="flex items-start gap-2 border-b border-warn/40 bg-warn/10 px-4 py-2.5 text-sm text-ink">
            <KeyRound size={16} aria-hidden="true" className="mt-0.5 shrink-0 text-warn" />
            <p>
              <span className="font-medium">Gemini API key not configured.</span>{" "}
              <span className="text-muted">Set <code className="font-mono text-[0.9em]">GEMINI_API_KEY</code> in the root .env and restart the backend. History still works.</span>
            </p>
          </div>
        )}
        {catalogQuery.error && !disabled && (
          <div role="alert" className="flex items-center gap-2 border-b border-bad/35 bg-bad/5 px-4 py-2.5 text-sm text-ink">
            <TriangleAlert size={16} aria-hidden="true" className="shrink-0 text-bad" />
            <p className="min-w-0 flex-1">Cannot reach SPOTbot&apos;s backend. Check that it is running.</p>
            <button type="button" onClick={() => void catalogQuery.mutate()} className="inline-flex cursor-pointer items-center gap-1 font-medium text-info hover:underline">
              <RotateCw size={13} aria-hidden="true" /> Retry
            </button>
          </div>
        )}

        <p role="status" aria-live="polite" className="sr-only">{announcement}</p>

        {!thread && pathId ? (
          // A chat URL whose thread isn't in the store yet (direct load, or
          // the first render before the load effect runs): never flash the
          // new-chat greeting here.
          <ThreadSkeleton />
        ) : !thread ? (
          <EmptyState composer={composer} onPick={(prompt) => send(prompt)} disabled={composerBlocked} />
        ) : thread.load === "missing" ? (
          <div className="m-auto max-w-sm p-6 text-center">
            <h1 className="text-lg font-semibold text-ink">This chat doesn’t exist</h1>
            <p className="mt-2 text-sm text-muted">It may have been deleted, or the link is wrong.</p>
            <button type="button" onClick={newChat} className="mt-4 inline-flex h-9 cursor-pointer items-center gap-2 rounded-full bg-accent px-4 text-sm font-medium text-on-accent hover:bg-accent-hover">
              <SquarePen size={15} aria-hidden="true" /> Start a new chat
            </button>
          </div>
        ) : thread.load === "error" && thread.messages.length === 0 ? (
          <div role="alert" className="m-auto max-w-sm p-6 text-center">
            <h1 className="text-lg font-semibold text-ink">Cannot load this chat</h1>
            <p className="mt-2 text-sm text-muted">{thread.loadError}</p>
            <button type="button" onClick={() => void loadThread(thread.id)} className="mt-4 inline-flex h-9 cursor-pointer items-center gap-2 rounded-full border border-line-strong px-4 text-sm hover:bg-surface-2">
              <RotateCw size={14} aria-hidden="true" /> Retry
            </button>
          </div>
        ) : (thread.load === "loading" || thread.load === "idle") && thread.messages.length === 0 ? (
          <ThreadSkeleton />
        ) : (
          <>
            <h1 className="sr-only">{title ?? "Chat"}</h1>
            <ThreadView
              thread={thread}
              models={models}
              composer={composer}
              onEdit={(messageId, content) => model && void editAndResend(thread.id, messageId, content, model, mode)}
              onRegenerate={() => model && void regenerate(thread.id, model, mode)}
              onRetry={() => (thread.id === DRAFT ? retryDraft(onCreated) : void retryTurn(thread.id))}
              onTryModel={(id) => {
                chooseModel(id);
                if (thread.id === DRAFT) return retryDraft(onCreated, id);
                if (lastMessage?.role === "assistant" || thread.error?.retry.regenerate) void regenerate(thread.id, id, mode);
                else void retryTurn(thread.id, id);
              }}
              onOpenSources={openSources}
            />
          </>
        )}
      </section>

      {showSources && <SourcesPanel view={showSources} onClose={closeSources} />}
    </div>
  );
}

function ThreadSkeleton() {
  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 px-6 pt-8" role="status" aria-label="Loading chat">
      <div className="ml-auto h-10 w-1/2 animate-pulse rounded-3xl bg-surface-2" />
      <div className="space-y-2.5">
        {[92, 84, 88, 60].map((w) => <div key={w} className="h-4 animate-pulse rounded bg-surface-2" style={{ width: `${w}%` }} />)}
      </div>
    </div>
  );
}

/** Static-shell stand-in while the pathname resolves (see layout.tsx). */
export function ChatSkeleton() {
  return (
    <div className="flex h-full min-h-0 overflow-clip" aria-hidden="true">
      <div className="hidden w-64 shrink-0 border-r border-line bg-chrome md:block" />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="h-12 shrink-0 border-b border-line" />
        <div className="m-auto h-8 w-72 max-w-[80%] animate-pulse rounded bg-surface-2" />
      </div>
    </div>
  );
}
