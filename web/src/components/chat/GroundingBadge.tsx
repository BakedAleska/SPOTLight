import { CircleSlash, ShieldAlert, ShieldCheck, TriangleAlert } from "lucide-react";
import { groundingBadge, type BadgeTone } from "@/lib/chat/grounding";
import type { Grounding } from "@/lib/chat/types";

/* Status is colour + icon + word, never colour alone. */
const TONE: Record<BadgeTone, { className: string; Icon: typeof ShieldCheck }> = {
  ok: { className: "border-ok/35 bg-ok/10 text-ok", Icon: ShieldCheck },
  warn: { className: "border-warn/40 bg-warn/10 text-warn", Icon: TriangleAlert },
  bad: { className: "border-bad/40 bg-bad/10 text-bad", Icon: ShieldAlert },
  muted: { className: "border-line bg-surface-2 text-muted", Icon: CircleSlash },
};

export function GroundingBadge({ grounding }: { grounding: Grounding | null | undefined }) {
  const badge = groundingBadge(grounding);
  if (!badge) return null;
  const { className, Icon } = TONE[badge.tone];
  return (
    <span
      title={badge.detail}
      className={`inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs font-medium ${className}`}
    >
      <Icon size={13} aria-hidden="true" />
      {badge.label}
      <span className="sr-only">. {badge.detail}</span>
    </span>
  );
}
