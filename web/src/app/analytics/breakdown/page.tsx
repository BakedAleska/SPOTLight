import Link from "next/link";
import { ArrowLeft, Route, Sparkles } from "lucide-react";
import { Panel } from "@/components/Panel";
import { breakdown, parseQuery, type ActionRow, type AnomalyRow, type MissionRow, type ReadingRow } from "@/lib/breakdown";
import { breakdownHref } from "@/lib/breakdown-link";

/* The rows behind one mark on an Analytics chart: click "Visual" on the
   inspection-type chart and this lists every visual inspection, with the same
   numbers the bar was drawn from. Filters arrive as query parameters
   (lib/breakdown-link.ts). */

const TH = "px-3 py-2 text-left text-xs font-normal text-muted";
const TD = "px-3 py-2 align-top";
const LINK = "text-accent-ink hover:underline";

const when = (ts: string) => ts.replace("T", " ").slice(0, 16);
const missionHref = (id: string) => breakdownHref({ view: "actions", mission: id });

export default async function BreakdownPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const data = breakdown(parseQuery(await searchParams));
  const rows = data.actions?.length ?? data.missions?.length ?? data.anomalies?.length ?? data.readings?.length ?? 0;

  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
      <header className="flex flex-col gap-2">
        <Link href="/analytics" className="inline-flex w-fit items-center gap-1.5 text-sm text-muted hover:text-ink">
          <ArrowLeft size={14} aria-hidden="true" /> Analytics
        </Link>
        <h1 className="text-2xl font-semibold text-ink">{data.title}</h1>
        {data.mission && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-muted">Reported {data.mission.reported} · configuration {data.mission.version}</span>
            <Link href={`/missions/replay?mission=${encodeURIComponent(data.mission.id)}`} className="inline-flex h-8 items-center gap-1.5 rounded border border-line bg-surface px-3 text-ink hover:bg-surface-2">
              <Route size={14} aria-hidden="true" /> Replay mission
            </Link>
            <Link href={`/assistant?ask=${encodeURIComponent(`What went wrong in mission ${data.mission.id}, and what should I check first?`)}`}
              className="inline-flex h-8 items-center gap-1.5 rounded border border-line bg-surface px-3 text-ink hover:bg-surface-2">
              <Sparkles size={14} aria-hidden="true" /> Ask SPOTbot
            </Link>
          </div>
        )}
      </header>

      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {data.stats.map((s) => (
          <div key={s.label} className="rounded-panel border border-line bg-surface px-4 py-3">
            <dt className="text-xs text-muted">{s.label}</dt>
            <dd className={`nums mt-1 font-mono text-xl font-semibold ${s.tone === "bad" ? "text-bad" : s.tone === "ok" ? "text-ok" : "text-ink"}`}>{s.value}</dd>
          </div>
        ))}
      </dl>

      <Panel title={data.query.view === "actions" ? "Actions" : data.query.view === "missions" ? "Missions" : data.query.view === "anomalies" ? "Anomalies" : "Readings"}
        aside={`${rows} row${rows === 1 ? "" : "s"}`} bodyClassName="">
        {rows === 0 ? <p className="px-4 py-8 text-center text-sm text-muted">Nothing in the kit matches this selection.</p>
          : <div className="overflow-x-auto">
            {data.actions && <ActionsTable rows={data.actions} highlight={data.query.highlight} />}
            {data.missions && <MissionsTable rows={data.missions} />}
            {data.anomalies && <AnomaliesTable rows={data.anomalies} />}
            {data.readings && <ReadingsTable rows={data.readings} />}
          </div>}
      </Panel>
    </div>
  );
}

function Waypoint({ id }: { id: string }) {
  return <Link href={breakdownHref({ view: "actions", waypoint: id })} className={LINK}>{id}</Link>;
}

