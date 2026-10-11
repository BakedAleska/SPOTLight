"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity, ArrowLeft, BatteryMedium, Brain, Camera, CheckCircle2, CircleDashed, Cpu, Database, Eye, History, Network, Play,
  Radio, ShieldAlert, ShieldCheck, Square, TriangleAlert,
} from "lucide-react";
import { Panel } from "./Panel";
import { LoadingDots } from "./LoadingDots";
import { QwenDraftView } from "./QwenDraft";
import { ModelExplanation } from "./ModelOutput";
import { RouteAhead } from "./RouteAhead";
import { LiveAnalytics } from "./LiveAnalytics";
import { anchors } from "@/lib/waypoints";
import { legsPath, routeLegs } from "@/lib/route-geometry";
import { detailPollMs, MOCK_ROUTE_ID, POLL_MS, type LiveCapture, type LiveRoute, type LiveRun, type LiveRunDetail, type LiveStop, type MockPatrol } from "@/lib/live";
import { advanceMock, cancelMock, MOCK_PHASE_MS, mockCaptures, mockRun, startMock, type MockState } from "@/lib/mock-playback";
import { draftFromCapture } from "@/lib/qwen-draft";
import { confidenceLabel, displayModel } from "@/lib/display";

// Colours only in the variants: same-property Tailwind utilities resolve by stylesheet order, not class order.
const controlBase = "flex min-h-11 items-center justify-center gap-2 rounded border px-3 text-sm disabled:opacity-40";
const control = `${controlBase} border-line bg-surface text-ink hover:bg-surface-2`;
const controlPrimary = `${controlBase} border-accent bg-accent text-on-accent hover:bg-accent-hover`;
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
  // Mission 1 replayed in the browser is the default: Start route plays it with no robot runner (lib/mock-playback.ts).
  const [routeId, setRouteId] = useState(MOCK_ROUTE_ID);
  const [mock, setMock] = useState<MockState | null>(null);
  const [mockPlan, setMockPlan] = useState<MockPatrol | null>(null);   // fetched up front so the idle feed can list its stops
  const [runs, setRuns] = useState<LiveRun[]>([]);
  const [runId, setRunId] = useState<number | null>(null);
  const [detail, setDetail] = useState<LiveRunDetail | null>(null);
  const [connection, setConnection] = useState("Connecting…");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);   // follow the active run until the operator picks another one

  useEffect(() => {
    if (routes.length) return;
    getJson<{ routes: LiveRoute[] }>("/api/live/routes").then(d => setRoutes(d.routes)).catch(() => undefined);
  }, [routes.length]);

  useEffect(() => {
    getJson<MockPatrol>("/api/live/mock?mission=M001").then(setMockPlan).catch(() => undefined);
  }, []);

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
    async function poll() {
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

  // A mock patrol, while on screen, replaces the backend run in every view; picking an earlier run clears it.
  const mockShown = useMemo(() => (mock ? { run: mockRun(mock), captures: mockCaptures(mock) } : null), [mock]);
  const run = mockShown ? mockShown.run : detail?.run.run_id === runId ? detail.run : selected ?? null;
  const route = mock ? mock.plan.route : (detail?.run.run_id === runId ? detail.route : null) ?? routes.find(r => r.route_id === (run?.route_id ?? routeId))
    ?? (!run && routeId === MOCK_ROUTE_ID ? mockPlan?.route : null) ?? null;
  const backendCaptures = useMemo(() => (detail?.run.run_id === runId ? detail.captures : []), [detail, runId]);
  const captures = mockShown ? mockShown.captures : backendCaptures;
  const liveRun = runs.find(active) ?? null;
  const mockRunning = mock?.status === "running";
  const isMock = routeId === MOCK_ROUTE_ID;

  // Mock playback: one phase per tick (arrived -> triaged -> done), then on to the next stop.
  useEffect(() => {
    if (!mock || mock.status !== "running") return;
    const t = setTimeout(() => setMock(m => (m ? advanceMock(m) : m)), MOCK_PHASE_MS[mock.step % 3]);
    return () => clearTimeout(t);
  }, [mock]);
  const notice = message && !(run && run.status !== "pending" && message.includes("queued")) ? message : null;

  const start = useCallback(async () => {
    setBusy(true); setMessage(null);
    try {
      if (routeId === MOCK_ROUTE_ID) {
        const plan = mockPlan ?? await getJson<MockPatrol>("/api/live/mock?mission=M001");
        setFollow(false);
        setMock(startMock(plan, new Date().toISOString()));
        return;
      }
      const d = await getJson<{ run: LiveRun }>("/api/live/runs", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ route_id: routeId, operator: "operator" }),
      });
      setMock(null); setFollow(true); setRunId(d.run.run_id); setDetail(null);
      setMessage(`Run #${d.run.run_id} queued: waiting for the robot runner to pick it up…`);
    } catch (e) { setMessage((e as Error).message); }
    finally { setBusy(false); }
  }, [routeId, mockPlan]);

  const cancel = useCallback(async () => {
    if (mock?.status === "running") { setMock(cancelMock(mock)); return; }
    if (!liveRun) return;
    setBusy(true);
    try { await getJson(`/api/live/runs/${liveRun.run_id}/cancel`, { method: "POST" }); setMessage(`Run #${liveRun.run_id} cancelled.`); }
    catch (e) { setMessage((e as Error).message); }
    finally { setBusy(false); }
  }, [liveRun, mock]);

  /* The page is a title over a grid of panels: the run's controls, what is
     live, and any notice sit in the Run panel at the head of the right
     column, beside the map they drive, rather than in loose bands above it. */
  const controls = <div className="flex flex-col gap-2">
    <label className="flex flex-col gap-1 text-xs text-muted">Route
      <select className={`${control} w-full justify-start`} value={routeId} onChange={e => setRouteId(e.target.value)} disabled={!!liveRun || mockRunning}>
        <option value={MOCK_ROUTE_ID}>Mission 1 · mock patrol (no robot needed)</option>
        {routes.map(r => <option key={r.route_id} value={r.route_id}>{r.route_id} · {r.name} ({r.planned_red} alerts planned)</option>)}
      </select>
    </label>
    <div className="grid grid-cols-2 gap-2">
      <button className={controlPrimary} onClick={start} disabled={busy || mockRunning || (!isMock && !!liveRun)}><Play size={16} />Start route</button>
      <button className={control} onClick={cancel} disabled={busy || !(mockRunning || liveRun)}><Square size={14} />Cancel</button>
    </div>
    {notice && <p className="rounded border border-line bg-surface-2 px-3 py-2 text-xs text-ink" role="status">{notice}</p>}
  </div>;
  const sources = <div className="flex flex-col gap-2 text-xs text-muted">
    <p className="font-semibold text-ink">What is live</p>
    <ul className="flex flex-col gap-1.5">
      {mock || isMock ? <>
        <Chip icon={<Database size={13} />} tag="KIT">Robot log: Mission 1&apos;s kit rows, in order</Chip>
        <Chip icon={<Camera size={13} />} tag="SIMULATED">Photos: kit samples by action type</Chip>
        <Chip icon={<Cpu size={13} />} tag="STORED">Triage: the flags the confidence model and rules raised</Chip>
        <Chip icon={<Brain size={13} />} tag="STORED">SPOTbot: its stored explanations</Chip>
      </> : <>
        <Chip icon={<Database size={13} />} tag="SIMULATED">Robot log: the kit row for each action, re-sent now</Chip>
        <Chip icon={<Camera size={13} />} tag="SIMULATED">Photos: simulated captures (kit samples)</Chip>
        <Chip icon={<Cpu size={13} />} tag="LIVE">Confidence model + rules triage</Chip>
        <Chip icon={<Brain size={13} />} tag="LIVE">SPOTbot on AMD</Chip>
        <Chip icon={<Network size={13} />} tag="LIVE">Neon rows, embeddings, knowledge graph</Chip>
      </>}
    </ul>
    {(mock || isMock) && <p className="leading-snug">
      <strong className="text-ink">Mock patrol.</strong> Mission 1 from the challenge kit, replayed stop by stop in your browser: each stop is the kit&apos;s real
      robot log, the stops that need attention are the ones the triage flagged, and their explanations are SPOTbot&apos;s stored answers. No robot, runner or model is called.
    </p>}
    <p role="status" className="text-faint">{connection}</p>
  </div>;

  return <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
    <header>
      <p className="mb-1 text-xs text-muted">LIVE OPERATIONS</p>
      <h1 className="flex items-center gap-2 text-2xl font-semibold text-ink"><Radio size={24} />SPOT live patrol</h1>
      <p className="mt-1 text-sm text-muted">Every stop is triaged by the confidence model and the rules, photographed and explained by SPOTbot while you watch.</p>
    </header>
    <div className="panel-columns grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
      <div className="panel-stack flex min-w-0 flex-col gap-4 xl:contents">
        <Panel title="Facility map" className="min-w-0 xl:col-start-1 xl:row-start-1" aside={run ? `${run.run_id === 0 ? "MOCK" : `RUN #${run.run_id}`} · ${run.route_id} · ${run.status.toUpperCase()}` : "NO RUN YET"}>
          <LiveMap route={route} captures={captures} run={run} />
          <p className="mt-3 text-xs text-muted">Dashed: route to the next stop · Solid: visited route · <span className="text-bad">!</span>: needs attention · <span className="text-ok">✓</span>: all good. Verdicts come from the live triage (confidence model ‖ rules), never from the photo.</p>
        </Panel>
        {/* Right under the map, so each stop's card rolls in where the eye already is. */}
        <Panel title="Inspection feed" className="min-w-0 xl:col-start-1 xl:row-start-2" aside={`${captures.length} of ${route?.stops.length ?? 8} stops · newest first`} bodyClassName="p-4">
          {captures.length === 0
            ? <p className="text-sm text-muted">{run && active(run) ? run.status === "pending" ? "Waiting for the robot runner to claim the run…" : "SPOT is heading to stop A…" : "Start a route to watch SPOT inspect the facility."}</p>
            : <ol className="flex flex-col gap-3">{[...captures].reverse().map(c => <li key={c.capture_id}><CaptureCard c={c} stop={route?.stops[c.stop_index]} /></li>)}</ol>}
          {/* The stops still to come, so the feed stretched beside the Run column shows the route rather than blank space. */}
          {route && captures.length < route.stops.length && <StopsAhead stops={route.stops.slice(captures.length)} />}
        </Panel>
      </div>
      {/* Stacked (under xl), this column goes first and side by side, so
          Start route is above the map rather than under the feed. At xl both
          stacks dissolve (display: contents) into one 2×2 grid, so Run shares
          the map's row and Live analytics the feed's, and their edges line up. */}
      <div className="panel-stack order-first grid min-w-0 content-start gap-4 md:grid-cols-2 xl:contents">
        <RunPanel className="xl:col-start-2 xl:row-start-1" run={run} route={route} captures={captures} controls={controls} sources={sources} />
        <LiveAnalytics className="xl:col-start-2 xl:row-start-2" captures={captures} route={route} />
      </div>
    </div>
    <Panel title="Earlier runs" aside={<span className="flex items-center gap-1"><History size={13} />{runs.length}</span>}>
      {runs.length === 0 ? <p className="text-sm text-muted">No runs yet.</p> :
        <div className="overflow-x-auto"><table className="w-full text-left text-sm">
          <thead className="text-xs text-muted"><tr><th className="py-1 pr-3 font-normal">Run</th><th className="pr-3 font-normal">Route</th><th className="pr-3 font-normal">Status</th><th className="pr-3 font-normal">Red</th><th className="pr-3 font-normal">Green</th><th className="pr-3 font-normal">SPOTbot / template</th><th className="font-normal">Started</th></tr></thead>
          <tbody>{runs.map(r => <tr key={r.run_id} className={`cursor-pointer border-t border-line hover:bg-surface-2 ${r.run_id === runId ? "bg-surface-2" : ""}`} onClick={() => { setMock(null); setFollow(false); setRunId(r.run_id); }}>
            <td className="py-1.5 pr-3 font-mono">#{r.run_id}</td><td className="pr-3">{r.route_id}</td><td className="pr-3">{r.status}</td>
            <td className="pr-3 text-bad">{r.red ?? 0}</td><td className="pr-3 text-ok">{r.green ?? 0}</td>
            <td className="pr-3">{r.counts?.qwen ?? "—"} / {r.counts?.template ?? "—"}</td>
            <td className="font-mono text-xs text-muted">{new Date(r.created_at).toLocaleTimeString()}</td></tr>)}</tbody>
        </table></div>}
    </Panel>
  </div>;
}

