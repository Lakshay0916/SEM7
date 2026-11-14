#!/usr/bin/env bash
# Ship the committed code to the EC2 host and (re)start the stack.
#
#   deploy/deploy.sh <public-ip> [ssh-key]
#
# 1. git archive HEAD  -> copy to ~/sem7 on the server (server-side .env and auth file are kept)
# 2. first run only: generate .env (random Postgres password, SITE_HOST) and the web login
# 3. docker compose (base + prod overlay) up --build --wait
# 4. smoke test: /health through Caddy over HTTPS
#
# AI agents: if the local .env has a Gemini key (GOOGLE_API_KEY), it is written to the server's .env over SSH
# (never committed, never printed) and the A2A agent services (compose profile "agents") are started too.
# DEPLOY_AGENTS=0 deploys without them. Monitoring (OpenTelemetry Collector, Tempo, Prometheus, Grafana)
# is on by default; DEPLOY_MONITORING=0 leaves it off. The server-side steps are deploy/remote-deploy.sh,
# the same script the "Deploy" workflow runs over AWS SSM.
set -euo pipefail

IP="${1:?usage: deploy/deploy.sh <public-ip> [ssh-key]}"
KEY="${2:-$HOME/.ssh/id_ed25519}"
SITE_HOST="${IP//./-}.sslip.io"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "ubuntu@$IP")
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARBALL="$(mktemp -t sem7-XXXXXX).tar.gz"

echo "waiting for SSH and Docker on $IP ..."
for _ in $(seq 60); do
  "${SSH[@]}" test -f /var/lib/cloud/sem7-ready 2>/dev/null && break
  sleep 5
done
"${SSH[@]}" test -f /var/lib/cloud/sem7-ready || { echo "server not ready (cloud-init still running?)"; exit 1; }

echo "packaging $(git -C "$ROOT" rev-parse --short HEAD) ..."
git -C "$ROOT" archive --format=tar.gz -o "$TARBALL" HEAD
scp -i "$KEY" -q "$TARBALL" "ubuntu@$IP:/tmp/sem7.tar.gz"
rm -f "$TARBALL"

# Gemini key for the agent services: read from the local .env, piped over SSH stdin (not on the command line).
GEMINI_KEY="$(grep -E '^GOOGLE_API_KEY=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- || true)"
AGENTS=keep
[[ "${DEPLOY_AGENTS:-1}" == "0" ]] && AGENTS=0
if [[ "${DEPLOY_AGENTS:-1}" == "1" && -n "$GEMINI_KEY" ]]; then
  AGENTS=1
  printf '%s' "$GEMINI_KEY" | "${SSH[@]}" 'umask 077; mkdir -p ~/sem7; cat > ~/sem7/.gemini-key'
fi

# Same server-side steps as the CD workflow: deploy/remote-deploy.sh (sent over SSH stdin).
"${SSH[@]}" SITE_HOST="$SITE_HOST" AGENTS="$AGENTS" MONITORING="${DEPLOY_MONITORING:-1}" \
  RELEASE=/tmp/sem7.tar.gz bash -s < "$ROOT/deploy/remote-deploy.sh"

echo "smoke test ..."
for _ in $(seq 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "https://api.$SITE_HOST/health" || true)"
  [[ "$code" == "401" ]] && break   # 401 = HTTPS works and the login is enforced
  sleep 5
done
echo "https://api.$SITE_HOST/health -> HTTP $code (401 expected without login)"
echo
echo "UI:      https://$SITE_HOST"
echo "API:     https://api.$SITE_HOST/docs"
[[ "${DEPLOY_MONITORING:-1}" != "0" ]] && echo "Grafana: https://grafana.$SITE_HOST"
