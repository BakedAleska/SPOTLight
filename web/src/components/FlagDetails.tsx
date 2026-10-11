"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { LoadingIndicator } from "./LoadingIndicator";
import { LoadingDots } from "./LoadingDots";
import { QwenDraftView } from "./QwenDraft";
import { ModelExplanation } from "./ModelOutput";
import type { Flag } from "@/lib/types";
import type { DashboardData } from "@/lib/dashboard";
import { explanationLabel, type FlagAction, type FlagDetailData, type FlagExplanation } from "@/lib/flag-detail";
import { loadExplanation, stageLabel, type ExplainProgress } from "@/lib/explanation-stream";
import { displayText, displayJson } from "@/lib/display";

const BUTTON = "rounded border border-line px-3 py-2 text-sm hover:bg-surface-2 disabled:opacity-50";

async function read<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(55_000) });
  if (!response.ok) {
    const body = await response.json();
    throw new Error(body.error || "Cannot load this flag.");
  }
  return response.json() as Promise<T>;
}

/* The explanation streams (lib/explanation-stream.ts): Qwen's unverified draft
   while it writes, then the checked answer; any stream failure falls back to
   the plain JSON route ("fallback"). */
type ExplainView =
  | { status: "idle" }
  | ({ status: "streaming" } & ExplainProgress)
  | { status: "fallback" }
  | { status: "done"; data: FlagExplanation }
  | { status: "error"; message: string };

