"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Ellipsis, LoaderCircle, PanelLeftClose, Pencil, Pin, PinOff, Search, SquarePen, Trash2, X } from "lucide-react";
import { chatFetcher, deleteConversation, updateConversation } from "@/lib/chat/api";
import { groupConversations } from "@/lib/chat/history";
import type { ConversationList, ConversationSummary } from "@/lib/chat/types";
import { MissionBadge } from "./MissionFocus";
import { moveMenuFocus, usePopover } from "./usePopover";

export const HISTORY_KEY = "/api/chat/conversations";

const ICON_BUTTON =
  "grid size-8 shrink-0 cursor-pointer place-items-center rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-ink";

function RowMenu({ conversation, onRename, onPin, onDelete }: {
  conversation: ConversationSummary;
  onRename: () => void;
  onPin: () => void;
  onDelete: () => void;
}) {
  const { open, setOpen, root, trigger } = usePopover();
  const menu = useRef<HTMLDivElement>(null);
  // The list scrolls, so a menu on one of the last rows would be clipped:
  // open upward when there isn't room below.
  const [up, setUp] = useState(false);
  useEffect(() => {
    if (open) menu.current?.querySelector<HTMLElement>("[role='menuitem']")?.focus();
  }, [open]);
  function toggle() {
    const rect = trigger.current?.getBoundingClientRect();
    const scroller = trigger.current?.closest("nav")?.getBoundingClientRect();
    if (rect) setUp(rect.bottom + 140 > Math.min(window.innerHeight, scroller?.bottom ?? window.innerHeight));
    setOpen(!open);
  }
  const item = "flex w-full cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-sm transition-colors hover:bg-surface-2 focus-visible:bg-surface-2";
  const pick = (action: () => void) => () => {
    setOpen(false);
    action();
  };
  return (
    // No transform here: a transformed wrapper is its own stacking context,
    // and later rows would paint over the open menu.
    <div ref={root} className="absolute inset-y-0 right-1 flex items-center">
      <button
        ref={trigger}
        type="button"
        onClick={toggle}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Options for ${conversation.title}`}
        className={`grid size-7 cursor-pointer place-items-center rounded-md text-muted transition hover:bg-surface hover:text-ink focus-visible:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100 ${open ? "opacity-100" : "opacity-0"}`}
      >
        <Ellipsis size={16} aria-hidden="true" />
      </button>
      {open && (
        <div
          ref={menu}
          role="menu"
          aria-label={`Options for ${conversation.title}`}
          onKeyDown={(event) => moveMenuFocus(menu.current, event)}
          className={`panel-shadow absolute right-0 z-40 w-44 rounded-lg border border-line bg-surface p-1 shadow-xl ${up ? "bottom-full mb-1" : "top-full mt-1"}`}
        >
          <button type="button" role="menuitem" onClick={pick(onRename)} className={`${item} text-ink`}>
            <Pencil size={14} aria-hidden="true" /> Rename
          </button>
          <button type="button" role="menuitem" onClick={pick(onPin)} className={`${item} text-ink`}>
            {conversation.pinned ? <PinOff size={14} aria-hidden="true" /> : <Pin size={14} aria-hidden="true" />}
            {conversation.pinned ? "Unpin" : "Pin"}
          </button>
          <button type="button" role="menuitem" onClick={pick(onDelete)} className={`${item} text-bad`}>
            <Trash2 size={14} aria-hidden="true" /> Delete
          </button>
        </div>
      )}
    </div>
  );
}

function RenameField({ initial, onDone }: { initial: string; onDone: (title: string | null) => void }) {
  const [value, setValue] = useState(initial);
  const input = useRef<HTMLInputElement>(null);
  const done = useRef(false);
  useEffect(() => {
    input.current?.focus();
    input.current?.select();
  }, []);
  function finish(save: boolean) {
    if (done.current) return;
    done.current = true;
    const title = value.trim().slice(0, 120);
    onDone(save && title && title !== initial ? title : null);
  }
  return (
    <input
      ref={input}
      value={value}
      maxLength={120}
      aria-label="Chat title"
      onChange={(event) => setValue(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter") finish(true);
        if (event.key === "Escape") { event.stopPropagation(); finish(false); }
      }}
      onBlur={() => finish(true)}
      className="h-9 w-full rounded-md border border-line-strong bg-surface px-2 text-sm text-ink outline-none"
    />
  );
}

function ConfirmDelete({ conversation, onCancel, onConfirm }: {
  conversation: ConversationSummary;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      onClose={onCancel}
      aria-labelledby="delete-chat-title"
      className="fixed inset-0 m-auto w-[min(92vw,26rem)] rounded-xl border border-line bg-surface p-5 text-ink backdrop:bg-black/50"
    >
      <h2 id="delete-chat-title" className="text-base font-semibold">Delete chat?</h2>
      <p className="mt-2 text-sm text-muted">
        This deletes <span className="font-medium text-ink">“{conversation.title}”</span> and all of its messages. This can’t be undone.
      </p>
      {error && <p role="alert" className="mt-3 text-sm text-bad">{error}</p>}
      <div className="mt-5 flex justify-end gap-2">
        <button type="button" disabled={busy} onClick={() => dialog.current?.close()} className="h-9 cursor-pointer rounded-full border border-line-strong px-4 text-sm hover:bg-surface-2 disabled:opacity-50">
          Cancel
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              await onConfirm();
              dialog.current?.close();
            } catch (cause) {
              setError(cause instanceof Error ? cause.message : "Could not delete this chat.");
              setBusy(false);
            }
          }}
          className="inline-flex h-9 cursor-pointer items-center gap-1.5 rounded-full bg-bad px-4 text-sm font-medium text-surface hover:opacity-90 disabled:opacity-50"
        >
          <Trash2 size={14} aria-hidden="true" />
          {busy ? "Deleting…" : "Delete"}
        </button>
      </div>
    </dialog>
  );
}

function useDebounced<T>(value: T, ms: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

/**
 * The chat list: New chat, search, date groups with pinned first, and a
 * per-row menu. It sits inside the page, separate from the app's nav rail —
 * docked from md up, a drawer below.
 */
export function HistoryPanel({ activeId, streamingIds, onNewChat, onCollapse, onNavigate, onDeleted, onChanged }: {
  activeId: string | null;
  streamingIds: ReadonlySet<string>;
  onNewChat: () => void;
  onCollapse: () => void;
  /** Called after a row is chosen (closes the mobile drawer). */
  onNavigate: () => void;
  onDeleted: (id: string) => void;
  onChanged: (conversation: ConversationSummary) => void;
}) {
  const [query, setQuery] = useState("");
  const q = useDebounced(query.trim(), 250);
  const key = q ? `${HISTORY_KEY}?limit=50&q=${encodeURIComponent(q)}` : `${HISTORY_KEY}?limit=50`;
  const { data, error, isLoading, mutate } = useSWR<ConversationList>(key, chatFetcher, { keepPreviousData: true, revalidateOnFocus: true });
  const [renaming, setRenaming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<ConversationSummary | null>(null);
  const [actionError, setActionError] = useState("");

  async function patch(conversation: ConversationSummary, body: { title?: string; pinned?: boolean }) {
    setActionError("");
    const optimistic = { ...conversation, ...body };
    try {
      await mutate(
        async (current) => {
          const updated = await updateConversation(conversation.conversation_id, body);
          onChanged({ ...optimistic, ...updated });
          return current && { ...current, items: current.items.map((c) => (c.conversation_id === conversation.conversation_id ? { ...c, ...updated } : c)) };
        },
        {
          optimisticData: (current) => current ? { ...current, items: current.items.map((c) => (c.conversation_id === conversation.conversation_id ? optimistic : c)) } : { items: [], total: 0 },
          rollbackOnError: true,
          revalidate: false,
        },
      );
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "Could not update the chat.");
    }
  }

  const groups = q ? (data?.items.length ? [{ label: "Results", items: data.items }] : []) : groupConversations(data?.items ?? []);

  return (
    <div className="flex h-full w-72 flex-col border-r border-line bg-chrome md:w-64">
      <div className="flex items-center gap-1 p-2">
        <button
          type="button"
          onClick={onNewChat}
          className="flex h-9 flex-1 cursor-pointer items-center gap-2 rounded-lg px-2.5 text-sm font-medium text-ink transition-colors hover:bg-surface-2"
        >
          <SquarePen size={16} aria-hidden="true" />
          New chat
        </button>
        <button type="button" onClick={onCollapse} aria-label="Hide chat history" title="Hide chat history" className={ICON_BUTTON}>
          <PanelLeftClose size={16} aria-hidden="true" className="max-md:hidden" />
          <X size={16} aria-hidden="true" className="md:hidden" />
        </button>
      </div>

      <div className="relative px-2 pb-2">
        <Search size={14} aria-hidden="true" className="pointer-events-none absolute top-1/2 left-4.5 -translate-y-1/2 text-muted" />
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Escape" && query) { event.stopPropagation(); setQuery(""); } }}
          placeholder="Search chats"
          aria-label="Search chats"
          className="h-9 w-full rounded-lg border border-line bg-surface-3 pr-8 pl-8 text-sm text-ink transition-colors placeholder:text-muted hover:border-line-strong"
        />
        {isLoading && q && (
          <LoaderCircle size={14} aria-hidden="true" className="absolute top-1/2 right-4.5 -translate-y-1/2 animate-spin text-muted" />
        )}
      </div>

      <nav aria-label="Chats" className="relative min-h-0 flex-1 overflow-y-auto overscroll-contain px-2 pb-4">
        {actionError && <p role="alert" className="mx-2 mb-2 text-xs text-bad">{actionError}</p>}
        {error && !data ? (
          <div className="px-2 py-4 text-sm text-muted">
            <p>{(error as Error).message || "Cannot load chats."}</p>
            <button type="button" onClick={() => void mutate()} className="mt-2 cursor-pointer text-sm font-medium text-info hover:underline">Retry</button>
          </div>
        ) : !data ? (
          <ul aria-label="Loading chats" className="space-y-1.5 px-2 pt-3">
            {[70, 55, 80, 62].map((w) => <li key={w} className="h-4 animate-pulse rounded bg-surface-2" style={{ width: `${w}%` }} />)}
          </ul>
        ) : groups.length === 0 ? (
          <p className="px-2 py-4 text-sm text-muted">{q ? `No chats match “${q}”.` : "No chats yet. Ask something to start one."}</p>
        ) : (
          groups.map((group) => (
            <section key={group.label} aria-labelledby={`history-${group.label}`} className="mt-3 first:mt-1">
              <h3 id={`history-${group.label}`} className="px-2.5 pb-1 text-xs font-medium text-muted">{group.label}</h3>
              <ul className="space-y-0.5">
                {group.items.map((conversation) => {
                  const id = conversation.conversation_id;
                  const active = id === activeId;
                  return (
                    // The row with an open menu sits above the rows after it.
                    <li key={id} className="group relative has-[[aria-expanded=true]]:z-20">
                      {renaming === id ? (
                        <RenameField
                          initial={conversation.title}
                          onDone={(title) => {
                            setRenaming(null);
                            if (title) void patch(conversation, { title });
                          }}
                        />
                      ) : (
                        <>
                          <Link
                            href={`/assistant/${id}`}
                            onClick={onNavigate}
                            aria-current={active ? "page" : undefined}
                            title={conversation.last_message_preview ?? conversation.title}
                            className={`flex h-9 items-center gap-2 rounded-lg pr-9 pl-2.5 text-sm transition-colors ${
                              active ? "bg-surface-2 font-medium text-ink" : "text-muted hover:bg-surface-2 hover:text-ink"
                            }`}
                          >
                            {conversation.pinned && (
                              <>
                                <Pin size={12} aria-hidden="true" className="shrink-0 text-accent-ink" />
                                <span className="sr-only">Pinned: </span>
                              </>
                            )}
                            <span className="truncate">{conversation.title}</span>
                            {conversation.mission_id && <MissionBadge missionId={conversation.mission_id} />}
                            {streamingIds.has(id) && (
                              <>
                                <span aria-hidden="true" className={`${conversation.mission_id ? "" : "ml-auto "}size-2 shrink-0 animate-pulse rounded-full bg-accent`} />
                                <span className="sr-only"> (answering)</span>
                              </>
                            )}
                          </Link>
                          <RowMenu
                            conversation={conversation}
                            onRename={() => setRenaming(id)}
                            onPin={() => void patch(conversation, { pinned: !conversation.pinned })}
                            onDelete={() => setDeleting(conversation)}
                          />
                        </>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          ))
        )}
        {data && !q && data.total > data.items.length && (
          <p className="px-2.5 pt-3 text-xs text-muted">Showing {data.items.length} of {data.total}. Search to find older chats.</p>
        )}
      </nav>

      {deleting && (
        <ConfirmDelete
          conversation={deleting}
          onCancel={() => setDeleting(null)}
          onConfirm={async () => {
            await deleteConversation(deleting.conversation_id);
            onDeleted(deleting.conversation_id);
            await mutate();
          }}
        />
      )}
    </div>
  );
}
