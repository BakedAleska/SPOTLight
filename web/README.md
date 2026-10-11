# SPOTlight dashboard

Run the frontend and backend together from the repository root:

```bash
make up
```

Open http://localhost:3000. Docker supplies Node.js and installs frontend dependencies automatically.
To start just the dashboard against the existing Docker backend:

```bash
docker compose up -d web
```

The overview fetches `/api/dashboard` immediately and every 10 seconds while visible. This Next.js server
route requests `/api/v1/insights/overview`, `/flags?status=OPEN&limit=500`, `/assets`, `/locations`, and
`/insights/reliability?group_by=battery_band` from FastAPI. Backend order and scores are preserved.
Up to 500 open escalated flags are displayed, with the total shown when the queue is larger. Metrics and
battery bands come from backend data, replacing the frontend CSV analysis. Failed requests show an error
and Retry; a failed refresh retains the last successful data with a connection warning. Refresh now
requests an immediate update.

The V2 comparison uses the backend's matched-battery metric (actions above 40% battery), rather than
unadjusted gain. Click a flag to see its evidence, status and action history. Load explanation requests
the backend's explanation on demand, with template/stub output labeled. Acknowledge or Resolve updates
the backend and removes the flag from the open queue. The panel stays open with Reopen flag available;
reopening returns it to OPEN and retains notes and audit history. The shell's search field remains unfinished.

## Reversible features

Set any of these in the root `.env` to `false` and run `docker compose up -d --no-deps web`:

```dotenv
FEATURE_FLAG_DETAILS=true
FEATURE_FLAG_ACTIONS=true
FEATURE_FLAG_EXPLANATIONS=true
```

Defaults are `true`. Actions and explanations can be disabled independently while details remain
available. Disabling details also disables both dependent features. Disabled features are hidden and
their Next.js routes reject requests. Set them back to `true` and run the same command to restore them.
The switches do not erase data or change the underlying FastAPI endpoints, and are not authentication.
For native development set them in `web/.env.local` and restart the dev server.

Reopen reverses the flag's status change; it does not delete the recorded acknowledgement, resolution,
or note. After closing an acknowledged/resolved flag, reopen its detail at `/?flag=<flag_id>`.

For native development with Node.js installed:

```bash
cd web
npm ci
npm run dev
```

The default backend is `http://127.0.0.1:8000/api/v1`. Set `BACKEND_URL` in `web/.env.local` to change it.
In Docker, set `WEB_BACKEND_URL` in the root `.env` instead. Include `/api/v1` in either URL. These are
server settings; the browser always requests its own `/api/dashboard` origin. No frontend admin secret
or browser CORS configuration is required for these reads.

## Deploy on Vercel

Import the repository as a new Vercel project with these settings:

- **Root Directory:** `web/`
- **Framework Preset:** Next.js (auto-detected; default build and output settings, no `vercel.json`)
- **Node.js:** 24.x, pinned by `engines.node` in `package.json`, matching CI

`/` is prerendered as a static shell at build time, and its data loads in the browser from `/api/dashboard`. That route
and the `/api/flags/*` routes run as Vercel Functions that proxy to FastAPI. Nothing reads `web/data/kit/*.csv` at
runtime, so no file tracing configuration is needed.

Environment variables (Project Settings → Environment Variables):

| Variable | Required | Value |
| --- | --- | --- |
| `BACKEND_URL` | Yes | Public HTTPS URL of the FastAPI backend, including `/api/v1` (its production domain, e.g. `https://<backend>.vercel.app/api/v1`: preview URLs sit behind Deployment Protection). Server-only; never sent to the browser. |
| `FEATURE_FLAG_DETAILS`, `FEATURE_FLAG_ACTIONS`, `FEATURE_FLAG_EXPLANATIONS` | No | Set `false` to switch a feature off (default `true`). |
| `EXPLAIN_TIMEOUT_MS` | No | Explanation request timeout, 5000–120000 (default 50000). |

Without `BACKEND_URL` the routes fall back to `http://127.0.0.1:8000/api/v1`, which does not exist on Vercel. The page
still loads, but the dashboard shows "Cannot reach the dashboard data". Env changes only apply to new deployments, so
redeploy after setting them. If the backend later moves to direct browser calls, it will need a `NEXT_PUBLIC_API_URL`
(inlined at build time) and CORS for the Vercel domain. The current server proxy needs neither.
