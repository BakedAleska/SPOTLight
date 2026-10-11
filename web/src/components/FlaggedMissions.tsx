import Link from "next/link";
import { getKit } from "@/lib/kit";
import type { Severity } from "@/lib/types";
import { replayHref } from "@/lib/chat/mission";
import { AskAiLink } from "./AskAiLink";
import { SeverityBadge, severityAccent } from "./SeverityBadge";

const rank: Record<Severity, number> = { CRITICAL: 3, HIGH: 2, MEDIUM: 1, LOW: 0 };

function tally(values: string[]) {
  const counts = new Map<string, number>();
  for (const v of values) if (v) counts.set(v, (counts.get(v) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1]);
}

const humanize = (s: string) => s.toLowerCase().replaceAll("_", " ");

/* The triage view of the mission list: only missions with a failed action or
   an anomaly, worst first, each card saying *why* it is here. "All missions"
   answers "what ran"; this page answers "what needs a look". */
export function FlaggedMissions() {
  const kit = getKit();

  const rows = kit.missions
    .map((m) => {
      const failed = (kit.actionsByMission.get(m.mission_id) ?? []).filter((a) => a.status === "FAILED");
      const anomalies = kit.anomalies.filter((a) => a.mission_id === m.mission_id);
      const worst = anomalies.reduce<(typeof anomalies)[number] | undefined>(
        (w, a) => (!w || rank[a.severity] > rank[w.severity] ? a : w),
        undefined,
      );
      return {
        m,
        failed,
        anomalies,
        worst,
        review: anomalies.filter((a) => a.operator_review_required).length,
        // Reported COMPLETED with failures inside — the case the status column hides.
        hidden: m.mission_status === "COMPLETED" && failed.length > 0,
      };
    })
    .filter((r) => r.failed.length || r.anomalies.length)
    .sort(
      (a, b) =>
        (b.worst ? rank[b.worst.severity] : -1) - (a.worst ? rank[a.worst.severity] : -1) ||
        b.review - a.review ||
        b.failed.length + b.anomalies.length - (a.failed.length + a.anomalies.length),
    );

  const summary = [
    { label: "Flagged", value: `${rows.length}`, note: `of ${kit.missions.length} missions` },
    { label: "Critical", value: `${rows.filter((r) => r.worst?.severity === "CRITICAL").length}`, note: "worst anomaly is critical" },
    { label: "Hidden failures", value: `${rows.filter((r) => r.hidden).length}`, note: "reported completed, actions failed" },
    { label: "Awaiting review", value: `${rows.reduce((n, r) => n + r.review, 0)}`, note: "anomalies needing an operator" },
  ];

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4">
      <h1 className="text-2xl font-semibold">Flagged missions</h1>
      <p className="text-sm text-muted">
        Missions with a failed action or a detected anomaly, worst first. Each card shows why it was flagged.
      </p>

      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {summary.map((s) => (
          <div key={s.label} className="rounded border border-line bg-surface p-3">
            <dt className="text-xs text-muted">{s.label}</dt>
            <dd className="text-2xl font-semibold tabular-nums">{s.value}</dd>
            <dd className="text-xs text-faint">{s.note}</dd>
          </div>
        ))}
      </dl>

      <div className="grid gap-3 sm:grid-cols-2">
        {rows.map(({ m, failed, anomalies, worst, review, hidden }) => (
          // Two sibling actions: the replay link is stretched over the card
          // (::after), and "Ask SPOTbot" sits above it. No nested links.
          <article
            key={m.mission_id}
            className="relative overflow-hidden rounded border border-line bg-surface p-4 pl-5 transition-colors hover:border-accent has-[a:focus-visible]:border-accent"
          >
            <span
              aria-hidden="true"
              className={`absolute inset-y-0 left-0 w-1 ${worst ? severityAccent(worst.severity) : "bg-line-strong"}`}
            />
            <div className="flex items-start justify-between gap-3">
              <h2 className="font-medium">
                {m.mission_id} · {m.mission_name}
              </h2>
              {worst ? <SeverityBadge severity={worst.severity} /> : <span className="text-xs font-semibold text-muted">NO ANOMALY</span>}
            </div>

            <p className="mt-2 text-sm text-muted">
              <span className="tabular-nums">{failed.length}</span> failed action{failed.length === 1 ? "" : "s"} ·{" "}
              <span className="tabular-nums">{anomalies.length}</span> anomal{anomalies.length === 1 ? "y" : "ies"}
              {review > 0 && <> · <span className="text-warn">{review} need review</span></>}
            </p>

            {hidden && (
              <p className="mt-2 text-sm text-bad">
                Reported {m.mission_status.toLowerCase()}, but {failed.length} action{failed.length === 1 ? "" : "s"} failed.
              </p>
            )}

            {worst && (
              <p className="mt-2 text-sm">
                <span className="font-medium">{humanize(worst.anomaly_type)}</span> on{" "}
                {kit.assetById.get(worst.asset_id)?.asset_name ?? worst.asset_id}
                <span className="text-muted"> · {kit.locationById.get(worst.location_id)?.location_name ?? worst.location_id}</span>
              </p>
            )}

            {failed.length > 0 && (
              <ul className="mt-3 flex flex-wrap gap-1.5">
                {tally(failed.map((a) => a.failure_type)).map(([type, n]) => (
                  <li key={type} className="rounded bg-surface-2 px-1.5 py-0.5 text-xs text-muted">
                    {humanize(type)}{n > 1 && ` ×${n}`}
                  </li>
                ))}
              </ul>
            )}

            <div className="mt-3 flex items-center justify-between gap-3">
              <Link
                href={replayHref(m.mission_id)}
                aria-label={`Investigate ${m.mission_id} · ${m.mission_name} in replay`}
                className="text-sm text-accent-ink after:absolute after:inset-0 after:content-['']"
              >
                Investigate in replay →
              </Link>
              <AskAiLink missionId={m.mission_id} />
            </div>
          </article>
        ))}
      </div>
      {!rows.length && <p>No flagged missions.</p>}
    </div>
  );
}
