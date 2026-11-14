#!/usr/bin/env bash
# Runs ON the EC2 host: unpack a release and (re)start the stack. Used by both deploy paths:
#   deploy/deploy.sh                (from a laptop, over SSH)
#   .github/workflows/deploy.yml    (the "Deploy" button, over AWS SSM - no SSH)
#
# Input (environment):
#   RELEASE     path of the release tarball (git archive of the commit), default /tmp/sem7.tar.gz
#   SITE_HOST   <ip-with-dashes>.sslip.io
#   AGENTS      1 = a new Gemini key is waiting in ~/sem7/.gemini-key · 0 = no agent services ·
#               keep (default) = agent services on if the server already has a key
#   MONITORING  1 (default) = OpenTelemetry Collector, Tempo, Prometheus, Grafana · 0 = off
#
# Server-only secrets are kept between releases: .env (database password, keys) and deploy/auth.caddy (login).
set -euo pipefail
RELEASE="${RELEASE:-/tmp/sem7.tar.gz}"
AGENTS="${AGENTS:-keep}"
MONITORING="${MONITORING:-1}"
: "${SITE_HOST:?SITE_HOST is required}"

first_login() {  # a newly created login: shown once - but never into CI logs, where it goes to a server-only file
  if [[ -n "${CI:-}" ]]; then
    (umask 077; echo "$1: user=admin password=$2" >> ~/sem7-first-logins.txt)
    echo "$1 created: saved on the server in ~/sem7-first-logins.txt (not printed in CI logs)"
  else
    echo "$1 (shown once): user=admin password=$2"
  fi
}

mkdir -p ~/sem7 && cd ~/sem7
# Replace the code, keep server-only secrets.
find . -mindepth 1 -maxdepth 1 ! -name .env ! -name .gemini-key ! -name deploy -exec rm -rf {} +
find deploy -mindepth 1 ! -name auth.caddy -delete 2>/dev/null || true
tar -xzf "$RELEASE" && rm -f "$RELEASE"

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
  first_login "WEB LOGIN" "$PASS"
fi

set_env() {  # set_env KEY VALUE - replace or add one line of .env
  sed -i "/^$1=/d" .env
  printf '%s=%s\n' "$1" "$2" >> .env
}

# Agent services: a new key replaces the old one; "keep" leaves whatever the server has.
if [[ "$AGENTS" == "1" && -f .gemini-key ]]; then
  KEY="$(cat .gemini-key)"; rm -f .gemini-key
  set_env GOOGLE_API_KEY "$KEY"
  set_env GEMINI_API_KEY "$KEY"
  echo "agent services: Gemini key updated"
fi
PROFILES=()
if [[ "$AGENTS" != "0" ]] && grep -q '^GOOGLE_API_KEY=.' .env; then
  PROFILES+=(agents)
fi

# Monitoring: the services export OpenTelemetry to the Collector; Grafana gets an admin password once.
if [[ "$MONITORING" != "0" ]]; then
  PROFILES+=(monitoring)
  set_env OTEL_EXPORTER_OTLP_ENDPOINT http://otel-collector:4318
  set_env SEM7_ENV production
  set_env GRAFANA_URL "https://grafana.$SITE_HOST"
  if ! grep -q '^GRAFANA_ADMIN_PASSWORD=.' .env; then
    GPASS="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)"
    set_env GRAFANA_ADMIN_PASSWORD "$GPASS"
    first_login "GRAFANA ADMIN" "$GPASS"
  fi
else
  sed -i '/^OTEL_EXPORTER_OTLP_ENDPOINT=/d; /^GRAFANA_URL=/d' .env
fi
set_env COMPOSE_PROFILES "$(IFS=,; echo "${PROFILES[*]:-}")"
chmod 600 .env
echo "compose profiles: ${PROFILES[*]:-none}"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
"${COMPOSE[@]}" up -d --build --wait --remove-orphans

# Caddy and the monitoring services bind-mount config files and folders. A release deletes and re-creates
# them, and a running container keeps pointing at the deleted copy (a re-created folder is empty for it),
# so they are restarted on every release - a few seconds, and always the current config.
for svc in caddy otel-collector tempo prometheus grafana; do
  if "${COMPOSE[@]}" ps --services --status running | grep -qx "$svc"; then
    "${COMPOSE[@]}" restart "$svc" >/dev/null && echo "restarted $svc (fresh config)"
  fi
done
docker image prune -f >/dev/null
"${COMPOSE[@]}" ps --format '{{.Service}}\t{{.Status}}'
