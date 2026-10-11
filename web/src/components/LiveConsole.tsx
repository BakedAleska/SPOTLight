"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity, BatteryMedium, Brain, Camera, CheckCircle2, CircleDashed, Cpu, Database, Eye, History, Network, Play,
  Radio, ShieldAlert, ShieldCheck, Square, TriangleAlert,
} from "lucide-react";
import { Panel } from "./Panel";
import { LoadingDots } from "./LoadingDots";
import { QwenDraftView } from "./QwenDraft";
import { anchors } from "@/lib/waypoints";
import { detailPollMs, POLL_MS, type LiveCapture, type LiveKg, type LiveRoute, type LiveRun, type LiveRunDetail, type LiveStop } from "@/lib/live";
import { draftFromCapture } from "@/lib/qwen-draft";

const control = "flex min-h-11 items-center justify-center gap-2 rounded border border-line bg-surface px-3 text-sm text-ink hover:bg-surface-2 disabled:opacity-40";
const active = (r?: LiveRun | null) => !!r && (r.status === "pending" || r.status === "running");
const human = (s?: string | null) => (s ?? "").replaceAll("_", " ").toLowerCase();
const ms = (v?: number | null) => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);
const pct = (v?: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`);

async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, { cache: "no-store", ...init });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((body as { error?: string }).error ?? `HTTP ${r.status}`);
  return body as T;
}

export function LiveConsole({ initialRoutes }: { initialRoutes: LiveRoute[] }) {
  const [routes, setRoutes] = useState(initialRoutes);
  const [routeId, setRouteId] = useState(initialRoutes[1]?.route_id ?? initialRoutes[0]?.route_id ?? "R2");
  const [runs, setRuns] = useState<LiveRun[]>([]);
  const [runId, setRunId] = useState<number | null>(null);
  const [detail, setDetail] = useState<LiveRunDetail | null>(null);
  const [kg, setKg] = useState<LiveKg | null>(null);
  const [connection, setConnection] = useState("Connecting…");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);   // follow the active run until the operator picks another one

  useEffect(() => {
    if (routes.length) return;
    getJson<{ routes: LiveRoute[] }>("/api/live/routes").then(d => setRoutes(d.routes)).catch(() => undefined);
  }, [routes.length]);

  // runs list: every 2 s; follows the newest active run
  useEffect(() => {
    let stop = false;
    async function poll() {
      try {
        const d = await getJson<{ items: LiveRun[] }>("/api/live/runs");
        if (stop) return;
        setRuns(d.items);
        setConnection("Connected · polling every 2 s");
        const act = d.items.find(active);
        setRunId(prev => (follow && act ? act.run_id : prev ?? d.items[0]?.run_id ?? null));
      } catch { if (!stop) setConnection("Backend unreachable · retrying every 2 s"); }
    }
    void poll();
    const t = setInterval(poll, POLL_MS);
    return () => { stop = true; clearInterval(t); };
  }, [follow]);

  // the selected run: while it is live or Qwen is still filling cards; every 750 ms while the run is live and
  // a card is pending (Qwen's draft is typed out live), else every 2 s. Polls are chained, never overlapping;
  // a restarted effect aborts the previous one's requests.
  const selected = runs.find(r => r.run_id === runId);
  const ownDetail = !!detail && detail.run.run_id === runId;
  const settled = ownDetail && !active(detail.run) && detail.captures.every(c => c.qwen_source !== "pending");
  const pollMs = ownDetail ? detailPollMs(detail.run, detail.captures) : POLL_MS;
  useEffect(() => {
    if (runId == null) return;
    const abort = new AbortController();
    const { signal } = abort;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let kgAt = -Infinity;
    let kgBusy = false;
    async function poll() {
      // the KG panel stays on the 2 s cadence even while drafts poll faster, one request at a time
      if (!kgBusy && Date.now() - kgAt >= POLL_MS) {
        kgAt = Date.now();
        kgBusy = true;
        getJson<LiveKg>(`/api/live/kg?run_id=${runId}`, { signal })
          .then(g => { if (!signal.aborted) setKg(g); }, () => { /* optional panel */ })
          .finally(() => { kgBusy = false; });
      }
      try {
        const d = await getJson<LiveRunDetail>(`/api/live/runs/${runId}`, { signal });
        if (!signal.aborted) setDetail(d);
      } catch { /* aborted, or the list poll reports the connection */ }
    }
    async function loop() {
      await poll();
      if (!signal.aborted && !settled) timer = setTimeout(loop, pollMs);
    }
    void loop();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [runId, settled, selected?.status, pollMs]);

  const run = detail?.run.run_id === runId ? detail.run : selected ?? null;
  const route = (detail?.run.run_id === runId ? detail.route : null) ?? routes.find(r => r.route_id === (run?.route_id ?? routeId)) ?? null;
  const captures = useMemo(() => (detail?.run.run_id === runId ? detail.captures : []), [detail, runId]);
  const liveRun = runs.find(active) ?? null;
  const notice = message && !(run && run.status !== "pending" && message.includes("queued")) ? message : null;

  const start = useCallback(async () => {
    setBusy(true); setMessage(null);
    try {
      const d = await getJson<{ run: LiveRun }>("/api/live/runs", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ route_id: routeId, operator: "operator" }),
      });
      setFollow(true); setRunId(d.run.run_id); setDetail(null); setKg(null);
      setMessage(`Run #${d.run.run_id} queued: waiting for the robot runner to pick it up…`);
    } catch (e) { setMessage((e as Error).message); }
    finally { setBusy(false); }
  }, [routeId]);

  const cancel = useCallback(async () => {
    if (!liveRun) return;
    setBusy(true);
    try { await getJson(`/api/live/runs/${liveRun.run_id}/cancel`, { method: "POST" }); setMessage(`Run #${liveRun.run_id} cancelled.`); }
    catch (e) { setMessage((e as Error).message); }
    finally { setBusy(false); }
  }, [liveRun]);

  return <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <p className="mb-1 text-xs text-muted">LIVE OPERATIONS</p>
        <h1 className="flex items-center gap-2 text-2xl font-semibold text-ink"><Radio size={24} />SPOT live patrol</h1>
        <p className="mt-1 text-sm text-muted">Every stop is triaged by Laya and the rules, photographed and explained by Qwen while you watch.</p>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-xs text-muted">Route
          <select className={control} value={routeId} onChange={e => setRouteId(e.target.value)} disabled={!!liveRun}>
            {routes.map(r => <option key={r.route_id} value={r.route_id}>{r.route_id} · {r.name} ({r.planned_red} alerts planned)</option>)}
          </select>
        </label>
        <button className={`${control} border-accent bg-accent text-on-accent hover:bg-accent-hover`} onClick={start} disabled={busy || !!liveRun}><Play size={16} />Start route</button>
        <button className={control} onClick={cancel} disabled={busy || !liveRun}><Square size={14} />Cancel</button>
      </div>
    </header>
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-panel border border-line bg-surface p-3 text-xs text-muted">
      <span className="font-semibold text-ink">What is live</span>
      <Chip icon={<Database size={13} />} live={false}>Robot log: the kit row for each action, re-sent now</Chip>
      <Chip icon={<Camera size={13} />} live={false}>Photos: simulated captures (kit samples)</Chip>
      <Chip icon={<Cpu size={13} />} live>Laya + rules triage</Chip>
      <Chip icon={<Brain size={13} />} live>Qwen 3.8 27B on AMD</Chip>
      <Chip icon={<Network size={13} />} live>Neon rows, embeddings, knowledge graph</Chip>
      <span className="ml-auto" role="status">{connection}</span>
    </div>
    {notice && <p className="rounded border border-line bg-surface-2 p-3 text-sm text-ink" role="status">{notice}</p>}
    <div className="panel-columns grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
      <Panel title="Facility map" aside={run ? `RUN #${run.run_id} · ${run.route_id} · ${run.status.toUpperCase()}` : "NO RUN YET"}>
        <LiveMap route={route} captures={captures} run={run} />
        <p className="mt-3 text-xs text-muted">Dashed: the route · <span className="text-bad">●</span> red ping: needs attention · <span className="text-ok">●</span> green ping: all good. Pings come from the live triage (Laya ‖ rules), never from the photo.</p>
      </Panel>
      <div className="panel-stack flex min-w-0 flex-col gap-4">
        <RunPanel run={run} route={route} captures={captures} />
        <KgPanel kg={kg} />
      </div>
    </div>
    <Panel title="Inspection feed" aside={`${captures.length} of ${route?.stops.length ?? 8} stops · newest first`} bodyClassName="p-4">
      {captures.length === 0
        ? <p className="text-sm text-muted">{run && active(run) ? run.status === "pending" ? "Waiting for the robot runner to claim the run…" : "SPOT is heading to stop A…" : "Start a route to watch SPOT inspect the facility."}</p>
        : <ol className="flex flex-col gap-3">{[...captures].reverse().map(c => <li key={c.capture_id}><CaptureCard c={c} stop={route?.stops[c.stop_index]} /></li>)}</ol>}
    </Panel>
    <Panel title="Earlier runs" aside={<span className="flex items-center gap-1"><History size={13} />{runs.length}</span>}>
      {runs.length === 0 ? <p className="text-sm text-muted">No runs yet.</p> :
        <div className="overflow-x-auto"><table className="w-full text-left text-sm">
          <thead className="text-xs text-muted"><tr><th className="py-1 pr-3 font-normal">Run</th><th className="pr-3 font-normal">Route</th><th className="pr-3 font-normal">Status</th><th className="pr-3 font-normal">Red</th><th className="pr-3 font-normal">Green</th><th className="pr-3 font-normal">Qwen / template</th><th className="font-normal">Started</th></tr></thead>
          <tbody>{runs.map(r => <tr key={r.run_id} className={`cursor-pointer border-t border-line hover:bg-surface-2 ${r.run_id === runId ? "bg-surface-2" : ""}`} onClick={() => { setFollow(false); setRunId(r.run_id); }}>
            <td className="py-1.5 pr-3 font-mono">#{r.run_id}</td><td className="pr-3">{r.route_id}</td><td className="pr-3">{r.status}</td>
            <td className="pr-3 text-bad">{r.red ?? 0}</td><td className="pr-3 text-ok">{r.green ?? 0}</td>
            <td className="pr-3">{r.counts?.qwen ?? "—"} / {r.counts?.template ?? "—"}</td>
            <td className="font-mono text-xs text-muted">{new Date(r.created_at).toLocaleTimeString()}</td></tr>)}</tbody>
        </table></div>}
    </Panel>
  </div>;
}

