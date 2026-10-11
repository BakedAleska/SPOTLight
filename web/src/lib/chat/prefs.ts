import { useSyncExternalStore } from "react";

/* Per-viewer conveniences in localStorage: last model, last mode, whether the
   history panel is open. Every access is wrapped — storage can be missing or
   throw (private mode, blocked site data) — and an in-memory copy keeps the
   choice for this tab when it does. Read with useSyncExternalStore so the
   server snapshot is null and hydration never mismatches. */

export const PREF_MODEL = "spotlight-chat-model";
export const PREF_MODE = "spotlight-chat-mode";
export const PREF_HISTORY = "spotlight-chat-history";

const memory = new Map<string, string>();
const CHANGED = "spotlight:chatpref";

export function readPref(key: string): string | null {
  try {
    const stored = localStorage.getItem(key);
    if (stored !== null) return stored;
  } catch {
    /* fall through to memory */
  }
  return memory.get(key) ?? null;
}

export function setPref(key: string, value: string) {
  memory.set(key, value);
  try {
    localStorage.setItem(key, value);
  } catch {
    /* Storage blocked: the in-memory copy still applies for this tab. */
  }
  window.dispatchEvent(new Event(CHANGED));
}

function subscribe(onChange: () => void) {
  window.addEventListener(CHANGED, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CHANGED, onChange);
    window.removeEventListener("storage", onChange);
  };
}

export function usePref(key: string): string | null {
  return useSyncExternalStore(subscribe, () => readPref(key), () => null);
}
