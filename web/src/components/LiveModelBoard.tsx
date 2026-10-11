"use client";
import { useState } from "react";
import useSWR from "swr";
import { Activity, Radio, RefreshCw, TriangleAlert } from "lucide-react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Panel } from "./Panel";
import { predictionSamples, rows, row, probability, assessmentSource, type ModelStream, type JsonRow } from "@/lib/model-stream";

async function fetchStream(url: string): Promise<ModelStream> {
  const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(20_000) });
  if (!response.ok) throw new Error("Model backend unavailable. Waiting for a connection.");
  return response.json();
}
const LABELS: Record<string, string> = { will_fail: "Failure probability", anomaly_expected: "Anomaly probability", escalate: "Escalation probability" };
function RawOutput({ value, label = "View complete returned output" }: { value: unknown; label?: string }) {
  return <details className="mt-3 text-xs"><summary className="cursor-pointer text-accent-ink">{label}</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-line bg-canvas p-3 font-mono text-muted">{JSON.stringify(value, null, 2)}</pre></details>;
}
function assessmentConfidence(value: JsonRow) { return assessmentSource(value) === "Model result" ? probability(value.confidence) : null; }
export function LiveModelBoard() {
  const [mission, setMission] = useState("");
  const { data, error, isValidating, mutate } = useSWR<ModelStream>(`/api/model-stream${mission ? `?mission_id=${encodeURIComponent(mission)}` : ""}`, fetchStream,
    { refreshInterval: 2500, dedupingInterval: 1500, keepPreviousData: false, revalidateOnFocus: true, shouldRetryOnError: false });
  const missions = [...new Set((data?.events ?? []).map(e => String(e.mission_id ?? "")).filter(Boolean))];
  const samples = data ? predictionSamples(data) : [];
  const graph = samples.map((s, i) => ({ index: i + 1, label: `${s.action} · ${s.metric} · ${s.time}`, [s.metric]: s.value }));
  const calls = [...(data?.calls ?? [])].reverse();
  const callGraph = calls.map((c, i) => ({ index: i + 1, label: `${c.system} · ${c.model} · ${c.ts}`, [String(c.system)]: typeof c.latency_ms === "number" ? c.latency_ms : null }));
  const systems = [...new Set(calls.map(c => String(c.system)))];
  const assessments = (data?.details ?? []).filter(d => Object.keys(row(d.assessment)).length);
  const confidenceGraph = assessments.map(d => { const a = row(d.assessment); return { flag: String(row(d.flag).flag_id), confidence: assessmentConfidence(a) }; }).filter(a => a.confidence !== null);
  const latest = samples.at(-1);
  return <section aria-label="Live model results" className="flex flex-col gap-4">
    <header className="flex flex-wrap items-center justify-between gap-3 rounded-panel border border-line bg-surface p-4">
      <div><h2 className="flex items-center gap-2 text-base font-semibold text-ink"><Radio size={18} className="text-accent-ink" />Live model results</h2><p className="mt-1 text-xs text-muted" role="status">{error ? data ? "Connection interrupted · retaining last received results" : "Waiting for backend · no model results received" : data ? "Polling model outputs every 2.5 seconds" : "Connecting to model outputs…"}</p></div>
      <div className="flex items-center gap-2"><label className="text-xs text-muted">Mission<select aria-label="Model results mission" value={mission} onChange={e => setMission(e.target.value)} className="ml-2 min-h-10 rounded border border-line bg-surface px-2 text-ink"><option value="">Latest across missions</option>{[...new Set([...missions, mission].filter(Boolean))].map(m => <option key={m}>{m}</option>)}</select></label><button onClick={() => void mutate()} disabled={isValidating} aria-label="Refresh model outputs" className="grid size-10 place-items-center rounded border border-line text-muted disabled:opacity-50"><RefreshCw size={16} /></button></div>
    </header>
    <div className="grid gap-4 lg:grid-cols-2">
      <Panel title="Laya risk over time" aside={latest ? `${latest.value.toFixed(1)}% latest ${LABELS[latest.metric]?.toLowerCase()}` : "WAITING FOR MODEL"}>
        {graph.length ? <div className="h-64 w-full min-w-0"><ResponsiveContainer width="100%" height="100%"><LineChart data={graph} margin={{ top: 10, right: 12, bottom: 8, left: 0 }}><CartesianGrid stroke="var(--c-line)" strokeDasharray="3 3" /><XAxis dataKey="index" tick={{ fill: "var(--c-muted)", fontSize: 11 }} /><YAxis domain={[0, 100]} unit="%" width={48} tick={{ fill: "var(--c-muted)", fontSize: 11 }} /><Tooltip labelFormatter={(_, payload) => payload[0]?.payload.label ?? ""} contentStyle={{ background: "var(--c-surface)", borderColor: "var(--c-line)", color: "var(--c-ink)" }} />{Object.keys(LABELS).map((metric, i) => <Line key={metric} name={LABELS[metric]} dataKey={metric} type="linear" connectNulls stroke={["var(--c-bad)", "var(--c-accent-ink)", "var(--c-ok)"][i]} strokeWidth={2} dot={{ r: 3 }} isAnimationActive={false} />)}</LineChart></ResponsiveContainer></div> : <div className="flex min-h-52 flex-col items-center justify-center gap-3 text-center text-muted"><Activity size={28} /><p className="text-sm">Awaiting Laya predictions</p><p className="max-w-sm text-xs">This chart changes when the backend stores model probabilities. Historical baselines and rule scores are excluded.</p></div>}
        <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted">{Object.values(LABELS).map((label, i) => <span key={label} className="inline-flex items-center gap-1.5"><span className="h-0.5 w-4" style={{ background: ["var(--c-bad)", "var(--c-accent-ink)", "var(--c-ok)"][i] }} />{label}</span>)}</div><p className="mt-3 text-xs text-muted">X: ordered returned predictions · Y: model probability. Missing values remain absent.</p>
      </Panel>
      <Panel title="Model response latency" aside={`${calls.length} recent calls`}>
        {callGraph.length ? <div className="h-64 w-full min-w-0"><ResponsiveContainer width="100%" height="100%"><LineChart data={callGraph}><CartesianGrid stroke="var(--c-line)" strokeDasharray="3 3" /><XAxis dataKey="index" tick={{ fill: "var(--c-muted)", fontSize: 11 }} /><YAxis unit="ms" width={65} tick={{ fill: "var(--c-muted)", fontSize: 11 }} /><Tooltip labelFormatter={(_, payload) => payload[0]?.payload.label ?? ""} contentStyle={{ background: "var(--c-surface)", color: "var(--c-ink)", borderColor: "var(--c-line)" }} />{systems.map((system, i) => <Line key={system} name={system} dataKey={system} connectNulls stroke={["var(--c-accent-ink)", "var(--c-ok)", "var(--c-bad)"][i % 3]} strokeWidth={2} dot={{ r: 2 }} isAnimationActive={false} />)}</LineChart></ResponsiveContainer></div> : <div className="flex min-h-52 items-center justify-center text-sm text-muted">Waiting for model-call telemetry</div>}
        <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted">{systems.map(s => <span key={s}>{s}</span>)}</div>
        {data?.stats.length ? <div className="mt-3 overflow-x-auto"><table className="w-full text-left text-xs"><thead className="text-muted"><tr><th>Model</th><th>Calls / errors</th><th>p95 ms</th><th>Tokens/s</th></tr></thead><tbody>{data.stats.map((s, i) => <tr key={i} className="border-t border-line"><td className="py-2">{String(s.model ?? s.system)}</td><td>{String(s.calls ?? "—")} / {String(s.errors ?? "—")}</td><td>{String(s.p95_ms ?? "—")}</td><td>{String(s.mean_tokens_per_sec ?? "—")}</td></tr>)}</tbody></table></div> : null}<p className="mt-3 text-xs text-muted">Actual backend call latency, including failed calls. Available when server admin access is configured.</p>
      </Panel>
    </div>
    <div className="grid gap-4 xl:grid-cols-2">
      <Panel title="System 2 · explanations and recommendations" aside={`${assessments.length} returned assessments`}>
        {confidenceGraph.length > 0 && <div className="mb-4"><p className="mb-2 text-xs text-muted">Model confidence by returned flag · templates excluded</p><div className="h-36 w-full"><ResponsiveContainer width="100%" height="100%"><LineChart data={confidenceGraph}><CartesianGrid stroke="var(--c-line)" strokeDasharray="3 3" /><XAxis dataKey="flag" tick={{ fill: "var(--c-muted)", fontSize: 11 }} /><YAxis domain={[0, 100]} unit="%" width={48} tick={{ fill: "var(--c-muted)", fontSize: 11 }} /><Tooltip contentStyle={{ background: "var(--c-surface)", color: "var(--c-ink)", borderColor: "var(--c-line)" }} /><Line name="System 2 confidence" dataKey="confidence" stroke="var(--c-accent-ink)" strokeWidth={2} dot={{ r: 3 }} isAnimationActive={false} /></LineChart></ResponsiveContainer></div></div>}
        <div className="flex max-h-[30rem] flex-col gap-3 overflow-y-auto">{assessments.length ? assessments.map(detail => {
          const flag = row(detail.flag), a = row(detail.assessment);const confidence = assessmentConfidence(a);
          return <article key={String(flag.flag_id)} className="rounded border border-line bg-surface-2 p-3"><div className="flex flex-wrap justify-between gap-2"><a href={`/?flag=${flag.flag_id}`} className="text-sm font-semibold text-accent-ink">Flag {String(flag.flag_id)} · {String(flag.title ?? "")}</a><span className="text-xs text-muted">{assessmentSource(a)} · {String(a.model ?? "unknown")}</span></div>
            <p className="mt-2 text-sm font-medium text-ink">{String(a.question ?? "Assessment")}</p><p className="mt-1 whitespace-pre-wrap text-sm text-ink">{String(a.answer ?? "Pending answer")}</p><p className="mt-2 whitespace-pre-wrap text-sm text-muted">{String(a.reasoning ?? "")}</p><p className="mt-3 text-sm text-ink"><strong>Recommended action: </strong>{String(a.recommended_action ?? "Pending")}</p><p className="mt-2 text-xs text-muted">{String(a.severity_assessment ?? "No severity")}{confidence !== null ? ` · ${confidence.toFixed(1)}% confidence` : ""} · {String(a.state ?? "Pending")}</p><RawOutput value={a} />
          </article>;
        }) : <p className="py-8 text-center text-sm text-muted">No stored explanations received. Open a flag and request an explanation; its stored result will appear here automatically.</p>}</div>
      </Panel>
      <Panel title="System 1 · all returned answers" aside={`${samples.length} probability samples`}>
        <div className="max-h-[30rem] overflow-auto"><table className="w-full text-left text-xs"><thead className="text-muted"><tr><th className="pb-2">Action</th><th>Question</th><th>Answer</th><th>Confidence</th></tr></thead><tbody>{(data?.details ?? []).flatMap(d => rows(d.system1)).filter((p, i, all) => all.findIndex(x => String(x.prediction_id ?? `${x.action_id}:${x.question}:${x.created_at}`) === String(p.prediction_id ?? `${p.action_id}:${p.question}:${p.created_at}`)) === i).map(p => <tr key={String(p.prediction_id ?? `${p.action_id}:${p.question}:${p.created_at}`)} className="border-t border-line"><td className="py-2">{String(p.action_id ?? p.mission_id ?? "—")}</td><td>{String(p.question)}</td><td>{String(p.answer ?? "—")}</td><td>{probability(p.confidence)?.toFixed(1) ?? "—"}%</td></tr>)}</tbody></table></div>
        <RawOutput value={{ predictions: (data?.details ?? []).map(d => ({ flag: row(d.flag).flag_id, answers: d.system1 })), forecasts: data?.forecasts ?? [] }} label="All prediction probabilities and forecast outputs" />
      </Panel>
    </div>
    <Panel title="Live decisions and pipeline outputs" aside={`${data?.events.length ?? 0} recent events`}>
      <div className="grid max-h-[26rem] gap-3 overflow-y-auto md:grid-cols-2">{[...(data?.events ?? [])].reverse().slice(0, 12).map(event => {
        const triage = row(row(event.payload).triage);
        return <article key={String(event.event_id)} className="rounded border border-line bg-surface-2 p-3"><p className="text-sm font-medium text-ink">{String(event.event_type).replaceAll("_", " ")} · {String(event.mission_id ?? "")}</p><p className="mt-1 font-mono text-xs text-muted">{String(event.event_id)} · {String(event.location_id ?? "No waypoint")} · {String(event.ts)}</p><p className="mt-2 text-sm text-ink">{String(triage.summary ?? triage.title ?? "Awaiting triage output")}</p><p className="mt-2 text-xs text-muted">{String(triage.agreement ?? "Not assessed")} · {String(triage.severity ?? "—")}</p><RawOutput value={event} /></article>;
      })}</div>
      {data?.warnings.length ? <ul className="mt-4 space-y-1 text-xs text-muted">{data.warnings.map(w => <li key={w} className="flex items-center gap-2"><TriangleAlert size={12} />{w}</li>)}</ul> : null}
      {data && <RawOutput value={{ stats: data.stats, routing: data.routing, calls: data.calls }} label="Model-call stats, routing and complete recent call records" />}
    </Panel>
  </section>;
}
