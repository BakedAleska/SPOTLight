"use client";

/* Mission chats (contract §9): the context card on /assistant?mission=,
   the "Focus on a mission" picker, the header chip inside a mission chat and
   the history badge. Status is always colour + icon + word. */

import { useEffect, useId, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  ChevronDown, CircleCheck, CircleDot, CircleX, Crosshair, EyeOff, Flag, Info, MessagesSquare, Route, Search, TriangleAlert, X,
} from "lucide-react";
import { filterMissions, hiddenFailureText, missionLabel, missionTone, plural, replayHref, stepIndex } from "@/lib/chat/mission";
import type { MissionRef, MissionSummary } from "@/lib/chat/types";
import { usePopover } from "./usePopover";

const TONE_TEXT = { ok: "text-ok", warn: "text-warn", bad: "text-bad" } as const;

/** Reported status as colour + icon + word ("Completed", "Failed"). */
export function MissionStatusBadge({ status, className = "" }: { status: string; className?: string }) {
  const upper = status.toUpperCase();
  const [Icon, tone] = upper === "COMPLETED" ? [CircleCheck, "text-ok"] : upper === "FAILED" ? [CircleX, "text-bad"] : [CircleDot, "text-muted"];
  const word = upper ? upper[0] + upper.slice(1).toLowerCase() : "Unknown";
  return (
    <span className={`inline-flex items-center gap-1 text-xs font-semibold ${tone} ${className}`}>
      <Icon size={13} strokeWidth={2.5} aria-hidden="true" />
      {word}
    </span>
  );
}

/** "Hidden failure" marker: the mission said COMPLETED but actions failed. */
export function HiddenFailureMark() {
  return (
    <span className="inline-flex items-center gap-1 text-xs font-semibold text-bad">
      <EyeOff size={12} strokeWidth={2.5} aria-hidden="true" />
      Hidden failure
    </span>
  );
}

function Stat({ label, value, note, tone }: { label: string; value: React.ReactNode; note?: string; tone?: "warn" | "bad" }) {
  return (
    <div className="min-w-0 rounded-lg bg-surface-2/60 px-3 py-2">
      <dt className="text-[11px] font-medium tracking-wide text-muted uppercase">{label}</dt>
      <dd className={`nums mt-0.5 text-lg leading-6 font-semibold ${tone ? TONE_TEXT[tone] : "text-ink"}`}>{value}</dd>
      {note && <dd className="truncate text-[11px] text-muted">{note}</dd>}
    </div>
  );
}

/**
 * The mission a new chat is about, shown above the composer: id, name,
 * reported status, failed actions, anomalies, open flags, and the hidden
 * failure warning when the reported status hides failed actions.
 */
