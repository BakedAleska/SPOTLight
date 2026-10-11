"use client";

import { memo, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Library, Pencil, RotateCw, Search, Square } from "lucide-react";
import { offersModelSwitch } from "@/lib/chat/api";
import { citationOrder, sourceResolver } from "@/lib/chat/citations";
import { modelLabel, nextModel } from "@/lib/chat/grounding";
import { retrievalFor, type LiveStream, type TurnError } from "@/lib/chat/store";
import type { Message, ModelInfo, Source } from "@/lib/chat/types";
import { CopyButton } from "./CopyButton";
import { ErrorCard } from "./ErrorCard";
import { GroundingBadge } from "./GroundingBadge";
import { Markdown } from "./Markdown";
import type { SourcesView } from "./SourcesPanel";
import { ThinkingDone, ThinkingLive } from "./ThinkingBlock";

const ACTION = "grid size-8 cursor-pointer place-items-center rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-40";
const PILL = "inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-full border border-line px-2.5 text-xs font-medium text-muted transition-colors hover:bg-surface-2 hover:text-ink";

/* ── user turn ───────────────────────────────────────────────────────────── */

function EditBox({ initial, onCancel, onSave }: { initial: string; onCancel: () => void; onSave: (text: string) => void }) {
  const [value, setValue] = useState(initial);
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, []);
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 320)}px`;
  }, [value]);
  const ok = value.trim().length > 0 && value.length <= 8000;
  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Escape") { event.preventDefault(); onCancel(); }
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      if (ok) onSave(value.trim());
    }
  }
  return (
    <div className="chat-composer w-full max-w-[85%] rounded-3xl border border-line-strong bg-surface-2 p-3">
      <label className="sr-only" htmlFor="chat-edit">Edit message</label>
      <textarea
        id="chat-edit"
        ref={area}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={onKeyDown}
        rows={2}
        className="chat-composer-input block w-full resize-none bg-transparent px-1 text-[15px] leading-7 text-ink"
      />
      <div className="mt-2 flex justify-end gap-2">
        <button type="button" onClick={onCancel} className="h-8 cursor-pointer rounded-full border border-line-strong px-3.5 text-sm text-ink hover:bg-surface">
          Cancel
        </button>
        <button type="button" disabled={!ok} onClick={() => onSave(value.trim())} className="h-8 cursor-pointer rounded-full bg-accent px-3.5 text-sm font-medium text-on-accent hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50">
          Send
        </button>
      </div>
    </div>
  );
}

/* Edit state lives in the parent (keyed by thread + message), so switching
   chats can never leave an editor open on another chat's turn. */
export function UserTurn({ message, busy, editing, onEditing, onEdit }: {
  message: Message;
  busy: boolean;
  editing: boolean;
  onEditing: (messageId: string | null) => void;
  onEdit: (messageId: string, content: string) => void;
}) {
  const setEditing = (on: boolean) => onEditing(on ? message.message_id : null);
  const local = message.message_id.startsWith("local-");
  return (
    <div className="group flex flex-col items-end chat-fade-in">
      <h3 className="sr-only">You said:</h3>
      {editing ? (
        <EditBox
          initial={message.content}
          onCancel={() => setEditing(false)}
          onSave={(text) => {
            setEditing(false);
            if (text !== message.content) onEdit(message.message_id, text);
          }}
        />
      ) : (
        <div className="max-w-[85%] rounded-3xl bg-surface-2 px-4 py-2.5 text-[15px] leading-7 break-words whitespace-pre-wrap text-ink">
          {message.content}
        </div>
      )}
      {!editing && (
        <div className="mt-1 flex gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100 [@media(hover:none)]:opacity-100">
          <CopyButton text={message.content} label="Copy message" />
          <button type="button" disabled={busy || local} onClick={() => setEditing(true)} aria-label="Edit message" title="Edit and resend" className={ACTION}>
            <Pencil size={14} aria-hidden="true" />
          </button>
        </div>
      )}
    </div>
  );
}

/* ── assistant turn ──────────────────────────────────────────────────────── */

function Status({ children }: { children: React.ReactNode }) {
  return <div className="flex items-center gap-2 text-sm text-muted">{children}</div>;
}

function seconds(ms: number | null | undefined) {
  return ms && ms > 0 ? `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s` : null;
}

/** Ids an answer cites, in chip order. A partial or empty answer falls back
    to what the backend's grounding check recorded. */
function citedInOrder(content: string, sources: Source[], fallback: string[] = []) {
  if (!content) return fallback;
  return citationOrder(content, sourceResolver(sources.map((source) => source.id)));
}

/** A streaming answer. Shares its key with the stored message that replaces
    it, so the swap on `done` doesn't remount anything. */
export function LiveTurn({ stream, onOpenSources }: { stream: LiveStream; onOpenSources: (view: SourcesView) => void }) {
  const open = (focusId?: string) => onOpenSources({
    key: "live",
    sources: stream.sources,
    cited: citedInOrder(stream.content, stream.sources),
    focusId,
    retrieval: stream.retrieval,
  });
  const searching = stream.phase === "connecting" || stream.phase === "retrieving";
  const waitingFast = stream.phase === "writing" && !stream.content;
  const thinkingNow = stream.phase === "thinking" && !stream.content;
  return (
    <article aria-label="Assistant answer, in progress" aria-busy="true" className="chat-fade-in">
      <h3 className="sr-only">Assistant said:</h3>
      {searching && (
        <Status>
          <Search size={15} aria-hidden="true" />
          <span className="chat-shimmer font-medium">Searching mission data…</span>
        </Status>
      )}
      {!searching && stream.sources.length > 0 && (
        <button
          type="button"
          onClick={() => open()}
          className={`${PILL} mb-3`}
        >
          <Library size={13} aria-hidden="true" />
          Read {stream.sources.length} sources
        </button>
      )}
      {thinkingNow && <ThinkingLive text={stream.thinking} />}
      {!thinkingNow && stream.thinking && (
        <ThinkingDone text={stream.thinking} ms={stream.thinkingStartedAt ? (stream.thinkingEndedAt ?? stream.thinkingStartedAt) - stream.thinkingStartedAt : null} />
      )}
      {waitingFast && (
        <div className="chat-dots flex h-7 items-center gap-1" aria-hidden="true">
          <span /><span /><span />
        </div>
      )}
      {stream.content && (
        <Markdown
          content={stream.content}
          sources={stream.sources}
          className="chat-streaming"
          onOpenSource={(id) => open(id)}
        />
      )}
    </article>
  );
}

export const AssistantTurn = memo(function AssistantTurn({
  message,
  models,
  isLast,
  busy,
  liveError,
  onRegenerate,
  onRetry,
  onTryModel,
  onOpenSources,
}: {
  message: Message;
  models: ModelInfo[];
  isLast: boolean;
  busy: boolean;
  liveError: TurnError | null;
  onRegenerate: () => void;
  onRetry: () => void;
  onTryModel: (id: string) => void;
  onOpenSources: (view: SourcesView) => void;
}) {
  const sources: Source[] = message.sources ?? [];
  const open = (focusId?: string) => onOpenSources({
    key: message.message_id,
    sources,
    cited: citedInOrder(message.content, sources, message.grounding?.cited_ids ?? message.citations ?? []),
    focusId,
    retrieval: retrievalFor(message.message_id) ?? (message.grounding?.retrieval_mode ? { mode: message.grounding.retrieval_mode } : null),
  });
  const label = modelLabel(models, message.model);
  const failed = message.status === "error";
  // A live error carries the countdown; a reloaded one has its stored code
  // (chat.2) and gets the same card without the countdown.
  const code = liveError?.code ?? message.error_code ?? "upstream_error";
  const failedModel = liveError?.model ?? message.model ?? "";
  const alternative = offersModelSwitch(code) ? nextModel(models, failedModel) : null;
  const hasThinking = !!message.thinking || (message.mode === "thinking" && !!message.thinking_ms);

  return (
    <article aria-label="Assistant answer" className="group chat-fade-in">
      <h3 className="sr-only">Assistant said:</h3>
      {hasThinking && <ThinkingDone text={message.thinking} ms={message.thinking_ms} stopped={message.status === "stopped" && !message.content} />}
      {message.content && <Markdown content={message.content} sources={sources} onOpenSource={(id) => open(id)} />}
      {failed && (
        <div className={message.content ? "mt-3" : ""}>
          <ErrorCard
            code={code}
            detail={liveError?.detail ?? message.error}
            retryAfterS={liveError?.retryAfterS ?? null}
            since={liveError?.at ?? 0}
            modelLabel={modelLabel(models, failedModel)}
            alternative={alternative && alternative.id !== failedModel ? alternative : null}
            onRetry={isLast && !busy ? (liveError ? onRetry : onRegenerate) : undefined}
            onTryModel={isLast && !busy ? onTryModel : undefined}
          />
        </div>
      )}
      {!failed && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          {message.status === "stopped" && (
            <span className="inline-flex h-7 items-center gap-1.5 rounded-full border border-line bg-surface-2 px-2.5 text-xs font-medium text-muted">
              <Square size={10} aria-hidden="true" fill="currentColor" />
              Stopped
            </span>
          )}
          <GroundingBadge grounding={message.grounding} />
          {sources.length > 0 && (
            <button type="button" onClick={() => open()} className={PILL}>
              <Library size={13} aria-hidden="true" />
              Sources <span className="nums">({sources.length})</span>
            </button>
          )}
          <span className="flex items-center">
            {message.content && <CopyButton text={message.content} label="Copy answer" />}
            {isLast && (
              <button type="button" disabled={busy} onClick={onRegenerate} aria-label="Regenerate answer" title="Regenerate" className={ACTION}>
                <RotateCw size={14} aria-hidden="true" />
              </button>
            )}
          </span>
          {(label || message.latency_ms) && (
            <span className="ml-auto text-xs text-muted">
              {[label, message.mode === "thinking" ? "thinking" : null, seconds(message.latency_ms)].filter(Boolean).join(" · ")}
            </span>
          )}
        </div>
      )}
    </article>
  );
});
