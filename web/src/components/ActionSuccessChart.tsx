import Link from "next/link";
import { Panel } from "./Panel";
import type { SuccessGroup } from "@/lib/types";
import { breakdownHref } from "@/lib/breakdown-link";

/* Action success by inspection type, as a column chart: the fleet-wide view of
   which kinds of work SPOT gets right and which it doesn't. Best type on the
   left; the weakest one, when it sits clearly under the fleet line, is drawn
   in the alarm colour.

   Plain CSS and no chart library, deliberately: Recharts would cost a client
   bundle and a hydration boundary to draw five rectangles, and this panel
   server-renders with the rest of the page. What it does borrow from a real
   chart is the chrome — a 0–100 axis with gridlines, so a bar's height means a
   number rather than just "taller than that one", and the fleet's overall
   success rate as a reference line, so each type's gap is measurable on the
   card instead of only asserted in the caption.

   LAYOUT. Two grid columns — the y-axis labels and the plot — over a row
   track that is always a *definite* height, and definite is the operative
   word. The first version of this panel let each bar's flex column size
   itself to its content, so every `height: N%` resolved against an auto
   height, which CSS resolves to `auto`: all four bars collapsed to nothing
   and the panel drew an empty box. Everything below is laid out against a
   height that is known before the bars are measured.

   Every value is printed on its bar and every sample size under it, so nothing
   here is readable only by hovering, or only by comparing colours. */

/** The plot's floor, in px. The panel sits beside the priority queue and is
    stretched to its height, so the plot takes whatever is left over after the
    axis and the caption — this is only what it falls back to when there is no
    height to share out, which is what happens at narrow widths where the two
    panels stack and each one sizes to its own content. */
const PLOT_MIN = 168;

/** Headroom above the 100% line for the value labels riding the bar caps —
    without it a bar near 100% pushes its own label out of the panel. */
const CAP_LABEL = 18;

/** Gridlines, in percent. 0 is left out: the x-axis rule below the plot is
    already that line, and drawing both would read as a double rule. */
const GRIDLINES = [25, 50, 75, 100];

/** 25 and 75 are drawn but not labelled. A labelled midline is enough to read
    the scale by, and five tick labels on a four-bar chart is more axis than
    data. */
const TICKS = [0, 50, 100];

/** Caps the bar at a sane width when there are only a handful of groups, so a
    wide panel gets air between the columns instead of fat blocks. */
const BAR_MAX = 24;

