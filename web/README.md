# web: SPOTlight operator console (Next.js)

The operator-facing UI. The system map is in `docs/ARCHITECTURE.md`.

```bash
cd web && npm install && npm run dev   # http://localhost:3000
```
It reads the backend at `BACKEND_URL` (default `http://localhost:8000`), so start that first — `make up` at the
repo root brings up the whole stack including this console on port 3000 (`web/Dockerfile`, profile `local`).

## Current state (2026-10-10)
- One page, `/` (`src/app/page.tsx`): the shift-start briefing. It shows the hidden-failures headline, stat tiles, the priority queue and the battery-cliff chart.
- The other sidebar items (Missions, Analytics, Assets, AI review, Settings) are placeholders (`src/components/shell/nav.ts`).
- **Data comes from the backend API**, read in the server process: `src/lib/api.ts` (the only place this app
  speaks HTTP) and `src/lib/dashboard.ts` (maps the responses onto the panels' props). The page calls
  `/insights/overview`, `/flags`, `/missions`, `/assets` and `/locations`; the flags are the ones the backend
  triaged with Laya and the policy validator, in Neon. Contract: `docs/backend-services-contract.md`,
  "Web console consumption".
- **Degraded mode.** If the backend can't be read, the page renders `src/lib/analysis.ts` instead — the kit CSVs in
  `data/kit/`, triaged in-process — behind a banner saying the numbers aren't live. A console that goes blank when
  one container is down is worse than one that shows sample data and labels it.
- `/api/health` is liveness for the container; it deliberately does not probe the backend.

## Next: the act half of the loop
The backend already serves the parts this page doesn't use yet:
- explanations (`POST /api/v1/flags/{id}/explain`) — Orbit-style question / answer / reasoning / action
- operator acknowledgements (`POST /api/v1/flags/{id}/ack`)
- the live replay (`POST /api/v1/replay/start`, needs `X-Admin-Token`)
- the live feed (`/api/v1/events`), forecasts, missions, search and `/api/v1/ask`

Those are writes, so they belong in Route Handlers or Server Actions, not in a Client Component: `BACKEND_URL` is
an internal address and `BACKEND_ADMIN_TOKEN` is a secret. Routes are listed in `backend/README.md`.

## Design rules (Figma track)
- Never show status by color alone: use color + icon + word.
- Everything works from the keyboard.
- Text meets WCAG AA contrast.
- Dark operator theme, with a light toggle.

## Environment
| Var | Meaning | Default |
|---|---|---|
| `BACKEND_URL` | the backend's base URL (no trailing `/api/v1`) | `http://localhost:8000` (image: `http://backend:8000`) |
| `BACKEND_TIMEOUT_MS` | per read; raise it for a slow Neon cold start | `10000` |
| `BACKEND_ADMIN_TOKEN` | `X-Admin-Token` for `/replay` and `/ops`; must equal the backend's `ADMIN_TOKEN` | unset |

None is `NEXT_PUBLIC_`, so none of them reaches the browser bundle.
