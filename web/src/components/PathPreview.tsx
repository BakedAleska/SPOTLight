"use client";

import { useEffect, useState } from "react";
import { Pause, Play, RotateCcw, Radio, Route, TriangleAlert } from "lucide-react";
import { Panel } from "./Panel";
import type { PathEvent, PathFeed, PathMission, PathPoint } from "@/lib/path-types";

const control = "flex min-h-11 items-center justify-center gap-2 rounded border border-line bg-surface px-3 text-sm text-ink hover:bg-surface-2 disabled:opacity-40";
export function PathPreview({ points, missions }: { points: PathPoint[]; missions: PathMission[] }) {
  const [missionId, setMissionId] = useState(missions[0]?.id ?? "");
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
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [liveEvents, setLiveEvents] = useState<PathEvent[]>([]);
  const [connection, setConnection] = useState("Connecting…");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  useEffect(() => {
    if (!playing || mode !== "replay" || index >= mission.events.length - 1) return;
    const timer = setInterval(() => setIndex(i => Math.min(i + 1, mission.events.length - 1)), 1400 / speed);
    return () => clearInterval(timer);
  }, [playing, mode, speed, mission.events.length, index]);
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
  const current = visited.at(-1);
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
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
      <Panel title="Facility route" aside={mode === "live" ? "LIVE FEED" : "RECORDED REPLAY"}>
        <div className="mb-4 flex flex-wrap gap-6 text-sm text-muted"><span>Position <strong className="text-ink">{current?.id ?? "Awaiting waypoint"}</strong></span><span>Battery <strong className="text-ink">{battery == null ? "—" : `${battery}%`}</strong></span><span>Errors <strong className="text-bad">{errors.length}</strong></span></div>
        <svg viewBox="0 0 1095 595" className="w-full rounded border border-line bg-surface-2" role="group" aria-label="Facility map with mission route, waypoint buttons and SPOT position">
          <image href="/kit/fictional_floor_plan.png" width="1095" height="595" />
          {mode === "replay" && <polyline points={path(planned)} fill="none" stroke="white" strokeWidth="3" strokeDasharray="8 8" opacity="0.65" />}
          <polyline points={path(visited)} fill="none" stroke="var(--c-accent-ink)" strokeWidth="5" strokeLinejoin="round" />
          {points.map(p => {
            const fault = errors.some(e => e.locationId === p.id);
            return <g key={p.id} role="button" tabIndex={0} aria-label={`${p.id}: ${p.name}${fault ? ", error detected" : ""}`} onClick={() => setSelectedId(p.id)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSelectedId(p.id); } }} className="cursor-pointer">
              <title>{`${p.id} · ${p.name}`}</title><circle cx={p.x} cy={p.y} r="22" fill="transparent" />
              <circle cx={p.x} cy={p.y} r={selected?.id === p.id ? 12 : 7} fill={fault ? "var(--c-bad)" : "var(--c-surface)"} stroke={fault ? "white" : "var(--c-accent-ink)"} strokeWidth="3" />
              {fault && <text x={p.x} y={p.y + 5} textAnchor="middle" fill="white" fontSize="15" fontWeight="bold">!</text>}
            </g>;
          })}
          {current && <g aria-label={`SPOT at ${current.id}`} style={{ transform: `translate(${current.x}px, ${current.y}px)`, transition: playing && mode === "replay" ? `transform ${1000 / speed}ms linear` : undefined }}>
            <circle r="26" fill="var(--c-surface)" stroke="var(--c-accent-ink)" strokeWidth="3" />
            <image href="/spotlight-logo-small.svg" x="-18" y="-20" width="36" height="40" />
            <text y="42" textAnchor="middle" fill="white" stroke="black" strokeWidth="3" paintOrder="stroke" fontSize="15" fontWeight="bold">SPOT</text>
          </g>}
        </svg>
        <p className="mt-3 text-xs text-muted">Dashed: planned route · Solid: observed waypoints · !: error · Logo: SPOT. Lines connect recorded stops; travel between them is not measured.</p>
        {mode === "replay" && <div className="mt-4 flex flex-col gap-3 border-t border-line pt-4">
          <label className="text-xs text-muted">Replay position · event {index + 1} of {mission.events.length}<input className="mt-2 w-full accent-accent-ink" type="range" min="0" max={Math.max(0, mission.events.length - 1)} value={index} onChange={e => setIndex(Number(e.target.value))} /></label>
          <div className="flex flex-wrap items-center gap-2"><button className={control} onClick={() => {
            if (playing && index < mission.events.length - 1) { setPlaying(false); return; }
            setIndex(i => i >= mission.events.length - 1 ? 0 : i + 1);
            setPlaying(true);
          }}>{playing && index < mission.events.length - 1 ? <Pause size={16} /> : <Play size={16} />}{playing && index < mission.events.length - 1 ? "Pause" : "Play"}</button><button className={control} onClick={() => { setIndex(0); setPlaying(false); }}><RotateCcw size={16} />Restart</button><label className="flex items-center gap-2 text-xs text-muted">Speed<select className={control} value={speed} onChange={e => setSpeed(Number(e.target.value))}>{[1, 2, 4].map(s => <option key={s} value={s}>{s}×</option>)}</select></label><span className="ml-auto font-mono text-xs text-muted">{last?.timestamp.replace("T", " · ") ?? "Awaiting events"}</span></div>
        </div>}
      </Panel>
      <div className="flex min-w-0 flex-col gap-4">
        <Panel title="Selected waypoint"><p className="text-lg font-semibold text-ink">{selected?.id ?? "No position yet"}</p><p className="mt-1 text-sm text-muted">{selected?.name ?? "Waiting for a located action."}</p>{selected && <p className="mt-3 text-xs text-muted">Floor {selected.floor} · {errors.filter(e => e.locationId === selected.id).length} observed errors</p>}</Panel>
        <Panel title="Errors along the path" aside={String(errors.length)}><div className="flex flex-col gap-3" aria-live="polite">{errors.length === 0 ? <p className="text-sm text-muted">No errors observed at this point in the mission.</p> : errors.map(e => <button key={e.id} className="rounded border border-line bg-surface-2 p-3 text-left hover:border-bad" onClick={() => setSelectedId(e.locationId ?? null)}><p className="flex items-center gap-2 text-sm font-medium text-bad"><TriangleAlert size={16} />{e.locationId ?? "Unlocated"} · {e.severity ?? "FAILED"}</p><p className="mt-2 text-sm text-ink">{e.error?.replaceAll("_", " ")}</p><p className="mt-2 font-mono text-xs text-muted">{e.timestamp.split("T")[1]} · {e.actionId}</p></button>)}</div></Panel>
        <Panel title="Event stream"><ol className="flex flex-col gap-3">{events.slice(-6).reverse().map(e => <li key={e.id} className="border-l-2 border-line pl-3"><p className="text-xs font-medium text-ink">{e.type.replaceAll("_", " ")}</p><p className="mt-1 font-mono text-xs text-muted">{e.timestamp.split("T")[1]} {e.locationId && `· ${e.locationId}`}</p></li>)}</ol>{!events.length && <p className="text-sm text-muted">Waiting for mission events.</p>}</Panel>
      </div>
    </div>
  </>;
}
