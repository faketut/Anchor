#!/usr/bin/env bash
# Anchor — one-shot ECS bootstrap.
#
# Idempotent. Run on a fresh Ubuntu 24.04 LTS ECS instance as root:
#
#   curl -fsSL https://raw.githubusercontent.com/faketut/Anchor/main/deploy/setup_ecs.sh | bash
#
# Or, after `git clone`:
#
#   bash deploy/setup_ecs.sh
#
# Covers sections 2-4 of deploy/alibaba-cloud.md:
#   - install Docker + python venv tooling
#   - clone Anchor (if not already in /opt/anchor)
#   - bring up Splunk with the Alibaba overlay
#   - install KV Store schema
#   - install Anchor CLI (with [alibaba] extra) for the OSS backup cron
#
# Out of scope (do these in the Alibaba Cloud console, then re-run this):
#   - provisioning the ECS instance itself
#   - opening security-group ports 22 + 8089 to your laptop IP
#   - creating the OSS bucket + RAM user
#   - filling in .env with SPLUNK_PASSWORD, QWEN_API_KEY, OSS_* creds

set -euo pipefail

ANCHOR_DIR="${ANCHOR_DIR:-/opt/anchor}"
REPO_URL="${REPO_URL:-https://github.com/faketut/Anchor.git}"
CONTAINER="${CONTAINER:-anchor-splunk}"

log() { printf '\n\033[1;36m[setup_ecs]\033[0m %s\n' "$*"; }

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (or with sudo). Got UID=${EUID}." >&2
  exit 1
fi

# ---- 1. apt packages -------------------------------------------------------

log "Installing system packages (docker, compose, python venv, git)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 git python3-venv curl

systemctl enable --now docker

# ---- 2. clone or update Anchor --------------------------------------------

if [[ ! -d "${ANCHOR_DIR}/.git" ]]; then
  log "Cloning Anchor into ${ANCHOR_DIR}…"
  git clone "${REPO_URL}" "${ANCHOR_DIR}"
else
  log "Updating existing checkout in ${ANCHOR_DIR}…"
  git -C "${ANCHOR_DIR}" pull --ff-only
fi

cd "${ANCHOR_DIR}"

# ---- 3. .env scaffold ------------------------------------------------------

if [[ ! -f .env ]]; then
  log "Creating .env from .env.example — EDIT THIS BEFORE PRODUCTION USE."
  cp .env.example .env
  echo
  echo "  >> Fill in /opt/anchor/.env with: SPLUNK_PASSWORD, QWEN_API_KEY,"
  echo "     and (for OSS backup) OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET"
  echo "     / OSS_ENDPOINT / OSS_BUCKET."
  echo
fi

# ---- 4. Splunk via docker compose -----------------------------------------

log "Bringing up Splunk with the Alibaba overlay…"
docker compose \
  -f docker-compose.yml \
  -f deploy/docker-compose.alibaba.yml \
  up -d

log "Waiting up to 120 s for Splunk mgmt API on :8089…"
for i in $(seq 1 24); do
  if curl -ks https://localhost:8089/services/server/info >/dev/null 2>&1; then
    log "Splunk is up (after ~$((i * 5)) s)."
    break
  fi
  sleep 5
  if [[ $i -eq 24 ]]; then
    echo "ERROR: Splunk did not respond on :8089 within 120 s." >&2
    echo "Try: docker compose logs splunk | tail -50" >&2
    exit 4
  fi
done

# ---- 5. KV Store schema ----------------------------------------------------

log "Installing KV Store collections.conf inside the container…"
docker exec -u splunk "${CONTAINER}" mkdir -p /opt/splunk/etc/apps/search/local
docker cp splunk/collections.conf "${CONTAINER}:/opt/splunk/etc/apps/search/local/collections.conf"
docker exec -u root "${CONTAINER}" chown splunk:splunk /opt/splunk/etc/apps/search/local/collections.conf
docker exec -u splunk "${CONTAINER}" /opt/splunk/bin/splunk restart >/dev/null

log "Waiting for Splunk to restart…"
for i in $(seq 1 24); do
  if curl -ks https://localhost:8089/services/server/info >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

# ---- 6. Anchor venv + CLI -------------------------------------------------

if [[ ! -d .venv ]]; then
  log "Creating Python virtualenv at ${ANCHOR_DIR}/.venv…"
  python3 -m venv .venv
fi

log "Installing Anchor CLI with [alibaba] extra (for OSS backup script)…"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e '.[alibaba]'

# ---- 7. summary ------------------------------------------------------------

cat <<EOF

────────────────────────────────────────────────────────────────────
 Anchor ECS bootstrap complete.

 Next steps (manual):

 1. Edit ${ANCHOR_DIR}/.env:
      SPLUNK_PASSWORD=<must match docker-compose env>
      QWEN_API_KEY=sk-...
      OSS_ACCESS_KEY_ID=LTAI...
      OSS_ACCESS_KEY_SECRET=...
      OSS_ENDPOINT=oss-ap-southeast-1.aliyuncs.com
      OSS_BUCKET=anchor-memory-backups-<suffix>

 2. Run the verifier:
      bash deploy/verify_setup.sh

 3. Schedule the daily OSS backup (section 7 of deploy/alibaba-cloud.md).

 4. From your laptop, set SPLUNK_HOST=<this-ecs-public-ip> and:
      anchor list   # should return [] for a fresh KV
────────────────────────────────────────────────────────────────────
EOF
