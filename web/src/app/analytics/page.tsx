import { Suspense } from "react";
import { connection } from "next/server";
import { ActionSuccessChart } from "@/components/ActionSuccessChart";
import { AnalyticsCharts } from "@/components/AnalyticsCharts";
import { getDashboard } from "@/lib/dashboard-loader";
import { fleetAnalytics } from "@/lib/fleet-analytics";

/* Kept here rather than in AnalyticsCharts: that module is a Client
   Component, and a plain array exported from one arrives on the server as a
   client reference, not as data. */
const SECTIONS = [
  { id: "reliability", label: "Reliability" },
  { id: "configuration", label: "Configuration" },
  { id: "missions", label: "Missions" },
  { id: "failures", label: "Failures & anomalies" },
  { id: "waypoints", label: "Waypoints" },
];

/* The fleet-wide success chart lives here rather than on the Overview, which
   keeps to the banner and the queue so an operator at shift start reads one
   question and one list. */
async function FleetSuccess() {
  await connection();
  const { actionSuccess } = await getDashboard();
  return <ActionSuccessChart groups={actionSuccess.groups} caption={actionSuccess.caption} benchmark={actionSuccess.fleetRate} collapsible />;
}

export default function Analytics() {
  const data = fleetAnalytics();
  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-6 p-4">
      <header className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline gap-x-3">
          <h1 className="text-2xl font-semibold text-ink">Analytics</h1>
          <p className="nums text-xs text-muted">Challenge kit · {data.coverage}</p>
        </div>
        <p className="text-xs text-muted">Click any bar, slice, point or cell to see the actions, missions, anomalies or readings behind it.</p>
        <nav aria-label="Analytics categories" className="flex flex-wrap gap-2">
          {SECTIONS.map((section) => (
            <a
              key={section.id}
              href={`#${section.id}`}
              className="rounded-full border border-line bg-surface px-3 py-1 text-xs text-muted transition-colors hover:border-line-strong hover:text-ink"
            >
              {section.label}
            </a>
          ))}
        </nav>
      </header>
      <Suspense fallback={null}><FleetSuccess /></Suspense>
      <AnalyticsCharts data={data} />
    </div>
  );
}
