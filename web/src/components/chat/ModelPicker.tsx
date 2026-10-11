"use client";

import { useEffect, useRef } from "react";
import { Check, ChevronDown, Sparkles } from "lucide-react";
import type { ModelInfo } from "@/lib/chat/types";
import { moveMenuFocus, usePopover } from "./usePopover";

/** The chat header's model switcher: label, one-line description, and a
    check on the current model — like the frontier chat apps. */
export function ModelPicker({ models, value, onChange, disabled = false }: {
  models: ModelInfo[];
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
}) {
  const { open, setOpen, root, trigger } = usePopover();
  const menu = useRef<HTMLDivElement>(null);
  const current = models.find((m) => m.id === value);

  // Opening moves focus to the checked item, so arrows start from there.
  useEffect(() => {
    if (!open) return;
    const checked = menu.current?.querySelector<HTMLElement>("[aria-checked='true']") ?? menu.current?.querySelector<HTMLElement>("[role='menuitemradio']");
    checked?.focus();
  }, [open]);

  return (
    <div ref={root} className="relative">
      <button
        ref={trigger}
        type="button"
        onClick={() => setOpen(!open)}
        disabled={disabled || models.length === 0}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Model: ${current?.label ?? value}. Change model`}
        className="inline-flex h-9 cursor-pointer items-center gap-1.5 rounded-lg px-2.5 text-[15px] font-medium text-ink transition-colors hover:bg-surface-2 disabled:cursor-default disabled:opacity-60"
      >
        <Sparkles size={15} aria-hidden="true" className="text-accent-ink" />
        <span className="max-w-[46vw] truncate sm:max-w-none">{current?.label ?? (value || "Model")}</span>
        <ChevronDown size={15} aria-hidden="true" className={`text-muted transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div
          ref={menu}
          role="menu"
          aria-label="Choose a model"
          onKeyDown={(event) => moveMenuFocus(menu.current, event)}
          className="panel-shadow absolute top-full left-0 z-40 mt-1 w-[min(22rem,calc(100vw-2rem))] rounded-xl border border-line bg-surface p-1.5 shadow-xl"
        >
          <p className="px-2.5 pt-1.5 pb-1 text-xs font-medium text-muted">Model</p>
          {models.map((model) => {
            const selected = model.id === value;
            return (
              <button
                key={model.id}
                type="button"
                role="menuitemradio"
                aria-checked={selected}
                onClick={() => {
                  onChange(model.id);
                  setOpen(false);
                  trigger.current?.focus();
                }}
                className="flex w-full cursor-pointer items-start gap-3 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-surface-2 focus-visible:bg-surface-2"
              >
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-ink">{model.label}</span>
                  <span className="mt-0.5 block text-xs leading-snug text-muted">{model.description}</span>
                </span>
                <Check size={16} aria-hidden="true" className={`mt-0.5 shrink-0 text-accent-ink ${selected ? "" : "invisible"}`} />
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
