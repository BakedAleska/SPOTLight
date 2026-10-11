/* Playing a mock patrol back in the browser (the plan comes from GET /api/live/mock). Client-safe and pure: the
   Live patrol page keeps a MockState and advances it on a timer; these functions turn it into the run and captures
   the normal live views already draw. Each stop goes arrived -> triaged (SPOTbot "analysing" on a red stop) -> done. */
import type { LiveCapture, LiveRun, MockPatrol } from "./live";

/** How long each phase of a stop shows: arrived, triaged/analysing, done (before SPOT walks on). */
export const MOCK_PHASE_MS = [1500, 1600, 1100] as const;

export type MockStatus = "running" | "done" | "cancelled";
export interface MockState { plan: MockPatrol; step: number; status: MockStatus; startedAt: string }

export function startMock(plan: MockPatrol, now: string): MockState {
  return { plan, step: 0, status: plan.captures.length ? "running" : "done", startedAt: now };
}

/** One phase on; the last phase of the last stop finishes the run. */
export function advanceMock(state: MockState): MockState {
  if (state.status !== "running") return state;
  const step = state.step + 1;
  return step >= state.plan.captures.length * 3 ? { ...state, step, status: "done" } : { ...state, step };
}

export function cancelMock(state: MockState): MockState {
  return state.status === "running" ? { ...state, status: "cancelled" } : state;
}

/** The captures shown so far: every earlier stop finished, the current one at its phase. */
export function mockCaptures(state: MockState): LiveCapture[] {
  const all = state.plan.captures;
  if (!all.length) return [];
  if (state.status === "done") return all;
  const current = Math.min(Math.floor(state.step / 3), all.length - 1);
  const phase = state.step % 3;
  return all.slice(0, current + 1).map((c, i) => (i < current || phase === 2 ? c : phase === 0
    ? { ...c, phase: "arrived", verdict: null, priority: null, severity: null, category: null, laya: null, rules: null,
        decision: null, qwen: null, qwen_source: null }
    : { ...c, phase: "analysing", qwen: null, qwen_source: c.verdict === "red" ? "pending" : null }));
}

export function mockRun(state: MockState): LiveRun {
  const shown = mockCaptures(state);
  return {
    run_id: 0, route_id: state.plan.route.route_id, mission_id: state.plan.route.route_id.replace(/^MOCK-/, ""),
    status: state.status === "running" ? "running" : state.status === "done" ? "done" : "cancelled",
    current_stop: shown.length, total_stops: state.plan.captures.length, runner: "mock patrol (in the browser)",
    error: null, counts: {}, created_at: state.startedAt, claimed_at: state.startedAt,
    finished_at: state.status === "running" ? null : state.startedAt, updated_at: state.startedAt,
    red: shown.filter((c) => c.verdict === "red").length, green: shown.filter((c) => c.verdict === "green").length,
  };
}
