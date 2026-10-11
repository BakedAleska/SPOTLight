import { connection } from "next/server";

import { search } from "@/lib/search";

/** Top-bar suggestions. Same matcher as the /search page, capped short. */
export async function GET(request: Request) {
  await connection();
  const q = new URL(request.url).searchParams.get("q") ?? "";
  try {
    return Response.json({ results: await search(q, 8) }, { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    console.error("Search suggestions failed", error);
    return Response.json({ results: [] }, { status: 502, headers: { "Cache-Control": "no-store" } });
  }
}
