import { Suspense } from "react";
import { connection } from "next/server";
import Dashboard from "@/components/Dashboard";
import { LoadingIndicator } from "@/components/LoadingIndicator";
import { getDashboard } from "@/lib/dashboard-loader";
import { overviewAnalytics } from "@/lib/overview-analytics";

/* The charts beside the queue are computed here, on the server, from the
   same kit as /analytics; only the blocks they draw cross to the client. */
async function DashboardContent() {
  await connection();
  return <Dashboard initialData={await getDashboard()} analytics={overviewAnalytics()} />;
}

export default function Page() {
  return <Suspense fallback={<LoadingIndicator label="Loading dashboard…" />}><DashboardContent /></Suspense>;
}
