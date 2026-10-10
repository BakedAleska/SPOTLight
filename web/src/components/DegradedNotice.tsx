import { TriangleAlert } from "lucide-react";

/* Shown only when the backend could not be read and the page is rendering the
   kit CSVs instead.

   It is deliberately a visible banner and not a console warning. Every number
   on this page is a claim about equipment, and the difference between "this is
   what the fleet is doing now" and "this is what the sample data said" is the
   difference between a console and a screenshot — an operator (or a judge)
   must never have to guess which one they are reading. The reason is printed
   verbatim because it is almost always the actionable part: a refused
   connection means the backend isn't up, a 500 means Neon is. */
export function DegradedNotice({ reason }: { reason: string }) {
  return (
    <div
      role="status"
      className="flex items-start gap-3 rounded-panel border border-line bg-surface-2 px-4 py-3"
    >
      <TriangleAlert size={16} aria-hidden="true" className="mt-0.5 shrink-0 text-warn" />
      <div className="min-w-0">
        <p className="text-sm font-medium text-ink">
          Live triage unavailable — showing the challenge kit’s own data.
        </p>
        <p className="mt-0.5 text-xs leading-snug text-muted">
          These are the kit CSVs, triaged in this app&rsquo;s own server process — not flags from the
          database. Nothing here reflects a live mission, and neither Laya nor System&nbsp;2 is in the loop.{" "}
          <span className="font-mono break-words">{reason}</span>
        </p>
      </div>
    </div>
  );
}