export function MissionContextCard({ mission, onClear }: { mission: MissionSummary; onClear?: () => void }) {
  const hidden = hiddenFailureText(mission);
  return (
    <section aria-label={`Mission ${mission.mission_id}`} className="chat-fade-in panel-shadow rounded-2xl border border-line bg-surface p-4">
      <div className="flex items-start gap-3">
        <span aria-hidden="true" className="grid size-9 shrink-0 place-items-center rounded-full bg-accent/15 text-accent-ink">
          <Crosshair size={18} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium text-muted">Focused on mission</p>
          <h2 className="truncate text-base font-semibold text-ink">
            <span className="font-mono">{mission.mission_id}</span> · {mission.mission_name}
          </h2>
          <p className="mt-0.5 text-xs text-muted">
            {[mission.scenario, mission.configuration_version, mission.duration_min != null ? `${mission.duration_min} min` : null,
              mission.start_battery_pct != null && mission.end_battery_pct != null ? `battery ${mission.start_battery_pct}% → ${mission.end_battery_pct}%` : null]
              .filter(Boolean).join(" · ")}
          </p>
        </div>
        <Link
          href={replayHref(mission.mission_id)}
          className="hidden h-8 shrink-0 items-center gap-1.5 rounded-full border border-line px-3 text-xs font-medium text-muted transition-colors hover:bg-surface-2 hover:text-ink sm:inline-flex"
        >
          <Route size={13} aria-hidden="true" /> Replay
        </Link>
        {onClear && (
          <button
            type="button"
            onClick={onClear}
            aria-label={`Stop focusing on ${mission.mission_id}`}
            title="Ask about the whole fleet instead"
            className="grid size-8 shrink-0 cursor-pointer place-items-center rounded-full text-muted transition-colors hover:bg-surface-2 hover:text-ink"
          >
            <X size={15} aria-hidden="true" />
          </button>
        )}
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className="min-w-0 rounded-lg bg-surface-2/60 px-3 py-2">
          <dt className="text-[11px] font-medium tracking-wide text-muted uppercase">Reported</dt>
          <dd className="mt-1"><MissionStatusBadge status={mission.mission_status} className="text-sm" /></dd>
        </div>
        <Stat label="Failed actions" value={mission.failed} note={`of ${mission.actions}`} tone={mission.failed ? "bad" : undefined} />
        <Stat label="Anomalies" value={mission.anomalies} note={mission.serious_anomalies ? `${mission.serious_anomalies} high or critical` : "none serious"} tone={mission.serious_anomalies ? "warn" : undefined} />
        <Stat label="Open flags" value={mission.open_flags} note={mission.open_flags ? "awaiting an operator" : "none open"} tone={mission.open_flags ? "warn" : undefined} />
      </dl>

      {hidden && (
        <p className="mt-3 flex items-start gap-2 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-ink">
          <TriangleAlert size={16} aria-hidden="true" className="mt-0.5 shrink-0 text-bad" />
          <span><span className="font-semibold text-bad">Hidden failure:</span> {hidden}.</span>
        </p>
      )}

      <p className="mt-3 flex items-center gap-1.5 text-xs text-muted">
        <Info size={12} aria-hidden="true" className="shrink-0" />
        <span>Every answer starts from this mission’s digest, failures, anomalies and flags.{mission.chats > 0 && ` ${plural(mission.chats, "earlier chat")} about it.`}</span>
      </p>
    </section>
  );
}

/** Placeholder while the mission list loads. */
export function MissionContextSkeleton() {
  return (
    <div aria-hidden="true" className="rounded-2xl border border-line bg-surface p-4">
      <div className="h-5 w-2/3 animate-pulse rounded bg-surface-2" />
      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[0, 1, 2, 3].map((i) => <div key={i} className="h-14 animate-pulse rounded-lg bg-surface-2/60" />)}
      </div>
    </div>
  );
}

/** Inside a mission chat: "M006 · Autowalk Inspection Route 06", linking to its replay. */
export function MissionChip({ mission }: { mission: MissionRef }) {
  return (
    <Link
      href={replayHref(mission.mission_id)}
      title={`${missionLabel(mission)} — open replay`}
      aria-label={`Mission ${missionLabel(mission)}. Open replay`}
      className="inline-flex h-8 min-w-0 items-center gap-1.5 rounded-full border border-accent/40 bg-accent/10 px-2.5 text-sm text-ink transition-colors hover:border-accent hover:bg-accent/15"
    >
      <Crosshair size={14} aria-hidden="true" className="shrink-0 text-accent-ink" />
      <span className="font-mono font-medium">{mission.mission_id}</span>
      {mission.mission_name && <span className="hidden max-w-[16rem] truncate text-muted lg:inline">· {mission.mission_name}</span>}
    </Link>
  );
}

/** History row badge for a mission chat. */
export function MissionBadge({ missionId }: { missionId: string }) {
  return (
    <span className="ml-auto inline-flex shrink-0 items-center gap-0.5 rounded border border-line px-1 font-mono text-[10px] leading-4 text-muted">
      <Crosshair size={9} aria-hidden="true" />
      <span className="sr-only">Mission </span>
      {missionId}
    </span>
  );
}

