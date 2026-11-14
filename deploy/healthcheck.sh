#!/usr/bin/env bash
# Post-deploy check, run ON the EC2 host (by the Deploy workflow): every service is running - and healthy where
# it has a health check - the API answers /health from inside the network, and Grafana answers when it runs.
# Exit code 0 = healthy.
set -uo pipefail
cd ~/sem7
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
fail=0

while IFS=$'\t' read -r svc state health; do
  if [[ "$state" != "running" || ( -n "$health" && "$health" != "healthy" ) ]]; then
    echo "✗ $svc: $state ${health:-}"; fail=1
  else
    echo "✓ $svc: running ${health:-}"
  fi
done < <("${COMPOSE[@]}" ps --format '{{.Service}}\t{{.State}}\t{{.Health}}')

api="$("${COMPOSE[@]}" exec -T api python -c \
  "import json, urllib.request; print(json.load(urllib.request.urlopen('http://localhost:8000/health'))['status'])" 2>&1)"
if [[ "$api" == "ok" ]]; then echo "✓ api /health: ok"; else echo "✗ api /health: $api"; fail=1; fi

if "${COMPOSE[@]}" ps --services --status running | grep -qx grafana; then
  if "${COMPOSE[@]}" exec -T grafana wget -qO- http://localhost:3000/api/health | grep -q '"database": *"ok"'; then
    echo "✓ grafana /api/health: ok"
  else
    echo "✗ grafana /api/health"; fail=1
  fi
fi
exit "$fail"
