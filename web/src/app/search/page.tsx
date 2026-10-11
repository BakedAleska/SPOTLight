import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { search } from "@/lib/search";
export default async function Page({ searchParams }: { searchParams: Promise<{ q?: string }> }) {
 const raw = (await searchParams).q;
 const q = typeof raw === "string" ? raw.trim() : "";
 const results = (await search(q)).map(r => ({ label: `${r.kind} ${r.id} · ${r.name}`, href: r.href }));
 return <div className="mx-auto max-w-4xl space-y-4 p-4"><h1 className="text-2xl font-semibold">Search</h1><form className="flex gap-2" action="/search"><input className="min-w-0 flex-1 rounded border border-line bg-surface p-3" name="q" aria-label="Search" defaultValue={q} /><button className="rounded bg-accent px-4 text-on-accent">Search</button></form><p role="status">{q ? `${results.length} results for “${q}”` : "Enter a mission, asset, or flag name or ID."}</p><ul className="space-y-2">{results.map(r => <li key={r.label}><Link className="flex items-center justify-between gap-2 rounded border border-line bg-surface p-3 hover:border-accent" href={r.href}>{r.label} <ArrowRight size={16} aria-hidden="true" className="shrink-0 text-muted" /></Link></li>)}</ul></div>;
}