function ActionsTable({ rows, highlight }: { rows: ActionRow[]; highlight?: string }) {
  return (
    <table className="w-full min-w-[60rem] text-sm">
      <thead><tr className="border-b border-line">
        <th className={TH}>Action</th><th className={TH}>When</th><th className={TH}>Mission</th><th className={TH}>Type</th>
        <th className={TH}>Asset</th><th className={TH}>Waypoint</th><th className={TH}>Battery</th><th className={TH}>Duration</th>
        <th className={TH}>Result</th><th className={TH}>Reading</th><th className={TH}>Anomaly</th>
      </tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.id} className={`border-b border-line last:border-0 ${r.id === highlight ? "bg-accent/10" : "hover:bg-surface-2"}`}>
          <td className={`${TD} font-mono text-xs text-ink`}>{r.id}</td>
          <td className={`${TD} nums font-mono text-xs text-muted`}>{when(r.when)}</td>
          <td className={TD}><Link href={missionHref(r.mission)} className={`${LINK} font-mono text-xs`}>{r.mission}</Link> <span className="text-xs text-faint">{r.version}</span></td>
          <td className={`${TD} text-ink`}>{r.type}</td>
          <td className={TD}><Link href={`/search?q=${encodeURIComponent(r.asset)}`} className={LINK}>{r.asset}</Link><p className="text-xs text-muted">{r.assetName}</p></td>
          <td className={TD}><Waypoint id={r.waypoint} /><p className="text-xs text-muted">{r.waypointName}</p></td>
          <td className={`${TD} nums font-mono ${r.battery <= 25 ? "text-bad" : "text-ink"}`}>{r.battery}%</td>
          <td className={`${TD} nums font-mono text-muted`}>{r.duration} s</td>
          <td className={TD}>{r.status === "FAILED"
            ? <><span className="font-semibold text-bad">Failed</span><p className="text-xs text-muted">{r.failure}</p></>
            : <span className="text-ok">Success</span>}</td>
          <td className={`${TD} text-muted`}>{r.reading ?? "—"}</td>
          <td className={TD}>{r.anomaly ? <><span className="text-ink">{r.anomaly.type}</span><p className={`text-xs ${r.anomaly.severity === "CRITICAL" || r.anomaly.severity === "HIGH" ? "text-bad" : "text-muted"}`}>{r.anomaly.severity}</p></> : <span className="text-faint">—</span>}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function MissionsTable({ rows }: { rows: MissionRow[] }) {
  return (
    <table className="w-full min-w-[48rem] text-sm">
      <thead><tr className="border-b border-line">
        <th className={TH}>Mission</th><th className={TH}>Started</th><th className={TH}>Config</th><th className={TH}>Reported</th>
        <th className={TH}>Actions</th><th className={TH}>Failed</th><th className={TH}>Anomalies</th><th className={TH}>Battery</th>
      </tr></thead>
      <tbody>{rows.map((m) => (
        <tr key={m.id} className="border-b border-line last:border-0 hover:bg-surface-2">
          <td className={TD}><Link href={missionHref(m.id)} className={`${LINK} font-mono text-xs`}>{m.id}</Link><p className="text-xs text-muted">{m.name}</p></td>
          <td className={`${TD} nums font-mono text-xs text-muted`}>{when(m.start)}</td>
          <td className={`${TD} text-ink`}>{m.version}</td>
          <td className={TD}><span className={m.reported === "COMPLETED" && m.failed > 0 ? "font-semibold text-warn" : m.reported === "COMPLETED" ? "text-ok" : "text-bad"}>{m.reported}</span></td>
          <td className={`${TD} nums font-mono text-ink`}>{m.succeeded + m.failed}</td>
          <td className={`${TD} nums font-mono ${m.failed ? "text-bad" : "text-muted"}`}>{m.failed}</td>
          <td className={`${TD} nums font-mono text-muted`}>{m.anomalies}</td>
          <td className={`${TD} nums font-mono ${m.battery[1] <= 25 ? "text-bad" : "text-muted"}`}>{m.battery[0]}% → {m.battery[1]}%</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function AnomaliesTable({ rows }: { rows: AnomalyRow[] }) {
  return (
    <table className="w-full min-w-[56rem] text-sm">
      <thead><tr className="border-b border-line">
        <th className={TH}>Anomaly</th><th className={TH}>When</th><th className={TH}>Severity</th><th className={TH}>Asset</th>
        <th className={TH}>Waypoint</th><th className={TH}>Mission</th><th className={TH}>Description</th>
      </tr></thead>
      <tbody>{rows.map((x) => (
        <tr key={x.id} className="border-b border-line last:border-0 hover:bg-surface-2">
          <td className={TD}><span className="font-mono text-xs text-ink">{x.id}</span><p className="text-xs text-muted">{x.type}</p></td>
          <td className={`${TD} nums font-mono text-xs text-muted`}>{when(x.when)}</td>
          <td className={TD}><span className={x.severity === "CRITICAL" || x.severity === "HIGH" ? "font-semibold text-bad" : "text-ink"}>{x.severity}</span>{x.review && <p className="text-xs text-warn">Review required</p>}</td>
          <td className={TD}><Link href={`/search?q=${encodeURIComponent(x.asset)}`} className={LINK}>{x.asset}</Link><p className="text-xs text-muted">{x.assetName}</p></td>
          <td className={TD}><Waypoint id={x.waypoint} /><p className="text-xs text-muted">{x.waypointName}</p></td>
          <td className={TD}><Link href={breakdownHref({ view: "actions", mission: x.mission, highlight: x.action })} className={`${LINK} font-mono text-xs`}>{x.mission}</Link></td>
          <td className={`${TD} max-w-md text-muted`}>{x.description}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function ReadingsTable({ rows }: { rows: ReadingRow[] }) {
  return (
    <table className="w-full min-w-[48rem] text-sm">
      <thead><tr className="border-b border-line">
        <th className={TH}>Reading</th><th className={TH}>When</th><th className={TH}>Measurement</th><th className={TH}>Value</th>
        <th className={TH}>Quality</th><th className={TH}>Asset</th><th className={TH}>Waypoint</th><th className={TH}>Mission</th>
      </tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.id} className="border-b border-line last:border-0 hover:bg-surface-2">
          <td className={`${TD} font-mono text-xs text-ink`}>{r.id}</td>
          <td className={`${TD} nums font-mono text-xs text-muted`}>{when(r.when)}</td>
          <td className={`${TD} text-ink`}>{r.measurement}</td>
          <td className={`${TD} nums font-mono text-ink`}>{r.value} {r.unit}</td>
          <td className={TD}><span className={r.quality === "QUESTIONABLE" ? "font-semibold text-warn" : "text-ok"}>{r.quality}</span></td>
          <td className={TD}><Link href={`/search?q=${encodeURIComponent(r.asset)}`} className={LINK}>{r.asset}</Link></td>
          <td className={TD}><Waypoint id={r.waypoint} /><p className="text-xs text-muted">{r.waypointName}</p></td>
          <td className={TD}><Link href={breakdownHref({ view: "actions", mission: r.mission, highlight: r.action })} className={`${LINK} font-mono text-xs`}>{r.mission}</Link></td>
        </tr>
      ))}</tbody>
    </table>
  );
}
