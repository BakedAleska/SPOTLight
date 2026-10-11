import { Panel } from "./Panel";
import { loadMedia, mediaSrc, type InspectionType, type MediaItem } from "@/lib/media";

/* The database's inspection images (raw.inspection_media with stored bytes), grouped by inspection type: what a
   capture of each asset looks like, its finding and where the image came from. The demo set's asset details are
   mock and each card says so, with the image's source and licence. */

const SECTIONS: { type: InspectionType; title: string }[] = [
  { type: "thermal", title: "Thermal inspections" },
  { type: "gauge", title: "Gauge readings" },
  { type: "acoustic", title: "Acoustic inspections" },
  { type: "visual", title: "Visual inspections" },
];

function captured(at: string | null) {
  if (!at) return "Capture time unknown";
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? at : d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function Card({ item }: { item: MediaItem }) {
  const m = item.metadata;
  const anomaly = m.condition === "anomaly";
  return (
    <li className="flex min-w-0 flex-col overflow-hidden rounded-panel border border-line bg-surface-2">
      {/* eslint-disable-next-line @next/next/no-img-element -- bytes come from our own API route, sized by CSS */}
      <img src={mediaSrc(item.media_id)} alt={`${m.inspection_type ?? "Inspection"} capture of ${m.asset_name ?? m.asset_id ?? "an asset"}`}
        loading="lazy" className="aspect-[4/3] w-full bg-canvas object-contain" />
      <div className="flex min-w-0 flex-1 flex-col gap-1 p-3 text-sm">
        <div className="flex items-start justify-between gap-2">
          <p className="min-w-0 font-medium text-ink">{m.asset_name ?? m.asset_id}<span className="ml-1.5 font-mono text-[11px] text-muted">{m.asset_id}</span></p>
          {m.condition && <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] font-semibold ${anomaly ? "bg-bad/10 text-bad" : "bg-ok/10 text-ok"}`}>
            {anomaly ? "Anomaly" : "Normal"}</span>}
        </div>
        <p className="text-xs text-muted">{m.location_name ?? "Unknown waypoint"}{m.location_id ? ` (${m.location_id})` : ""} · {captured(item.captured_at)}</p>
        {m.finding && <p className="text-ink">{m.finding}</p>}
        {m.reading && <p className="text-xs text-muted">Reading <strong className="text-ink">{m.reading.value} {m.reading.unit}</strong></p>}
        <p className="mt-auto pt-1 text-[11px] leading-snug text-faint">
          {m.mock && "Demo image, mock asset details · "}
          {m.source?.url ? <a href={m.source.url} target="_blank" rel="noreferrer" className="underline">{m.source.dataset}</a> : m.source?.dataset}
          {m.source?.licence ? ` · ${m.source.licence}` : ""}{m.source?.credit ? ` · ${m.source.credit}` : ""}
        </p>
      </div>
    </li>
  );
}

export async function MediaGallery() {
  const items = await loadMedia();
  if (!items?.length) return null;
  return (
    <div className="flex flex-col gap-4">
      {SECTIONS.map(({ type, title }) => {
        const rows = items.filter((item) => item.metadata.inspection_type === type);
        if (!rows.length) return null;
        const anomalies = rows.filter((item) => item.metadata.condition === "anomaly").length;
        return (
          <Panel key={type} title={title} aside={<span className="text-xs text-muted">{rows.length} captures · {anomalies} anomalies</span>} collapsible>
            <ul className="grid grid-cols-[repeat(auto-fill,minmax(14rem,1fr))] gap-3">
              {rows.map((item) => <Card key={item.media_id} item={item} />)}
            </ul>
          </Panel>
        );
      })}
    </div>
  );
}
