"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import type { Flag } from "@/lib/types";
import type { DashboardData } from "@/lib/dashboard";
import { explanationLabel, type FlagAction, type FlagDetailData, type FlagExplanation } from "@/lib/flag-detail";

const BUTTON = "rounded border border-line px-3 py-2 text-sm hover:bg-surface-2 disabled:opacity-50";

async function read<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(55_000) });
  if (!response.ok) {
    const body = await response.json();
    throw new Error(body.error || "Cannot load this flag.");
  }
  return response.json() as Promise<T>;
}

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
  const [showExplanation, setShowExplanation] = useState(false);
  const { data, error, isValidating, mutate } = useSWR<FlagDetailData>(`/api/flags/${flag.id}`, read, { revalidateOnFocus: true });
  const explanation = useSWR<FlagExplanation>(
    features.flagExplanations && showExplanation ? `/api/flags/${flag.id}/explanation` : null,
    read, { revalidateOnFocus: false, shouldRetryOnError: false },
  );

  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal();
  }, []);

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
      {!data && !error && <p role="status" className="mt-4">Loading flag details…</p>}
      <button type="button" disabled={saving || isValidating} onClick={() => void mutate()} className={`${BUTTON} mt-3`}>Refresh flag</button>
      {data && <>
        <p className="mt-4 text-sm">Status: <strong>{data.flag.status}</strong> · {data.flag.severity} · score {data.flag.score}</p>
        <p className="mt-1 text-sm text-muted">Mission {data.flag.mission_id ?? "unassigned"}{data.flag.action_id ? ` · Action ${data.flag.action_id}` : ""}</p>
        <h3 className="mt-5 font-semibold">Evidence</h3>
        <ul className="mt-2 list-disc space-y-2 pl-5 text-sm">
          {data.flag.reasons.filter((reason) => typeof reason.detail === "string").map((reason, index) => <li key={`${reason.rule}-${index}`}>{reason.detail}</li>)}
        </ul>
        {features.flagExplanations && <section className="mt-5" aria-label="Flag explanation">
          <button type="button" disabled={explanation.isLoading} onClick={() => { setShowExplanation(true); if (showExplanation) void explanation.mutate(); }} className={BUTTON}>
            {explanation.isLoading ? "Loading explanation…" : showExplanation ? "Reload explanation" : "Load explanation"}
          </button>
          {explanation.error && <p role="alert" className="mt-2 text-bad">{explanation.error.message}</p>}
          {explanation.data && <div className="mt-3 space-y-2 text-sm">
            <p className="text-muted">{explanationLabel(explanation.data)}</p>
            <p>{explanation.data.answer}</p>
            <p>{explanation.data.reasoning}</p>
            <p><strong>Recommended action: </strong>{explanation.data.recommended_action ?? "No recommendation available."}</p>
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
      <p role="status" className="mt-3 text-sm">{saving ? "Saving…" : message}</p>
      {actionError && <p role="alert" className="mt-2 text-bad">{actionError}</p>}
    </dialog>
  );
}
