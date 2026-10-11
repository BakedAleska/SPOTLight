#!/usr/bin/env bash
# Entrypoint of the db tooling container (also usable on a host with the db/ venv active).
#   all            migrate + seed + refresh
#   refresh [args] ingest [args] (waits for the embedding service) -> graph -> status. graph and status run even
#                  if ingest fails; graph then runs with --no-embed (it still rebuilds nodes and edges and copies
#                  the document vectors). DB_GRAPH_ARGS (env) is passed to graph. Exits with the first failing
#                  step's code.
#   migrate|seed|reset|status ...   -> python db/setup.py <cmd> [args]
#   ingest [args]  -> python amd/embed/ingest.py [args]
#   smoke [args]   -> python db/smoke_test.py [args]
#   graph [args]   -> python db/graph_build.py [args]   (knowledge graph, contract §7; after ingest)
# EMBED_WAIT_S (default 900) bounds the wait for the embedding service; an EMBED_URL host name that does not
# resolve fails at once (amd/embed/client.py).
set -euo pipefail
cd "$(dirname "$0")/.."
cmd="${1:-all}"; shift || true

refresh() {
  local rc=0 grc=0 src=0 no_embed=""
  python amd/embed/ingest.py "$@" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "job.sh: ingest failed (exit $rc); building the graph with --no-embed" >&2
    no_embed="--no-embed"
  fi
  # shellcheck disable=SC2086  # DB_GRAPH_ARGS is a list of flags
  python db/graph_build.py ${DB_GRAPH_ARGS:-} $no_embed || grc=$?
  python db/setup.py status || src=$?
  if [ "$rc" -ne 0 ]; then return "$rc"; fi
  if [ "$grc" -ne 0 ]; then return "$grc"; fi
  return "$src"
}

case "$cmd" in
  all)     python db/setup.py migrate && python db/setup.py seed && refresh "$@" ;;
  refresh) refresh "$@" ;;
  migrate|seed|reset|status) python db/setup.py "$cmd" "$@" ;;
  ingest)  python amd/embed/ingest.py "$@" ;;
  smoke)   python db/smoke_test.py "$@" ;;
  graph)   python db/graph_build.py "$@" ;;
  *) echo "usage: job.sh all|refresh|migrate|seed|reset|status|ingest|graph|smoke [args]" >&2; exit 2 ;;
esac
