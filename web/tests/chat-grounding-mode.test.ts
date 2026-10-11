/* chat.4 (docs/chat-contract.md §10): a general answer gets a neutral badge, or a warning when it names plant ids. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { groundingBadge } from "../src/lib/chat/grounding";
import type { Grounding } from "../src/lib/chat/types";

const base: Grounding = { grounded: false, cited_ids: [], unknown_ids: [], unverified_numbers: [], source_count: 0,
  retrieval_mode: null, graph_seeds: [] };

test("a general answer reads 'General answer', never 'unverified'", () => {
  const badge = groundingBadge({ ...base, mode: "general", fleet_ids: [] })!;
  assert.equal(badge.tone, "muted");
  assert.equal(badge.label, "General answer");
  assert.doesNotMatch(badge.detail, /unverified/i);
});

test("a general answer that names plant ids is flagged", () => {
  const badge = groundingBadge({ ...base, mode: "general", fleet_ids: ["M007", "HX-217"] })!;
  assert.equal(badge.tone, "warn");
  assert.equal(badge.label, "Mentions plant data without sources");
  assert.match(badge.detail, /M007, HX-217/);
});

test("older rows without a mode keep the grounded badges", () => {
  assert.equal(groundingBadge({ ...base, retrieval_mode: "hybrid", source_count: 5 })!.label, "No sources cited");
  assert.equal(groundingBadge({ ...base, grounded: true, cited_ids: ["incident:AN012"], retrieval_mode: "hybrid",
    mode: "grounded" })!.label, "Grounded · 1 source");
});
