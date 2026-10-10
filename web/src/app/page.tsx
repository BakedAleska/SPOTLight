import { Suspense } from "react";
import { connection } from "next/server";
import Dashboard from "@/components/Dashboard";
import { LoadingIndicator } from "@/components/LoadingIndicator";
import { getDashboard } from "@/lib/dashboard-loader";

async function DashboardContent() {
  await connection();
  return <Dashboard initialData={await getDashboard()} />;
}

export default function Page() {
  return <Suspense fallback={<LoadingIndicator label="Loading dashboard…" />}><DashboardContent /></Suspense>;
}