function Chip({ icon, live, children }: { icon: React.ReactNode; live: boolean; children: React.ReactNode }) {
  return <span className="inline-flex items-center gap-1.5">{icon}{children}<span className={`rounded px-1 text-[10px] font-semibold ${live ? "bg-ok/15 text-ok" : "bg-surface-2 text-muted"}`}>{live ? "LIVE" : "SIMULATED"}</span></span>;
}

function LiveMap({ route, captures, run }: { route: LiveRoute | null; captures: LiveCapture[]; run: LiveRun | null }) {
  const stops = route?.stops ?? [];
  const pt = (wp: string) => anchors[wp] ?? [0, 0];
  const latest = captures.at(-1);
  const pos = latest ? pt(latest.location_id) : stops[0] ? pt(stops[0].location_id) : null;
  const working = latest && latest.phase === "arrived";
  return <svg viewBox="0 0 1095 595" className="w-full rounded border border-line bg-surface-2" role="img" aria-label={`Facility map: ${captures.length} stops inspected${latest ? `, SPOT at ${latest.location_id}` : ""}`}>
    <defs><filter id="live-spot-shadow" x="-25%" y="-25%" width="150%" height="150%"><feDropShadow dx="0" dy="1" stdDeviation="1.5" floodColor="black" floodOpacity="0.9" /></filter></defs>
    <image href="/kit/fictional_floor_plan.png" width="1095" height="595" />
    <polyline points={stops.map(s => pt(s.location_id).join(",")).join(" ")} fill="none" stroke="white" strokeWidth="3" strokeDasharray="8 8" opacity="0.7" />
    <polyline points={captures.map(c => pt(c.location_id).join(",")).join(" ")} fill="none" stroke="var(--c-accent-ink)" strokeWidth="5" strokeLinejoin="round" />
    {stops.map((s, i) => {
      const [x, y] = pt(s.location_id);
      const c = captures.find(c => c.stop_index === i);
      const color = c?.verdict === "red" ? "var(--c-bad)" : c?.verdict === "green" ? "var(--c-ok)" : "var(--c-surface)";
      return <g key={s.stop}>
        {c?.verdict && c === latest && <circle cx={x} cy={y} r="14" fill="none" stroke={color} strokeWidth="4" className="live-ping" />}
        <circle cx={x} cy={y} r={c?.verdict ? 13 : 9} fill={color} stroke="white" strokeWidth="3" />
        <text x={x} y={y + 5} textAnchor="middle" fontSize="13" fontWeight="bold" fill={c?.verdict ? "white" : "var(--c-ink)"}>{c?.verdict === "red" ? "!" : s.stop}</text>
        <text x={x + 16} y={y - 12} fontSize="13" fontWeight="bold" fill="white" stroke="black" strokeWidth="3" paintOrder="stroke">{s.stop}</text>
      </g>;
    })}
    {pos && <g className="live-spot" style={{ transform: `translate(${pos[0]}px, ${pos[1]}px)` }} aria-hidden="true">
      {working && <foreignObject x="-15" y="-31" width="30" height="12" transform="scale(2)"><div className="spot-working-dots flex h-full items-center justify-center"><LoadingDots color="white" /></div></foreignObject>}
      <circle cx="0" cy="-30" r="24" fill="black" opacity="0.55" />
      <image href="/spotlight-logo-small.svg" x="-20" y="-52" width="40" height="44" filter="url(#live-spot-shadow)" />
      <text y="-62" textAnchor="middle" fill="white" stroke="black" strokeWidth="3" paintOrder="stroke" fontSize="14" fontWeight="bold">{run && active(run) ? "SPOT" : "SPOT · parked"}</text>
    </g>}
  </svg>;
}

