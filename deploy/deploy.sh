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
# DEPLOY_AGENTS=0 deploys without them.
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
AGENTS=0
if [[ "${DEPLOY_AGENTS:-1}" == "1" && -n "$GEMINI_KEY" ]]; then
  AGENTS=1
  printf '%s' "$GEMINI_KEY" | "${SSH[@]}" 'umask 077; mkdir -p ~/sem7; cat > ~/sem7/.gemini-key'
fi

"${SSH[@]}" SITE_HOST="$SITE_HOST" AGENTS="$AGENTS" bash -s <<'REMOTE'
set -euo pipefail
mkdir -p ~/sem7 && cd ~/sem7
# Replace code, keep server-only secrets.
find . -mindepth 1 -maxdepth 1 ! -name .env ! -name .gemini-key ! -name deploy -exec rm -rf {} +
find deploy -mindepth 1 ! -name auth.caddy -delete 2>/dev/null || true
tar -xzf /tmp/sem7.tar.gz && rm /tmp/sem7.tar.gz

if [[ ! -f .env ]]; then
  cat > .env <<EOF
LLM_PROVIDER=heuristic
POSTGRES_USER=sem7
POSTGRES_PASSWORD=$(openssl rand -hex 24)
POSTGRES_DB=sem7
SITE_HOST=$SITE_HOST
EOF
  chmod 600 .env
fi

if [[ ! -f deploy/auth.caddy ]]; then
  PASS="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)"
  HASH="$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$PASS")"
  printf 'basic_auth {\n\tadmin %s\n}\n' "$HASH" > deploy/auth.caddy
  echo "WEB LOGIN (shown once): user=admin password=$PASS"
fi

# Agent services: put the key into .env (replacing any old value) and enable the "agents" compose profile.
if [[ "$AGENTS" == "1" && -f .gemini-key ]]; then
  KEY="$(cat .gemini-key)"; rm -f .gemini-key
  sed -i '/^GOOGLE_API_KEY=/d; /^GEMINI_API_KEY=/d; /^COMPOSE_PROFILES=/d' .env
  printf 'GOOGLE_API_KEY=%s\nGEMINI_API_KEY=%s\nCOMPOSE_PROFILES=agents\n' "$KEY" "$KEY" >> .env
  chmod 600 .env
  echo "agent services enabled (Gemini key set in server .env)"
elif [[ "$AGENTS" == "0" ]]; then
  sed -i '/^COMPOSE_PROFILES=/d' .env
fi

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --wait --remove-orphans
docker image prune -f >/dev/null
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
REMOTE

echo "smoke test ..."
for _ in $(seq 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "https://api.$SITE_HOST/health" || true)"
  [[ "$code" == "401" ]] && break   # 401 = HTTPS works and the login is enforced
  sleep 5
done
echo "https://api.$SITE_HOST/health -> HTTP $code (401 expected without login)"
echo
echo "UI:  https://$SITE_HOST"
echo "API: https://api.$SITE_HOST/docs"
