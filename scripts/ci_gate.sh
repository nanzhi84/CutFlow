#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"
DATABASE_URL="${CUTAGENT_DATABASE_URL:-postgresql+psycopg://cutagent:cutagent@localhost:55432/cutagent}"
TEMPORAL_ADDRESS="${CUTAGENT_TEMPORAL_ADDRESS:-localhost:7233}"

run_pytest() {
  timeout -k 5 600 "$PYTHON_BIN" -m pytest -q "$@"
}

run_pytest

"$PYTHON_BIN" scripts/export_openapi.py
git diff --exit-code apps/web/src/api/openapi.json

(
  cd apps/web
  npm ci
  npm run generate:api
  git diff --exit-code src/api/schema.d.ts
  npm run build
)

CUTAGENT_RUN_DB_TESTS=1 \
CUTAGENT_STORAGE_BACKEND=sqlalchemy \
CUTAGENT_DATABASE_URL="$DATABASE_URL" \
"$PYTHON_BIN" scripts/bootstrap_database.py

CUTAGENT_RUN_DB_TESTS=1 \
CUTAGENT_STORAGE_BACKEND=sqlalchemy \
CUTAGENT_DATABASE_URL="$DATABASE_URL" \
run_pytest tests/integration

CUTAGENT_RUN_TEMPORAL_TESTS=1 \
CUTAGENT_STORAGE_BACKEND=sqlalchemy \
CUTAGENT_DATABASE_URL="$DATABASE_URL" \
CUTAGENT_WORKFLOW_RUNTIME=temporal \
CUTAGENT_TEMPORAL_ADDRESS="$TEMPORAL_ADDRESS" \
CUTAGENT_TEMPORAL_NAMESPACE="${CUTAGENT_TEMPORAL_NAMESPACE:-default}" \
CUTAGENT_TEMPORAL_TASK_QUEUE="${CUTAGENT_TEMPORAL_TASK_QUEUE:-cutagent-production}" \
run_pytest tests/temporal
