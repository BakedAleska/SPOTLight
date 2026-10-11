"use client";

import { useEffect, useMemo, useState } from "react";
import { Pause, Play, RotateCcw, Radio, Route, TriangleAlert, ScanLine, CheckCircle2, ArrowRight, AudioLines, ArrowUp, ArrowDown } from "lucide-react";
import { Panel } from "./Panel";
import { LoadingDots } from "./LoadingDots";
import type { PathEvent, PathFeed, PathMission, PathPoint } from "@/lib/path-types";

import { buildPlayback, playbackAt, eventLabel } from "@/lib/path-playback";

const issueColor: Record<string, string> = { CRITICAL: "var(--c-bad)", HIGH: "var(--c-warn)", MEDIUM: "var(--c-info)", LOW: "var(--c-faint)" };
const control = "flex min-h-11 items-center justify-center gap-2 rounded border border-line bg-surface px-3 text-sm text-ink hover:bg-surface-2 disabled:opacity-40";
export function PathPreview({ points, missions, initialMissionId }: { points: PathPoint[]; missions: PathMission[]; initialMissionId?: string }) {
  const [missionId, setMissionId] = useState(missions.find(m => m.id === initialMissionId)?.id ?? missions[0]?.id ?? "");
  return <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
    <header className="flex flex-wrap items-center justify-between gap-4">
      <div><p className="mb-1 text-xs text-muted">MISSION CONTROL</p><h1 className="flex items-center gap-2 text-2xl font-semibold text-ink"><Route size={24} />SPOT path preview</h1><p className="mt-1 text-sm text-muted">Follow the route. Catch the failure where it happens.</p></div>
      <label className="flex flex-col gap-1 text-xs text-muted">Mission<select className={control} value={missionId} onChange={e => setMissionId(e.target.value)}>{missions.map(m => <option key={m.id} value={m.id}>{m.id} · {m.name}</option>)}</select></label>
    </header>
    {missions.find(m => m.id === missionId) && <MissionPath key={missionId} mission={missions.find(m => m.id === missionId)!} points={points} />}
  </div>;
}
function MissionPath({ mission, points }: { mission: PathMission; points: PathPoint[] }) {
  const [mode, setMode] = useState<"replay" | "live">("replay");
  const [elapsed, setElapsed] = useState(0);
  const segments = useMemo(() => buildPlayback(mission, points), [mission, points]);
  const duration = segments.at(-1)?.end ?? 0;
  const frame = playbackAt(segments, elapsed);
  const index = frame?.eventIndex ?? 0;
  const finished = elapsed >= duration;
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [liveEvents, setLiveEvents] = useState<PathEvent[]>([]);
  const [connection, setConnection] = useState("Connecting…");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  useEffect(() => {
    if (!playing || mode !== "replay") return;
    let request: number;
    let previous = performance.now();
    function tick(now: number) {
      const delta = Math.min(now - previous, 100) * speed;
      previous = now;
      setElapsed(value => Math.min(duration, value + delta));
      request = requestAnimationFrame(tick);
    }
    request = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(request);
  }, [playing, mode, speed, duration]);
  useEffect(() => {
    if (mode !== "live") return;
    let stopped = false;
    let cursor: string | null = null;
    let polling = false;
    const controller = new AbortController();
    async function poll() {
      if (polling) return;
      polling = true;
      try {
        const query = new URLSearchParams({ mission_id: mission.id });
        if (cursor) query.set("after", cursor);
        const response = await fetch(`/api/path-feed?${query}`, { signal: controller.signal });
        if (!response.ok) throw new Error();
        const feed: PathFeed = await response.json();
        if (stopped) return;
        cursor = feed.nextCursor;
        setLiveEvents(previous => [...new Map([...previous, ...feed.items].map(e => [e.id, e])).values()]);
        setConnection("Connected · polling every 2s");
      } catch { if (!stopped) setConnection("Disconnected · retrying every 2s"); }
      finally { polling = false; }
    }
    void poll();
    const timer = setInterval(() => { void poll(); }, 2000);
    return () => { stopped = true; controller.abort(); clearInterval(timer); };
  }, [mode, mission.id]);
  const events = mode === "replay" ? mission.events.slice(0, index + 1) : liveEvents;
  // Completion records can omit location; retain the action's last known waypoint.
  const located = events.map(e => ({ ...e, locationId: e.locationId ?? events.find(start => start.actionId && start.actionId === e.actionId && start.locationId)?.locationId }));
  const visited = located.flatMap(e => { const p = points.find(p => p.id === e.locationId); return p ? [p] : []; }).filter((p, i, list) => i === 0 || list[i - 1].id !== p.id);
  const current = mode === "replay" ? (frame?.moving ? frame.from : frame?.to) : visited.at(-1);
  const changingFloor = mode === "replay" && !!frame?.moving && frame.from?.floor !== frame.to?.floor;
  const floorUp = (frame?.to?.floor ?? 0) > (frame?.from?.floor ?? 0);
  const floorLabel = `Floor ${frame?.from?.floor} → Floor ${frame?.to?.floor}`;
  const marker = mode === "replay" && frame ? { x: frame.x, y: frame.y } : current;
  const active = mode === "replay" && playing && !finished;
  const activity = changingFloor ? `${floorUp ? "Going up" : "Going down"} · ${floorLabel}` : mode === "replay" ? frame?.activity ?? "Ready" : "Latest observed position";
  const errors = located.filter(e => e.error);
  const selected = points.find(p => p.id === selectedId) ?? current;
  const battery = events.filter(e => e.battery != null).at(-1)?.battery;
  const last = events.at(-1);
  const planned = mission.events.filter(e => e.type === "ACTION_STARTED").flatMap(e => { const p = points.find(p => p.id === e.locationId); return p ? [p] : []; });
  const path = (list: PathPoint[]) => list.map(p => `${p.x},${p.y}`).join(" ");
  return <>
    <div className="flex flex-wrap items-center gap-2 rounded-panel border border-line bg-surface p-3">
      <button className={control} aria-pressed={mode === "replay"} onClick={() => setMode("replay")}>Kit replay</button>
      <button className={control} aria-pressed={mode === "live"} onClick={() => setMode("live")}><Radio size={16} />Live feed</button>
      <p className="ml-auto text-xs text-muted" role="status">{mode === "live" ? connection : "Recorded challenge-kit events · no robot connected"}</p>
    </div>
    <div className="panel-columns grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
      <Panel title="Facility route" aside={mode === "live" ? "LIVE FEED" : "RECORDED REPLAY"}>
        <div className="mb-4 flex flex-wrap gap-6 text-sm text-muted"><span>Position <strong className="text-ink">{current?.id ?? "Awaiting waypoint"}</strong></span><span>Battery <strong className="text-ink">{battery == null ? "—" : `${battery}%`}</strong></span><span>Errors <strong className="text-bad">{errors.length}</strong></span></div>
        <svg viewBox="0 0 1095 595" className="w-full rounded border border-line bg-surface-2" role="group" aria-label="Facility map with mission route, waypoint buttons and SPOT position">
          <defs><filter id="spot-marker-contrast" x="-25%" y="-25%" width="150%" height="150%"><feComponentTransfer><feFuncA type="linear" slope="2" /></feComponentTransfer><feDropShadow dx="0" dy="1" stdDeviation="1.5" floodColor="black" floodOpacity="0.9" /></filter></defs>
          <image href="/kit/fictional_floor_plan.png" width="1095" height="595" />
          {mode === "replay" && <polyline points={path(planned)} fill="none" stroke="white" strokeWidth="3" strokeDasharray="8 8" opacity="0.65" />}
          <polyline points={path(visited)} fill="none" stroke="var(--c-accent-ink)" strokeWidth="5" strokeLinejoin="round" />
          {mode === "replay" && frame?.moving && <line x1={frame.from?.x} y1={frame.from?.y} x2={frame.x} y2={frame.y} stroke="var(--c-accent-ink)" strokeWidth="5" strokeDasharray={changingFloor ? "8 6" : undefined} />}
          {points.map(p => {
            const fault = errors.some(e => e.locationId === p.id);
            return <g key={p.id} role="button" tabIndex={0} aria-label={`${p.id}: ${p.name}${fault ? ", error detected" : ""}`} onClick={() => setSelectedId(p.id)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSelectedId(p.id); } }} className="cursor-pointer">
              <title>{`${p.id} · ${p.name}`}</title><circle cx={p.x} cy={p.y} r="22" fill="transparent" />
              {(p.id !== current?.id || frame?.moving) && <circle cx={p.x} cy={p.y} r="7" fill={fault ? "var(--c-bad)" : "var(--c-surface)"} stroke={fault ? "white" : "var(--c-accent-ink)"} strokeWidth={selected?.id === p.id ? 4 : 3} />}
              {fault && (p.id !== current?.id || frame?.moving) && <text x={p.x} y={p.y + 5} textAnchor="middle" fill="white" fontSize="15" fontWeight="bold">!</text>}
            </g>;
          })}
          {current && marker && <g aria-label={`SPOT at ${current.id}`} transform={`translate(${marker.x} ${marker.y})`}>
            {((active && !frame?.moving && last?.type === "ACTION_STARTED") || (mode === "live" && last?.type === "ACTION_STARTED")) && <foreignObject x="-15" y="-31" width="30" height="12" transform="scale(2)" aria-label="SPOT working"><div className="spot-working-dots flex h-full items-center justify-center"><LoadingDots color="white" /></div></foreignObject>}
            {last?.error && !frame?.moving && <g aria-label={`Issue detected: ${last.severity ?? "FAILED"}`} className={active || mode === "live" ? "spot-issue spot-issue-flash" : "spot-issue"}>
              <path d="M 0 -64 L 14 -39 L -14 -39 Z" fill={issueColor[last.severity ?? ""] ?? "var(--c-bad)"} stroke="white" strokeWidth="2.5" strokeLinejoin="round" />
              <path d="M 0 -57 V -49" stroke="white" strokeWidth="3.5" strokeLinecap="round" /><circle cy="-43.5" r="2" fill="white" />
            </g>}
            {changingFloor && <g aria-label={`Changing floor: ${floorLabel}`} className={active ? "spot-floor-arrow" : undefined} style={{ "--floor-direction": floorUp ? "-1" : "1" } as React.CSSProperties}>
              <path d={floorUp ? "M 0 -45 V -73 M -12 -61 L 0 -73 L 12 -61" : "M 0 -73 V -45 M -12 -57 L 0 -45 L 12 -57"} fill="none" stroke="white" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
            </g>}
            <image href="/spotlight-logo-small.svg" x="-30" y="-34" width="60" height="68" filter="url(#spot-marker-contrast)" />
            <text y="65" textAnchor="middle" fill="white" stroke="black" strokeWidth="3" paintOrder="stroke" fontSize="15" fontWeight="bold">SPOT</text>
          </g>}
        </svg>
        <p className="mt-3 text-xs text-muted">Dashed: planned route · Solid: visited route · !: error. Travel is interpolated between stops; activity durations are compressed from the logs.</p>
        {mode === "replay" && <div className="mt-4 flex flex-col gap-3 border-t border-line pt-4">
          <label className="text-xs text-muted">Replay position · event {index + 1} of {mission.events.length}<input className="mt-2 w-full accent-accent-ink" type="range" min="0" max={duration} value={elapsed} onChange={e => setElapsed(Number(e.target.value))} /></label>
          <div className="flex flex-wrap items-center gap-2"><button className={control} onClick={() => {
            if (playing && !finished) { setPlaying(false); return; }
            if (finished) setElapsed(0);
            setPlaying(true);
          }}>{active ? <Pause size={16} /> : <Play size={16} />}{active ? "Pause" : "Play"}</button><button className={control} onClick={() => { setElapsed(0); setPlaying(false); }}><RotateCcw size={16} />Restart</button><label className="flex items-center gap-2 text-xs text-muted">Speed<select className={control} value={speed} onChange={e => setSpeed(Number(e.target.value))}>{[1, 2, 4].map(s => <option key={s} value={s}>{s}×</option>)}</select></label><span className="ml-auto font-mono text-xs text-muted">{last?.timestamp.replace("T", " · ") ?? "Awaiting events"}</span></div>
        </div>}
      </Panel>
      <div className="panel-stack flex min-h-0 min-w-0 flex-col gap-4">
        <Panel title="SPOT activity" aside={active ? "IN PROGRESS" : finished ? "COMPLETE" : "PAUSED"}>
          <div className="flex items-center gap-3"><div className={`grid size-12 shrink-0 place-items-center rounded-full border border-line bg-surface-2 text-accent-ink ${active && !frame?.moving ? "spot-activity-icon" : ""}`}>
            {changingFloor ? floorUp ? <ArrowUp size={24} /> : <ArrowDown size={24} /> : frame?.moving ? <ArrowRight size={24} /> : activity.includes("Listening") ? <AudioLines size={24} /> : finished ? <CheckCircle2 size={24} /> : <ScanLine size={24} />}
          </div><div><p className="text-sm font-semibold text-ink">{activity}</p><p className="mt-1 text-xs text-muted">{frame?.moving ? `${frame.from?.id} → ${frame.to?.id}` : last?.assetName ?? current?.name ?? "Preparing mission"}</p></div></div>
          {mode === "replay" && <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-surface-2"><div className="h-full bg-accent-ink" style={{ width: `${(frame?.progress ?? 0) * 100}%` }} /></div>}
        </Panel>
        <Panel title="Selected waypoint"><p className="text-lg font-semibold text-ink">{selected?.id ?? "No position yet"}</p><p className="mt-1 text-sm text-muted">{selected?.name ?? "Waiting for a located action."}</p>{selected && <p className="mt-3 text-xs text-muted">Floor {selected.floor} · {errors.filter(e => e.locationId === selected.id).length} observed errors</p>}</Panel>
        <Panel title="Errors along the path" className="max-h-80" bodyClassName="overflow-y-auto p-4" aside={String(errors.length)}><div className="flex flex-col gap-3" aria-live="polite">{errors.length === 0 ? <p className="text-sm text-muted">No errors observed at this point in the mission.</p> : errors.map(e => <button key={e.id} className="rounded border border-line bg-surface-2 p-3 text-left hover:border-bad" onClick={() => setSelectedId(e.locationId ?? null)}><p className="flex items-center gap-2 text-sm font-medium text-bad"><TriangleAlert size={16} />{e.locationId ?? "Unlocated"} · {e.severity ?? "FAILED"}</p><p className="mt-2 text-sm text-ink">{e.error?.replaceAll("_", " ")}</p><p className="mt-2 font-mono text-xs text-muted">{e.timestamp.split("T")[1]} · {e.actionId}</p></button>)}</div></Panel>
        <Panel title="Mission timeline" aside={`${events.length} events`}>
          <ol className="flex max-h-[32rem] flex-col gap-1 overflow-y-auto">{events.slice().reverse().map((e, i) => <li key={e.id}>
            <button onClick={() => setSelectedId(e.locationId ?? null)} className={`flex w-full gap-3 rounded p-3 text-left transition-colors hover:bg-surface-2 ${i === 0 ? "border border-line bg-surface-2" : ""}`}>
              <span className={`mt-0.5 shrink-0 ${e.error ? "text-bad" : "text-accent-ink"}`}>{e.error ? <TriangleAlert size={18} /> : e.type === "ACTION_STARTED" ? <ScanLine size={18} /> : <CheckCircle2 size={18} />}</span>
              <span className="min-w-0"><span className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink">{eventLabel(e)}{i === 0 && <span className="rounded bg-canvas px-1.5 py-0.5 text-[10px] text-accent-ink">LATEST</span>}</span>
              <span className="mt-1 block text-xs text-muted">{e.locationId ?? "Mission"}{e.assetName ? ` · ${e.assetName}` : ""}</span>
              {e.error && <span className="mt-1 block text-xs text-bad">{e.error.replaceAll("_", " ")}</span>}
              <span className="mt-2 block font-mono text-xs text-muted">{e.timestamp.split("T")[1]}{e.durationSec && e.type === "ACTION_STARTED" ? ` · ${e.durationSec}s recorded` : ""}</span></span>
            </button>
          </li>)}</ol>{!events.length && <p className="text-sm text-muted">Waiting for mission events.</p>}
        </Panel>
      </div>
    </div>
  </>;
}
