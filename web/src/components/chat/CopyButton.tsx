"use client";

import { useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Clipboard API unavailable (http origin, permissions): fall back to a
    // hidden textarea and the legacy copy command.
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

export function CopyButton({ text, label = "Copy", compact = false }: { text: string; label?: string; compact?: boolean }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(timer);
  }, [copied]);
  return (
    <button
      type="button"
      onClick={async () => setCopied(await copyText(text))}
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
      className={
        compact
          ? "inline-flex cursor-pointer items-center gap-1 rounded px-1.5 py-0.5 text-xs text-muted transition-colors hover:bg-surface-2 hover:text-ink"
          : "grid size-8 cursor-pointer place-items-center rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-ink"
      }
    >
      {copied ? <Check size={14} aria-hidden="true" className="text-ok" /> : <Copy size={14} aria-hidden="true" />}
      {compact && <span>{copied ? "Copied" : "Copy"}</span>}
      <span className="sr-only" aria-live="polite">{copied ? "Copied to clipboard" : ""}</span>
    </button>
  );
}
