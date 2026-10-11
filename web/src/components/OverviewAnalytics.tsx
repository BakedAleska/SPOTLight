"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ErrorBar,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Panel } from "./Panel";
import type { Flag } from "@/lib/types";
import { breakdownHref, type BreakdownQuery } from "@/lib/breakdown-link";
import {
  activeChart,
  bandOf,
  bandsSpanned,
  DEFAULT_CHART,
  failuresFor,
  OVERVIEW_CHARTS,
  parseChart,
  typesFor,
  type OverviewAnalyticsData,
  type OverviewChart,
} from "@/lib/overview";

/* The Overview's analytics, beside the queue: four of the Analytics page's
   charts behind a toggle, so the fleet picture is one click from the list it
   explains. With a flag open the panel turns to that flag — it says so, the
   flag's mission is marked on the Missions chart, and the other charts set
   the mission's own numbers (in the accent, like the open row) against the
   fleet's.

   Same conventions as AnalyticsCharts: status tokens (ok/bad) only where a
   mark means success or failure, the series token for the fleet, solid
   hairline grids, a tooltip on every mark, a legend whenever there are two
   series. Every mark drills down to the rows behind it
   (/analytics/breakdown). */

const STORE = "spotlight:overview-chart";

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
const VALUE_LABEL = { fill: "var(--c-muted)", fontSize: 10 };

/** Marks outside the open flag's mission step back so its own read first. */
const DIM = 0.28;
const FADE = "transition-[fill-opacity] duration-300";

const PILL = "h-7 rounded px-2.5 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-accent";
const PILL_ON = `${PILL} bg-surface-2 font-semibold text-ink`;
const PILL_OFF = `${PILL} text-muted hover:text-ink`;

const percent = (value: unknown) => (value == null ? "" : `${Math.round(Number(value))}%`);

/** The row under a chart-level click (Recharts reports the hovered category's index). */
function at<T>(rows: T[], state: unknown): T | undefined {
  const i = Number((state as { activeTooltipIndex?: unknown } | null)?.activeTooltipIndex);
  return Number.isInteger(i) ? rows[i] : undefined;
}

type Mission = OverviewAnalyticsData["missions"][number];

