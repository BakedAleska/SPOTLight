import { LoadingDots } from "./LoadingDots";

/** Shared loading state: the same blinking dots SPOT shows in Replay. Use compact inside a panel; pages fill their content area. */
export function LoadingIndicator({ label = "Loading…", compact = false }: {
  label?: string;
  compact?: boolean;
}) {
  return (
    <div role="status" aria-live="polite" aria-busy="true"
      className={`flex flex-col items-center justify-center gap-4 text-accent-ink ${compact ? "min-h-36 py-6" : "h-full min-h-[60dvh] p-6"}`}>
      <div className={compact ? "scale-150" : "scale-[2.5]"}>
        <LoadingDots color="currentColor" />
      </div>
      <span className="sr-only">{label}</span>
    </div>
  );
}
