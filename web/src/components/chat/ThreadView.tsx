"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ArrowDown } from "lucide-react";
import type { Thread } from "@/lib/chat/store";
import type { ModelInfo } from "@/lib/chat/types";
import { describeError, offersModelSwitch } from "@/lib/chat/api";
import { modelLabel, nextModel } from "@/lib/chat/grounding";
import { ErrorCard } from "./ErrorCard";
import type { SourcesView } from "./SourcesPanel";
import { AssistantTurn, LiveTurn, UserTurn } from "./Turns";

/** How close to the bottom still counts as "following" the answer. */
const STICKY_PX = 72;

/**
 * The scrolling message list. Auto-scroll follows a streaming answer only
 * while the reader is at the bottom: scrolling up (wheel, touch, keys) pauses
 * it and shows "Jump to latest"; getting back to the bottom resumes it.
 * Growth is observed with a ResizeObserver, so tokens, images of tables and
 * expanding thought panels all count — not just new messages.
 */
export function ThreadView({ thread, models, composer, onEdit, onRegenerate, onRetry, onTryModel, onOpenSources }: {
  thread: Thread;
  models: ModelInfo[];
  composer: ReactNode;
  onEdit: (messageId: string, content: string) => void;
  onRegenerate: () => void;
  onRetry: () => void;
  onTryModel: (id: string) => void;
  onOpenSources: (view: SourcesView) => void;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const [editing, setEditing] = useState<{ thread: string; message: string } | null>(null);
  const editingId = editing?.thread === thread.id ? editing.message : null;
  const onEditing = useCallback((messageId: string | null) => setEditing(messageId ? { thread: thread.id, message: messageId } : null), [thread.id]);

  const toBottom = useCallback((smooth = false) => {
    const el = scroller.current;
    if (!el) return;
    follow.current = true;
    setAtBottom(true);
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }, []);

  const onScroll = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < STICKY_PX;
    follow.current = near;
    setAtBottom(near);
  }, []);

  // Follow growth while following.
  useEffect(() => {
    const el = scroller.current;
    const inner = content.current;
    if (!el || !inner) return;
    const observer = new ResizeObserver(() => {
      if (follow.current) el.scrollTop = el.scrollHeight;
    });
    observer.observe(inner);
    return () => observer.disconnect();
  }, []);

  // Opening a chat lands at its end.
  useLayoutEffect(() => {
    toBottom(false);
  }, [thread.id, toBottom]);

  // Sending (a new user turn appears) always returns to the bottom.
  const lastUserKey = [...thread.messages].reverse().find((m) => m.role === "user")?.message_id;
  useEffect(() => {
    if (lastUserKey) toBottom(true);
  }, [lastUserKey, toBottom]);

  const busy = !!thread.stream;
  const last = thread.messages[thread.messages.length - 1];
  const nextSeq = (last?.seq ?? 0) + 1;
  const error = thread.error;
  const orphanError = error && !error.messageId ? error : null;
  const alternative = orphanError && offersModelSwitch(orphanError.code) ? nextModel(models, orphanError.model) : null;
  const orphanLabel = orphanError ? modelLabel(models, orphanError.model) : "";

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={scroller}
        onScroll={onScroll}
        onWheel={(event) => { if (event.deltaY < 0) follow.current = false; }}
        // relative: the sr-only headings inside are position:absolute, and
        // without a positioned scroller they escape it and make the shell
        // scroll. overscroll-contain: a wheel past the end stops here.
        className="relative min-h-0 flex-1 overflow-y-auto overscroll-contain [overflow-anchor:none]"
      >
        <div ref={content} className="mx-auto w-full max-w-3xl space-y-8 px-4 pt-6 pb-10 sm:px-6">
          {thread.messages.map((message, index) =>
            message.role === "user" ? (
              <UserTurn
                key={`user-${message.seq}`}
                message={message}
                busy={busy}
                editing={editingId === message.message_id}
                onEditing={onEditing}
                onEdit={onEdit}
              />
            ) : (
              <AssistantTurn
                key={`assistant-${message.seq}`}
                message={message}
                models={models}
                isLast={index === thread.messages.length - 1 && !busy}
                busy={busy}
                liveError={error?.messageId === message.message_id ? error : null}
                onRegenerate={onRegenerate}
                onRetry={onRetry}
                onTryModel={onTryModel}
                onOpenSources={onOpenSources}
              />
            ),
          )}
          {thread.stream && <LiveTurn key={`assistant-${nextSeq}`} stream={thread.stream} onOpenSources={onOpenSources} />}
          {orphanError && !busy && (
            <ErrorCard
              code={orphanError.code}
              detail={orphanError.detail}
              retryAfterS={orphanError.retryAfterS}
              since={orphanError.at}
              modelLabel={orphanLabel}
              alternative={alternative && alternative.id !== orphanError.model ? alternative : null}
              onRetry={onRetry}
              onTryModel={onTryModel}
            />
          )}
        </div>
      </div>

      <div className="relative shrink-0 px-3 pb-3 sm:px-6">
        {!atBottom && (
          <button
            type="button"
            onClick={() => toBottom(true)}
            aria-label="Jump to latest message"
            className="panel-shadow absolute -top-12 left-1/2 grid size-9 -translate-x-1/2 cursor-pointer place-items-center rounded-full border border-line bg-surface text-ink shadow-md transition-colors hover:bg-surface-2"
          >
            <ArrowDown size={16} aria-hidden="true" />
          </button>
        )}
        <div className="mx-auto w-full max-w-3xl">{composer}</div>
      </div>
      {/* Screen-reader summary for the orphan error, which has no message. */}
      {orphanError && <p className="sr-only" role="alert">{describeError(orphanError.code, orphanError.detail, orphanLabel).title}</p>}
    </div>
  );
}