export function OverviewAnalytics({
  data,
  flag,
  delay,
}: {
  data: OverviewAnalyticsData;
  /** The open queue row, if any: the panel becomes context for it. */
  flag: Flag | null;
  delay?: number;
}) {
  const router = useRouter();
  const go = (query: BreakdownQuery) => router.push(breakdownHref(query));
  const [saved, setSaved] = useState<OverviewChart>(DEFAULT_CHART);
  const [pickedWhileOpen, setPickedWhileOpen] = useState(false);

  // The last pick is a per-viewer convenience: a blocked or empty store just
  // means the default chart.
  useEffect(() => {
    try {
      const stored = parseChart(window.localStorage.getItem(STORE));
      // eslint-disable-next-line react-hooks/set-state-in-effect -- localStorage is only readable after hydration
      if (stored) setSaved(stored);
    } catch {
      /* storage unavailable */
    }
  }, []);

  function pick(next: OverviewChart) {
    setSaved(next);
    if (flag) setPickedWhileOpen(true);
    try {
      window.localStorage.setItem(STORE, next);
    } catch {
      /* storage unavailable */
    }
  }

  const missionId = flag?.missionId && flag.missionId !== "Unassigned" ? flag.missionId : null;
  const mission = missionId ? data.missions.find((m) => m.id === missionId) ?? null : null;
  const chart = activeChart(saved, !!flag, pickedWhileOpen);
  const section = OVERVIEW_CHARTS.find((c) => c.key === chart)?.section ?? "reliability";

  const view =
    chart === "types" ? <TypesChart data={data} mission={mission} go={go} />
    : chart === "battery" ? <BatteryChart data={data} mission={mission} go={go} />
    : chart === "missions" ? <MissionsChart data={data} mission={mission} go={go} />
    : <FailuresChart data={data} mission={mission} go={go} />;

  return (
    <Panel
      title="Fleet analytics"
      aside={mission ? <span className="text-accent-ink">Mission {mission.id} marked</span> : "Challenge kit"}
      className="h-full"
      bodyClassName="gap-3 p-4"
      delay={delay}
    >
      {/* One row of toggles at any width: a narrow panel gets the short labels. */}
      <div className="@container">
        <div role="group" aria-label="Chart" className="flex w-fit max-w-full items-center gap-0.5 overflow-x-auto rounded border border-line bg-surface p-0.5">
          {OVERVIEW_CHARTS.map((c) => (
            <button key={c.key} type="button" aria-pressed={chart === c.key} aria-label={c.label} onClick={() => pick(c.key)} className={chart === c.key ? PILL_ON : PILL_OFF}>
              <span className="whitespace-nowrap @sm:hidden">{c.short}</span>
              <span className="hidden whitespace-nowrap @sm:inline">{c.label}</span>
            </button>
          ))}
        </div>
      </div>

      {flag && (
        <div key={flag.id} className="anim-rise rounded border-l-2 border-accent bg-accent/10 px-3 py-2 text-xs leading-snug text-muted" style={{ "--rise": "6px" } as React.CSSProperties}>
          <p className="truncate">
            Context for <span className="font-medium text-ink" title={flag.title}>{flag.title}</span>
          </p>
          {mission ? (
            <p className="mt-0.5">
              Mission <span className="font-mono text-ink">{mission.id}</span> · {mission.version} · {mission.succeeded} of {mission.succeeded + mission.failed} actions succeeded · reported {mission.reported.toLowerCase()} ·{" "}
              <Link href={breakdownHref({ view: "actions", mission: mission.id })} className="font-medium text-accent-ink hover:underline">
                its actions
              </Link>
            </p>
          ) : (
            <p className="mt-0.5">This flag has no mission in the challenge kit, so the charts show the fleet only.</p>
          )}
        </div>
      )}

      {/* Keyed by chart, so each one plays its entrance when it is picked. */}
      <div key={chart} className="flex min-h-0 flex-1 flex-col">{view}</div>

      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-faint">
        <span>Click a bar to see the actions behind it.</span>
        <Link href={`/analytics#${section}`} className="ml-auto inline-flex items-center gap-1 text-muted hover:text-ink hover:underline">
          More on Analytics <ArrowRight size={12} aria-hidden="true" />
        </Link>
      </p>
    </Panel>
  );
}

type ChartProps = { data: OverviewAnalyticsData; mission: Mission | null; go: (query: BreakdownQuery) => void };

/** Fills whatever height the panel has left (it is stretched to the queue's),
    never under 14rem, which is also its height when the two stack. */
function Plot({ label, children }: { label: string; children: React.ReactElement }) {
  return (
    <figure aria-label={label} className="relative min-h-56 flex-1 cursor-pointer">
      <div className="absolute inset-0">
        <ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer>
      </div>
    </figure>
  );
}

function Caption({ children }: { children: React.ReactNode }) {
  return <p className="mt-2 text-xs leading-snug text-muted">{children}</p>;
}

function SeriesKey({ items }: { items: { label: string; color: string; dashed?: boolean }[] }) {
  return (
    <ul className="mt-2 flex flex-wrap justify-center gap-x-4 gap-y-1 text-xs text-muted">
      {items.map((item) => (
        <li key={item.label} className="flex items-center gap-1.5">
          {item.dashed
            ? <span aria-hidden className="inline-block w-3.5 border-t border-dashed" style={{ borderColor: item.color }} />
            : <span aria-hidden className="inline-block size-2.5 rounded-sm" style={{ background: item.color }} />}
          {item.label}
        </li>
      ))}
    </ul>
  );
}

/* Rows are memoised: the dashboard re-renders on every 2.5s poll, and Recharts
   replays a bar's animation (hiding its value labels) whenever its data array
   is a new object, even an identical one. */

