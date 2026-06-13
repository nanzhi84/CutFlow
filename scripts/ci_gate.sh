#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"
DATABASE_URL="${CUTAGENT_DATABASE_URL:-postgresql+psycopg://cutagent:cutagent@localhost:55432/cutagent}"
TEMPORAL_ADDRESS="${CUTAGENT_TEMPORAL_ADDRESS:-localhost:7233}"

# MinIO/S3 endpoint + credentials for the tiered ObjectStore. Defaults mirror the
# local demo (docker-compose.yml + .env.example): minioadmin/minioadmin, path-style
# addressing, durable bucket cutagent-local and a distinct ephemeral bucket
# cutagent-ephemeral. The Temporal pytest run below sets
# CUTAGENT_WORKFLOW_RUNTIME=temporal; the ephemeral fail-fast guard
# (packages/core/storage/object_store_env.py) refuses a node-local ephemeral tier
# under Temporal, so the ephemeral tier must point at this shared MinIO.
OBJECTSTORE_ENDPOINT="${CUTAGENT_OBJECTSTORE_ENDPOINT:-http://127.0.0.1:9000}"
OBJECTSTORE_ACCESS_KEY="${CUTAGENT_OBJECTSTORE_ACCESS_KEY:-minioadmin}"
OBJECTSTORE_SECRET_KEY="${CUTAGENT_OBJECTSTORE_SECRET_KEY:-minioadmin}"
OBJECTSTORE_BUCKET="${CUTAGENT_OBJECTSTORE_BUCKET:-cutagent-local}"
EPHEMERAL_OBJECTSTORE_BUCKET="${CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET:-cutagent-ephemeral}"

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

# Pre-create the durable + ephemeral MinIO buckets (distinct names). The
# S3ObjectStore auto-creates its bucket on first connect, but creating them up
# front keeps the gate correct by construction and surfaces MinIO connectivity
# problems before the Temporal tests run. boto3 ships with the dev extra, so this
# uses the same client config the app uses (path-style, SigV4).
CUTAGENT_OBJECTSTORE_ENDPOINT="$OBJECTSTORE_ENDPOINT" \
CUTAGENT_OBJECTSTORE_ACCESS_KEY="$OBJECTSTORE_ACCESS_KEY" \
CUTAGENT_OBJECTSTORE_SECRET_KEY="$OBJECTSTORE_SECRET_KEY" \
CUTAGENT_OBJECTSTORE_BUCKET="$OBJECTSTORE_BUCKET" \
CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET="$EPHEMERAL_OBJECTSTORE_BUCKET" \
"$PYTHON_BIN" - <<'PY'
import os

import boto3
from botocore.config import Config

client = boto3.client(
    "s3",
    endpoint_url=os.environ["CUTAGENT_OBJECTSTORE_ENDPOINT"],
    aws_access_key_id=os.environ["CUTAGENT_OBJECTSTORE_ACCESS_KEY"],
    aws_secret_access_key=os.environ["CUTAGENT_OBJECTSTORE_SECRET_KEY"],
    region_name="us-east-1",
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
for bucket in (
    os.environ["CUTAGENT_OBJECTSTORE_BUCKET"],
    os.environ["CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET"],
):
    try:
        client.create_bucket(Bucket=bucket)
        print(f"created bucket {bucket}")
    except client.exceptions.BucketAlreadyOwnedByYou:
        print(f"bucket {bucket} already exists")
PY

# Mirror the local demo for the Temporal run: durable + ephemeral both on the
# shared MinIO (distinct buckets, path-style addressing). The ephemeral S3 config
# is what satisfies the fail-fast guard under CUTAGENT_WORKFLOW_RUNTIME=temporal.
CUTAGENT_RUN_TEMPORAL_TESTS=1 \
CUTAGENT_STORAGE_BACKEND=sqlalchemy \
CUTAGENT_DATABASE_URL="$DATABASE_URL" \
CUTAGENT_WORKFLOW_RUNTIME=temporal \
CUTAGENT_TEMPORAL_ADDRESS="$TEMPORAL_ADDRESS" \
CUTAGENT_TEMPORAL_NAMESPACE="${CUTAGENT_TEMPORAL_NAMESPACE:-default}" \
CUTAGENT_TEMPORAL_TASK_QUEUE="${CUTAGENT_TEMPORAL_TASK_QUEUE:-cutagent-production}" \
CUTAGENT_OBJECTSTORE_BACKEND=s3 \
CUTAGENT_OBJECTSTORE_ENDPOINT="$OBJECTSTORE_ENDPOINT" \
CUTAGENT_OBJECTSTORE_BUCKET="$OBJECTSTORE_BUCKET" \
CUTAGENT_OBJECTSTORE_ACCESS_KEY="$OBJECTSTORE_ACCESS_KEY" \
CUTAGENT_OBJECTSTORE_SECRET_KEY="$OBJECTSTORE_SECRET_KEY" \
CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE=path \
CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND=s3 \
CUTAGENT_EPHEMERAL_OBJECTSTORE_ENDPOINT="$OBJECTSTORE_ENDPOINT" \
CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET="$EPHEMERAL_OBJECTSTORE_BUCKET" \
CUTAGENT_EPHEMERAL_OBJECTSTORE_ACCESS_KEY="$OBJECTSTORE_ACCESS_KEY" \
CUTAGENT_EPHEMERAL_OBJECTSTORE_SECRET_KEY="$OBJECTSTORE_SECRET_KEY" \
CUTAGENT_EPHEMERAL_OBJECTSTORE_ADDRESSING_STYLE=path \
run_pytest tests/temporal