export function FlagDetails({ flag, features, onClose, onChanged }: {
  flag: Flag;
  features: DashboardData["features"];
  onClose: () => void;
  onChanged: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [note, setNote] = useState("");
  const [operator, setOperator] = useState("operator");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const [explain, setExplain] = useState<ExplainView>({ status: "idle" });
  const explainRun = useRef<AbortController | null>(null);
  const { data, error, isValidating, mutate } = useSWR<FlagDetailData>(`/api/flags/${flag.id}`, read, { revalidateOnFocus: true, refreshInterval: 2500 });
  const explaining = explain.status === "streaming" || explain.status === "fallback";

  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal();
  }, []);

  // Closing the dialog unmounts it: stop the stream (and the backend relay) with it.
  useEffect(() => () => explainRun.current?.abort(), []);

  function startExplanation() {
    explainRun.current?.abort();
    const run = new AbortController();
    explainRun.current = run;
    const live = () => !run.signal.aborted;
    setExplain({ status: "streaming", stage: null, draft: null });
    loadExplanation(flag.id, run.signal, {
      onProgress: (progress) => { if (live()) setExplain({ status: "streaming", ...progress }); },
      onFallback: () => { if (live()) setExplain({ status: "fallback" }); },
    }).then(
      (result) => { if (live()) setExplain({ status: "done", data: result }); },
      (cause: unknown) => { if (live()) setExplain({ status: "error", message: cause instanceof Error ? cause.message : "Cannot load the explanation." }); },
    );
  }

  async function update(action: FlagAction) {
    if (saving) return;
    setSaving(true);
    setActionError("");
    setMessage("");
    try {
      const response = await fetch(`/api/flags/${flag.id}/actions`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, note, operator }),
        signal: AbortSignal.timeout(35_000),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not update the flag.");
      // Keep the detail open after it leaves the open queue, so Reopen is available.
      if (data) await mutate({ ...data, flag: { ...data.flag, status: result.flag_status }, acks: [...data.acks, result.ack] }, { revalidate: false });
      setMessage(action === "ESCALATE" ? "Flag reopened in the queue." : `Flag ${result.flag_status.toLowerCase()}. You can reopen it below.`);
      setNote("");
      onChanged();
      void mutate();
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "Cannot confirm the update. Refresh the flag before trying again.");
      // A lost response can happen after the backend committed; read status instead of retrying the write.
      void mutate();
      onChanged();
    } finally { setSaving(false); }
  }

  return (
    <dialog ref={dialog} aria-labelledby="flag-detail-title" onClose={onClose}
      onCancel={(event) => { if (saving) event.preventDefault(); }}
      className="fixed inset-0 m-auto max-h-[90dvh] w-[min(95vw,48rem)] overflow-y-auto rounded-panel border border-line bg-surface p-6 text-ink backdrop:bg-black/50">
      <div className="flex items-start justify-between gap-4">
        <h2 id="flag-detail-title" className="text-lg font-semibold">{data?.flag.title ?? flag.title}</h2>
        <button type="button" disabled={saving} onClick={() => dialog.current?.close()} className={BUTTON}>Close</button>
      </div>
      <p className="mt-2 text-sm text-muted">{flag.assetName || data?.flag.asset_id || "No asset"} · {flag.locationName || data?.flag.location_id || "No waypoint"}</p>
      {error && <p role="alert" className="mt-4 text-bad">{error.message}</p>}
      {!data && !error && <LoadingIndicator compact label="Loading flag details…" />}
      <button type="button" disabled={saving || isValidating} onClick={() => void mutate()} className={`${BUTTON} mt-3`}>Refresh flag</button>
      {data && <>
        <p className="mt-4 text-sm">Status: <strong>{data.flag.status}</strong> · {data.flag.severity} · Confidence {Math.round(data.flag.score)}%</p>
        <p className="mt-1 text-sm text-muted">Mission {data.flag.mission_id ?? "unassigned"}{data.flag.action_id ? ` · Action ${data.flag.action_id}` : ""}</p>
        {(data.assessment || (data.system1?.length ?? 0) > 0) && <section className="mt-5" aria-label="SPOTbot analysis">
          <h3 className="font-semibold">SPOTbot analysis</h3>
          {data.assessment && <div className="mt-2 space-y-2 text-sm"><ModelExplanation answer={typeof data.assessment.answer === "string" ? data.assessment.answer : null} evidence={data.assessment.evidence} reasoning={data.assessment.reasoning} recommendedAction={typeof data.assessment.recommended_action === "string" ? data.assessment.recommended_action : null} />{typeof data.assessment.recommended_action !== "string" && <p><strong>Recommended action: </strong>Pending</p>}</div>}
          <details className="mt-3 text-xs"><summary className="cursor-pointer text-accent-ink">All prediction and assessment fields</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-line bg-canvas p-3">{displayJson({ system1: data.system1, assessment: data.assessment })}</pre></details>
        </section>}
        <h3 className="mt-5 font-semibold">Evidence</h3>
        <ul className="mt-2 list-disc space-y-2 pl-5 text-sm">
          {data.flag.reasons.filter((reason) => typeof reason.detail === "string").map((reason, index) => <li key={`${reason.rule}-${index}`}>{displayText(reason.detail ?? "")}</li>)}
        </ul>
        {features.flagExplanations && <section className="mt-5" aria-label="Flag explanation">
          <button type="button" disabled={explaining} onClick={startExplanation} className={BUTTON}>
            {explain.status === "idle" ? "Load explanation" : "Reload explanation"}
          </button>
          {/* One polite announcement per stage and on completion; the draft itself is aria-busy, never read token by token. */}
          <p role="status" className="sr-only">
            {explain.status === "streaming" ? stageLabel(explain.stage, !!explain.draft) : explain.status === "done" ? "Explanation loaded." : ""}
          </p>
          {/* One container from the first stage to the answer (or the fallback): its min-height keeps the dialog
              from jumping when the draft is swapped for the checked answer. */}
          {explain.status !== "idle" && <div className="mt-3 min-h-36 space-y-2 text-sm">
            {explain.status === "streaming" && <>
              {/* The draft box names its own stage, including the checks after Qwen stops writing. */}
              {!explain.draft && <p aria-hidden="true" className="flex items-center gap-2 text-xs text-muted">{stageLabel(explain.stage, false)} <LoadingDots color="currentColor" /></p>}
              {explain.draft && <QwenDraftView draft={explain.draft} catchUpMs={220} finished={explain.stage === "storing"}
                label={explain.stage === "storing" ? "Checking and saving… · unverified draft" : "SPOTbot is writing · unverified draft"} />}
            </>}
            {explain.status === "fallback" && <LoadingIndicator compact label="Loading explanation…" />}
            {explain.status === "error" && <p role="alert" className="text-bad">{explain.message}</p>}
            {explain.status === "done" && <>
              <p className="text-muted">{explanationLabel(explain.data)}</p>
              <ModelExplanation answer={explain.data.answer} evidence={explain.data.evidence} reasoning={explain.data.reasoning}
                recommendedAction={explain.data.recommended_action} />
              {!explain.data.recommended_action && <p><strong>Recommended action: </strong>No recommendation available.</p>}
            </>}
          </div>}
        </section>}
        {features.flagActions && <section className="mt-5 border-t border-line pt-4" aria-label="Flag actions">
          <label className="block text-sm">Operator
            <input value={operator} onChange={(event) => setOperator(event.target.value)} disabled={saving} maxLength={100} className="mt-1 block w-full rounded border border-line bg-surface-2 p-2" />
          </label>
          <label className="mt-3 block text-sm">Optional note
            <textarea value={note} onChange={(event) => setNote(event.target.value)} disabled={saving} maxLength={4000} rows={3} className="mt-1 block w-full rounded border border-line bg-surface-2 p-2" />
          </label>
          <div className="mt-3 flex flex-wrap gap-2">
            {data.flag.status === "OPEN" && <button type="button" disabled={saving || !!error || !operator.trim()} onClick={() => void update("ACKNOWLEDGE")} className={BUTTON}>Acknowledge</button>}
            {data.flag.status !== "RESOLVED" && <button type="button" disabled={saving || !!error || !operator.trim()} onClick={() => void update("RESOLVE")} className={BUTTON}>Resolve</button>}
            {data.flag.status !== "OPEN" && <button type="button" disabled={saving || !!error || !operator.trim()} onClick={() => void update("ESCALATE")} className={BUTTON}>Reopen flag</button>}
          </div>
          <p className="mt-2 text-xs text-muted">Acknowledged and resolved flags leave the open queue. Reopen returns them; notes and action history are kept.</p>
        </section>}
        {data.acks.length > 0 && <section className="mt-5" aria-label="Action history">
          <h3 className="font-semibold">Action history</h3>
          <ul className="mt-2 space-y-2 text-sm">{data.acks.map((ack) => <li key={ack.ack_id}>
            <strong>{ack.action === "ESCALATE" ? "REOPEN" : ack.action}</strong> · {ack.operator} · {new Date(ack.created_at).toLocaleString()}
            {ack.note && <p className="whitespace-pre-wrap text-muted">{ack.note}</p>}
          </li>)}</ul>
        </section>}
      </>}
      {saving ? <LoadingIndicator compact label="Saving flag…" /> : <p role="status" className="mt-3 text-sm">{message}</p>}
      {actionError && <p role="alert" className="mt-2 text-bad">{actionError}</p>}
    </dialog>
  );
}