function TypesChart({ data, mission, go }: ChartProps) {
  const id = mission?.id ?? null;
  const rows = useMemo(() => typesFor(data, id), [data, id]);
  const weakest = rows[rows.length - 1];
  // The weakest type is called out only on the fleet view, and only when it
  // is a real gap; beside a mission's bars a red one would read as the mission.
  const flagged = !mission && weakest && data.fleetRate - weakest.rate > 1 ? weakest.key : null;
  const missionRuns = rows.filter((row) => row.mission !== null);
  const missionWorst = missionRuns.reduce<(typeof rows)[number] | undefined>((a, b) => (!a || (b.mission ?? 0) < (a.mission ?? 0) ? b : a), undefined);

  return (
    <>
      <Plot label="Action success rate by inspection type">
        <BarChart data={rows} margin={{ top: 16, right: 8, bottom: 0, left: -8 }} barGap={2}>
          <CartesianGrid {...GRID} />
          <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} interval={0} />
          <YAxis domain={[0, 100]} unit="%" tick={AXIS} tickLine={false} axisLine={false} width={44} />
          <Tooltip
            {...TOOLTIP}
            formatter={(value, name, item) => [
              `${value}% · n=${item.dataKey === "mission" ? item.payload.missionN : item.payload.n}`,
              name,
            ]}
          />
          <ReferenceLine y={data.fleetRate} stroke="var(--c-line-strong)" strokeDasharray="4 3" />
          <Bar dataKey="rate" name="Fleet" radius={[4, 4, 0, 0]} maxBarSize={32} animationDuration={700} animationEasing="ease-out"
            onClick={(_, i) => go({ view: "actions", type: rows[i]?.key })}
            label={mission ? undefined : { position: "top", ...VALUE_LABEL, formatter: percent }}>
            {rows.map((row) => <Cell key={row.key} fill={row.key === flagged ? "var(--c-bad)" : "var(--c-series-1)"} />)}
          </Bar>
          {mission && (
            <Bar dataKey="mission" name={`Mission ${mission.id}`} fill="var(--c-accent)" radius={[4, 4, 0, 0]} maxBarSize={32}
              animationBegin={250} animationDuration={700} animationEasing="ease-out"
              onClick={(_, i) => go({ view: "actions", type: rows[i]?.key, mission: mission.id })}
              label={{ position: "top", ...VALUE_LABEL, formatter: percent }} />
          )}
        </BarChart>
      </Plot>
      <SeriesKey items={[
        ...(mission ? [{ label: "Fleet", color: "var(--c-series-1)" }, { label: `Mission ${mission.id}`, color: "var(--c-accent)" }] : []),
        { label: `Fleet average ${Math.round(data.fleetRate)}%`, color: "var(--c-line-strong)", dashed: true },
      ]} />
      <Caption>
        {mission && missionWorst
          ? `In ${mission.id}, ${missionWorst.label.toLowerCase()} actions did worst: ${Math.round(missionWorst.mission ?? 0)}% of ${missionWorst.missionN} succeeded, against ${Math.round(missionWorst.rate)}% across the fleet.`
          : weakest && `${weakest.label} actions succeed ${Math.round(weakest.rate)}% of the time (n=${weakest.n}), against ${Math.round(data.fleetRate)}% for the fleet — the type to look at first.`}
      </Caption>
    </>
  );
}

