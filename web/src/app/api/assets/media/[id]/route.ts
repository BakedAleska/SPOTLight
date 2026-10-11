import { backendUrl } from "@/lib/backend";
import { validMediaId } from "@/lib/media";

/* One stored inspection image (the Assets page gallery): the backend's GET /assets/media/{id} bytes, passed through
   so the browser never talks to the backend directly. Immutable per id. */
export async function GET(_request: Request, context: RouteContext<"/api/assets/media/[id]">) {
  const { id } = await context.params;
  if (!validMediaId(id)) return Response.json({ error: "Invalid media id." }, { status: 400 });
  try {
    const response = await fetch(backendUrl(`/assets/media/${encodeURIComponent(id)}`), { signal: AbortSignal.timeout(15_000) });
    if (!response.ok) return Response.json({ error: "No such image." }, { status: response.status === 404 ? 404 : 502 });
    return new Response(await response.arrayBuffer(), {
      headers: { "Content-Type": response.headers.get("content-type") ?? "image/jpeg", "Cache-Control": "public, max-age=86400" },
    });
  } catch {
    return Response.json({ error: "The backend is unavailable." }, { status: 502 });
  }
}
