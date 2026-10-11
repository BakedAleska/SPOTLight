"use client";

import { useEffect, useState } from "react";
import { Clock, RotateCw, TriangleAlert, Zap } from "lucide-react";
import { describeError, offersModelSwitch } from "@/lib/chat/api";
import type { ChatErrorCode } from "@/lib/chat/types";

function useCountdown(seconds: number | null, since: number) {
  const remaining = () => (seconds ? Math.max(0, Math.ceil(seconds - (Date.now() - since) / 1000)) : 0);
  const [left, setLeft] = useState(remaining);
  useEffect(() => {
    if (!seconds) return;
    const timer = setInterval(() => {
      const next = Math.max(0, Math.ceil(seconds - (Date.now() - since) / 1000));
      setLeft(next);
      if (next === 0) clearInterval(timer);
    }, 250);
    return () => clearInterval(timer);
  }, [seconds, since]);
  return left;
}

/**
 * A failed answer, live or reloaded from history (chat.2 `error_code`).
 * rate_limited and model_unavailable offer a one-click switch to the next
 * model (each model has its own free-tier quota); rate_limited also counts
 * down when the server said how long to wait (a stored one has no
 * countdown). Everything gets the plain-language reason and a Retry.
 */
export function ErrorCard({
  code,
  detail,
  retryAfterS = null,
  since = 0,
  modelLabel,
  alternative,
  onRetry,
  onTryModel,
}: {
  code: ChatErrorCode;
  detail?: string | null;
  retryAfterS?: number | null;
  since?: number;
  modelLabel?: string;
  alternative?: { id: string; label: string } | null;
  onRetry?: () => void;
  onTryModel?: (id: string) => void;
}) {
  const limited = code === "rate_limited";
  const countdown = limited && retryAfterS != null && retryAfterS > 0;
  const left = useCountdown(countdown ? retryAfterS : null, since);
  const { title, hint } = describeError(code, detail, modelLabel);
  const switchable = offersModelSwitch(code) && !!alternative && !!onTryModel;
  const Icon = limited ? Clock : TriangleAlert;
  return (
    <div
      role="group"
      aria-label={title}
      className={`rounded-xl border p-4 ${limited ? "border-warn/40 bg-warn/5" : "border-bad/35 bg-bad/5"}`}
    >
      <div className="flex gap-3">
        <Icon size={18} aria-hidden="true" className={`mt-0.5 shrink-0 ${limited ? "text-warn" : "text-bad"}`} />
        <div className="min-w-0 flex-1">
          <p className="font-medium text-ink">{title}</p>
          <p className="mt-1 text-sm text-muted">
            {hint}
            {countdown && (left > 0 ? <> Try again in <span className="nums font-mono text-ink">{left}s</span>.</> : " You can try again now.")}
          </p>
          {detail && !limited && code !== "invalid_request" && (
            <p className="mt-1 text-xs break-words text-faint">{detail}</p>
          )}
          {(onRetry || switchable) && (
            <div className="mt-3 flex flex-wrap gap-2">
              {switchable && (
                <button
                  type="button"
                  onClick={() => onTryModel!(alternative!.id)}
                  className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-full bg-accent px-3.5 text-sm font-medium text-on-accent transition-colors hover:bg-accent-hover"
                >
                  <Zap size={14} aria-hidden="true" />
                  Try with {alternative!.label}
                </button>
              )}
              {onRetry && (
                <button
                  type="button"
                  onClick={onRetry}
                  disabled={countdown && left > 0}
                  className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-full border border-line-strong px-3.5 text-sm text-ink transition-colors hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <RotateCw size={14} aria-hidden="true" />
                  {countdown && left > 0 ? <>Retry in <span className="nums font-mono">{left}s</span></> : "Retry"}
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
