#!/usr/bin/env bash
# Print the Vercel env vars that point the deployed backend + MCP at the AMD box, given the public HTTPS URLs of
# the laptop tunnel ports (e.g. from `cloudflared tunnel --url http://localhost:1808x`). The box's System 2 key is
# copied to the clipboard (pbcopy), not printed; pass --show-key to print it.
#   show_vercel_env.sh <laya-url (18082)> <embed-url (18081)> <system2-url (18083)> [--show-key]
set -euo pipefail
[ $# -ge 3 ] || { sed -n '2,6p' "$0"; exit 2; }
LAYA="${1%/}" EMBED="${2%/}" S2="${3%/}"
KEY=$(ssh -o BatchMode=yes "${SPOT_AMD_HOST:-spot-amd}" "grep '^SYSTEM2_API_KEY=' /root/spot-secrets.env | cut -d= -f2-")
[ -n "$KEY" ] || { echo "no SYSTEM2_API_KEY on the box: run bootstrap.sh first" >&2; exit 1; }
if [ "${4:-}" = "--show-key" ]; then SHOWN="$KEY"; else printf %s "$KEY" | pbcopy; SHOWN="<on your clipboard>"; fi
for u in "$LAYA/health" "$EMBED/health" "$S2/health"; do
  printf '%-60s %s\n' "$u" "$(curl -s -m 30 -o /dev/null -w '%{http_code}' "$u")"
done
cat <<OUT

── Vercel: backend project ──────────────────────────────
SYSTEM1_URL=$LAYA
EMBED_URL=$EMBED
SYSTEM2_URL=$S2
SYSTEM2_API_KEY=$SHOWN
SYSTEM1_TIMEOUT_S=15
EMBED_TIMEOUT_S=15
SEARCH_EMBED_TIMEOUT_S=8
SYSTEM2_TIMEOUT_S=110
REQUEST_DEADLINE_S=120
(leave ORCHESTRATOR_URL unset: the orchestrator does not run on Vercel or the box)

── Vercel: MCP project ──────────────────────────────────
EMBED_URL=$EMBED

Redeploy both, then: curl -s https://<backend>.vercel.app/health | jq '{system1, embed, system2}'
OUT
