"use client";

import type { ReactNode } from "react";
import useSWR from "swr";
import { ArrowUpRight, Info } from "lucide-react";
import { chatFetcher } from "@/lib/chat/api";
import type { MissionSummary, Suggestion } from "@/lib/chat/types";
import { MissionContextCard, MissionContextSkeleton } from "./MissionFocus";

/** chat.3: what /assistant?mission= resolved to. */
export type MissionFocusState =
  | { kind: "none" }
  | { kind: "loading"; missionId: string }
  | { kind: "found"; mission: MissionSummary }
  /** The mission list can't be read: keep the focus, without details. */
  | { kind: "unverified"; missionId: string }
  | { kind: "unknown"; raw: string };

/* Used only if /chat/suggestions can't be reached, so the empty state never
   shows a hole. The backend's own suggestions carry real mission ids. */
const FALLBACK: Suggestion[] = [
  { title: "Hidden failures", prompt: "Which missions reported success but had failed actions underneath?" },
  { title: "Battery cliff", prompt: "How does starting battery level change action success rates?" },
  { title: "Problem waypoints", prompt: "Which waypoints fail most often, and what goes wrong there?" },
  { title: "V1 vs V2", prompt: "Did configuration V2 improve reliability over V1 at matched battery?" },
];

/** New chat: a centred greeting, the composer, and four starter prompts. */
export function EmptyState({ composer, onPick, disabled, focus = { kind: "none" }, onClearMission }: {
  composer: ReactNode;
  onPick: (prompt: string) => void;
  disabled: boolean;
  focus?: MissionFocusState;
  onClearMission?: () => void;
}) {
  // A mission chat asks for that mission's prompts (chat.3). While the
  // mission is still being looked up, wait rather than flash the fleet ones.
  const missionId = focus.kind === "found" ? focus.mission.mission_id : focus.kind === "unverified" ? focus.missionId : null;
  const key = focus.kind === "loading" ? null : missionId ? `/api/chat/suggestions?mission_id=${encodeURIComponent(missionId)}` : "/api/chat/suggestions";
  const { data, error } = useSWR<{ suggestions: Suggestion[] }>(key, chatFetcher, { revalidateOnFocus: false });
  const suggestions = (data?.suggestions?.length ? data.suggestions : error ? FALLBACK : []).slice(0, 4);
  const focusedId = focus.kind === "found" ? focus.mission.mission_id : focus.kind === "loading" || focus.kind === "unverified" ? focus.missionId : null;
  return (
    <div className="relative flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain">
      <div className="m-auto w-full max-w-3xl px-4 py-10 sm:px-6">
        <h1 className="text-center text-2xl font-semibold tracking-tight text-ink sm:text-3xl">
          {focusedId ? <>What should we look at on <span className="font-mono">{focusedId}</span>?</> : "What can I help you inspect?"}
        </h1>
        <p className="mx-auto mt-2 max-w-lg text-center text-sm text-muted">
          {focusedId
            ? "This chat is about one mission. You can still ask about others; every answer cites the data it used."
            : "Ask about missions, assets, waypoints and flags. Every answer cites the mission data it used."}
        </p>
        {focus.kind === "unknown" && (
          <p role="status" className="mx-auto mt-4 flex max-w-lg items-center justify-center gap-1.5 text-center text-xs text-muted">
            <Info size={13} aria-hidden="true" className="shrink-0" />
            <span>No mission “{focus.raw.slice(0, 40)}” was found, so this is a normal new chat.</span>
          </p>
        )}
        {focus.kind === "found" && <div className="mt-6"><MissionContextCard mission={focus.mission} onClear={onClearMission} /></div>}
        {focus.kind === "loading" && <div className="mt-6"><MissionContextSkeleton /></div>}
        <div className={focus.kind === "found" || focus.kind === "loading" ? "mt-4" : "mt-8"}>{composer}</div>
        <ul aria-label="Suggested questions" className="mt-6 grid gap-2.5 sm:grid-cols-2">
          {suggestions.length === 0 && !error
            ? [0, 1, 2, 3].map((i) => <li key={i} aria-hidden="true" className="h-[4.5rem] animate-pulse rounded-xl border border-line bg-surface-2/60" />)
            : suggestions.map((suggestion) => (
                <li key={suggestion.prompt}>
                  <button
                    type="button"
                    disabled={disabled}
                    onClick={() => onPick(suggestion.prompt)}
                    className="group flex h-full w-full cursor-pointer items-start gap-3 rounded-xl border border-line bg-surface p-3.5 text-left transition-colors hover:border-line-strong hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium text-ink">{suggestion.title}</span>
                      <span className="mt-0.5 line-clamp-2 block text-[13px] leading-snug text-muted">{suggestion.prompt}</span>
                    </span>
                    <ArrowUpRight size={15} aria-hidden="true" className="mt-0.5 shrink-0 text-faint transition-colors group-hover:text-ink" />
                  </button>
                </li>
              ))}
        </ul>
      </div>
    </div>
  );
}
