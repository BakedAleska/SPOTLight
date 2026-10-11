"use client";

import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { Children, cloneElement, isValidElement, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Panel } from "./Panel";
import { valueBar } from "./ValueBar";
import type { FleetAnalytics } from "@/lib/fleet-analytics";
import { breakdownHref, type BreakdownQuery } from "@/lib/breakdown-link";

/* The Analytics page's chart catalogue, grouped by the question it answers:
   reliability, configuration, missions, failures & anomalies, waypoints.

   Colour follows the dataviz rules the rest of the console keeps: status
   tokens (ok/bad) only where a mark means success or failure, the series
   tokens for identity (V1 vs V2), and a single-hue ramp for anything ordered
   (severity, failure rate). Grids are solid hairlines; every chart with more
   than one series carries a legend, and every mark has a tooltip. */

const AXIS = { fill: "var(--c-muted)", fontSize: 11 };
const GRID = { stroke: "var(--c-line)", vertical: false } as const;
const TOOLTIP = {
  contentStyle: {
    background: "var(--c-surface)",
    border: "1px solid var(--c-line)",
    borderRadius: 4,
    color: "var(--c-ink)",
    fontSize: 12,
  },
  labelStyle: { color: "var(--c-ink)", fontWeight: 500 },
  itemStyle: { color: "var(--c-ink)" },
  cursor: { fill: "var(--c-surface-2)" },
};

/** Severity is ordered, so it is one hue stepped light → dark, not four hues. */
const SEVERITY_FILL = {
  LOW: "color-mix(in oklab, var(--c-bad) 40%, var(--c-surface))",
  MEDIUM: "color-mix(in oklab, var(--c-bad) 60%, var(--c-surface))",
  HIGH: "color-mix(in oklab, var(--c-bad) 80%, var(--c-surface))",
  CRITICAL: "var(--c-bad)",
} as const;

const TRUTH_FILL: Record<string, string> = {
  clean: "var(--c-ok)",
  hiding: "var(--c-accent)",
  failed: "var(--c-bad)",
};

const VERSION_FILL: Record<string, string> = { V1: "var(--c-series-1)", V2: "var(--c-series-2)" };

