import { Suspense } from "react";
import { connection } from "next/server";
import { LiveConsole } from "@/components/LiveConsole";
import { backendRequest } from "@/lib/backend";
import type { LiveRoute } from "@/lib/live";

export const metadata = { title: "Live patrol · SPOTlight" };

export default function LivePage() {
  return <Suspense fallback={<LiveConsole initialRoutes={[]} />}><WithRoutes /></Suspense>;
}

async function WithRoutes() {
  await connection();   // request time: the routes come from the backend
  let routes: LiveRoute[] = [];
  try { routes = (await backendRequest<{ routes: LiveRoute[] }>("/live/routes", {}, 5000)).routes; }
  catch { /* the console fetches them again in the browser */ }
  return <LiveConsole initialRoutes={routes} />;
}
