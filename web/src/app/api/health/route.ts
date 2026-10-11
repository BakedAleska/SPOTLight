import { NextResponse } from "next/server";

/* Liveness for the container's HEALTHCHECK and for compose's `depends_on`.
   A static response on purpose, twice over: it answers instantly, and it does
   NOT probe the backend. A console whose health check fails when a dependency
   is down goes offline exactly when its degraded mode is the thing you need —
   the backend's own `/health` is where dependency status belongs. */
export async function GET() {
  return NextResponse.json({ ok: true, service: "web" });
}
