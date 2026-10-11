import Dashboard from "@/components/Dashboard";
import { getDashboard } from "@/lib/dashboard-loader";
export default async function Page() { return <><h1 className="px-4 pt-4 text-2xl font-semibold">AI review</h1><p className="px-4 pt-2 text-sm text-muted">Select a live flag to request its explanation, review evidence, and record an operator action.</p><Dashboard initialData={await getDashboard()} /></>; }
