"use client";

import { forwardRef, useEffect, useImperativeHandle, useLayoutEffect, useRef, type FormEvent, type KeyboardEvent } from "react";
import { ArrowUp, Brain, Square } from "lucide-react";
import type { ChatMode } from "@/lib/chat/types";

const MAX = 8000;
const MAX_HEIGHT = 240;

export interface ComposerHandle {
  focus: () => void;
}

/**
 * The message box. Enter sends and Shift+Enter adds a line — except while an
 * IME is composing (Japanese, Chinese, Korean input), where Enter confirms
 * the composition and must not send half a word.
 */
export const Composer = forwardRef<ComposerHandle, {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  streaming: boolean;
  disabled?: boolean;
  disabledReason?: string;
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  thinkingHint?: string;
  placeholder?: string;
  /** Extra controls after the Thinking toggle (the mission picker in a new chat). */
  tools?: React.ReactNode;
}>(function Composer({ value, onChange, onSend, onStop, streaming, disabled = false, disabledReason, mode, onModeChange, thinkingHint, placeholder = "Ask about missions, assets, failures…", tools }, ref) {
  const area = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => ({ focus: () => area.current?.focus() }), []);

  // The composer is re-created when the empty state hands over to the thread
  // view (after the first send) and when a chat finishes loading. If focus
  // went with the old one — it's now on <body> — take it, so typing the next
  // question just works. Focus that is somewhere real is left alone.
  useEffect(() => {
    if (!document.activeElement || document.activeElement === document.body) area.current?.focus({ preventScroll: true });
  }, []);

  // Grow with the text up to MAX_HEIGHT, then scroll inside.
  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, [value]);

  const canSend = !disabled && !streaming && value.trim().length > 0 && value.length <= MAX;

  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (canSend) onSend();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) return;
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    submit();
  }

  const thinking = mode === "thinking";
  return (
    <form
      onSubmit={submit}
      aria-label="Message composer"
      className="chat-composer panel-shadow relative rounded-3xl border border-line bg-surface transition-[border-color,box-shadow]"
    >
      <label htmlFor="chat-composer" className="sr-only">Message</label>
      <textarea
        id="chat-composer"
        ref={area}
        rows={1}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder={disabled && disabledReason ? disabledReason : placeholder}
        disabled={disabled}
        maxLength={MAX + 500}
        aria-describedby="chat-disclaimer"
        className="chat-composer-input block max-h-60 w-full resize-none bg-transparent px-5 pt-4 pb-2 text-[15px] leading-6 text-ink placeholder:text-muted disabled:cursor-not-allowed"
      />
      <div className="flex items-center gap-2 px-3 pb-3">
        <button
          type="button"
          onClick={() => onModeChange(thinking ? "fast" : "thinking")}
          aria-pressed={thinking}
          title={thinkingHint}
          disabled={disabled}
          className={`inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-full border px-3 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
            thinking
              ? "border-accent bg-accent/15 font-medium text-accent-ink"
              : "border-line text-muted hover:bg-surface-2 hover:text-ink"
          }`}
        >
          <Brain size={14} aria-hidden="true" />
          Thinking
        </button>
        {tools}
        {value.length > MAX - 1000 && (
          <span className={`nums ml-auto font-mono text-xs ${value.length > MAX ? "text-bad" : "text-muted"}`}>
            {value.length}/{MAX}
          </span>
        )}
        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            aria-label="Stop generating"
            title="Stop generating"
            className="ml-auto grid size-9 cursor-pointer place-items-center rounded-full bg-ink text-canvas transition-opacity hover:opacity-85"
          >
            <Square size={13} aria-hidden="true" fill="currentColor" />
          </button>
        ) : (
          <button
            type="submit"
            disabled={!canSend}
            aria-label="Send message"
            title="Send (Enter)"
            className={`grid size-9 cursor-pointer place-items-center rounded-full bg-accent text-on-accent transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-faint ${value.length > MAX - 1000 ? "" : "ml-auto"}`}
          >
            <ArrowUp size={18} aria-hidden="true" strokeWidth={2.25} />
          </button>
        )}
      </div>
    </form>
  );
});

export function Disclaimer() {
  return (
    <p id="chat-disclaimer" className="mt-2 text-center text-xs text-muted">
      Answers are grounded in SPOT mission data. Check the cited sources.
    </p>
  );
}