function Section({ id, title, blurb, children }: { id: string; title: string; blurb: string; children: React.ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-h`} className="scroll-mt-4">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-3">
        <h2 id={`${id}-h`} className="text-base font-semibold text-ink">{title}</h2>
        <p className="text-xs text-muted">{blurb}</p>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">{children}</div>
    </section>
  );
}

/** Every chart drills down: a click on a mark opens the rows behind it (/analytics/breakdown). */
/** Series that play an entrance animation, and so replay it on every resize. */
const ANIMATED = new Set<unknown>([Area, Bar, Line, Pie, Scatter]);

/* Recharts replays a series' entrance animation from zero whenever the chart's
   size changes. Folding the sidebar or a row partner resizes every chart at
   once, and the whole page redrawing from empty reads as a reload. So a chart
   animates on mount only: the first size change after the one it mounted at
   switches its series' animation off, and resizes reflow it in place.

   Sizes that land while the page is still loading (a scrollbar appearing,
   a sibling resolving, sub-pixel layout) don't count: switching animation
   off mid-entrance snaps the chart to its end state, so on open it looked
   as if nothing had animated. Only a change after the entrance has had time
   to play, compared at whole pixels as Recharts lays out, settles it. */
const ENTRANCE_MS = 1600;

function Plot({ height = 240, children }: { height?: number; children: React.ReactElement<{ children?: React.ReactNode }> }) {
  const [settled, setSettled] = useState(false);
  const size = useRef<string | null>(null);
  const mounted = useRef<number | null>(null);

  const onResize = (width: number, h: number) => {
    const next = `${Math.round(width)}x${Math.round(h)}`;
    const now = performance.now();
    mounted.current ??= now;
    if (size.current !== null && size.current !== next && now - mounted.current > ENTRANCE_MS) setSettled(true);
    size.current = next;
  };

  const chart = settled
    ? cloneElement(children, undefined, Children.map(children.props.children, (child) =>
        isValidElement(child) && ANIMATED.has(child.type) ? cloneElement(child as React.ReactElement<{ isAnimationActive?: boolean }>, { isAnimationActive: false }) : child,
      ))
    : children;

  return (
    <div className="my-auto w-full min-w-0 cursor-pointer" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%" onResize={onResize}>{chart}</ResponsiveContainer>
    </div>
  );
}

function Caption({ children }: { children: React.ReactNode }) {
  return <p className="mt-3 text-xs leading-snug text-muted">{children}</p>;
}

/** The row under a chart-level click (Recharts reports the hovered category's index). */
function at<T>(rows: T[], state: unknown): T | undefined {
  const i = Number((state as { activeTooltipIndex?: unknown } | null)?.activeTooltipIndex);
  return Number.isInteger(i) ? rows[i] : undefined;
}

export function AnalyticsCharts({ data }: { data: FleetAnalytics }) {
  const router = useRouter();
  const go = (query: BreakdownQuery) => router.push(breakdownHref(query));
  const cliff = data.battery[0];
  const best = data.battery.reduce((a, b) => (b.rate > a.rate ? b : a), data.battery[0]);
  const v1 = data.drain.find((d) => d.version === "V1");
  const v2 = data.drain.find((d) => d.version === "V2");
  const hiding = data.truth.find((t) => t.key === "hiding")?.count ?? 0;
  const completed = data.truth.filter((t) => t.key !== "failed").reduce((s, t) => s + t.count, 0);
  const topMode = data.failureModes[0];
  const hottest = data.heatmap.rows[0];
  const worstEnv = data.environments[data.environments.length - 1];
  const failedBattery = avg(data.scatter.failed.map((p) => p.battery));
  const successBattery = avg(data.scatter.success.map((p) => p.battery));

  return (
    <div className="flex flex-col gap-8">
      <Section id="reliability" title="Reliability" blurb="Where SPOT's actions succeed, and the conditions that break them.">
        <Panel collapsible title="Success rate by battery band" aside="95% interval shaded">
          <Plot>
            <ComposedChart data={data.battery} margin={{ top: 8, right: 12, bottom: 0, left: -8 }}
              onClick={(s) => { const row = at(data.battery, s); if (row) go({ view: "actions", band: row.key }); }}>
              <CartesianGrid {...GRID} />
              <XAxis dataKey="band" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} />
              <YAxis domain={[0, 100]} unit="%" tick={AXIS} tickLine={false} axisLine={false} width={44} />
              <Tooltip
                {...TOOLTIP}
                cursor={{ stroke: "var(--c-line-strong)" }}
                formatter={(value, name) => (Array.isArray(value) ? [`${value[0]}–${value[1]}%`, "95% interval"] : [`${value}%`, name])}
                labelFormatter={(band, payload) => `Battery ${band} · n=${payload?.[0]?.payload?.n ?? 0}`}
              />
              <Area dataKey="ci" stroke="none" fill="var(--c-series-1)" fillOpacity={0.16} animationDuration={700} animationEasing="ease-out" name="95% interval" />
              <Line dataKey="rate" name="Success" stroke="var(--c-series-1)" strokeWidth={2} dot={{ r: 4, strokeWidth: 2, fill: "var(--c-surface)" }} animationBegin={250} animationDuration={1200} animationEasing="ease-in-out" />
            </ComposedChart>
          </Plot>
          {cliff && best && (
            <Caption>
              Below 25% battery only {cliff.rate}% of actions succeed (n={cliff.n}), against {best.rate}% in the {best.band} band — the cliff the triage rules weight.
            </Caption>
          )}
        </Panel>

        <Panel collapsible title="Success rate by environment">
          <Plot height={Math.max(180, data.environments.length * 40)}>
            <BarChart data={data.environments} layout="vertical" margin={{ top: 0, right: 40, bottom: 0, left: 8 }} barCategoryGap={8}
              onClick={(s) => { const row = at(data.environments, s); if (row) go({ view: "actions", env: row.key }); }}>
              <CartesianGrid stroke="var(--c-line)" horizontal={false} />
              <XAxis type="number" domain={[0, 100]} unit="%" tick={AXIS} tickLine={false} axisLine={false} />
              <YAxis type="category" dataKey="environment" tick={AXIS} tickLine={false} axisLine={false} width={96} />
              <Tooltip {...TOOLTIP} formatter={(value, _name, item) => [`${value}% · n=${item.payload.n}`, "Success"]} />
              <Bar dataKey="rate" name="Success" radius={[0, 4, 4, 0]} animationDuration={900} animationEasing="ease-out" shape={valueBar({ position: "right", fontSize: 11, format: (v) => `${v}%` })}>
                {data.environments.map((row) => (
                  <Cell key={row.environment} fill={row === worstEnv ? "var(--c-bad)" : "var(--c-series-1)"} />
                ))}
              </Bar>
            </BarChart>
          </Plot>
          {worstEnv && <Caption>{worstEnv.environment} waypoints are the weakest setting at {worstEnv.rate}% success across {worstEnv.n} actions.</Caption>}
        </Panel>
      </Section>

      <Section id="configuration" title="Configuration" blurb="V1 against V2, by action type and by the battery each mission spends.">
        <Panel collapsible title="V1 vs V2 success by action type">
          <Plot>
            <BarChart data={data.config} margin={{ top: 8, right: 12, bottom: 0, left: -8 }} barGap={2}>
              <CartesianGrid {...GRID} />
              <XAxis dataKey="type" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} />
              <YAxis domain={[0, 100]} unit="%" tick={AXIS} tickLine={false} axisLine={false} width={44} />
              <Tooltip
                {...TOOLTIP}
                formatter={(value, name, item) => [`${value ?? "—"}% · n=${item.payload[`n${String(name)}`]}`, name]}
              />
              <Bar dataKey="V1" fill="var(--c-series-1)" onClick={(_, i) => go({ view: "actions", type: data.config[i]?.key, version: "V1" })} radius={[4, 4, 0, 0]} maxBarSize={22} animationDuration={700} animationEasing="ease-out" />
              <Bar dataKey="V2" fill="var(--c-series-2)" onClick={(_, i) => go({ view: "actions", type: data.config[i]?.key, version: "V2" })} radius={[4, 4, 0, 0]} maxBarSize={22} animationBegin={350} animationDuration={700} animationEasing="ease-out" />
            </BarChart>
          </Plot>
          <SeriesKey items={[{ label: "V1", color: VERSION_FILL.V1 }, { label: "V2", color: VERSION_FILL.V2 }]} />
          <Caption>Per-type success on each configuration. Small groups swing hard — hover a bar for its sample size.</Caption>
        </Panel>

        <Panel collapsible title="Mean battery drain per mission">
          <Plot height={Math.max(160, data.drain.length * 56)}>
            <BarChart data={data.drain} layout="vertical" margin={{ top: 0, right: 48, bottom: 0, left: 8 }} barCategoryGap={14}
              onClick={(s) => { const row = at(data.drain, s); if (row) go({ view: "missions", version: row.version }); }}>
              <CartesianGrid stroke="var(--c-line)" horizontal={false} />
              <XAxis type="number" tickFormatter={(v: number) => `${v} pts`} tick={AXIS} tickLine={false} axisLine={false} />
              <YAxis type="category" dataKey="version" tick={AXIS} tickLine={false} axisLine={false} width={32} />
              <Tooltip {...TOOLTIP} formatter={(value, _name, item) => [`${value} pts · ${item.payload.missions} missions`, "Mean drain"]} />
              <Bar dataKey="drain" name="Mean drain" radius={[0, 4, 4, 0]} animationDuration={1300} animationEasing="ease-in-out" shape={valueBar({ position: "right", fontSize: 11, format: (v) => `${v} pts` })}>
                {data.drain.map((row) => <Cell key={row.version} fill={VERSION_FILL[row.version] ?? "var(--c-series-3)"} />)}
              </Bar>
            </BarChart>
          </Plot>
          {v1 && v2 && (
            <Caption>
              A V2 mission spends {v2.drain} battery points on average, against {v1.drain} on V1 ({v2.drain - v1.drain > 0 ? "+" : ""}{v2.drain - v1.drain} pts).
            </Caption>
          )}
        </Panel>
      </Section>

      <Section id="missions" title="Missions" blurb="Every mission in time order, and what its reported status was worth.">
        <Panel collapsible title="Action outcomes per mission" className="lg:col-span-2">
          <Plot height={260}>
            <BarChart data={data.missions} margin={{ top: 8, right: 12, bottom: 0, left: -16 }} barCategoryGap={3}
              onClick={(s) => { const row = at(data.missions, s); if (row) go({ view: "actions", mission: row.id }); }}>
              <CartesianGrid {...GRID} />
              <XAxis dataKey="id" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} interval="preserveStartEnd" minTickGap={8} />
              <YAxis allowDecimals={false} tick={AXIS} tickLine={false} axisLine={false} width={40} />
              <Tooltip {...TOOLTIP} labelFormatter={(id, payload) => `${id} · ${payload?.[0]?.payload?.version ?? ""} · reported ${payload?.[0]?.payload?.reported ?? ""}`} />
              <Bar dataKey="succeeded" name="Succeeded" stackId="a" fill="var(--c-ok)" animationDuration={650} animationEasing="ease-out" />
              <Bar dataKey="failed" name="Failed" stackId="a" fill="var(--c-bad)" radius={[4, 4, 0, 0]} animationBegin={650} animationDuration={450} animationEasing="ease-in" />
            </BarChart>
          </Plot>
          <SeriesKey items={[{ label: "Succeeded", color: "var(--c-ok)" }, { label: "Failed", color: "var(--c-bad)" }]} />
          <Caption>Stacked actions per mission. A red cap on a mission that reported COMPLETED is a failure the status field hid.</Caption>
        </Panel>

        <Panel collapsible title="Reported status vs reality">
          <div className="my-auto flex flex-col items-center justify-center gap-6 sm:flex-row">
            <div className="relative h-48 w-48 shrink-0">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Tooltip {...TOOLTIP} formatter={(value, name) => [`${value} missions`, name]} />
                  <Pie data={data.truth} onClick={(_, i) => go({ view: "missions", truth: data.truth[i]?.key })} className="cursor-pointer" dataKey="count" nameKey="label" innerRadius="62%" outerRadius="100%" paddingAngle={2} stroke="var(--c-surface)" strokeWidth={2} startAngle={90} endAngle={-270} animationBegin={100} animationDuration={1100} animationEasing="ease-out">
                    {data.truth.map((slice) => <Cell key={slice.key} fill={TRUTH_FILL[slice.key]} />)}
                  </Pie>
                </PieChart>
              </ResponsiveContainer>
              <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                <span className="nums font-mono text-2xl font-semibold text-ink">{data.missions.length}</span>
                <span className="text-[11px] text-muted">missions</span>
              </div>
            </div>
            <ul className="flex flex-col gap-2 text-sm">
              {data.truth.map((slice) => (
                <li key={slice.key}>
                  <Link href={breakdownHref({ view: "missions", truth: slice.key })} className="flex items-center gap-2 rounded hover:underline">
                    <span aria-hidden className="inline-block size-2.5 rounded-sm" style={{ background: TRUTH_FILL[slice.key] }} />
                    <span className="text-ink">{slice.label}</span>
                    <span className="nums ml-auto pl-4 font-mono text-muted">{slice.count}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </div>
          <Caption>{hiding} of {completed} missions that reported COMPLETED had at least one failed action inside.</Caption>
        </Panel>

        <Panel collapsible title="Battery spent per mission">
          <Plot height={220}>
            <BarChart data={data.missions} margin={{ top: 8, right: 12, bottom: 0, left: -8 }} barCategoryGap={3}
              onClick={(s) => { const row = at(data.missions, s); if (row) go({ view: "actions", mission: row.id }); }}>
              <CartesianGrid {...GRID} />
              <XAxis dataKey="id" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} interval="preserveStartEnd" minTickGap={8} />
              <YAxis domain={[0, 100]} unit="%" tick={AXIS} tickLine={false} axisLine={false} width={44} />
              <Tooltip {...TOOLTIP} formatter={(value, _name, item) => [Array.isArray(value) ? `${value[1]}% → ${value[0]}%` : String(value), `Battery (${item.payload.version})`]} />
              <Bar dataKey="battery" name="Battery start → end" radius={4} animationDuration={1000} animationEasing="ease-in-out">
                {data.missions.map((m) => <Cell key={m.id} fill={VERSION_FILL[m.version] ?? "var(--c-series-3)"} />)}
              </Bar>
            </BarChart>
          </Plot>
          <SeriesKey items={Object.entries(VERSION_FILL).map(([label, color]) => ({ label, color }))} />
          <Caption>Each bar spans start-of-mission battery down to where it finished. Bars that reach under 25% ended inside the cliff band.</Caption>
        </Panel>
      </Section>

      <Section id="failures" title="Failures & anomalies" blurb="What went wrong, how badly, and whether the measurement held up.">
        <Panel collapsible title="Failure modes">
          <Plot height={Math.max(180, data.failureModes.length * 36)}>
            <BarChart data={data.failureModes} layout="vertical" margin={{ top: 0, right: 32, bottom: 0, left: 8 }} barCategoryGap={8}
              onClick={(s) => { const row = at(data.failureModes, s); if (row) go({ view: "actions", failure: row.key }); }}>
              <CartesianGrid stroke="var(--c-line)" horizontal={false} />
              <XAxis type="number" allowDecimals={false} tick={AXIS} tickLine={false} axisLine={false} />
              <YAxis type="category" dataKey="mode" tick={AXIS} tickLine={false} axisLine={false} width={150} />
              <Tooltip {...TOOLTIP} formatter={(value) => [`${value} actions`, "Failed"]} />
              <Bar dataKey="count" name="Failed actions" fill="var(--c-series-1)" radius={[0, 4, 4, 0]} animationDuration={800} animationEasing="ease-out" shape={valueBar({ position: "right", fontSize: 11 })} />
            </BarChart>
          </Plot>
          {topMode && <Caption>{topMode.mode} is the leading cause, behind {topMode.count} failed actions.</Caption>}
        </Panel>

        <Panel collapsible title="Anomalies by type and severity">
          <Plot height={Math.max(200, data.anomalies.length * 34 + 40)}>
            <BarChart data={data.anomalies} layout="vertical" margin={{ top: 0, right: 16, bottom: 0, left: 8 }} barCategoryGap={8}>
              <CartesianGrid stroke="var(--c-line)" horizontal={false} />
              <XAxis type="number" allowDecimals={false} tick={AXIS} tickLine={false} axisLine={false} />
              <YAxis type="category" dataKey="type" tick={AXIS} tickLine={false} axisLine={false} width={160} />
              <Tooltip {...TOOLTIP} />
              {(["LOW", "MEDIUM", "HIGH", "CRITICAL"] as const).map((severity, i) => (
                <Bar
                  key={severity}
                  dataKey={severity}
                  onClick={(_, row) => go({ view: "anomalies", anomaly: data.anomalies[row]?.key, severity })}
                  name={severity.charAt(0) + severity.slice(1).toLowerCase()}
                  stackId="s"
                  fill={SEVERITY_FILL[severity]}
                  stroke="var(--c-surface)"
                  strokeWidth={1}
                  radius={i === 3 ? [0, 4, 4, 0] : 0}
                  animationBegin={i * 220}
                  animationDuration={420}
                  animationEasing="linear"
                />
              ))}
            </BarChart>
          </Plot>
          <SeriesKey items={(["LOW", "MEDIUM", "HIGH", "CRITICAL"] as const).map((severity) => ({ label: severity.charAt(0) + severity.slice(1).toLowerCase(), color: SEVERITY_FILL[severity] }))} />
          <Caption>Darker is more severe. High and critical findings are the ones the queue escalates first.</Caption>
        </Panel>

        <Panel collapsible title="Action duration vs battery">
          <Plot height={260}>
            <ScatterChart margin={{ top: 8, right: 12, bottom: 8, left: -8 }}>
              <CartesianGrid stroke="var(--c-line)" />
              <XAxis type="number" dataKey="battery" name="Battery" unit="%" domain={[0, 100]} tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} />
              <YAxis type="number" dataKey="duration" name="Duration" unit="s" tick={AXIS} tickLine={false} axisLine={false} width={48} />
              <ZAxis range={[36, 36]} />
              <Tooltip {...TOOLTIP} cursor={{ strokeDasharray: "3 3", stroke: "var(--c-line-strong)" }} />
              <Scatter name="Succeeded" data={data.scatter.success} onClick={(_, i) => { const p = data.scatter.success[i]; if (p) go({ view: "actions", mission: p.mission, highlight: p.id }); }} fill="var(--c-ok)" fillOpacity={0.55} shape="circle" animationDuration={700} animationEasing="ease-out" />
              <Scatter name="Failed" data={data.scatter.failed} onClick={(_, i) => { const p = data.scatter.failed[i]; if (p) go({ view: "actions", mission: p.mission, highlight: p.id }); }} fill="var(--c-bad)" shape="cross" animationBegin={500} animationDuration={600} animationEasing="ease-out" />
            </ScatterChart>
          </Plot>
          <SeriesKey items={[{ label: "Succeeded (●)", color: "var(--c-ok)" }, { label: "Failed (+)", color: "var(--c-bad)" }]} />
          <Caption>
            Every action as a point. Failed actions ran at {failedBattery}% battery on average, against {successBattery}% for successful ones; duration barely separates them.
          </Caption>
        </Panel>

        <Panel collapsible title="Reading quality by measurement">
          <Plot height={Math.max(180, data.readings.length * 34 + 40)}>
            <BarChart data={data.readings} layout="vertical" stackOffset="expand" margin={{ top: 0, right: 16, bottom: 0, left: 8 }} barCategoryGap={8}>
              <CartesianGrid stroke="var(--c-line)" horizontal={false} />
              <XAxis type="number" tickFormatter={(v: number) => `${Math.round(v * 100)}%`} tick={AXIS} tickLine={false} axisLine={false} />
              <YAxis type="category" dataKey="measurement" tick={AXIS} tickLine={false} axisLine={false} width={130} />
              <Tooltip {...TOOLTIP} formatter={(value, name) => [`${value} readings`, name]} />
              <Bar dataKey="good" name="Good" stackId="q" onClick={(_, i) => go({ view: "readings", measurement: data.readings[i]?.key, quality: "GOOD" })} fill="var(--c-ok)" stroke="var(--c-surface)" strokeWidth={1} animationDuration={700} animationEasing="ease-out" />
              <Bar dataKey="questionable" name="Questionable" stackId="q" onClick={(_, i) => go({ view: "readings", measurement: data.readings[i]?.key, quality: "QUESTIONABLE" })} fill="var(--c-accent)" stroke="var(--c-surface)" strokeWidth={1} radius={[0, 4, 4, 0]} animationBegin={700} animationDuration={500} animationEasing="ease-out" />
            </BarChart>
          </Plot>
          <SeriesKey items={[{ label: "Good", color: "var(--c-ok)" }, { label: "Questionable", color: "var(--c-accent)" }]} />
          <Caption>Share of each measurement the robot itself marked QUESTIONABLE — readings no one should quote as fact.</Caption>
        </Panel>
      </Section>

      <Section id="waypoints" title="Waypoints" blurb="Failure rate at every stop on the route, split by the kind of work done there.">
        <Panel collapsible title="Failure rate by waypoint and action type" className="lg:col-span-2">
          <Heatmap heatmap={data.heatmap} />
          {hottest && (
            <Caption>
              {hottest.name} ({hottest.environment.toLowerCase()}) fails {hottest.rate}% of its {hottest.n} actions — the worst stop on the route. Blank cells are pairings that never ran.
            </Caption>
          )}
        </Panel>
      </Section>
    </div>
  );
}

function avg(values: number[]) {
  return values.length ? Math.round(values.reduce((s, v) => s + v, 0) / values.length) : 0;
}

function SeriesKey({ items }: { items: { label: string; color: string }[] }) {
  return (
    <ul className="mt-2 flex justify-center gap-4 text-xs text-muted">
      {items.map((item) => (
        <li key={item.label} className="flex items-center gap-1.5">
          <span aria-hidden className="inline-block size-2.5 rounded-sm" style={{ background: item.color }} />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

/** Plain CSS grid: a heatmap is a table with colour, and Recharts has no
    native one. Colour is one hue whose strength is the failure rate, capped so
    ink stays readable on the hottest cell. */
function Heatmap({ heatmap }: { heatmap: FleetAnalytics["heatmap"] }) {
  const max = Math.max(1, ...heatmap.rows.flatMap((row) => row.cells.map((cell) => cell?.rate ?? 0)));
  const fill = (rate: number) => `color-mix(in oklab, var(--c-bad) ${Math.round(6 + (rate / max) * 64)}%, var(--c-surface))`;
  const columns = `minmax(9rem, 1.4fr) repeat(${heatmap.types.length}, minmax(3.5rem, 1fr)) minmax(3.5rem, 0.8fr)`;

  return (
    <div className="overflow-x-auto">
      <div role="table" aria-label="Failure rate by waypoint and action type" className="grid min-w-[36rem] gap-0.5 text-xs" style={{ gridTemplateColumns: columns }}>
        <div role="row" className="contents">
          <span role="columnheader" className="px-2 py-1 text-faint">Waypoint</span>
          {heatmap.types.map((type) => (
            <span key={type.key} role="columnheader" className="px-1 py-1 text-center text-faint">{type.label}</span>
          ))}
          <span role="columnheader" className="px-1 py-1 text-center font-medium text-faint">All</span>
        </div>
        {heatmap.rows.map((row, r) => (
          <div key={row.id} role="row" className="contents">
            <Link role="rowheader" href={breakdownHref({ view: "actions", waypoint: row.id })} className="truncate px-2 py-1.5 text-ink hover:underline" title={`${row.id} · ${row.environment}`}>
              {row.name}
            </Link>
            {row.cells.map((cell, i) => (
              <Link
                key={heatmap.types[i].key}
                href={breakdownHref({ view: "actions", waypoint: row.id, type: heatmap.types[i].key })}
                role="cell"
                title={cell ? `${row.name} · ${heatmap.types[i].label}: ${cell.rate}% failed, n=${cell.n}` : `${row.name} · ${heatmap.types[i].label}: no actions`}
                className="heat-cell nums rounded-sm px-1 py-1.5 text-center font-mono text-ink hover:outline hover:outline-1 hover:outline-line-strong"
                style={{ background: cell ? fill(cell.rate) : "transparent", animationDelay: `${(r + i) * 28}ms` }}
              >
                {cell ? `${Math.round(cell.rate)}%` : ""}
              </Link>
            ))}
            <Link role="cell" href={breakdownHref({ view: "actions", waypoint: row.id })} className="heat-cell nums rounded-sm px-1 py-1.5 text-center font-mono font-semibold text-ink hover:outline hover:outline-1 hover:outline-line-strong" style={{ background: fill(row.rate), animationDelay: `${(r + heatmap.types.length) * 28}ms` }} title={`${row.name}: ${row.rate}% failed, n=${row.n}`}>
              {Math.round(row.rate)}%
            </Link>
          </div>
        ))}
      </div>
      <div className="mt-3 flex items-center gap-2 text-[11px] text-faint">
        <span>0%</span>
        <span aria-hidden className="h-2 w-32 rounded-sm" style={{ background: `linear-gradient(to right, ${fill(0)}, ${fill(max)})` }} />
        <span>{Math.round(max)}% failed</span>
      </div>
    </div>
  );
}
