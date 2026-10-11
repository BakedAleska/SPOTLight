/* System 2's evidence, shaped for reading (every place a Qwen answer shows).

   Qwen answers in JSON: `evidence[{claim, ids}]`, a one- or two-sentence
   `answer` and a `recommended_action` code. Its claims are often compact
   `key=value; key=value` copies of the cited rows, and a stored assessment
   keeps only `reasoning`, the claims flattened as "<claim> [<ids>] ..."
   (backend `evidence_reasoning`). These helpers turn either form back into
   claims, split structured claims into labelled fields and name the cited
   rows, so the flag detail, the draft and /live render them the same way.
   Pure: no React, no fetch. */

export interface EvidenceItem { claim: string; ids: string[] }

export interface ClaimField { key: string; label: string; value: string }

export interface ParsedClaim {
  /** Prose before the first field ("Mission M001" in "Mission M001 reported_status=COMPLETED; ..."). */
  lead: string;
  /** Empty when the claim is prose. */
  fields: ClaimField[];
  /** Prose claim, or a draft's unfinished last segment ("measurement_ty"). */
  text: string;
}

export interface Citation { id: string; label: string }

/* A cited ID: namespaced (action:A0089, s1:326, manual:p60-1) or a packet row (F1, V2, S1). Brackets holding
   anything else are values (safe_band=[72.0, null]), not citations. */
const ID = /^(?:[a-z][a-z0-9_]*:[^\s,[\]]+|[A-Z]{1,2}\d{1,4})$/;
const KEY_AT = /(^|\s)([A-Za-z][A-Za-z0-9_]*)=/g;