function BatteryChart({ data, mission, go }: ChartProps) {
  const rows = useMemo(
    () => data.battery.map((row) => ({ ...row, err: [Math.round((row.rate - row.ci[0]) * 10) / 10, Math.round((row.ci[1] - row.rate) * 10) / 10] })),
    [data],
  );
  const cliff = rows[0];
  const best = rows.reduce((a, b) => (b.rate > a.rate ? b : a), rows[0]);
  const spanned = mission ? new Set(bandsSpanned(mission.battery)) : null;
  const ended = mission ? bandOf(mission.battery[0]) : null;
  const endedRow = ended ? rows.find((row) => row.key === ended.key) : undefined;

  return (
    <>
      <Plot label="Action success rate by battery band">
        <BarChart data={rows} margin={{ top: 16, right: 8, bottom: 0, left: -8 }}>
          <CartesianGrid {...GRID} />
          <XAxis dataKey="band" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} interval={0} />
          <YAxis domain={[0, 100]} unit="%" tick={AXIS} tickLine={false} axisLine={false} width={44} />
          {endedRow && (
            <ReferenceArea x1={endedRow.band} x2={endedRow.band} fill="var(--c-accent)" fillOpacity={0.12} stroke="var(--c-accent)" strokeOpacity={0.6}
              label={{ value: `${mission?.id} ended here`, position: "insideTop", fill: "var(--c-accent-ink)", fontSize: 10 }} />
          )}
          <Tooltip
            {...TOOLTIP}
            formatter={(value, name, item) => [`${value}% · n=${item.payload.n} · 95% interval ${item.payload.ci[0]}–${item.payload.ci[1]}%`, name]}
            labelFormatter={(band) => `Battery ${band}`}
          />
          <Bar dataKey="rate" name="Success" radius={[4, 4, 0, 0]} maxBarSize={44} animationDuration={800} animationEasing="ease-out"
            onClick={(_, i) => go({ view: "actions", band: rows[i]?.key })}
            label={{ position: "top", ...VALUE_LABEL, formatter: percent, offset: 10 }}>
            {rows.map((row) => (
              <Cell key={row.key} className={FADE}
                fill={row === cliff && best.rate - cliff.rate > 10 ? "var(--c-bad)" : "var(--c-series-1)"}
                fillOpacity={spanned && !spanned.has(row.key) ? DIM : 1} />
            ))}
            <ErrorBar dataKey="err" width={5} strokeWidth={1.5} stroke="var(--c-ink)" strokeOpacity={0.45} direction="y" />
          </Bar>
        </BarChart>
      </Plot>
      <Caption>
        {mission && ended && endedRow
          ? `${mission.id} ran from ${mission.battery[1]}% down to ${mission.battery[0]}% battery and finished in the ${ended.band} band, where ${Math.round(endedRow.rate)}% of actions succeed. Bands it never reached are faded.`
          : cliff && best && `Below 25% battery only ${Math.round(cliff.rate)}% of actions succeed (n=${cliff.n}), against ${Math.round(best.rate)}% in the ${best.band} band. Whiskers are the 95% interval.`}
      </Caption>
    </>
  );
}