function Chip({ icon, tag, children }: { icon: React.ReactNode; tag: "LIVE" | "SIMULATED" | "STORED" | "KIT"; children: React.ReactNode }) {
  return <li className="flex items-start gap-2"><span className="mt-px shrink-0">{icon}</span><span className="min-w-0 flex-1 leading-snug">{children}</span><span className={`shrink-0 rounded px-1 text-[10px] font-semibold ${tag === "LIVE" ? "bg-ok/15 text-ok" : "bg-surface-2 text-muted"}`}>{tag}</span></li>;
}

function LiveMap({ route, captures, run }: { route: LiveRoute | null; captures: LiveCapture[]; run: LiveRun | null }) {
  const stops = route?.stops ?? [];
  const pt = (wp: string) => anchors[wp] ?? [0, 0];
  const latest = captures.at(-1);
  const pos = latest ? pt(latest.location_id) : stops[0] ? pt(stops[0].location_id) : null;
  const working = latest && latest.phase === "arrived";
  const legs = useMemo(() => routeLegs((route?.stops ?? []).map(s => s.location_id)), [route]);
  // Travel k leads to stop k + 1: every travel before the latest captured stop has been walked, and only the next one
  // is drawn ahead of SPOT.
  const walked = latest ? legsPath(legs.filter(l => l.travel < latest.stop_index)) : "";
  const issue = latest?.verdict === "red";
  // Drawn like the Replay's map: the blueprint's beacons already mark every waypoint, so a stop only gets a marker of
  // its own once it has a verdict, and not while SPOT is standing on it (SPOT's own marker says it then).
  return <svg viewBox="0 0 1095 595" className="w-full rounded border border-line bg-surface-2" role="img" aria-label={`Facility map: ${captures.length} stops inspected${latest ? `, SPOT at ${latest.location_id}` : ""}`}>
    <defs><filter id="live-spot-contrast" x="-25%" y="-25%" width="150%" height="150%"><feComponentTransfer><feFuncA type="linear" slope="2" /></feComponentTransfer><feDropShadow dx="0" dy="1" stdDeviation="1.5" floodColor="black" floodOpacity="0.9" /></filter></defs>
    <image href="/spot-blueprint.svg" width="1095" height="595" />
    <path d={walked} fill="none" stroke="var(--c-accent-ink)" strokeWidth="5" strokeLinejoin="round" strokeLinecap="round" />
    <RouteAhead d={legsPath(legs.filter(l => l.travel === (latest?.stop_index ?? 0)))} />
    {stops.map((s, i) => {
      const [x, y] = pt(s.location_id);
      const c = captures.find(c => c.stop_index === i);
      if (!c?.verdict || s.location_id === latest?.location_id) return null;
      const red = c.verdict === "red";
      return <g key={s.stop}>
        <title>{`Stop ${s.stop} · ${s.location_id} · ${red ? "needs attention" : "all good"}`}</title>
        <circle cx={x} cy={y} r="7" fill={red ? "var(--c-bad)" : "var(--c-ok)"} stroke="white" strokeWidth="3" />
        {red
          ? <text x={x} y={y + 5} textAnchor="middle" fill="white" fontSize="15" fontWeight="bold">!</text>
          : <path d={`M ${x - 3} ${y} l 2 2.2 l 4 -4.4`} fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />}
      </g>;
    })}
    {pos && <g className="live-spot" style={{ transform: `translate(${pos[0]}px, ${pos[1]}px)` }} aria-hidden="true">
      {latest?.verdict && <circle r="16" fill="none" stroke={issue ? "var(--c-bad)" : "var(--c-ok)"} strokeWidth="3" className="live-ping" />}
      {working && <foreignObject x="-15" y="-31" width="30" height="12" transform="scale(2)"><div className="spot-working-dots flex h-full items-center justify-center"><LoadingDots color="white" /></div></foreignObject>}
      {issue && !working && <g className={run && active(run) ? "spot-issue spot-issue-flash" : "spot-issue"}>
        <path d="M 0 -64 L 14 -39 L -14 -39 Z" fill="var(--c-bad)" stroke="white" strokeWidth="2.5" strokeLinejoin="round" />
        <path d="M 0 -57 V -49" stroke="white" strokeWidth="3.5" strokeLinecap="round" /><circle cy="-43.5" r="2" fill="white" />
      </g>}
      <image href="/spotlight-logo-small.svg" x="-30" y="-34" width="60" height="68" filter="url(#live-spot-contrast)" />
      <text y="65" textAnchor="middle" fill="white" stroke="black" strokeWidth="3" paintOrder="stroke" fontSize="15" fontWeight="bold">{run && active(run) ? "SPOT" : "SPOT · parked"}</text>
    </g>}
  </svg>;
}

