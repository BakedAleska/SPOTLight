"use client";
import { useEffect, useRef, useState } from "react";
import { Play, Square, Sparkles } from "lucide-react";
import { useSWRConfig } from "swr";
import type { JsonRow } from "@/lib/model-stream";

export function AIRunControls({ mission }: { mission?: string }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("Run the supplied kit events through Laya, then request explanations for flagged results.");
  const [error, setError] = useState("");
  const [progress, setProgress] = useState(0);
  const stopped = useRef(false);
  const identity = useRef<{ run: number; startedAt: string } | null>(null);
  const { mutate } = useSWRConfig();
  useEffect(() => () => { stopped.current = true; }, []);
  const refresh = () => { void mutate(key => typeof key === "string" && (key.startsWith("/api/model-stream") || key === "/api/dashboard")); };
  async function call(action: string, fields: JsonRow = {}): Promise<JsonRow> {
    const response = await fetch("/api/ai-run", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...(mission ? { mission } : {}), ...identity.current, ...fields }), signal: AbortSignal.timeout(60000) });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error ?? "AI run failed.");
    return body;
  }
  async function start() {
    if (busy) return;
    stopped.current = false;identity.current = null;
    setBusy(true);setError("");setProgress(0);setMessage("Starting the kit data run…");
    try {
      let status = await call("start");
      if (!Number.isSafeInteger(status.run) || typeof status.started_at !== "string") throw new Error("Backend returned no run identity. Check replay status before trying again.");
      identity.current = { run: Number(status.run), startedAt: status.started_at };
      while (status.state === "running" && !stopped.current) {
        setMessage(`Processing events · ${status.sent ?? 0} / ${status.total ?? "—"} · ${status.laya_unavailable ?? 0} Laya fallbacks`);
        setProgress(Number(status.total) ? 70 * Number(status.sent) / Number(status.total) : 0);refresh();
        await new Promise(resolve => setTimeout(resolve, 1000));
        if (!stopped.current) status = await call("next");
      }
      if (stopped.current) return;
      if (status.state !== "finished") throw new Error(`Run ${String(status.state)}. ${String(status.error ?? "")}`);
      const flags = await call("flags");
      const ids = Array.isArray(flags.flagIds) ? flags.flagIds : [];
      let fallbacks = 0, failed = 0;
      for (let i = 0; i < ids.length && !stopped.current; i++) {
        setMessage(`Explaining flagged results · ${i + 1} / ${ids.length}`);
        try { const result = await call("explain", { flagId: ids[i] }); if (result.source !== "system2") fallbacks++; }
        catch { failed++; }
        setProgress(70 + 30 * (i + 1) / ids.length);refresh();
      }
      if (!stopped.current) { setProgress(100);setMessage(`Run complete · ${status.sent ?? 0} events · ${ids.length - failed} explanations · ${status.laya_unavailable ?? 0} Laya fallbacks · ${fallbacks} explanation fallbacks${failed ? ` · ${failed} explanations unavailable` : ""}${flags.explanationsEnabled === false ? " · explanations disabled" : ""}.`); }
      refresh();
    } catch (cause) { if (!stopped.current) setError(cause instanceof Error ? cause.message : "AI run unavailable."); }
    finally { setBusy(false); }
  }
  async function stop() {
    if (!identity.current) { setMessage("Waiting for the backend to assign a run before it can be stopped."); return; }
    stopped.current = true;
    try { if (identity.current) await call("stop");setMessage("Stop requested. The current request may finish; produced results remain available."); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Cannot confirm the stop."); }
    refresh();
  }
  return <div className="rounded-panel border border-line bg-surface p-4">
    <div className="flex flex-wrap items-center gap-3"><Sparkles size={18} className="text-accent-ink" /><div className="min-w-0 flex-1"><p className="text-sm font-semibold text-ink">Run an AI analysis</p><p className="mt-1 text-xs text-muted">{mission ? `Mission ${mission}` : "All supplied kit missions"} · Processing starts only when you click Run.</p></div>
      <button type="button" onClick={() => void start()} disabled={busy} className="flex min-h-11 items-center gap-2 rounded bg-accent px-4 text-sm font-medium text-on-accent disabled:opacity-50"><Play size={16} />{busy ? "AI run in progress…" : "Run data through AI"}</button>
      {busy && <button type="button" onClick={() => void stop()} className="flex min-h-11 items-center gap-2 rounded border border-line px-3 text-sm text-ink"><Square size={14} />Stop</button>}
    </div>
    <p className="mt-3 text-xs text-muted" role="status">{message}</p>
    {busy && <progress aria-label="AI analysis progress" className="mt-3 h-2 w-full accent-accent-ink" max="100" value={progress} />}
    {error && <p role="alert" className="mt-3 text-sm text-bad">{error}</p>}
  </div>;
}
