import { getKit } from "@/lib/kit";
import { getDashboard } from "@/lib/dashboard-loader";

export type SearchKind = "Mission" | "Location" | "Asset" | "Flag";

export interface SearchResult {
  kind: SearchKind;
  id: string;
  /** The human name, which is also what the search box autofills toward. */
  name: string;
  href: string;
}

/** Lower is better: exact ID, then a prefix of the ID or name, then a prefix
    of any word in the name, then a plain substring anywhere. -1 is no match. */
function rank(q: string, id: string, name: string, extra = ""): number {
  const i = id.toLowerCase();
  const n = name.toLowerCase();
  if (i === q) return 0;
  if (i.startsWith(q) || n.startsWith(q)) return 1;
  if (n.split(/[\s·\-_/]+/).some((word) => word.startsWith(q))) return 2;
  if (`${i} ${n} ${extra.toLowerCase()}`.includes(q)) return 3;
  return -1;
}

/** One search over the kit's missions, waypoints and assets and the dashboard's flags,
    shared by the /search page and the top-bar suggestions so the two never
    disagree about what matches. An empty query matches nothing. */
export async function search(query: string, limit = Infinity): Promise<SearchResult[]> {
  const q = query.trim().toLowerCase();
  if (!q) return [];

  const kit = getKit();
  const data = await getDashboard();

  // `extra` is searched but never shown: a flag is found by its asset or
  // mission as well as by its own title.
  const candidates: (SearchResult & { extra?: string })[] = [
    ...kit.missions.map((m) => ({
      kind: "Mission" as const,
      id: m.mission_id,
      name: m.mission_name,
      href: `/missions/replay?mission=${encodeURIComponent(m.mission_id)}`,
    })),
    // Waypoints have no deep link of their own yet, so they open the replay
    // map, where every one of them is drawn.
    ...kit.locations.map((l) => ({
      kind: "Location" as const,
      id: l.location_id,
      name: l.location_name,
      href: "/missions/replay",
    })),
    ...kit.assets.map((a) => ({
      kind: "Asset" as const,
      id: a.asset_id,
      name: a.asset_name,
      href: "/assets",
    })),
    ...data.flags.map((f) => ({
      kind: "Flag" as const,
      id: f.id,
      name: f.title,
      extra: `${f.assetName} ${f.missionId}`,
      href: data.features.flagDetails ? `/?flag=${encodeURIComponent(f.id)}` : "/#queue",
    })),
  ];

  return candidates
    .map(({ extra, ...result }, order) => ({ result, order, score: rank(q, result.id, result.name, extra) }))
    .filter((c) => c.score >= 0)
    // Stable on the original order within a rank, so missions/assets/flags
    // keep their natural grouping when they tie.
    .sort((a, b) => a.score - b.score || a.order - b.order)
    .slice(0, limit)
    .map((c) => c.result);
}
