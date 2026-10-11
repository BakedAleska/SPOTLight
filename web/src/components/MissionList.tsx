import Link from "next/link";
import { getKit } from "@/lib/kit";
import { replayHref } from "@/lib/chat/mission";
import { AskAiLink } from "./AskAiLink";

/* Each card has two sibling actions: the replay link, stretched over the
   whole card with ::after so a click anywhere still opens the replay, and
   "Ask SPOTbot", raised above that overlay. No <a> inside an <a>. */
export function MissionList({ query = "" }: { query?: string }) {
  const kit = getKit();
  const missions = kit.missions.filter(m => `${m.mission_id} ${m.mission_name}`.toLowerCase().includes(query.toLowerCase()));
  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4">
      <h1 className="text-2xl font-semibold">All missions</h1>
      <p className="text-sm text-muted">Challenge kit records · {missions.length} missions. Open a mission to replay its route and inspect failures, or ask the AI about it.</p>
      <div className="grid gap-3 sm:grid-cols-2">
        {missions.map(m => (
          <article key={m.mission_id} className="relative rounded border border-line bg-surface p-4 transition-colors hover:border-accent has-[a:focus-visible]:border-accent">
            <h2 className="font-medium">{m.mission_id} · {m.mission_name}</h2>
            <p className="mt-2 text-sm text-muted">{m.mission_status} · {m.duration_min} min · battery {m.end_battery_pct}%</p>
            <div className="mt-3 flex items-center justify-between gap-3">
              <Link
                href={replayHref(m.mission_id)}
                aria-label={`Open replay of ${m.mission_id} · ${m.mission_name}`}
                className="text-sm text-accent-ink after:absolute after:inset-0 after:rounded after:content-['']"
              >
                Open replay →
              </Link>
              <AskAiLink missionId={m.mission_id} />
            </div>
          </article>
        ))}
      </div>
      {!missions.length && <p>No matching missions.</p>}
    </div>
  );
}