function RunPanel({ run, route, captures }: { run: LiveRun | null; route: LiveRoute | null; captures: LiveCapture[] }) {
  const red = captures.filter(c => c.verdict === "red").length;
  const green = captures.filter(c => c.verdict === "green").length;
  const qwen = captures.filter(c => c.qwen_source === "system2").length;
  const template = captures.filter(c => c.qwen_source === "template").length;
  const latest = captures.at(-1);
  return <Panel title="Run" aside={run ? run.status.toUpperCase() : "IDLE"}>
    {!run ? <p className="text-sm text-muted">No run selected.</p> : <div className="flex flex-col gap-3 text-sm">
      <p className="font-semibold text-ink">#{run.run_id} · {route?.route_id} {route?.name}</p>
      <p className="text-xs text-muted">{route?.description}</p>
      <div className="flex gap-1" aria-label="Stops">{(route?.stops ?? []).map((s, i) => {
        const c = captures.find(c => c.stop_index === i);
        return <span key={s.stop} title={`${s.stop} · ${s.location_id}`} className={`grid h-7 flex-1 place-items-center rounded text-[11px] font-semibold ${c?.verdict === "red" ? "bg-bad text-white" : c?.verdict === "green" ? "bg-ok text-white" : c ? "bg-accent text-on-accent" : "bg-surface-2 text-muted"}`}>{s.stop}</span>;
      })}</div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted">Needs attention</dt><dd className="font-semibold text-bad">{red}</dd>
        <dt className="text-muted">All good</dt><dd className="font-semibold text-ok">{green}</dd>
        <dt className="text-muted">Qwen answers</dt><dd className="text-ink">{qwen}</dd>
        <dt className="text-muted">Template fallbacks</dt><dd className="text-ink">{template}</dd>
        <dt className="text-muted">Battery</dt><dd className="flex items-center gap-1 text-ink"><BatteryMedium size={13} />{latest ? `${latest.battery_pct}%` : "—"}</dd>
        <dt className="text-muted">Runner</dt><dd className="truncate text-ink">{run.runner ?? (run.status === "pending" ? "waiting…" : "—")}</dd>
      </dl>
      {run.error && <p className="text-xs text-bad">{run.error}</p>}
    </div>}
  </Panel>;
}