function StopsAhead({ stops }: { stops: LiveStop[] }) {
  return <div className="mt-4 flex flex-col gap-2">
    <p className="text-[11px] font-semibold tracking-wide text-muted uppercase">Still ahead</p>
    <ol className="grid gap-2 sm:grid-cols-2">{stops.map(s => <li key={s.stop} className="flex items-center gap-3 rounded border border-line bg-surface-2 px-3 py-2 text-xs">
      <span className="grid size-7 shrink-0 place-items-center rounded bg-surface font-mono text-[11px] font-semibold text-muted">{s.stop}</span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-ink">{s.location_name ?? s.location_id}</span>
        <span className="block truncate text-muted">{s.asset_name ?? s.asset_id} · {s.action_type.replaceAll("_", " ").toLowerCase()}</span>
      </span>
    </li>)}</ol>
  </div>;
}

function RunPanel({ className, run, route, captures, controls, sources }: {
  className?: string; run: LiveRun | null; route: LiveRoute | null; captures: LiveCapture[]; controls: React.ReactNode; sources: React.ReactNode;
}) {
  const red = captures.filter(c => c.verdict === "red").length;
  const green = captures.filter(c => c.verdict === "green").length;
  const qwen = captures.filter(c => c.qwen_source === "system2").length;
  const template = captures.filter(c => c.qwen_source === "template").length;
  const latest = captures.at(-1);
  return <Panel title="Run" className={className} aside={run ? run.status.toUpperCase() : "IDLE"} bodyClassName="divide-y divide-line [&>*]:p-4">
    {controls}
    {!run ? <p className="text-sm text-muted">No run yet. Pick a route and start it to watch SPOT patrol.</p> : <div className="flex flex-col gap-3 text-sm">
      <p className="font-semibold text-ink">{run.run_id === 0 ? "Mock patrol" : `#${run.run_id}`} · {route?.name}</p>
      <p className="text-xs text-muted">{route?.description}</p>
      <div className="flex gap-1" aria-label="Stops">{(route?.stops ?? []).map((s, i) => {
        const c = captures.find(c => c.stop_index === i);
        return <span key={s.stop} title={`${s.stop} · ${s.location_id}`} className={`grid h-7 flex-1 place-items-center rounded text-[11px] font-semibold ${c?.verdict === "red" ? "bg-bad text-white" : c?.verdict === "green" ? "bg-ok text-white" : c ? "bg-accent text-on-accent" : "bg-surface-2 text-muted"}`}>{s.stop}</span>;
      })}</div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted">Needs attention</dt><dd className="font-semibold text-bad">{red}</dd>
        <dt className="text-muted">All good</dt><dd className="font-semibold text-ok">{green}</dd>
        <dt className="text-muted">SPOTbot answers</dt><dd className="text-ink">{qwen}</dd>
        <dt className="text-muted">Template fallbacks</dt><dd className="text-ink">{template}</dd>
        <dt className="text-muted">Battery</dt><dd className="flex items-center gap-1 text-ink"><BatteryMedium size={13} />{latest ? `${latest.battery_pct}%` : "—"}</dd>
        <dt className="text-muted">Runner</dt><dd className="truncate text-ink">{run.runner ?? (run.status === "pending" ? "waiting…" : "—")}</dd>
      </dl>
      {run.error && <p className="text-xs text-bad">{run.error}</p>}
    </div>}
    {sources}
  </Panel>;
}

