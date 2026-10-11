"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { Pause, Play, RotateCcw, Route, TriangleAlert, ScanLine, CheckCircle2, ArrowRight, AudioLines, ArrowUp, ArrowDown, Timer, BatteryMedium } from "lucide-react";
import { Panel } from "./Panel";
import { LoadingDots } from "./LoadingDots";
import { RouteAhead } from "./RouteAhead";
import type { PathMission, PathPoint } from "@/lib/path-types";

import { buildPlayback, playbackAt, eventLabel } from "@/lib/path-playback";
import { legsPath } from "@/lib/route-geometry";

const issueColor: Record<string, string> = { CRITICAL: "var(--c-bad)", HIGH: "var(--c-warn)", MEDIUM: "var(--c-info)", LOW: "var(--c-faint)" };
const control = "flex min-h-11 items-center justify-center gap-2 rounded border border-line bg-surface px-3 text-sm text-ink hover:bg-surface-2 disabled:opacity-40";
export function PathPreview({ points, missions, initialMissionId }: { points: PathPoint[]; missions: PathMission[]; initialMissionId?: string }) {
  const [missionId, setMissionId] = useState(missions.find(m => m.id === initialMissionId)?.id ?? missions[0]?.id ?? "");
  return <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
    {/* Same columns as the map and side panels below, so the Mission picker lines up with them. */}
    <header className="grid items-end gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
      <div><p className="mb-1 text-xs text-muted">MISSION CONTROL</p><h1 className="flex items-center gap-2 text-2xl font-semibold text-ink"><Route size={24} />SPOT path preview</h1><p className="mt-1 text-sm text-muted">Replay a recorded challenge-kit mission. For a running robot, open <Link href="/live" className="text-accent-ink hover:underline">Live patrol</Link>.</p></div>
      <label className="flex flex-col gap-1 text-xs text-muted">Mission<select className={`${control} w-full`} value={missionId} onChange={e => setMissionId(e.target.value)}>{missions.map(m => <option key={m.id} value={m.id}>{m.id} · {m.name}</option>)}</select></label>
    </header>
    {missions.find(m => m.id === missionId) && <MissionPath key={missionId} mission={missions.find(m => m.id === missionId)!} points={points} />}
  </div>;
}
function MissionPath({ mission, points }: { mission: PathMission; points: PathPoint[] }) {
  const [elapsed, setElapsed] = useState(0);
  const segments = useMemo(() => buildPlayback(mission, points), [mission, points]);
  // Whole ms: segment ends are float sums that differ in the last digit between engines, breaking hydration.
  const duration = Math.ceil(segments.at(-1)?.end ?? 0);
  const frame = playbackAt(segments, elapsed);
  const index = frame?.eventIndex ?? 0;
  const finished = elapsed >= duration;
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  /** Rewind (paused) to the moment event `i` happens. Travel toward it is filed under the previous event, so this is
      the first stationary segment for `i`. At least 1ms in, so the first event keeps the timeline open. */
  function seekTo(i: number) {
    const at = segments.find(s => s.eventIndex === i && !s.moving);
    if (!at) return;
    setPlaying(false);
    setElapsed(Math.max(1, at.start));
  }
  useEffect(() => {
    if (!playing) return;
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
  }, [playing, speed, duration]);
  const timeline = useRef<HTMLOListElement>(null);
  // The replay timeline stays empty until Play is first pressed, so the panel
  // opens on the mission's first event rather than starting out pre-filled.
  // Restarting (elapsed back to 0, paused) empties it again.
  const started = playing || elapsed > 0;
  const events = mission.events.slice(0, index + 1);
  const timelineOpen = started && events.length > 0;
  // Completion records can omit location; retain the action's last known waypoint.
  const located = events.map(e => ({ ...e, locationId: e.locationId ?? events.find(start => start.actionId && start.actionId === e.actionId && start.locationId)?.locationId }));
  const current = frame?.moving ? frame.from : frame?.to;
  const changingFloor = !!frame?.moving && frame.from?.floor !== frame.to?.floor;
  const floorUp = (frame?.to?.floor ?? 0) > (frame?.from?.floor ?? 0);
  const floorLabel = `Floor ${frame?.from?.floor} → Floor ${frame?.to?.floor}`;
  const marker = frame ? { x: frame.x, y: frame.y } : current;
  const active = playing && !finished;
  // On the stairs SPOT fades out at the floor-1 stair, then fades back in on the floor-2 landing (or the reverse).
  const climbFade = frame?.leg?.kind === "climb" ? Math.abs(1 - 2 * frame.progress) : 1;
  const activity = changingFloor ? `${floorUp ? "Going up" : "Going down"} · ${floorLabel}` : frame?.activity ?? "Ready";
  const errors = located.filter(e => e.error);
  // Unless one is picked, show where SPOT is headed while it travels, and where it is while it works.
  const selected = points.find(p => p.id === selectedId) ?? (frame?.moving ? frame.destination : current);
  const battery = events.filter(e => e.battery != null).at(-1)?.battery;
  const last = events.at(-1);
  // Only the trip underway, or the next one while SPOT is stopped, is drawn ahead of it.
  const nextTravel = segments.find(s => s.leg && s.end > elapsed)?.leg?.travel;
  const upcoming = segments.filter(s => s.leg && s.end > elapsed && s.leg.travel === nextTravel);
  // Mid-walk, the route ahead starts at SPOT rather than redrawing what it has already covered.
  const ahead = legsPath(upcoming.map(s => s.leg!), { from: upcoming[0] && upcoming[0].start <= elapsed ? frame?.progress : 0 });
  // Walked so far: every finished leg, plus the one underway cut at SPOT's position.
  const underway = frame?.leg && elapsed < frame.end ? frame.leg : undefined;
  const walked = legsPath([...segments.filter(s => s.leg && s.end <= elapsed).map(s => s.leg!), ...(underway ? [underway] : [])], { to: underway ? frame?.progress : 1 });
  useEffect(() => { const list = timeline.current; if (list) list.scrollTo({ left: list.scrollWidth, behavior: "smooth" }); }, [events.length]);
  return <>
    <div className="panel-columns grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
      <Panel title="Facility route" aside="RECORDED REPLAY">
        <div className="mb-4 flex flex-wrap gap-6 text-sm text-muted"><span>Position <strong className="text-ink">{current?.id ?? "Awaiting waypoint"}</strong></span><span>Battery <strong className="text-ink">{battery == null ? "—" : `${battery}%`}</strong></span><span>Errors <strong className="text-bad">{errors.length}</strong></span></div>
        <svg viewBox="0 0 1095 595" className="w-full rounded border border-line bg-surface-2" role="group" aria-label="Facility map with mission route, waypoint buttons and SPOT position">
          <defs><filter id="spot-marker-contrast" x="-25%" y="-25%" width="150%" height="150%"><feComponentTransfer><feFuncA type="linear" slope="2" /></feComponentTransfer><feDropShadow dx="0" dy="1" stdDeviation="1.5" floodColor="black" floodOpacity="0.9" /></filter></defs>
          <image href="/spot-blueprint.svg" width="1095" height="595" />
          <path d={walked} fill="none" stroke="var(--c-accent-ink)" strokeWidth="5" strokeLinejoin="round" strokeLinecap="round" />
          <RouteAhead d={ahead} />
          {points.map(p => {
            const fault = errors.some(e => e.locationId === p.id);
            return <g key={p.id} role="button" tabIndex={0} aria-label={`${p.id}: ${p.name}${fault ? ", error detected" : ""}`} onClick={() => setSelectedId(p.id)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSelectedId(p.id); } }} className="cursor-pointer">
              <title>{`${p.id} · ${p.name}`}</title><circle cx={p.x} cy={p.y} r="22" fill="transparent" />
              {/* The blueprint's beacons mark every waypoint; only faults get a marker of their own. */}
              {fault && (p.id !== current?.id || frame?.moving) && <circle cx={p.x} cy={p.y} r="7" fill="var(--c-bad)" stroke="white" strokeWidth={selected?.id === p.id ? 4 : 3} />}
              {fault && (p.id !== current?.id || frame?.moving) && <text x={p.x} y={p.y + 5} textAnchor="middle" fill="white" fontSize="15" fontWeight="bold">!</text>}
            </g>;
          })}
          {current && marker && <g aria-label={`SPOT at ${current.id}`} transform={`translate(${marker.x} ${marker.y})`} opacity={climbFade}>
            {active && !frame?.moving && last?.type === "ACTION_STARTED" && <foreignObject x="-15" y="-31" width="30" height="12" transform="scale(2)" aria-label="SPOT working"><div className="spot-working-dots flex h-full items-center justify-center"><LoadingDots color="white" /></div></foreignObject>}
            {last?.error && !frame?.moving && <g aria-label={`Issue detected: ${last.severity ?? "FAILED"}`} className={active ? "spot-issue spot-issue-flash" : "spot-issue"}>
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
        <p className="mt-3 text-xs text-muted">Dashed: route to the next stop · Solid: visited route · !: error. Travel follows the hallways and stairs between stops; activity durations are compressed from the logs.</p>
        <div className="mt-4 flex flex-col gap-3 border-t border-line pt-4">
          <label className="text-xs text-muted">Replay position · event {index + 1} of {mission.events.length}<input className="mt-2 w-full accent-accent-ink" type="range" min="0" max={duration} value={elapsed} onChange={e => setElapsed(Number(e.target.value))} /></label>
          <div className="flex flex-wrap items-center gap-2"><button className={control} onClick={() => {
            if (playing && !finished) { setPlaying(false); return; }
            if (finished) setElapsed(0);
            setPlaying(true);
          }}>{active ? <Pause size={16} /> : <Play size={16} />}{active ? "Pause" : "Play"}</button><button className={control} onClick={() => { setElapsed(0); setPlaying(false); }}><RotateCcw size={16} />Restart</button><label className="flex items-center gap-2 text-xs text-muted">Speed<select className={control} value={speed} onChange={e => setSpeed(Number(e.target.value))}>{[1, 2, 4].map(s => <option key={s} value={s}>{s}×</option>)}</select></label><span className="ml-auto font-mono text-xs text-muted">{last?.timestamp.replace("T", " · ") ?? "Awaiting events"}</span></div>
        </div>
      </Panel>
      <div className="panel-stack flex min-h-0 min-w-0 flex-col gap-4">
        <Panel title="SPOT activity" aside={active ? "IN PROGRESS" : finished ? "COMPLETE" : "PAUSED"}>
          <div className="flex items-center gap-3"><div className={`grid size-12 shrink-0 place-items-center rounded-full border border-line bg-surface-2 text-accent-ink ${active && !frame?.moving ? "spot-activity-icon" : ""}`}>
            {changingFloor ? floorUp ? <ArrowUp size={24} /> : <ArrowDown size={24} /> : frame?.moving ? <ArrowRight size={24} /> : activity.includes("Listening") ? <AudioLines size={24} /> : finished ? <CheckCircle2 size={24} /> : <ScanLine size={24} />}
          </div><div><p className="text-sm font-semibold text-ink">{activity}</p><p className="mt-1 text-xs text-muted">{frame?.moving ? `${frame.from?.id} → ${frame.to?.id}` : last?.assetName ?? current?.name ?? "Preparing mission"}</p></div></div>
          <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-surface-2"><div className="h-full bg-accent-ink" style={{ width: `${(frame?.progress ?? 0) * 100}%` }} /></div>
        </Panel>
        <Panel title="Selected waypoint"><p className="text-lg font-semibold text-ink">{selected?.id ?? "No position yet"}</p><p className="mt-1 text-sm text-muted">{selected?.name ?? "Waiting for a located action."}</p>{selected && <p className="mt-3 text-xs text-muted">Floor {selected.floor} · {errors.filter(e => e.locationId === selected.id).length} observed errors</p>}</Panel>
        <Panel title="Errors along the path" className="max-h-80 xl:max-h-none" bodyClassName="relative overflow-y-auto p-4 xl:min-h-40 xl:overflow-hidden xl:p-0" aside={String(errors.length)}>{/* On desktop the list is lifted out of flow so it adds no height of its own: the panel fills whatever the map leaves in the column and scrolls inside that. */}<div className="flex flex-col gap-3 xl:absolute xl:inset-0 xl:overflow-y-auto xl:p-4" aria-live="polite">{errors.length === 0 ? <p className="text-sm text-muted">No errors observed at this point in the mission.</p> : errors.map(e => <button key={e.id} className="rounded border border-line bg-surface-2 p-3 text-left hover:border-bad" onClick={() => setSelectedId(e.locationId ?? null)}><p className="flex items-center gap-2 text-sm font-medium text-bad"><TriangleAlert size={16} />{e.locationId ?? "Unlocated"} · {e.severity ?? "FAILED"}</p><p className="mt-2 text-sm text-ink">{e.error?.replaceAll("_", " ")}</p><p className="mt-2 font-mono text-xs text-muted">{e.timestamp.split("T")[1]} · {e.actionId}</p></button>)}</div></Panel>
      </div>
    </div>
    <Panel title="Mission timeline" aside={started ? `${events.length} event${events.length === 1 ? "" : "s"}` : "Not started"}>
      {/* The tiles are fixed-size, so the open height is known up front
          (a 144px tile + 8px scrollbar room) and height can transition
          directly — one line for the prompt, growing to one row of tiles. */}
      <div className="relative overflow-hidden transition-[height] duration-700 ease-in-out" style={{ height: timelineOpen ? 152 : 20 }}>
      <p className={`absolute inset-x-0 top-0 text-xs text-muted transition-opacity duration-300 ${timelineOpen ? "pointer-events-none opacity-0" : "opacity-100"}`}>{started ? "Waiting for mission events." : "Press play to start the mission timeline."}</p>
      <ol ref={timeline} className={`flex snap-x gap-2.5 overflow-x-auto pb-2 transition-opacity duration-500 ${timelineOpen ? "opacity-100 delay-200" : "opacity-0"}`}>{(started ? events : []).map((e, i) => { const latest = i === events.length - 1; const place = e.locationId ? `${e.locationId}${e.assetName ? ` · ${e.assetName}` : ""}` : `Mission ${e.missionId}`; return <li key={e.id} className="aspect-square w-36 shrink-0 snap-end">
        {/* A fixed-size tile with a fixed skeleton — header, title, place, footer —
            so a sparse event and a busy one read as the same shape. */}
        <button onClick={() => { seekTo(i); setSelectedId(e.locationId ?? null); }} title={`Rewind to: ${eventLabel(e)} · ${place}`} className={`flex h-full w-full flex-col rounded border p-2.5 text-left transition-colors hover:bg-surface-2 ${latest ? "border-accent-ink bg-surface-2" : e.error ? "border-bad/40" : "border-line"}`}>
          <span className="flex items-center justify-between gap-2">
            <span className={e.error ? "text-bad" : "text-accent-ink"}>{e.error ? <TriangleAlert size={15} /> : e.type === "ACTION_STARTED" ? <ScanLine size={15} /> : <CheckCircle2 size={15} />}</span>
            {latest ? <span className="rounded bg-canvas px-1.5 py-0.5 text-[9px] font-semibold tracking-wide text-accent-ink">LATEST</span> : <span className="nums font-mono text-[10px] text-faint">#{i + 1}</span>}
          </span>
          <span className="mt-2 line-clamp-2 text-[13px] leading-tight font-medium text-ink">{eventLabel(e)}</span>
          <span className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted">{place}</span>
          {e.error && <span className="mt-1 truncate text-[10px] font-medium tracking-wide text-bad uppercase">{e.error.replaceAll("_", " ")}</span>}
          <span className="nums mt-auto flex items-center justify-between gap-2 border-t border-line pt-1.5 font-mono text-[10px] text-muted">
            <span>{e.timestamp.split("T")[1]}</span>
            <span className="flex items-center gap-2">
              {e.durationSec && e.type === "ACTION_STARTED" ? <span className="flex items-center gap-0.5"><Timer size={10} aria-hidden="true" />{e.durationSec}s</span> : null}
              {e.battery != null && <span className="flex items-center gap-0.5"><BatteryMedium size={11} aria-hidden="true" />{e.battery}%</span>}
            </span>
          </span>
        </button>
      </li>; })}</ol>
      </div>
    </Panel>
  </>;
}