function KgPanel({ kg }: { kg: LiveKg | null }) {
  const embedded = kg?.nodes.filter(n => n.embedded).length ?? 0;
  return <Panel title="Knowledge graph · live" aside={kg?.totals ? `${kg.totals.nodes} nodes · ${kg.totals.edges} edges total` : undefined}>
    {!kg || kg.nodes.length === 0 ? <p className="text-sm text-muted">Nodes and edges this run adds to Neon appear here.</p> : <div className="flex flex-col gap-2 text-xs">
      <p className="text-ink"><strong>{kg.nodes.length}</strong> nodes and <strong>{kg.edges.length}</strong> edges added this run · {embedded} embedded</p>
      <ul className="flex max-h-56 flex-col gap-1 overflow-y-auto font-mono text-[11px] text-muted">
        {[...kg.edges].reverse().slice(0, 40).map(e => <li key={`${e.src}|${e.rel}|${e.dst}`} className="truncate"><span className="text-ink">{e.src}</span> <span className="text-accent-ink">-{e.rel}→</span> {e.dst}</li>)}
      </ul>
    </div>}
  </Panel>;
}

function CaptureCard({ c, stop }: { c: LiveCapture; stop?: LiveStop }) {
  const red = c.verdict === "red";
  const [annotated, setAnnotated] = useState(false);
  const [ack, setAck] = useState<"idle" | "busy" | "done" | string>("idle");
  const img = annotated && c.vision?.annotated_image_b64 ? `data:image/jpeg;base64,${c.vision.annotated_image_b64}` : c.photo?.path;
  const laya = c.laya;
  // Qwen's unverified draft (only while qwen_source is "pending"; /analysis overwrites it with the checked result)
  const draft = draftFromCapture(c.qwen);
  const acknowledge = async () => {
    if (!c.flag_id) return;
    setAck("busy");
    try {
      await getJson(`/api/flags/${c.flag_id}/actions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "ACKNOWLEDGE", operator: "operator", note: `Live run ${c.run_id} stop ${c.stop}: verified on the live console.` }) });
      setAck("done");
    } catch (e) { setAck((e as Error).message); }
  };
  return <article className={`live-card-in overflow-hidden rounded-panel border bg-surface ${red ? "border-bad" : c.verdict ? "border-line" : "border-accent"}`} aria-label={`Stop ${c.stop}, ${c.location_id}, ${c.verdict ?? "triaging"}`}>
    <header className={`flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 ${red ? "bg-bad/10" : c.verdict ? "bg-ok/10" : "bg-surface-2"}`}>
      <span className="font-mono text-sm font-semibold text-ink">STOP {c.stop}</span>
      <span className="text-sm text-ink">{c.location_id} · {stop?.location_name}</span>
      <span className="text-sm text-muted">{c.asset_id} {stop?.asset_name ? `(${stop.asset_name}, ${stop.criticality})` : ""}</span>
      <span className="ml-auto">{c.verdict == null ? <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-accent-ink"><CircleDashed size={14} />TRIAGING <LoadingDots color="currentColor" /></span>
        : red ? <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-bad"><TriangleAlert size={14} />NEEDS ATTENTION · {c.priority}</span>
        : <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-ok"><CheckCircle2 size={14} />ALL GOOD · {c.priority}</span>}</span>
    </header>
    <div className="grid gap-4 p-4 md:grid-cols-[220px_minmax(0,1fr)_minmax(0,1.3fr)]">
      <figure className="flex flex-col gap-1.5">
        <div className="relative overflow-hidden rounded border border-line bg-black">
          {/* eslint-disable-next-line @next/next/no-img-element -- data: URLs for the vision overlay */}
          {img && <img src={img} alt={`Simulated capture for stop ${c.stop} (${human(c.action_type)})`} className="aspect-[4/3] w-full object-cover" />}
          <span className="absolute left-1.5 top-1.5 rounded bg-black/75 px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-white">SIMULATED CAPTURE</span>
        </div>
        <figcaption className="text-[11px] text-muted">{c.photo?.note ?? "Kit sample image picked by action type."}</figcaption>
        {c.vision?.annotated_image_b64 && <button className="text-left text-[11px] text-accent-ink underline" onClick={() => setAnnotated(a => !a)}>{annotated ? "Show original" : "Show vision overlay"}</button>}
      </figure>
      <div className="flex min-w-0 flex-col gap-3 text-sm">
        <Section icon={<Activity size={14} />} title="Robot log (kit row, re-sent live)">
          <p className="text-ink">{human(c.action_type)} · <span className={c.status === "FAILED" ? "font-semibold text-bad" : ""}>{c.status}{c.failure_type ? ` (${human(c.failure_type)})` : ""}</span> · battery {c.battery_pct}%</p>
          {c.reading && <p className="text-muted">Reading {human(c.reading.measurement_type)} <strong className="text-ink">{c.reading.value} {c.reading.unit}</strong> · quality {c.reading.quality}</p>}
          {c.anomaly && <p className="text-bad">Anomaly {human(c.anomaly.anomaly_type)} · {c.anomaly.severity}</p>}
          <p className="font-mono text-[11px] text-faint">{c.action_id} ← {c.source_action_id}</p>
        </Section>
        <Section icon={<Cpu size={14} />} title="Laya (System 1)">
          {!laya ? <p className="text-muted">Waiting…</p> : laya.status === "ok" ? <>
            <p className="text-ink">Escalate: <strong>{laya.verdict?.escalate ? "yes" : "no"}</strong> (p = {laya.verdict?.p_escalate?.toFixed(2) ?? "—"}) · priority {laya.answers?.priority?.answer ?? "—"} ({pct(laya.answers?.priority?.confidence)})</p>
            <p className="text-[11px] text-muted">{laya.model}{laya.device ? ` on ${laya.device}` : ""} · {ms(laya.latency_ms)}</p>
          </> : <p className="text-warn" title={laya.error}>Laya unavailable: the rules decided alone.</p>}
        </Section>
        <Section icon={<ShieldAlert size={14} />} title={`Rules (${c.decision?.mode ?? "parallel"} with Laya)`}>
          {c.rules?.fired?.length ? <ul className="flex flex-wrap gap-1">{c.rules.fired.map(r => <li key={r} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-ink">{r}</li>)}</ul>
            : <p className="text-muted">{c.rules ? `No rule fired (${c.rules.priority}).` : "Waiting…"}</p>}
        </Section>
        <Section icon={<Eye size={14} />} title="Vision on the photo (demo pairing)">
          {!c.vision ? <p className="text-muted">{c.verdict ? "Reading the photo…" : "—"}</p> : c.vision.error ? <p className="text-warn">{c.vision.error}</p> : <>
            {(c.vision.rows ?? []).length ? <ul className="text-ink">{c.vision.rows!.map(r => <li key={r.id}><span className="font-mono text-[11px] text-muted">{r.id}</span> {human(r.quantity ?? r.kind)} {r.value ?? "—"} {r.unit ?? ""} · {r.trust} trust ({pct(r.confidence)})</li>)}</ul> : <p className="text-muted">No numeric reading on this image.</p>}
            <p className="text-[11px] text-muted">Never compared with this asset&apos;s log: the photo is a simulated capture. {ms(c.vision.latency_ms)}</p>
          </>}
        </Section>
      </div>
      <div className="flex min-w-0 flex-col gap-3 text-sm">
        <Section icon={<Brain size={14} />} title="Qwen explanation (System 2)">
          {c.qwen_source === "pending" && draft ? <QwenDraftView draft={draft} label="Qwen writing · unverified draft" formatAction={human} catchUpMs={450} compact />
            : c.qwen_source === "pending" || (!c.qwen && c.verdict && c.qwen_source !== "error") ? <p className="flex items-center gap-2 text-accent-ink">Qwen analysing <LoadingDots color="currentColor" /></p>
            : !c.qwen ? <p className="text-muted">{c.qwen_source === "error" ? "The run ended before Qwen's answer was stored." : "—"}</p>
            : draft ? <p className="text-muted">Qwen did not finish; its unverified draft was discarded.</p> : <>
              {c.qwen.source === "template" && <p className="rounded bg-warn/10 px-2 py-1 text-xs font-semibold text-warn">TEMPLATE · Qwen did not answer ({c.qwen.fallback_reason?.slice(0, 120)})</p>}
              {c.qwen.question && <p className="text-xs text-muted">Q: {c.qwen.question}</p>}
              <p className="text-ink">{c.qwen.answer}</p>
              {c.qwen.recommended_action && <p className="text-ink">Recommended: <strong>{human(c.qwen.recommended_action)}</strong></p>}
              {c.qwen.evidence?.length ? <ul className="list-disc pl-4 text-xs text-muted">{c.qwen.evidence.slice(0, 4).map((e, i) => <li key={i}>{e.claim} <span className="font-mono">[{e.ids.join(", ")}]</span></li>)}</ul>
                : c.qwen.reasoning && <p className="line-clamp-4 text-xs text-muted" title={c.qwen.reasoning}>{c.qwen.reasoning}</p>}
              <p className="text-[11px] text-muted">{c.qwen.source === "system2" ? `${c.qwen.model} · ${ms(c.qwen.latency_ms)}${c.qwen.tokens_per_sec ? ` · ${c.qwen.tokens_per_sec.toFixed(1)} tok/s` : ""}${c.qwen.confidence_label ? ` · confidence ${c.qwen.confidence_label}` : ""}${c.qwen.image ? ` · image ${c.qwen.image}` : ""}` : "template (checked rules text, not a model answer)"}</p>
            </>}
        </Section>
        {red && <div className="flex flex-wrap items-center gap-3 rounded border border-bad bg-bad/10 p-3">
          <ShieldAlert size={18} className="text-bad" />
          <p className="text-sm font-semibold text-bad">Needs human verification</p>
          {c.flag_id && <button className={`${control} ml-auto min-h-9`} onClick={acknowledge} disabled={ack === "busy" || ack === "done"}>{ack === "done" ? <><ShieldCheck size={15} />Acknowledged</> : ack === "busy" ? "Saving…" : "Acknowledge"}</button>}
          {ack !== "idle" && ack !== "busy" && ack !== "done" && <p className="w-full text-xs text-bad">{ack}</p>}
        </div>}
        <p className="mt-auto font-mono text-[11px] text-faint">
          {c.flag_id ? `flag #${c.flag_id} · ` : ""}{c.event_id ?? ""}{c.doc_id ? ` · ${c.doc_id}` : ""}<br />
          ingest {ms(c.timings?.ingest_ms)} · vision {ms(c.timings?.vision_ms)} · Qwen {ms(c.timings?.system2_ms)} · KG +{c.kg?.triage?.nodes?.length ?? 0} nodes
        </p>
      </div>
    </div>
  </article>;
}

function Section({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return <section><h3 className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-muted uppercase">{icon}{title}</h3>{children}</section>;
}

