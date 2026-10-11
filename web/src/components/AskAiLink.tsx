import Link from "next/link";
import { Sparkles } from "lucide-react";
import { assistantHref } from "@/lib/chat/mission";

/**
 * "Ask SPOTbot" on a mission: opens a new chat focused on it
 * (/assistant?mission=<id>, contract §9). On a card it sits above the card's
 * stretched replay link (`relative z-10`), as a sibling, never nested inside it.
 */
export function AskAiLink({ missionId, label = "Ask SPOTbot", className = "" }: { missionId: string; label?: string; className?: string }) {
  return (
    <Link
      href={assistantHref(missionId)}
      aria-label={label === "Ask SPOTbot" ? `Ask SPOTbot about ${missionId}` : undefined}
      className={`relative z-10 inline-flex min-h-9 items-center gap-1.5 rounded-full border border-accent/50 bg-accent/10 px-3 text-sm font-medium text-accent-ink transition-colors hover:border-accent hover:bg-accent/20 ${className}`}
    >
      <Sparkles size={14} aria-hidden="true" />
      {label}
    </Link>
  );
}
