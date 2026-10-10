# Mission path preview

Open `/missions/replay` through Missions → Replay. Choose a mission and play,
pause, restart, change speed, or scrub recorded events. Errors appear only once
that event has been reached. Select a waypoint or an error to inspect its location.

The map is the supplied `fictional_floor_plan.png`. Its 1095 × 595 image anchors
are recorded in `src/lib/path-data.ts`, at the tips of the labeled waypoint pins.
The synthetic CSV x/y coordinates do not align with this illustration. Route
lines connect recorded stops and do not assert the robot's traveled geometry.
The robot uses `spotlight-logo-small.svg`.

Replay reads `data/kit/replay.ndjson`; the mission selector includes only missions
with recorded events. Playback advances one recorded event per tick (1.4 seconds
at 1×), not real robot time. No events are ingested into the backend by this page.

Live feed polls `/api/path-feed` every two seconds. The server forwards read-only
requests to the existing backend `/api/v1/events` with the selected mission and
cursor. `BACKEND_URL` follows the dashboard's existing configuration including
`/api/v1`. Position is the most recently observed waypoint, not continuous robot
telemetry. Disconnections retain observed events and show a retry status.

Validation: `npx tsx --test tests/path-data.test.ts`, `npm run test:flags`,
`npx tsc --noEmit`, `npm run lint`, and `npm run build` from `web/`.
