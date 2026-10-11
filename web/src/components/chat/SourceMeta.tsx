import { Database, FileText, Waypoints } from "lucide-react";
import type { SourceOrigin } from "@/lib/chat/types";

const ORIGIN: Record<SourceOrigin, { label: string; Icon: typeof Database }> = {
  rag: { label: "Document", Icon: FileText },
  graph: { label: "Graph", Icon: Waypoints },
  sql: { label: "SQL", Icon: Database },
};

/** "Document · incident", with an icon so origin isn't colour-only. */
export function OriginLabel({ origin, kind }: { origin: SourceOrigin; kind?: string }) {
  const { label, Icon } = ORIGIN[origin] ?? ORIGIN.rag;
  return (
    <span className="inline-flex min-w-0 items-center gap-1">
      <Icon size={12} aria-hidden="true" className="shrink-0" />
      <span className="truncate">
        {label}
        {kind ? ` · ${kind.replace(/_/g, " ")}` : ""}
      </span>
    </span>
  );
}