function MissionOption({ mission, active, selected, id, onPick, onHover }: {
  mission: MissionSummary;
  active: boolean;
  selected: boolean;
  id: string;
  onPick: () => void;
  onHover: () => void;
}) {
  const tone = missionTone(mission);
  return (
    <li
      id={id}
      role="option"
      aria-selected={selected}
      data-active={active || undefined}
      onPointerDown={(event) => event.preventDefault()}
      onClick={onPick}
      onPointerMove={onHover}
      className={`cursor-pointer rounded-lg px-2.5 py-2 transition-colors ${active ? "bg-surface-2" : ""}`}
    >
      <div className="flex items-center gap-2">
        <span aria-hidden="true" className={`size-1.5 shrink-0 rounded-full ${tone === "ok" ? "bg-ok" : tone === "warn" ? "bg-warn" : "bg-bad"}`} />
        <span className="font-mono text-sm font-medium text-ink">{mission.mission_id}</span>
        <span className="min-w-0 flex-1 truncate text-sm text-muted">{mission.mission_name}</span>
        <MissionStatusBadge status={mission.mission_status} />
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-3.5 text-xs text-muted">
        <span className={mission.failed ? "text-ink" : ""}>{plural(mission.failed, "failure")}</span>
        <span>{plural(mission.anomalies, "anomaly", "anomalies")}</span>
        {mission.open_flags > 0 && (
          <span className="inline-flex items-center gap-1"><Flag size={11} aria-hidden="true" />{mission.open_flags} open</span>
        )}
        {mission.hidden_failure && <HiddenFailureMark />}
        {mission.chats > 0 && (
          <span className="ml-auto inline-flex items-center gap-1"><MessagesSquare size={11} aria-hidden="true" />{plural(mission.chats, "chat")}</span>
        )}
      </div>
    </li>
  );
}

/**
 * "Focus on a mission": a searchable listbox of /api/chat/missions in a
 * popover. The search box is a combobox — arrows move, Enter picks, Escape
 * closes and returns focus to the trigger. When a mission is set the
 * trigger becomes a chip with its own × button.
 */
