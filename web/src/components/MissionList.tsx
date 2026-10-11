import Link from "next/link";
import { getKit } from "@/lib/kit";
export function MissionList({ flagged = false, query = "" }: { flagged?: boolean; query?: string }) {
  const kit = getKit();
  const missions = kit.missions.filter(m => (!flagged || kit.actionsByMission.get(m.mission_id)?.some(a => a.status === "FAILED") || kit.anomalies.some(a => a.mission_id === m.mission_id)) && `${m.mission_id} ${m.mission_name}`.toLowerCase().includes(query.toLowerCase()));
  return <div className="mx-auto max-w-6xl space-y-4 p-4"><h1 className="text-2xl font-semibold">{flagged ? "Flagged missions" : "All missions"}</h1><p className="text-sm text-muted">Challenge kit records · {missions.length} missions. Open a mission to replay its route and inspect failures.</p><div className="grid gap-3 sm:grid-cols-2">{missions.map(m => <Link className="rounded border border-line bg-surface p-4 hover:border-accent" key={m.mission_id} href={`/missions/replay?mission=${encodeURIComponent(m.mission_id)}`}><h2 className="font-medium">{m.mission_id} · {m.mission_name}</h2><p className="mt-2 text-sm text-muted">{m.mission_status} · {m.duration_min} min · battery {m.end_battery_pct}%</p><p className="mt-2 text-sm text-accent">Open replay →</p></Link>)}</div>{!missions.length && <p>No matching missions.</p>}</div>;
}
