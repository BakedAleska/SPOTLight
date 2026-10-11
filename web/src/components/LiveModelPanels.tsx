"use client";

import useSWR from "swr";
import { TriangleAlert } from "lucide-react";
import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Panel } from "./Panel";
import type { LiveCapture, LiveRoute } from "@/lib/live";
import type { ModelStream } from "@/lib/model-stream";

/* The model telemetry that used to live on Analytics as the "Live model
   results" board, folded into Live patrol where the runs actually happen.

   The capture cards already show each stop's Laya and Qwen answers in full, so
   what this adds is the view across them: how Laya's escalation probability
   moved over the run, where the time went at each stop, and — across every
   run — how each model is holding up (calls, errors, p95, throughput). */

const AXIS = { fill: "var(--c-muted)", fontSize: 11 };
const TOOLTIP = {
  contentStyle: { background: "var(--c-surface)", border: "1px solid var(--c-line)", borderRadius: 4, color: "var(--c-ink)", fontSize: 12 },
  labelStyle: { color: "var(--c-ink)", fontWeight: 500 },
  itemStyle: { color: "var(--c-ink)" },
  cursor: { fill: "var(--c-surface-2)" },
};

/** One hue per pipeline stage, in the validated series order. */
const STAGES = [
  { key: "Laya", color: "var(--c-series-1)" },
  { key: "Vision", color: "var(--c-series-2)" },
  { key: "Qwen", color: "var(--c-series-3)" },
] as const;