export function ActionSuccessChart({
  groups,
  caption,
  benchmark = null,
  delay,
  collapsible = false,
}: {
  groups: SuccessGroup[];
  /** The takeaway sentence, generated from the same numbers as the bars. */
  caption: string;
  /** Success across every action, drawn as the reference line. Null when
      there is no data — the chart then just drops the line rather than
      inventing a baseline. */
  benchmark?: number | null;
  delay?: number;
  collapsible?: boolean;
}) {
  // An empty dataset is a real state here: the console comes up before a
  // replay has been run. Axes over no data look like a rendering fault, so
  // the caption (which says exactly that) is all that's shown.
  if (groups.length === 0) {
    return (
      <Panel title="Action success by inspection type" className="h-full" delay={delay} collapsible={collapsible}>
        <p className="text-xs leading-snug text-muted">{caption}</p>
      </Panel>
    );
  }

  // Groups arrive best first, so the last one is the weakest. It is only
  // called out when it is a real gap — more than a point under the fleet.
  const weakest = groups[groups.length - 1];
  const flagged = benchmark !== null && benchmark - weakest.successRate > 1 ? weakest.key : null;

  return (
    <Panel
      title="Action success by inspection type"
      collapsible={collapsible}
      // Fill the shared grid row so the chart and attention queue align.
      className="h-full"
      aside={
        benchmark === null ? undefined : (
          <span className="inline-flex items-center gap-1.5">
            <span
              aria-hidden
              className="inline-block w-3.5 border-t border-dashed border-line-strong"
            />
            {benchmark}% fleet
          </span>
        )
      }
      delay={delay}
    >
      {/* Two rows: the plot, which absorbs the panel's slack, and the axis
          band under it. `minmax(…, 1fr)` is doing the load-bearing work —
          it keeps the track a *definite* height in both cases, which is what
          every percentage below resolves against. */}
      <div
        className="grid min-h-0 flex-1 grid-cols-[1.75rem_1fr] gap-x-2"
        style={{ gridTemplateRows: `minmax(${PLOT_MIN + CAP_LABEL}px, 1fr) auto` }}
      >
        {/* Y axis. Absolute labels on a box of the same height as the plot, so
            each one sits exactly on its gridline rather than on a flex guess. */}
        <div className="relative" style={{ marginTop: CAP_LABEL }}>
          {TICKS.map((tick) => (
            <span
              key={tick}
              className="nums absolute right-0 translate-y-1/2 font-mono text-[10px] text-faint"
              style={{ bottom: `${tick}%` }}
            >
              {tick}
            </span>
          ))}
        </div>

        <div className="relative" style={{ marginTop: CAP_LABEL }}>
          {/* Hairline, solid, one step off the surface: a grid should be
              legible and otherwise invisible. Dashes are reserved for the
              reference line, where the dashing actually means something. */}
          {GRIDLINES.map((line) => (
            <div
              key={line}
              aria-hidden
              className="absolute inset-x-0 border-t border-line"
              style={{ bottom: `${line}%` }}
            />
          ))}

          {benchmark !== null && (
            <div
              aria-hidden
              className="absolute inset-x-0 border-t border-dashed border-line-strong"
              style={{ bottom: `${benchmark}%` }}
            />
          )}

          {/* The bars. `inset-0` is what makes the columns' height definite,
              and therefore what makes the bars have any height at all. */}
          <div className="absolute inset-0 flex items-end gap-2">
            {groups.map((group, i) => {
              const isFlagged = group.key === flagged;
              return (
                <Link
                  key={group.key}
                  // The hover band is Grafana's: the whole column lights up,
                  // so a 24px bar doesn't have to be hit precisely. `title`
                  // carries the counts behind the percentage; the same string
                  // is the column's accessible name, so it isn't mouse-only.
                  // A click opens every action of this type (/analytics/breakdown).
                  href={breakdownHref({ view: "actions", type: group.key })}
                  className="group relative flex h-full flex-1 items-end justify-center rounded-t-sm transition-colors hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-accent"
                  aria-label={`${group.label}: ${group.successRate}% of ${group.sampleSize} actions succeeded. Show these actions.`}
                  title={`${group.label} — ${group.successRate}% success, n=${group.sampleSize}. Click to see them.`}
                >
                  <div
                    className={`anim-grow relative w-full rounded-t-[4px] ${isFlagged ? "bg-bad" : "bg-ok"}`}
                    style={{
                      height: `${group.successRate}%`,
                      // Grow once the panel has floated in, left to right.
                      animationDelay: `${(delay ?? 0) + 200 + i * 90}ms`,
                      // A 0% group still gets a visible stub: "no action
                      // succeeded here" and "no data here" must not look alike.
                      minHeight: 2,
                      maxWidth: BAR_MAX,
                    }}
                  >
                    {/* Value on the cap. The flagged type's is full-strength ink and
                        the rest are muted — the labels are all present, but
                        only one of them is the point of the chart. */}
                    <span
                      className={`nums absolute bottom-full left-1/2 -translate-x-1/2 pb-0.5 font-mono text-xs ${
                        isFlagged ? "font-semibold text-ink" : "text-muted"
                      }`}
                    >
                      {group.successRate}%
                    </span>
                  </div>
                </Link>
              );
            })}
          </div>
        </div>

        {/* X axis. Its rule is the plot's baseline — no gap, or the 0 gridline
            and this border read as two lines. Each group's sample size rides
            with its label, so the bars can be discounted by eye where n is
            small. */}
        <div />
        <div className="flex gap-2 border-t border-line pt-1.5">
          {groups.map((group) => (
            <div key={group.key} className="min-w-0 flex-1 text-center">
              <p className="nums truncate font-mono text-xs font-medium text-ink">
                {group.label}
              </p>
              <p className="nums font-mono text-[11px] text-faint">n={group.sampleSize}</p>
            </div>
          ))}
        </div>
      </div>

      <p className="mt-3 text-xs leading-snug text-muted">{caption}</p>
    </Panel>
  );
}