/** Claims from structured evidence when the API sent it, else from the flattened `reasoning` string. */
export function evidenceItems(evidence: unknown, reasoning?: unknown): EvidenceItem[] {
  if (Array.isArray(evidence)) {
    const items = evidence.flatMap((row) => {
      if (!row || typeof row !== "object") return [];
      const { claim, ids } = row as { claim?: unknown; ids?: unknown };
      if (typeof claim !== "string" || !claim.trim()) return [];
      return [{ claim: claim.trim(), ids: Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string" && !!id) : [] }];
    });
    if (items.length) return items;
  }
  return typeof reasoning === "string" ? parseReasoning(reasoning) : [];
}

/** "<claim> [<ids>] <claim> [<ids>]" back into claims. Text with no citation brackets is one prose claim. */
export function parseReasoning(reasoning: string): EvidenceItem[] {
  const items: EvidenceItem[] = [];
  let start = 0;
  for (const match of reasoning.matchAll(/\[([^[\]]*)\]/g)) {
    const ids = match[1].split(",").map((id) => id.trim());
    if (!ids.length || !ids.every((id) => ID.test(id))) continue;   // a value in brackets, part of the claim
    const claim = reasoning.slice(start, match.index).trim();
    if (claim) items.push({ claim, ids });
    start = match.index + match[0].length;
  }
  const rest = reasoning.slice(start).trim();
  if (rest) items.push({ claim: rest, ids: [] });
  return items;
}

/** A claim as fields when it is `key=value` pairs (`;`- or space-separated), else prose. With `partial` (a draft
    still being typed) an unfinished last segment stays as `text` instead of turning the whole claim into prose. */
export function parseClaim(claim: string, partial = false): ParsedClaim {
  const prose = { lead: "", fields: [], text: claim };
  const segments = claim.split(/;\s*/).map((s) => s.trim()).filter(Boolean);
  const raw: { key: string; value: string }[] = [];
  let lead = "";
  let text = "";
  for (const [index, segment] of segments.entries()) {
    const keys = [...segment.matchAll(KEY_AT)];
    if (!keys.length) {
      if (partial && index === segments.length - 1 && raw.length) { text = segment; break; }
      return prose;
    }
    const before = segment.slice(0, keys[0].index).trim();
    if (before) {
      if (raw.length) return prose;      // prose between fields: keep the model's sentence as written
      lead = before;
    }
    keys.forEach((match, k) => {
      const end = k + 1 < keys.length ? keys[k + 1].index : segment.length;
      raw.push({ key: match[2], value: segment.slice(match.index + match[0].length, end).trim() });
    });
  }
  if (raw.length < 2 && !(raw.length === 1 && !lead)) return prose;
  return { lead, fields: formatFields(raw), text };
}

const LABELS: Record<string, string> = {
  action_type: "Inspection", action_id: "Action", asset_id: "Asset", asset_name: "Asset", location_id: "Waypoint",
  location_name: "Waypoint", mission_id: "Mission", status: "Status", failure_type: "Failure", battery_pct: "Battery",
  battery_status: "Battery status", measurement_type: "Measurement", value: "Reading", unit: "Unit", quality: "Quality",
  trust: "Trust", reading_trust: "Reading trust", category: "Category", priority: "Priority", escalate: "Escalate",
  suggested_action: "Suggested action", severity: "Severity", anomaly_type: "Anomaly", safe_band: "Safe band",
  assessment: "Assessment", reported_status: "Reported status", actions_failed: "Failed actions",
  actions_total: "Actions", anomalies_by_severity: "Anomalies", end_battery_pct: "End battery",
  start_battery_pct: "Start battery", duration_sec: "Duration", true_health_pct: "True health",
  operator_review_required: "Operator review", criticality: "Criticality", environment: "Environment",
  in_safe_band: "In safe band", configuration_version: "Configuration",
};

/* Keys whose values are codes (ACOUSTIC_INSPECTION, REINSPECT_NOW): shown as words. IDs and numbers stay as sent. */
const CODE_KEYS = new Set(["action_type", "failure_type", "measurement_type", "category", "suggested_action", "status",
  "quality", "severity", "battery_status", "anomaly_type", "reported_status", "trust", "reading_trust", "criticality",
  "environment", "recommended_action"]);

const CODES: Record<string, string> = {
  SOUND_DB: "Sound level", TEMPERATURE_C: "Temperature", PRESSURE_PSI: "Pressure", VISUAL_SCORE: "Visual score",
  LOCALIZATION_CONFIDENCE_PCT: "Localization confidence", RETURN_TO_DOCK: "Return to dock",
  REINSPECT_NOW: "Reinspect now", MANUAL_REVIEW: "Manual review", NO_ACTION: "No action",
};

/** ACOUSTIC_INSPECTION -> "Acoustic inspection"; anything that is not a plain code is returned as sent. */
export function humanizeCode(value: string): string {
  if (CODES[value]) return CODES[value];
  if (!/^[A-Z][A-Z]+(?:_[A-Z0-9]+)*$/.test(value)) return value;
  const words = value.toLowerCase().replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function label(key: string): string {
  if (LABELS[key]) return LABELS[key];
  const words = key.replace(/_(id|pct)$/, "").replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function formatValue(key: string, value: string): string {
  const v = value.trim();
  if (key === "safe_band") {
    const band = /^\[\s*([^,\]]*)\s*,\s*([^\]]*)\s*\]$/.exec(v);
    if (band) {
      const [lo, hi] = [band[1].trim(), band[2].trim()].map((x) => (x === "null" || x === "" ? null : x));
      return lo && hi ? `${lo} – ${hi}` : lo ? `≥ ${lo}` : hi ? `≤ ${hi}` : v;
    }
  }
  if (v.startsWith("{")) {
    try {
      const counts = JSON.parse(v) as Record<string, unknown>;
      const shown = Object.entries(counts).filter(([, n]) => n !== 0 && n !== null);
      return shown.length ? shown.map(([k, n]) => `${humanizeCode(k)} ${String(n)}`).join(", ") : "none";
    } catch { return v; }
  }
  if (/_pct$/.test(key) && /^-?\d+(\.\d+)?$/.test(v)) return `${v}%`;
  if (key === "duration_sec" && /^\d+(\.\d+)?$/.test(v)) return `${v} s`;
  if (v === "true" || v === "yes") return "Yes";
  if (v === "false" || v === "no") return "No";
  if (!CODE_KEYS.has(key)) return v;
  return humanizeCode(/^[a-z]+(?:_[a-z0-9]+)*$/.test(v) ? v.toUpperCase() : v);   // data_quality, untrusted too
}

/** Labels and readable values; `value` and `unit` merge into one reading ("81.5 dB"). */
export function formatFields(raw: { key: string; value: string }[]): ClaimField[] {
  const unit = raw.find((f) => f.key === "unit")?.value.trim();
  const hasValue = raw.some((f) => f.key === "value");
  return raw.filter((f) => !(f.key === "unit" && hasValue)).map(({ key, value }) => ({
    key,
    label: label(key),
    value: key === "value" && unit ? `${value.trim()} ${unit}` : formatValue(key, value),
  }));
}

const NAMESPACES: Record<string, string> = {
  action: "Action", reading: "Reading", anomaly: "Anomaly", flag: "Flag", mission: "Mission", asset: "Asset",
  location: "Waypoint", manual: "Manual", note: "Operator note", s1: "Laya answer", incident: "Incident",
};

/** A cited row by name: action:A0089 -> "Action A0089", F1 -> "Fact F1", V2 -> "Image reading V2". */
export function citationLabel(id: string): string {
  const [ns, local] = id.includes(":") ? [id.slice(0, id.indexOf(":")), id.slice(id.indexOf(":") + 1)] : ["", id];
  if (!ns) {
    if (/^F\d+$/.test(id)) return `Fact ${id}`;
    if (/^V\d+$/.test(id)) return `Image reading ${id}`;
    return id;
  }
  return `${NAMESPACES[ns] ?? humanizeCode(ns.toUpperCase())} ${local}`;
}

export function citations(ids: string[]): Citation[] {
  return ids.map((id) => ({ id, label: citationLabel(id) }));
}
