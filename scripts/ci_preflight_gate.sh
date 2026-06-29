#!/usr/bin/env bash
# Production-preflight CI gate (issue #70). Single source of truth shared by BOTH
# the local gate (scripts/ci_gate.sh) and the remote CI (.github/workflows/ci.yml
# production-preflight job) so the two never drift.
#
# Asserts the fail-closed startup preflight (scripts/preflight.py, issue #66):
#   1. the dev .env.example defaults under CUTAGENT_ENV=production MUST fail;
#   2. a minimal safe production config MUST pass.
#
# Self-skips until scripts/preflight.py is merged (PR for #66), so this gate can
# land independently and activates automatically once the preflight ships.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"

if [ ! -f scripts/preflight.py ]; then
  echo "ci_preflight_gate: scripts/preflight.py not present yet (#66 preflight not merged) — skipping."
  exit 0
fi

echo "== production preflight MUST FAIL on unsafe (dev-default) production config =="
set +e
env -i PATH="$PATH" HOME="$HOME" \
  CUTAGENT_ENV=production \
  CUTAGENT_STORAGE_BACKEND=memory \
  CUTAGENT_ALLOW_SANDBOX_FALLBACK=1 \
  "$PYTHON_BIN" scripts/preflight.py >/dev/null 2>&1
rc=$?
set -e
if [ "$rc" -eq 0 ]; then
  echo "FAIL: preflight unexpectedly PASSED on an unsafe production config." >&2
  exit 1
fi
echo "  ok (preflight exited $rc as expected)"

echo "== production preflight MUST PASS on a minimal safe production config =="
env -i PATH="$PATH" HOME="$HOME" \
  CUTAGENT_ENV=production \
  CUTAGENT_STORAGE_BACKEND=sqlalchemy \
  CUTAGENT_DATABASE_URL="postgresql+psycopg://u:p@db:5432/cutagent" \
  CUTAGENT_REGISTRATION_OPEN=false \
  CUTAGENT_REGISTRATION_CODE_SALT=ci-unique-production-salt \
  CUTAGENT_SEED_LOCAL_AUTH=false \
  CUTAGENT_AUTH_COOKIE_SECURE=true \
  CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST=1 \
  CUTAGENT_PUBLISHING_LOCAL_PROXY=1 \
  CUTAGENT_WORKFLOW_RUNTIME=local \
  "$PYTHON_BIN" scripts/preflight.py
echo "  ok (preflight passed)"
