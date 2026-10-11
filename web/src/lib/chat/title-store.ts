"use client";

import { useSyncExternalStore } from "react";

/* The open chat's title, published by the chat page and read by the app
   shell's breadcrumb. The shell sits above the /assistant layout, so a prop
   can't reach it; a tiny external store can, without a context provider
   around the whole app. */

const titles = new Map<string, string>();
const listeners = new Set<() => void>();
let version = 0;

export function publishChatTitle(conversationId: string, title: string) {
  if (titles.get(conversationId) === title) return;
  titles.set(conversationId, title);
  version++;
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The title for this conversation id, or null until the page knows it. */
export function useChatTitle(conversationId: string | null): string | null {
  useSyncExternalStore(subscribe, () => version, () => 0);
  return conversationId ? titles.get(conversationId) ?? null : null;
}
