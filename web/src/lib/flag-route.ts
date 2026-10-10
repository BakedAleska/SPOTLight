import { BackendError } from "./backend";

export function validFlagId(id: string) {
  return /^[1-9]\d*$/.test(id) && Number.isSafeInteger(Number(id));
}

export function flagFailure(error: unknown) {
  console.error("Flag request failed", error);
  const status = error instanceof BackendError && error.status === 404 ? 404 : 502;
  return Response.json({ error: status === 404 ? "This flag no longer exists." : "Cannot confirm the request. Refresh the flag before trying again." }, { status });
}
