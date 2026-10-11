import { Suspense } from "react";
import { connection } from "next/server";
import Dashboard from "@/components/Dashboard";
import { LoadingIndicator } from "@/components/LoadingIndicator";
import { getDashboard } from "@/lib/dashboard-loader";

async function ReviewContent() {
  await connection();
  return <Dashboard initialData={await getDashboard()} />;
}

export default function Page() {
  return <><h1 className="px-4 pt-4 text-2xl font-semibold">AI review</h1><Suspense fallback={<LoadingIndicator label="Loading flags…" />}><ReviewContent /></Suspense></>;
}