function Key({ items }: { items: { label: string; color: string }[] }) {
  return (
    <ul className="mt-2 flex flex-wrap justify-center gap-4 text-xs text-muted">
      {items.map((item) => (
        <li key={item.label} className="flex items-center gap-1.5">
          <span aria-hidden className="inline-block size-2.5 rounded-sm" style={{ background: item.color }} />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="flex min-h-28 items-center justify-center text-center text-sm text-muted">{children}</p>;
}

/** Laya's escalation probability and per-stage latency for the selected run. */
export function RunModelCharts({ captures, route }: { captures: LiveCapture[]; route: LiveRoute | null }) {
  const stops = route?.stops.map((s) => s.stop) ?? [...new Set(captures.map((c) => c.stop))];
  const byStop = new Map(captures.map((c) => [c.stop, c]));

  const risk = stops.map((stop) => {
    const c = byStop.get(stop);
    const p = c?.laya?.verdict?.p_escalate;
    return { stop, p: typeof p === "number" ? Math.round(p * 1000) / 10 : null, verdict: c?.verdict ?? null };
  });
  const scored = risk.filter((r) => r.p !== null);

  const latency = stops.map((stop) => {
    const c = byStop.get(stop);
    const s = (v?: number) => (typeof v === "number" ? Math.round(v / 100) / 10 : null);
    return { stop, Laya: s(c?.laya?.latency_ms), Vision: s(c?.vision?.latency_ms), Qwen: s(c?.qwen?.latency_ms) };
  });
  const timed = latency.filter((l) => l.Laya !== null || l.Vision !== null || l.Qwen !== null);
  const total = (l: (typeof latency)[number]) => (l.Laya ?? 0) + (l.Vision ?? 0) + (l.Qwen ?? 0);
  const slowest = timed.reduce<(typeof latency)[number] | null>((a, b) => (!a || total(b) > total(a) ? b : a), null);

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Panel collapsible title="Laya escalation probability by stop" aside={stops.length ? `${scored.length} of ${stops.length} stops scored` : undefined}>
        {scored.length === 0 ? <Empty>Laya&apos;s probability for each stop appears here as the run triages it.</Empty> : <>
          <div className="my-auto h-56 w-full min-w-0">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={risk} margin={{ top: 8, right: 12, bottom: 0, left: -8 }} barCategoryGap="25%">
                <CartesianGrid stroke="var(--c-line)" vertical={false} />
                <XAxis dataKey="stop" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} />
                <YAxis domain={[0, 100]} unit="%" tick={AXIS} tickLine={false} axisLine={false} width={44} />
                <Tooltip {...TOOLTIP} labelFormatter={(stop) => `Stop ${stop}`} formatter={(value, _n, item) => [value == null ? "not scored" : `${value}%`, `p(escalate) · ${item.payload.verdict ?? "triaging"}`]} />
                <Bar dataKey="p" name="p(escalate)" radius={[4, 4, 0, 0]} maxBarSize={36} isAnimationActive={false}>
                  {risk.map((r) => <Cell key={r.stop} fill={r.verdict === "red" ? "var(--c-bad)" : r.verdict === "green" ? "var(--c-ok)" : "var(--c-line-strong)"} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <Key items={[{ label: "Needs attention", color: "var(--c-bad)" }, { label: "All good", color: "var(--c-ok)" }, { label: "Triaging", color: "var(--c-line-strong)" }]} />
          <p className="mt-3 text-xs text-muted">Bar height is Laya&apos;s probability; colour is the final verdict after the rules ran alongside it, so a short red bar is the rules overruling the model.</p>
        </>}
      </Panel>

      <Panel collapsible title="Model latency by stop" aside="seconds, stacked">
        {timed.length === 0 ? <Empty>Time spent in Laya, the vision reader and Qwen at each stop appears here.</Empty> : <>
          <div className="my-auto h-56 w-full min-w-0">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={latency} margin={{ top: 8, right: 12, bottom: 0, left: -8 }} barCategoryGap="25%">
                <CartesianGrid stroke="var(--c-line)" vertical={false} />
                <XAxis dataKey="stop" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} />
                <YAxis unit="s" tick={AXIS} tickLine={false} axisLine={false} width={44} />
                <Tooltip {...TOOLTIP} labelFormatter={(stop) => `Stop ${stop}`} formatter={(value, name) => [value == null ? "—" : `${value} s`, name]} />
                {STAGES.map((stage, i) => (
                  <Bar key={stage.key} dataKey={stage.key} stackId="t" fill={stage.color} stroke="var(--c-surface)" strokeWidth={1} maxBarSize={36} radius={i === STAGES.length - 1 ? [4, 4, 0, 0] : 0} isAnimationActive={false} />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
          <Key items={STAGES.map((s) => ({ label: s.key, color: s.color }))} />
          {slowest && <p className="mt-3 text-xs text-muted">Stop {slowest.stop} took longest at {total(slowest).toFixed(1)} s across the three models.</p>}
        </>}
      </Panel>
    </div>
  );
}

async function fetchStream(url: string): Promise<ModelStream> {
  const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(20_000) });
  if (!response.ok) throw new Error("Model backend unavailable.");
  return response.json();
}

const n = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Model-call health across every run: the backend's `app.model_calls`. */
export function ModelCallsPanel() {
  const { data, error } = useSWR<ModelStream>("/api/model-stream", fetchStream, {
    refreshInterval: 5000, dedupingInterval: 2500, revalidateOnFocus: true, shouldRetryOnError: false,
  });
  const calls = [...(data?.calls ?? [])].reverse();
  const systems = [...new Set(calls.map((c) => String(c.system)))].slice(0, 3);
  const graph = calls.map((c, i) => ({ index: i + 1, label: `${c.system} · ${c.model} · ${c.ts}`, [String(c.system)]: n(c.latency_ms) }));
  const stats = data?.stats ?? [];

  return (
    <Panel collapsible title="Model calls · all runs" aside={error && !data ? "backend unreachable" : `${calls.length} recent calls`}>
      <div className="grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="min-w-0">
          {graph.length === 0 ? <Empty>Waiting for model-call telemetry.</Empty> : <>
            <div className="h-56 w-full min-w-0">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={graph} margin={{ top: 8, right: 12, bottom: 0, left: -4 }}>
                  <CartesianGrid stroke="var(--c-line)" vertical={false} />
                  <XAxis dataKey="index" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--c-line)" }} />
                  <YAxis unit="ms" tick={AXIS} tickLine={false} axisLine={false} width={60} />
                  <Tooltip {...TOOLTIP} cursor={{ stroke: "var(--c-line-strong)" }} labelFormatter={(_, payload) => payload?.[0]?.payload?.label ?? ""} />
                  {systems.map((system, i) => <Line key={system} name={system} dataKey={system} connectNulls stroke={STAGES[i].color} strokeWidth={2} dot={{ r: 2 }} isAnimationActive={false} />)}
                </LineChart>
              </ResponsiveContainer>
            </div>
            <Key items={systems.map((s, i) => ({ label: s, color: STAGES[i].color }))} />
          </>}
        </div>
        <div className="min-w-0 overflow-x-auto">
          {stats.length === 0 ? <Empty>Per-model stats appear once the backend reports model calls.</Empty> :
            <table className="w-full text-left text-xs">
              <thead className="text-muted"><tr><th className="pb-2 font-normal">Model</th><th className="font-normal">Calls</th><th className="font-normal">Errors</th><th className="font-normal">p95</th><th className="font-normal">Tok/s</th></tr></thead>
              <tbody className="nums font-mono">{stats.map((s, i) => (
                <tr key={i} className="border-t border-line">
                  <td className="py-2 pr-2 font-sans text-ink">{String(s.model ?? s.system)}</td>
                  <td>{String(s.calls ?? "—")}</td>
                  <td className={n(s.errors) ? "text-bad" : ""}>{String(s.errors ?? "—")}</td>
                  <td>{n(s.p95_ms) === null ? "—" : `${Math.round(n(s.p95_ms)!)} ms`}</td>
                  <td>{n(s.mean_tokens_per_sec) === null ? "—" : n(s.mean_tokens_per_sec)!.toFixed(1)}</td>
                </tr>
              ))}</tbody>
            </table>}
        </div>
      </div>
      {data?.warnings.length ? <ul className="mt-4 space-y-1 text-xs text-muted">{data.warnings.map((w) => <li key={w} className="flex items-center gap-2"><TriangleAlert size={12} />{w}</li>)}</ul> : null}
    </Panel>
  );
}
