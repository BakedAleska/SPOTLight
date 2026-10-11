import { Suspense } from "react";
import { connection } from "next/server";
import { ActionSuccessChart } from "@/components/ActionSuccessChart";
import { LiveModelBoard } from "@/components/LiveModelBoard";
import { getDashboard } from "@/lib/dashboard-loader";

/* The fleet-wide success chart lives here rather than on the Overview, which
   keeps to the banner and the queue so an operator at shift start reads one
   question and one list. */
async function FleetSuccess() {
  await connection();
  const { actionSuccess } = await getDashboard();
  return <ActionSuccessChart groups={actionSuccess.groups} caption={actionSuccess.caption} benchmark={actionSuccess.fleetRate} />;
}

export default function Analytics() {
  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
      <h1 className="text-2xl font-semibold text-ink">Analytics</h1>
      <Suspense fallback={null}><FleetSuccess /></Suspense>
      <LiveModelBoard allowRun />
    </div>
  );
}
