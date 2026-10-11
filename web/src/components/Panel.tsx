import { CollapsiblePanel } from "./CollapsiblePanel";

/* Every box on the dashboard is this. One primitive means the border, radius,
   header height and shadow are defined once and can never drift between
   panels — which is the single thing that makes a console look built rather
   than assembled.

   Grafana's panel title is sentence case at body size, not a small-caps label.
   It reads as the name of a thing you can open, which is what it is. */

/* A Server Component can't call useId, and the heading needs a stable id for
   aria-labelledby. Panel titles are unique on a dashboard by definition — two
   panels with the same name would be a design bug — so the title slug is a
   safe id. */
function slugify(title: string) {
  return `panel-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}`;
}

export function Panel({
  title,
  aside,
  children,
  className = "",
  bodyClassName = "p-4",
  delay = 0,
  collapsible = false,
  toolbar = false,
}: {
  title: string;
  /** Right-aligned header slot: counts, units, a time range. */
  aside?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  /** ms to hold before this panel floats in, for load cascades. */
  delay?: number;
  /** Adds a chevron that folds the body away. */
  collapsible?: boolean;
  /** The aside is a control group (pills, toggles): it sits as far from the
      header's right edge as from its top and bottom. */
  toolbar?: boolean;
}) {
  const id = slugify(title);
  const style = delay ? { animationDelay: `${delay}ms` } : undefined;

  if (collapsible) {
    return (
      <CollapsiblePanel id={id} title={title} aside={aside} className={className} bodyClassName={bodyClassName} style={style}>
        {children}
      </CollapsiblePanel>
    );
  }

  return (
    <section
      aria-labelledby={id}
      // An inline delay rather than a Tailwind arbitrary class: the value is a
      // prop, and Tailwind can only generate classes it can read in the source.
      style={style}
      className={`anim-float panel-shadow flex flex-col overflow-hidden rounded-panel border border-line bg-surface ${className}`}
    >
      <header className={`flex h-9 shrink-0 items-center gap-3 border-b border-line ${toolbar ? "pr-1 pl-4" : "px-4"}`}>
        <h2 id={id} className="truncate text-sm font-medium text-ink">
          {title}
        </h2>
        {aside && (
          <div className="nums ml-auto shrink-0 text-xs text-muted">{aside}</div>
        )}
      </header>
      <div className={`flex min-h-0 flex-1 flex-col ${bodyClassName}`}>
        {children}
      </div>
    </section>
  );
}