export function MissionPicker({ missions, loading, error, value, onSelect, onClear, disabled = false }: {
  missions: MissionSummary[] | undefined;
  loading: boolean;
  error: boolean;
  value: string | null;
  onSelect: (missionId: string) => void;
  onClear: () => void;
  disabled?: boolean;
}) {
  const { open, setOpen, root, trigger } = usePopover();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(-1);
  // Opens upward when the list wouldn't fit below the trigger.
  const [up, setUp] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const listId = useId();
  const results = useMemo(() => filterMissions(missions ?? [], query), [missions, query]);
  const optionId = (i: number) => `${listId}-opt-${i}`;

  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  useEffect(() => {
    list.current?.querySelector("[data-active]")?.scrollIntoView({ block: "nearest" });
  }, [active]);

  function show() {
    setQuery("");
    const current = (missions ?? []).findIndex((m) => m.mission_id === value);
    setActive(current >= 0 ? current : 0);
    const rect = trigger.current?.getBoundingClientRect();
    setUp(!!rect && rect.bottom + 400 > window.innerHeight && rect.top > 400);
    setOpen(true);
  }

  function pick(mission: MissionSummary) {
    setOpen(false);
    onSelect(mission.mission_id);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      if ((event.key === "Home" || event.key === "End") && query) return; // let the caret move in the text
      event.preventDefault();
      setActive((current) => stepIndex(current, event.key, results.length));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const chosen = results[active] ?? (results.length === 1 ? results[0] : undefined);
      if (chosen) pick(chosen);
    } else if (event.key === "Tab") {
      setOpen(false);
    }
  }

  const current = value ? missions?.find((m) => m.mission_id === value) : undefined;

  return (
    // Below sm the popover anchors to the composer (the nearest positioned
    // ancestor) and spans its width; from sm up it hangs off the trigger.
    <div ref={root} className="min-w-0 sm:relative">
      {value ? (
        <span className="inline-flex h-8 max-w-full min-w-0 items-center rounded-full border border-accent/50 bg-accent/10 text-sm text-ink">
          <button
            ref={trigger}
            type="button"
            onClick={() => (open ? setOpen(false) : show())}
            disabled={disabled}
            aria-haspopup="listbox"
            aria-expanded={open}
            aria-label={`Focused on mission ${current ? missionLabel(current) : value}. Change mission`}
            className="inline-flex h-full min-w-0 cursor-pointer items-center gap-1.5 rounded-l-full pl-2.5 pr-1 disabled:cursor-not-allowed"
          >
            <Crosshair size={14} aria-hidden="true" className="shrink-0 text-accent-ink" />
            <span className="font-mono font-medium">{value}</span>
            {current?.hidden_failure && <EyeOff size={12} aria-label="hidden failure" className="shrink-0 text-bad" />}
          </button>
          <button
            type="button"
            onClick={onClear}
            aria-label={`Clear mission focus (${value})`}
            title="Clear mission focus"
            className="grid h-full w-7 shrink-0 cursor-pointer place-items-center rounded-r-full text-muted transition-colors hover:text-ink"
          >
            <X size={13} aria-hidden="true" />
          </button>
        </span>
      ) : (
        <button
          ref={trigger}
          type="button"
          onClick={() => (open ? setOpen(false) : show())}
          disabled={disabled}
          aria-haspopup="listbox"
          aria-expanded={open}
          className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-full border border-line px-3 text-sm text-muted transition-colors hover:bg-surface-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Crosshair size={14} aria-hidden="true" />
          <span className="hidden sm:inline">Focus on a mission</span>
          <span className="sm:hidden">Mission</span>
          <ChevronDown size={13} aria-hidden="true" className={`transition-transform ${open ? "rotate-180" : ""}`} />
        </button>
      )}

      {open && (
        <div
          onBlur={(event) => { if (!root.current?.contains(event.relatedTarget as Node | null)) setOpen(false); }}
          className={`panel-shadow absolute left-0 z-40 w-full ${up ? "bottom-full mb-2" : "top-full mt-2"} rounded-xl border border-line bg-surface p-1.5 shadow-xl sm:w-[26rem]`}
        >
          <div className="relative">
            <Search size={14} aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-muted" />
            <input
              ref={input}
              type="text"
              role="combobox"
              aria-expanded="true"
              aria-controls={listId}
              aria-autocomplete="list"
              aria-activedescendant={active >= 0 && results[active] ? optionId(active) : undefined}
              aria-label="Search missions"
              placeholder="Search missions, e.g. M006, hidden, failed"
              value={query}
              onChange={(event) => { setQuery(event.target.value); setActive(0); }}
              onKeyDown={onKeyDown}
              className="h-9 w-full rounded-lg border border-line bg-surface-3 pr-2 pl-8 text-sm text-ink placeholder:text-muted"
            />
          </div>
          <p className="px-2.5 pt-2 pb-1 text-xs font-medium text-muted">
            {loading ? "Loading missions…" : error ? "Cannot load missions." : `${results.length} mission${results.length === 1 ? "" : "s"}`}
          </p>
          <ul ref={list} id={listId} role="listbox" aria-label="Missions" className="max-h-72 overflow-y-auto overscroll-contain">
            {results.map((mission, i) => (
              <MissionOption
                key={mission.mission_id}
                id={optionId(i)}
                mission={mission}
                active={i === active}
                selected={mission.mission_id === value}
                onPick={() => pick(mission)}
                onHover={() => setActive(i)}
              />
            ))}
            {!loading && !error && results.length === 0 && (
              <li role="presentation" className="px-2.5 py-3 text-sm text-muted">No mission matches “{query}”.</li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}
