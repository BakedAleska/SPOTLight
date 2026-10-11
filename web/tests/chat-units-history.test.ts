/* History date groups (contract §6: Today / Yesterday / Previous 7 days /
   Older) across midnight and both DST changes. Groups are calendar days in
   the viewer's time zone, so this file pins TZ (node --test runs each file in
   its own process, so the other test files are unaffected). */

process.env.TZ = "America/New_York";

import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { groupConversations } from "../src/lib/chat/history";
import type { ConversationSummary } from "../src/lib/chat/types";

function conv(id: string, updated: Date | string, pinned = false): ConversationSummary {
  return {
    conversation_id: id, title: id, title_source: "auto", model: "m", mode: "fast", pinned,
    created_at: "2026-01-01T00:00:00Z", updated_at: typeof updated === "string" ? updated : updated.toISOString(),
    message_count: 2, last_message_preview: null,
  };
}

/** A local wall-clock time in America/New_York. */
const local = (y: number, m: number, d: number, h = 0, min = 0, s = 0, ms = 0) => new Date(y, m - 1, d, h, min, s, ms);

function labels(items: ConversationSummary[], now: Date) {
  const out: Record<string, string> = {};
  for (const group of groupConversations(items, now)) for (const item of group.items) out[item.conversation_id] = group.label;
  return out;
}

describe("history groups: time zone, midnight and DST", () => {
  test("the zone is really pinned (EDT in October, EST in November)", () => {
    assert.equal(local(2026, 10, 31, 12).getTimezoneOffset(), 240);
    assert.equal(local(2026, 11, 2, 12).getTimezoneOffset(), 300);
  });

  test("midnight is the boundary, to the millisecond", () => {
    const midnight = local(2026, 10, 10);
    const items = [conv("at-midnight", midnight), conv("just-before", new Date(midnight.getTime() - 1))];
    assert.deepEqual(labels(items, midnight), { "at-midnight": "Today", "just-before": "Yesterday" });
    assert.deepEqual(labels(items, local(2026, 10, 10, 23, 59, 59, 999)), { "at-midnight": "Today", "just-before": "Yesterday" });
    // One minute later it's a new day: yesterday's midnight item is now Yesterday.
    assert.equal(labels(items, local(2026, 10, 11, 0, 1))["at-midnight"], "Yesterday");
  });

  test("UTC timestamps from the API are grouped by the viewer's local day", () => {
    // 01:30 UTC on Oct 10 is still Oct 9 (21:30) in New York.
    const items = [conv("late-evening", "2026-10-10T01:30:00Z"), conv("morning", "2026-10-10T13:00:00Z")];
    assert.deepEqual(labels(items, local(2026, 10, 10, 15)), { "late-evening": "Yesterday", morning: "Today" });
  });

  test("fall back (Nov 1 2026 has 25 hours): yesterday starts at its own midnight", () => {
    const now = local(2026, 11, 2, 0, 30);
    const items = [
      conv("nov1-00:15-edt", local(2026, 11, 1, 0, 15)),   // 24 h arithmetic would call this "Previous 7 days"
      conv("nov1-01:30-repeated-hour", "2026-11-01T06:30:00Z"), // the second 01:30 (EST)
      conv("nov1-23:59", local(2026, 11, 1, 23, 59)),
      conv("oct31-23:59", local(2026, 10, 31, 23, 59)),
      conv("oct26-00:00", local(2026, 10, 26)),              // exactly 7 calendar days back
      conv("oct25-23:59", local(2026, 10, 25, 23, 59)),
    ];
    assert.deepEqual(labels(items, now), {
      "nov1-00:15-edt": "Yesterday",
      "nov1-01:30-repeated-hour": "Yesterday",
      "nov1-23:59": "Yesterday",
      "oct31-23:59": "Previous 7 days",
      "oct26-00:00": "Previous 7 days",
      "oct25-23:59": "Older",
    });
  });

  test("spring forward (Mar 8 2026 has 23 hours): the hour before midnight stays out of Yesterday", () => {
    const now = local(2026, 3, 9, 0, 30);
    const items = [
      conv("mar7-23:30", local(2026, 3, 7, 23, 30)),         // 24 h arithmetic would call this "Yesterday"
      conv("mar8-00:00", local(2026, 3, 8)),
      conv("mar8-03:05-edt", local(2026, 3, 8, 3, 5)),
      conv("mar9-00:10", local(2026, 3, 9, 0, 10)),
      conv("mar2-00:00", local(2026, 3, 2)),
      conv("mar1-23:59", local(2026, 3, 1, 23, 59)),
    ];
    assert.deepEqual(labels(items, now), {
      "mar7-23:30": "Previous 7 days",
      "mar8-00:00": "Yesterday",
      "mar8-03:05-edt": "Yesterday",
      "mar9-00:10": "Today",
      "mar2-00:00": "Previous 7 days",
      "mar1-23:59": "Older",
    });
  });

  test("pinned wins over any date, and a clock-skewed future time is Today", () => {
    const now = local(2026, 10, 10, 12);
    const groups = groupConversations([conv("future", local(2026, 10, 11, 9)), conv("pinned-today", now, true), conv("pinned-old", local(2020, 1, 1), true)], now);
    assert.deepEqual(groups.map((g) => [g.label, g.items.map((c) => c.conversation_id)]), [
      ["Pinned", ["pinned-today", "pinned-old"]],
      ["Today", ["future"]],
    ]);
  });

  test("another zone groups the same instants differently", () => {
    const items = [conv("x", "2026-10-09T19:00:00Z")];
    const now = new Date("2026-10-10T06:00:00Z");
    assert.equal(labels(items, now).x, "Yesterday", "New York: Oct 9 15:00 vs Oct 10 02:00");
    process.env.TZ = "Asia/Kolkata";
    try {
      assert.equal(labels(items, now).x, "Today", "Kolkata: Oct 10 00:30 vs Oct 10 11:30");
    } finally {
      process.env.TZ = "America/New_York";
    }
  });
});
