# Live model dashboard

The Overview and Analytics pages render `LiveModelBoard`. Dashboard cards, flag
queue and open flag details refresh every 2.5 seconds. The model board independently
polls `/api/model-stream` every 2.5 seconds (SWR prevents overlapping revalidation).
Backend disconnection retains the last successful model snapshot and labels it stale.
No backend results means empty, explicitly waiting model panels, never kit predictions.

## Existing endpoint consumption

`BACKEND_URL` is server-only and includes `/api/v1`, following the dashboard:

- `GET /events?limit=200[&mission_id=...]`: latest ingested events with triage payloads.
- `GET /flags?limit=12[&mission_id=...]`: current priority queue, not all historical flags.
- `GET /flags/{id}`: predictions, stored assessment, evidence and event for each returned flag.
- `GET /system1/forecast?mission_id=...&live=false`: stored model predictions for the selected
  or latest observed mission; baseline results are visible in the output disclosure but excluded from model charts.
- `GET /ops/model-calls?recent=100`: actual latency, stats and routing. Optional server-only
  `BACKEND_ADMIN_TOKEN` authenticates the request. It is never returned to the browser.

The proxy returns a `ModelStream` snapshot: connected, updatedAt, warnings, events,
details, forecasts, calls, stats and routing (`src/lib/model-stream.ts`). Independent
endpoint failures yield a partial snapshot and a warning; both event and flag endpoints
failing returns 503. Requests use a 5-second cap per stage. No model inference is
triggered by polling. Open a flag and Load explanation to request the existing inference
workflow; subsequent stored results appear automatically in both board and details.

## What moves

Laya chart plots actual `probs.A` for `will_fail`, `anomaly_expected` and `escalate`,
plus stored System 1 forecasts when the same action/question isn't already present.
Prediction IDs deduplicate repeated mission-level responses. Missing/invalid probabilities
stay absent; zero is a valid measurement. X axis is prediction order; timestamps and
model identifiers are in tooltips. These are three distinct probabilities, not a combined score.
Latency chart uses returned `app.model_calls` values, not invented polling latency.
System 2 cards show question, answer, reasoning, recommendation, severity, confidence,
model and state. TEMPLATE/stub results are labeled fallback, never model confidence.
Complete returned prediction, assessment, event, call and routing payloads are expandable.

## Limits of the existing backend

There is no global streaming prediction endpoint. The view is bounded to the latest
200 events, 12 priority flags and their returned prediction windows (backend limit 60
per detail), stored forecasts for one mission and 100 model calls. Routine predictions
not available in these existing endpoints cannot be displayed. The current backend
persists only selected assessment columns; full orchestrator/vision traces are not stored
in flag details. Adding those backend endpoints or stored fields belongs to the backend owner.
The battery chart remains SQL action-success statistics; it is not labeled model output.

## Validation

From `web/`: `npx tsx --test tests/model-stream.test.ts`, `npm run test:flags`,
`npx tsc --noEmit`, `npm run lint`, `npm run build`. Tests use an isolated local
HTTP fixture to verify changed probabilities, baseline exclusion, actual zero, read-only
requests, optional admin authorization and token non-disclosure. Fixture values are
never committed as dashboard data or presented as actual model runs.
