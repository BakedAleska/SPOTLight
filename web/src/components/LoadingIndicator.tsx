import { SpotlightLogo } from "./SpotlightLogo";

/** Shared loading state. Use compact inside a panel; pages fill their content area. */
export function LoadingIndicator({ label = "Loading…", compact = false }: {
  label?: string;
  compact?: boolean;
}) {
  return (
    <div role="status" aria-live="polite" aria-busy="true"
      className={`spotlight-loading flex flex-col items-center justify-center gap-4 ${compact ? "min-h-36 py-6" : "h-full min-h-[60dvh] p-6"}`}>
      <div className={`p-3 ${compact ? "size-20" : "size-28"}`}>
        <SpotlightLogo animated small className="h-full w-full" />
      </div>
      <span className="sr-only">{label}</span>
    </div>
  );
}