function MissionsChart({ data, mission, go }: ChartProps) {
  const rows = data.missions;
  const dim = (row: Mission) => (mission && row.id !== mission.id ? DIM : 1);
  const completed = rows.filter((row) => row.reported === "COMPLETED");
  const hiding = completed.filter((row) => row.failed > 0).length;

  return (
    <>
      <Plot label="Action outcomes per mission">
        <BarChart data={rows} margin={{ top: 18, right: 8, bottom: 0, left: -16 }} barCategoryGap={2}
          onClick={(state) => { const row = at(rows, state); if (row) go({ view: "actions", mission: row.id }); }}>
          <CartesianGrid {...GRID} />
          <XAxis dataKey="id" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} interval="preserveStartEnd" minTickGap={8} />
          <YAxis allowDecimals={false} tick={AXIS} tickLine={false} axisLine={false} width={40} />
          {mission && (
            <ReferenceArea x1={mission.id} x2={mission.id} fill="var(--c-accent)" fillOpacity={0.14} stroke="var(--c-accent)" strokeOpacity={0.7}
              ifOverflow="visible" label={{ value: mission.id, position: "top", fill: "var(--c-accent-ink)", fontSize: 11, fontWeight: 600 }} />
          )}
          <Tooltip {...TOOLTIP} labelFormatter={(id, payload) => `${id} · ${payload?.[0]?.payload?.version ?? ""} · reported ${String(payload?.[0]?.payload?.reported ?? "").toLowerCase()}`} />
          <Bar dataKey="succeeded" name="Succeeded" stackId="a" fill="var(--c-ok)" animationDuration={650} animationEasing="ease-out">
            {rows.map((row) => <Cell key={row.id} className={FADE} fillOpacity={dim(row)} />)}
          </Bar>
          <Bar dataKey="failed" name="Failed" stackId="a" fill="var(--c-bad)" radius={[4, 4, 0, 0]} animationBegin={650} animationDuration={450} animationEasing="ease-in">
            {rows.map((row) => <Cell key={row.id} className={FADE} fillOpacity={dim(row)} />)}
          </Bar>
        </BarChart>
      </Plot>
      <SeriesKey items={[{ label: "Succeeded", color: "var(--c-ok)" }, { label: "Failed", color: "var(--c-bad)" }, ...(mission ? [{ label: `Mission ${mission.id}`, color: "var(--c-accent)" }] : [])]} />
      <Caption>
        {mission
          ? mission.reported !== "COMPLETED"
            ? `${mission.id} reported ${mission.reported}: ${mission.failed} of its ${mission.succeeded + mission.failed} actions failed.`
            : mission.failed > 0
              ? `${mission.id} reported COMPLETED, yet ${mission.failed} of its ${mission.succeeded + mission.failed} actions failed — one of ${hiding} missions whose status hid a failure.`
              : `${mission.id} reported COMPLETED and all ${mission.succeeded} of its actions succeeded.`
          : `Actions per mission, in time order. ${hiding} of ${completed.length} missions that reported COMPLETED had a failed action inside (the red caps).`}
      </Caption>
    </>
  );
}

function FailuresChart({ data, mission, go }: ChartProps) {
  const id = mission?.id ?? null;
  const rows = useMemo(() => failuresFor(data, id), [data, id]);
  const own = mission ? rows.filter((row) => (row.mission ?? 0) > 0) : [];
  const top = rows[0];

  return (
    <>
      <Plot label="Failed actions by failure mode">
        <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 28, bottom: 0, left: 4 }} barCategoryGap={mission ? 5 : 7} barGap={2}>
          <CartesianGrid stroke="var(--c-line)" horizontal={false} />
          <XAxis type="number" allowDecimals={false} tick={AXIS} tickLine={false} axisLine={false} />
          <YAxis type="category" dataKey="mode" tick={AXIS} tickLine={false} axisLine={false} width={128} interval={0} />
          <Tooltip {...TOOLTIP} formatter={(value, name) => [`${value} failed action${value === 1 ? "" : "s"}`, name]} />
          <Bar dataKey="count" name="Fleet" fill="var(--c-series-1)" radius={[0, 4, 4, 0]} animationDuration={800} animationEasing="ease-out"
            onClick={(_, i) => go({ view: "actions", failure: rows[i]?.key })}
            label={{ position: "right", ...VALUE_LABEL }} />
          {mission && (
            <Bar dataKey="mission" name={`Mission ${mission.id}`} fill="var(--c-accent)" radius={[0, 4, 4, 0]} animationBegin={250} animationDuration={700} animationEasing="ease-out"
              onClick={(_, i) => go({ view: "actions", failure: rows[i]?.key, mission: mission.id })}
              label={{ position: "right", ...VALUE_LABEL, formatter: (v: unknown) => (Number(v) > 0 ? String(v) : "") }} />
          )}
        </BarChart>
      </Plot>
      {mission && <SeriesKey items={[{ label: "Fleet", color: "var(--c-series-1)" }, { label: `Mission ${mission.id}`, color: "var(--c-accent)" }]} />}
      <Caption>
        {mission
          ? own.length
            ? `${mission.id}'s failed actions: ${own.map((row) => `${row.mode.toLowerCase()} (${row.mission})`).join(", ")}.`
            : `No action failed in ${mission.id}; this flag comes from what the robot saw or read, not from a failed action.`
          : top && `${top.mode} is the leading cause, behind ${top.count} failed actions.`}
      </Caption>
    </>
  );
}
