import Link from "next/link";
import { getKit } from "@/lib/kit";
import { getDashboard } from "@/lib/dashboard-loader";
export default async function Page({ searchParams }: { searchParams: Promise<{ q?: string }> }) {
 const raw = (await searchParams).q;
 const q = typeof raw === "string" ? raw.trim() : "";
 const kit = getKit(); const data = await getDashboard();
 const matches = (text: string) => !!q && text.toLowerCase().includes(q.toLowerCase());
 const results = [
 ...kit.missions.filter(m => matches(`${m.mission_id} ${m.mission_name}`)).map(m => ({ label: `Mission ${m.mission_id} · ${m.mission_name}`, href: `/missions/replay?mission=${encodeURIComponent(m.mission_id)}` })),
 ...kit.assets.filter(a => matches(`${a.asset_id} ${a.asset_name}`)).map(a => ({ label: `Asset ${a.asset_id} · ${a.asset_name}`, href: "/assets" })),
 ...data.flags.filter(f => matches(`${f.id} ${f.title} ${f.assetName} ${f.missionId}`)).map(f => ({ label: `Flag ${f.id} · ${f.title}`, href: data.features.flagDetails ? `/?flag=${encodeURIComponent(f.id)}` : "/#queue" }))];
 return <div className="mx-auto max-w-4xl space-y-4 p-4"><h1 className="text-2xl font-semibold">Search</h1><form className="flex gap-2" action="/search"><input className="min-w-0 flex-1 rounded border border-line bg-surface p-3" name="q" aria-label="Search" defaultValue={q} /><button className="rounded bg-accent px-4 text-on-accent">Search</button></form><p role="status">{q ? `${results.length} results for “${q}”` : "Enter a mission, asset, or flag name or ID."}</p><ul className="space-y-2">{results.map(r => <li key={r.label}><Link className="block rounded border border-line bg-surface p-3 hover:border-accent" href={r.href}>{r.label} →</Link></li>)}</ul></div>;
}
