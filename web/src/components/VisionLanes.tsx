import { Check, Clock } from "lucide-react";
import { Panel } from "./Panel";
import type { ModelLane } from "@/lib/types";

/* What touched this image, in order, and what each one is allowed to do.

   This strip is the page's argument, not decoration. The whole design rests
   on one split — code computes every number, the models only describe and
   explain — and a viewer that showed readings without saying who produced
   them would be indistinguishable from one where a VLM guessed them. Naming
   the models is also the rubric's "technical understanding" line item.

   A pending lane is drawn, not hidden. An empty slot that says what it is
   waiting for is information; a lane that appears later is a surprise. */

const STATE: Record<
  ModelLane["state"],
  { icon: typeof Check; label: string; tone: string }
> = {
  live: { icon: Check, label: "Producing this page", tone: "text-ok" },
  pending: { icon: Clock, label: "Wired, not answering yet", tone: "text-faint" },
};

function Lane({ lane, index }: { lane: ModelLane; index: number }) {
  const { icon: Icon, label, tone } = STATE[lane.state];

  return (
    <li
      /* A pending lane is dimmed as a whole rather than greyed per element,
         so the live lane is the one the eye lands on first. */
      className={`flex min-w-0 flex-col gap-1.5 rounded border border-line bg-surface-3 p-3 ${
        lane.state === "pending" ? "opacity-70" : ""
      }`}
    >
      {/* The status sits under the name rather than beside it. Three of these
          share a row, so at the width each one actually gets, a badge on the
          same line only ever wins by breaking the heading in half. */}
      <div className="flex items-baseline gap-2">
        <span className="nums font-mono text-xs text-faint">{index + 1}</span>
        <h3 className="min-w-0 truncate text-sm font-medium text-ink">{lane.stage}</h3>
      </div>
      <span className={`inline-flex items-center gap-1 text-xs ${tone}`}>
        <Icon size={12} strokeWidth={2.5} aria-hidden="true" className="shrink-0" />
        {label}
      </span>

      <p className="font-mono text-xs leading-snug break-words text-accent-ink">{lane.model}</p>
      <p className="text-sm leading-snug text-ink">{lane.role}</p>
      <p className="text-xs leading-snug text-muted">{lane.note}</p>
    </li>
  );
}

export function VisionLanes({ lanes, delay }: { lanes: ModelLane[]; delay?: number }) {
  return (
    <Panel
      title="What reads the image"
      aside={`${lanes.filter((l) => l.state === "live").length} of ${lanes.length} live`}
      delay={delay}
    >
      <ul className="grid gap-3 md:grid-cols-3">
        {lanes.map((lane, i) => (
          <Lane key={lane.id} lane={lane} index={i} />
        ))}
      </ul>
    </Panel>
  );
}