function CaptureCard({ c, stop }: { c: LiveCapture; stop?: LiveStop }) {
  const red = c.verdict === "red";
  const [annotated, setAnnotated] = useState(false);
  const [ack, setAck] = useState<"idle" | "busy" | "done" | string>("idle");
  const img = annotated && c.vision?.annotated_image_b64 ? `data:image/jpeg;base64,${c.vision.annotated_image_b64}` : c.photo?.path;
  const laya = c.laya;
  const mock = c.run_id === 0;   // a mock-patrol capture (lib/mock-playback.ts): no vision call, no flag to acknowledge
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
        : red ? <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-bad"><TriangleAlert size={14} />NEEDS ATTENTION{c.priority ? ` · ${c.priority}` : ""}</span>
        : <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-ok"><CheckCircle2 size={14} />ALL GOOD{c.priority ? ` · ${c.priority}` : ""}</span>}</span>
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
          <p className="flex items-center gap-1 font-mono text-[11px] text-faint">{c.action_id} <ArrowLeft size={11} aria-label="from" /> {c.source_action_id}</p>
        </Section>
        <Section icon={<Cpu size={14} />} title="Confidence (System 1)">
          {!laya ? <p className="text-muted">Waiting…</p> : laya.status === "ok" ? <>
            <p className="text-ink">Escalate: <strong>{laya.verdict?.escalate ? "yes" : "no"}</strong> · {confidenceLabel(laya.verdict?.p_escalate) ?? (laya.verdict?.escalate === false ? "not escalated" : "Confidence —")}{(laya.answers?.priority?.answer ?? c.priority) ? ` · priority ${laya.answers?.priority?.answer ?? c.priority}` : ""}{laya.answers?.priority?.confidence != null ? ` (${pct(laya.answers.priority.confidence)})` : ""}</p>
            <p className="text-[11px] text-muted">{displayModel(laya.model)}{laya.device ? ` on ${laya.device}` : ""}{laya.latency_ms != null ? ` · ${ms(laya.latency_ms)}` : ""}</p>
          </> : <p className="text-warn" title={laya.error}>Confidence model unavailable: the rules decided alone.</p>}
        </Section>
        <Section icon={<ShieldAlert size={14} />} title={`Rules (${c.decision?.mode ?? "parallel"} with the confidence model)`}>
          {c.rules?.fired?.length ? <ul className="flex flex-wrap gap-1">{c.rules.fired.map(r => <li key={r} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-ink">{r}</li>)}</ul>
            : <p className="text-muted">{c.rules ? `No rule fired${c.rules.priority ? ` (${c.rules.priority})` : ""}.` : "Waiting…"}</p>}
        </Section>
        <Section icon={<Eye size={14} />} title="Vision on the photo (demo pairing)">
          {mock ? <p className="text-muted">Not run in the mock patrol: the photo is a kit sample.</p> : !c.vision ? <p className="text-muted">{c.verdict ? "Reading the photo…" : "—"}</p> : c.vision.error ? <p className="text-warn">{c.vision.error}</p> : <>
            {(c.vision.rows ?? []).length ? <ul className="text-ink">{c.vision.rows!.map(r => <li key={r.id}><span className="font-mono text-[11px] text-muted">{r.id}</span> {human(r.quantity ?? r.kind)} {r.value ?? "—"} {r.unit ?? ""} · {r.trust} trust ({pct(r.confidence)})</li>)}</ul> : <p className="text-muted">No numeric reading on this image.</p>}
            <p className="text-[11px] text-muted">Never compared with this asset&apos;s log: the photo is a simulated capture. {ms(c.vision.latency_ms)}</p>
          </>}
        </Section>
      </div>
      <div className="flex min-w-0 flex-col gap-3 text-sm">
        <Section icon={<Brain size={14} />} title="SPOTbot explanation">
          {c.qwen_source === "pending" && draft ? <QwenDraftView draft={draft} label="SPOTbot writing · unverified draft" catchUpMs={450} compact />
            : mock && !c.qwen && c.verdict && c.qwen_source !== "pending" ? <p className="text-muted">Not escalated: no explanation needed for this stop.</p>
            : c.qwen_source === "pending" || (!c.qwen && c.verdict && c.qwen_source !== "error") ? <p className="flex items-center gap-2 text-accent-ink">SPOTbot analysing <LoadingDots color="currentColor" /></p>
            : !c.qwen ? <p className="text-muted">{c.qwen_source === "error" ? "The run ended before SPOTbot's answer was stored." : "—"}</p>
            : draft ? <p className="text-muted">SPOTbot did not finish; its unverified draft was discarded.</p> : <>
              {c.qwen.source === "template" && <p className="rounded bg-warn/10 px-2 py-1 text-xs font-semibold text-warn">TEMPLATE · SPOTbot did not answer ({c.qwen.fallback_reason?.slice(0, 120)})</p>}
              {c.qwen.question && <p className="text-xs text-muted">Q: {c.qwen.question}</p>}
              <ModelExplanation answer={c.qwen.answer} evidence={c.qwen.evidence} reasoning={c.qwen.reasoning}
                recommendedAction={c.qwen.recommended_action} compact limit={4} />
              <p className="text-[11px] text-muted">{c.qwen.source === "system2" ? `SPOTbot${c.qwen.latency_ms != null ? ` · ${ms(c.qwen.latency_ms)}` : ""}${c.qwen.tokens_per_sec ? ` · ${c.qwen.tokens_per_sec.toFixed(1)} tok/s` : ""}${c.qwen.confidence_label ? ` · confidence ${c.qwen.confidence_label}` : ""}${c.qwen.image ? ` · image ${c.qwen.image}` : ""}` : "template (checked rules text, not a model answer)"}</p>
            </>}
        </Section>
        {red && <div className="flex flex-wrap items-center gap-3 rounded border border-bad bg-bad/10 p-3">
          <ShieldAlert size={18} className="text-bad" />
          <p className="text-sm font-semibold text-bad">Needs human verification</p>
          {c.flag_id && <button className={`${control} ml-auto min-h-9`} onClick={acknowledge} disabled={ack === "busy" || ack === "done"}>{ack === "done" ? <><ShieldCheck size={15} />Acknowledged</> : ack === "busy" ? "Saving…" : "Acknowledge"}</button>}
          {ack !== "idle" && ack !== "busy" && ack !== "done" && <p className="w-full text-xs text-bad">{ack}</p>}
        </div>}
        <p className="mt-auto font-mono text-[11px] text-faint">
          {mock ? <>Mock patrol · kit row {c.source_action_id} · {c.arrived_at.replace("T", " ").slice(0, 16)}</> : <>
            {c.flag_id ? `flag #${c.flag_id} · ` : ""}{c.event_id ?? ""}{c.doc_id ? ` · ${c.doc_id}` : ""}<br />
            ingest {ms(c.timings?.ingest_ms)} · vision {ms(c.timings?.vision_ms)} · SPOTbot {ms(c.timings?.system2_ms)} · KG +{c.kg?.triage?.nodes?.length ?? 0} nodes
          </>}
        </p>
      </div>
    </div>
  </article>;
}

function Section({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return <section><h3 className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-muted uppercase">{icon}{title}</h3>{children}</section>;
}

