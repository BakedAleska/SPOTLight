import { backendRequest } from "@/lib/backend";
import { dashboardFeatures } from "@/lib/features";
import { flagFailure, validFlagId } from "@/lib/flag-route";
import type { FlagDetailData } from "@/lib/flag-detail";

export async function GET(_request: Request, context: RouteContext<"/api/flags/[id]">) {
  if (!dashboardFeatures().flagDetails) return Response.json({ error: "Flag details are disabled." }, { status: 404 });
  const { id } = await context.params;
  if (!validFlagId(id)) return Response.json({ error: "Invalid flag." }, { status: 400 });
  try {
    return Response.json(await backendRequest<FlagDetailData>(`/flags/${id}`), { headers: { "Cache-Control": "no-store" } });
  } catch (error) { return flagFailure(error); }
}
