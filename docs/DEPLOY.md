# Deployment on AWS EC2

The three-container stack (UI + API + PostgreSQL) runs on a single EC2 instance using the
same `docker-compose.yml` as local development, plus a small production overlay.
Everything is created with plain AWS CLI scripts (no Terraform).

```
 Browser ──HTTPS──▶ EC2 t3.small (ap-south-1, Ubuntu 24.04, Docker)
                    ┌────────────────────────────────────────────────────┐
                    │  caddy :80/:443  (TLS cert + login)                 │
                    │     ├── <ip>.sslip.io      ──▶ ui  :8502 (Streamlit)│
                    │     └── api.<ip>.sslip.io  ──▶ api :8000 (FastAPI) │
                    │                                  └──▶ db :5432 (PG) │
                    └────────────────────────────────────────────────────┘
 Security group: 22 from the admin IP only · 80/443 public. ui/api/db are not exposed.
```

## Files

| File | Purpose |
|---|---|
| `deploy/provision.sh` | Creates key pair, security group, instance (20 GB gp3), Elastic IP. Idempotent. |
| `deploy/cloud-init.sh` | First-boot script: 2 GB swap, Docker Engine + compose plugin. |
| `deploy/deploy.sh` | Ships the committed code (`git archive`), generates server secrets on first run, `docker compose up --build --wait`, HTTPS smoke test. |
| `deploy/Caddyfile` | Reverse proxy: automatic Let's Encrypt HTTPS and basic-auth login for UI and API. |
| `docker-compose.prod.yml` | Overlay: removes host ports from ui/api, adds the Caddy service. |
| `deploy/teardown.sh` | Deletes everything that was created (stops all charges). |

## Prerequisites

- AWS CLI profile with EC2 access, e.g. an IAM user with `AmazonEC2FullAccess` and
  `AmazonSSMReadOnlyAccess` configured via `aws configure --profile sem7` (region `ap-south-1`).
- `~/.ssh/id_ed25519.pub` (imported as the instance key).

## Commands

```bash
AWS_PROFILE=sem7 deploy/provision.sh        # prints the public IP
deploy/deploy.sh <public-ip>                # first run prints the web login once
# after code changes: commit, then
deploy/deploy.sh <public-ip>
AWS_PROFILE=sem7 deploy/teardown.sh          # remove everything
```

URLs: `https://<ip-with-dashes>.sslip.io` (UI) and `https://api.<ip-with-dashes>.sslip.io/docs` (API).
[sslip.io](https://sslip.io) is free wildcard DNS that maps the name to the IP, which lets Caddy
obtain a real certificate without buying a domain.

## Secrets

Generated on the server on the first deploy and never committed:

- `~/sem7/.env`: random PostgreSQL password, `SITE_HOST`.
- `~/sem7/deploy/auth.caddy`: bcrypt hash of the web login (user `admin`).

To reset the web login, delete `deploy/auth.caddy` on the server and run `deploy/deploy.sh` again.

## Operating

```bash
ssh ubuntu@<ip>
cd ~/sem7
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f api
```

Containers use `restart: unless-stopped`, so the stack comes back after a reboot.
If your home IP changes, re-run `deploy/provision.sh` to allow SSH from the new address.

## Cost (ap-south-1, approximate)

| Item | Cost |
|---|---|
| t3.small running | ~US$0.50/day |
| 20 GB gp3 disk | ~US$1.6/month |
| Elastic IP | ~US$0.12/day (public IPv4 is charged whether attached or not) |

Stopping the instance in the console stops the compute charge. `deploy/teardown.sh`
removes everything.
