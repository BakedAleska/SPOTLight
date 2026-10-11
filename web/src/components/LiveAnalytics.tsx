"use client";

import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Panel } from "./Panel";
import type { LiveCapture, LiveRoute } from "@/lib/live";

/* What is happening on the patrol right now, beside the map: how far SPOT has
   got, what needs attention, the battery it has left (the triage rules weight
   the ≤ 25% cliff), and every finding so far. It replaces the knowledge-graph
   panel; model telemetry is not shown here, the operator cares about the plant. */

const AXIS = { fill: "var(--c-muted)", fontSize: 11 };
const TOOLTIP = {
  contentStyle: { background: "var(--c-surface)", border: "1px solid var(--c-line)", borderRadius: 4, color: "var(--c-ink)", fontSize: 12 },
  labelStyle: { color: "var(--c-ink)", fontWeight: 500 },
  itemStyle: { color: "var(--c-ink)" },
};
const human = (s?: string | null) => (s ?? "").replaceAll("_", " ").toLowerCase();

export interface LiveTally {
  done: number; total: number; red: number; green: number; battery: number | null;
  findings: { stop: string; text: string; tone: "bad" | "warn" }[];
}

/** The numbers the panel shows, from the captures so far (pure; tested). */
export function liveTally(captures: LiveCapture[], route: LiveRoute | null): LiveTally {
  const triaged = captures.filter((c) => c.verdict);
  const findings: LiveTally["findings"] = [];
  for (const c of captures) {
    if (c.status === "FAILED") findings.push({ stop: c.stop, text: `${human(c.action_type)} failed: ${human(c.failure_type) || "unknown"}`, tone: "bad" });
    if (c.anomaly?.anomaly_type) {
      const serious = c.anomaly.severity === "HIGH" || c.anomaly.severity === "CRITICAL";
      findings.push({ stop: c.stop, text: `${human(c.anomaly.anomaly_type)} (${c.anomaly.severity ?? "?"}) on ${c.asset_id}`, tone: serious ? "bad" : "warn" });
    }
    if (c.reading?.quality === "QUESTIONABLE") findings.push({ stop: c.stop, text: `questionable ${human(c.reading.measurement_type)} reading`, tone: "warn" });
  }
  return {
    done: triaged.length,
    total: route?.stops.length ?? captures.length,
    red: triaged.filter((c) => c.verdict === "red").length,
    green: triaged.filter((c) => c.verdict === "green").length,
    battery: captures.at(-1)?.battery_pct ?? null,
    findings,
  };
}

function Tile({ label, value, tone }: { label: string; value: string; tone?: "bad" | "ok" | "warn" }) {
  return (
    <div className="rounded border border-line bg-surface-2 px-3 py-2">
      <p className="text-[11px] text-muted">{label}</p>
      <p className={`nums font-mono text-lg font-semibold ${tone === "bad" ? "text-bad" : tone === "ok" ? "text-ok" : tone === "warn" ? "text-warn" : "text-ink"}`}>{value}</p>
    </div>
  );
}

export function LiveAnalytics({ className, captures, route }: { className?: string; captures: LiveCapture[]; route: LiveRoute | null }) {
  const t = liveTally(captures, route);
  const points = captures.map((c) => ({ stop: c.stop, battery: c.battery_pct, verdict: c.verdict }));
  return (
    <Panel title="Live analytics" className={className} aside={t.total ? `${t.done} of ${t.total} stops` : undefined}>
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-2">
          <Tile label="Stops triaged" value={`${t.done}/${t.total || "—"}`} />
          <Tile label="Battery now" value={t.battery == null ? "—" : `${t.battery}%`} tone={t.battery != null && t.battery <= 25 ? "bad" : undefined} />
          <Tile label="Needs attention" value={String(t.red)} tone={t.red ? "bad" : undefined} />
          <Tile label="All good" value={String(t.green)} tone={t.green ? "ok" : undefined} />
        </div>

        <div>
          <p className="mb-1 text-xs font-medium text-ink">Battery along the route</p>
          {points.length < 2 ? <p className="flex h-36 items-center justify-center text-center text-xs text-muted">No readings yet.</p> : (
            <div className="h-36 w-full min-w-0">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={points} margin={{ top: 6, right: 8, bottom: 0, left: -18 }}>
                  <CartesianGrid stroke="var(--c-line)" vertical={false} />
                  <XAxis dataKey="stop" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} />
                  <YAxis domain={[0, 100]} unit="%" tick={AXIS} tickLine={false} axisLine={false} width={44} />
                  <ReferenceLine y={25} stroke="var(--c-bad)" strokeDasharray="4 3" />
                  <Tooltip {...TOOLTIP} labelFormatter={(stop) => `Stop ${stop}`} formatter={(v, _n, item) => [`${v}% · ${item.payload.verdict === "red" ? "needs attention" : item.payload.verdict === "green" ? "all good" : "triaging"}`, "Battery"]} />
                  <Line dataKey="battery" stroke="var(--c-series-1)" strokeWidth={2} isAnimationActive={false}
                    dot={(p: { cx?: number; cy?: number; index?: number; payload?: { verdict: string | null } }) => (
                      <circle key={p.index} cx={p.cx} cy={p.cy} r={4} stroke="var(--c-surface)" strokeWidth={1.5}
                        fill={p.payload?.verdict === "red" ? "var(--c-bad)" : p.payload?.verdict === "green" ? "var(--c-ok)" : "var(--c-line-strong)"} />
                    )} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
          <p className="mt-1 text-[11px] text-muted">Dashed: 25% cliff</p>
        </div>

        <div>
          <p className="mb-1 text-xs font-medium text-ink">Findings so far</p>
          {t.findings.length === 0 ? <p className="text-xs text-muted">{captures.length ? "Nothing abnormal yet." : "None yet."}</p> : (
            <ul className="flex max-h-48 flex-col gap-1 overflow-y-auto text-xs">
              {[...t.findings].reverse().map((f, i) => (
                <li key={`${f.stop}-${i}`} className="flex gap-2">
                  <span className="nums w-5 shrink-0 font-mono font-semibold text-ink">{f.stop}</span>
                  <span className={f.tone === "bad" ? "text-bad" : "text-warn"}>{f.text}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Panel>
  );
}
