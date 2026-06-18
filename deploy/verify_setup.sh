#!/usr/bin/env bash
# Anchor — pre-flight verifier for the Alibaba Cloud setup.
#
# Run from the ECS host (or anywhere with .env + .venv + Splunk reachable):
#
#   bash deploy/verify_setup.sh
#
# Each check is independent and reports PASS / FAIL / SKIP. Exit code is
# the count of FAILs (0 = all good). Designed to be re-run after every
# config tweak.

set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0
FAIL=0
SKIP=0

green()  { printf '\033[1;32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
red()    { printf '\033[1;31mFAIL\033[0m %s\n'    "$*"; FAIL=$((FAIL+1)); }
yellow() { printf '\033[1;33mSKIP\033[0m %s\n'    "$*"; SKIP=$((SKIP+1)); }
info()   { printf '\033[1;36m----\033[0m %s\n'    "$*"; }

# ---- 1. .env file ----------------------------------------------------------

info ".env file"
if [[ -f .env ]]; then
  green ".env present"
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
else
  red ".env missing — copy from .env.example and fill in"
  echo "  Total: ${PASS} pass, ${FAIL} fail, ${SKIP} skipped"
  exit "${FAIL}"
fi

# ---- 2. Required env vars --------------------------------------------------

info "Required env vars"
for var in SPLUNK_HOST SPLUNK_PORT SPLUNK_USERNAME SPLUNK_PASSWORD; do
  if [[ -n "${!var:-}" ]]; then
    green "${var} is set"
  else
    red  "${var} is empty"
  fi
done

if [[ -n "${QWEN_API_KEY:-}" && "${QWEN_API_KEY}" != "sk-..." ]]; then
  green "QWEN_API_KEY looks set"
else
  red "QWEN_API_KEY is empty or placeholder"
fi

# ---- 3. Splunk reachability -----------------------------------------------

info "Splunk mgmt API"
URL="https://${SPLUNK_HOST}:${SPLUNK_PORT}/services/server/info"
if curl -ks --max-time 10 "${URL}" | grep -q '<title>'; then
  green "reachable at ${URL}"
else
  red "unreachable at ${URL} (check container running + security group)"
fi

# ---- 4. KV Store collections present --------------------------------------

info "KV Store schema"
if [[ -d .venv ]]; then
  if .venv/bin/python -c "
from anchor.splunk_client import connect
svc = connect()
need = {'anchors', 'drift_history', 'signal_weights'}
have = {c.name for c in svc.kvstore}
missing = need - have
if missing:
  print('MISSING:', missing)
  raise SystemExit(1)
print('all 3 collections present')
" 2>/dev/null; then
    green "all 3 KV collections present (anchors / drift_history / signal_weights)"
  else
    red "KV collections missing — re-run setup_ecs.sh or copy splunk/collections.conf"
  fi
else
  yellow ".venv missing — skipped KV check"
fi

# ---- 5. OSS credentials (optional) -----------------------------------------

info "OSS backup credentials"
if [[ -z "${OSS_ACCESS_KEY_ID:-}" ]]; then
  yellow "OSS_* env vars unset — backup is optional but recommended"
else
  if [[ -d .venv ]]; then
    if .venv/bin/python -c "
import os, oss2
auth = oss2.Auth(os.environ['OSS_ACCESS_KEY_ID'], os.environ['OSS_ACCESS_KEY_SECRET'])
bucket = oss2.Bucket(auth, os.environ['OSS_ENDPOINT'], os.environ['OSS_BUCKET'])
bucket.get_bucket_info()  # raises on auth / bucket-not-found
print('OSS bucket reachable')
" 2>/dev/null; then
      green "OSS bucket reachable with provided creds"
    else
      red "OSS check failed — verify endpoint/bucket/AK pair"
    fi
  else
    yellow ".venv missing — skipped OSS check"
  fi
fi

# ---- 6. End-to-end smoke (read-only) --------------------------------------

info "End-to-end CLI smoke"
if [[ -d .venv ]]; then
  if .venv/bin/anchor list >/dev/null 2>&1; then
    green "\`anchor list\` succeeded (CLI ↔ KV path is healthy)"
  else
    red "\`anchor list\` failed — run it manually to see the error"
  fi
else
  yellow ".venv missing — skipped CLI smoke"
fi

# ---- summary ---------------------------------------------------------------

printf '\n────────────────────────────────────────────────────────────────────\n'
printf ' Summary: \033[1;32m%d pass\033[0m, \033[1;31m%d fail\033[0m, \033[1;33m%d skipped\033[0m\n' "${PASS}" "${FAIL}" "${SKIP}"
printf '────────────────────────────────────────────────────────────────────\n'

exit "${FAIL}"
