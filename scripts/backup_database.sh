#!/usr/bin/env bash
# Create a PostgreSQL custom-format backup and prove it can be restored before publishing it.
set -Eeuo pipefail
umask 077

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_DIR="${CUTAGENT_BACKUP_DIR:-$ROOT/.data/db-backups}"
BACKUP_PREFIX="${CUTAGENT_BACKUP_PREFIX:-cutagent-auto}"
RETENTION_DAYS="${CUTAGENT_BACKUP_RETENTION_DAYS:-14}"
MAX_COUNT="${CUTAGENT_BACKUP_MAX_COUNT:-32}"
MAX_BYTES="${CUTAGENT_BACKUP_MAX_BYTES:-2147483648}"
MIN_KEEP="${CUTAGENT_BACKUP_MIN_KEEP:-4}"
LOG_FILE="${CUTAGENT_BACKUP_LOG_FILE:-}"
LOG_MAX_BYTES="${CUTAGENT_BACKUP_LOG_MAX_BYTES:-10485760}"
PYTHON_BIN="${CUTAGENT_BACKUP_PYTHON:-/usr/bin/python3}"
VERIFY_RESTORE=1
PRUNE_AFTER_BACKUP=1
PRINT_CONFIG=0

main_checkout_dir() {
  local common_dir
  common_dir="$(git -C "$ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
  if [[ -n "$common_dir" ]]; then
    [[ "$common_dir" == /* ]] || common_dir="$ROOT/$common_dir"
    if [[ "$(basename "$common_dir")" == ".git" ]]; then
      dirname "$common_dir"
      return 0
    fi
  fi
  case "$ROOT" in
    */.claude/worktrees/*) printf '%s\n' "${ROOT%%/.claude/worktrees/*}" ;;
    *) printf '%s\n' "$ROOT" ;;
  esac
}

COMPOSE_DIR="$(main_checkout_dir)"
[[ -f "$COMPOSE_DIR/docker-compose.yml" ]] || COMPOSE_DIR="$ROOT"
COMPOSE_PROJECT="${CUTAGENT_BACKUP_COMPOSE_PROJECT:-$(basename "$COMPOSE_DIR")}"

usage() {
  cat <<'EOF'
Usage: scripts/backup_database.sh [--no-verify] [--no-prune] [--print-config]

The default path creates a pg_dump custom archive, restores it into an isolated
temporary database, records verification metadata, publishes it atomically, and
then applies the bounded retention policy.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-verify) VERIFY_RESTORE=0 ;;
    --no-prune) PRUNE_AFTER_BACKUP=0 ;;
    --print-config) PRINT_CONFIG=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

file_size() {
  wc -c <"$1" | tr -d '[:space:]'
}

configure_log() {
  [[ -n "$LOG_FILE" ]] || return 0
  mkdir -p "$(dirname "$LOG_FILE")"
  chmod 700 "$(dirname "$LOG_FILE")"
  if [[ -f "$LOG_FILE" ]] && (( $(file_size "$LOG_FILE") > LOG_MAX_BYTES )); then
    local keep_bytes temporary
    keep_bytes=$((LOG_MAX_BYTES / 2))
    (( keep_bytes > 0 )) || keep_bytes=1
    temporary="$(dirname "$LOG_FILE")/.${LOG_FILE##*/}.rotate"
    tail -c "$keep_bytes" "$LOG_FILE" >"$temporary"
    chmod 600 "$temporary"
    mv -f "$temporary" "$LOG_FILE"
  fi
  touch "$LOG_FILE"
  chmod 600 "$LOG_FILE"
  exec >>"$LOG_FILE" 2>&1
}

configure_log

timestamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() { printf '[%s] %s\n' "$(timestamp)" "$*"; }
die() { log "失败：$*" >&2; exit 1; }

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_DIR/docker-compose.yml" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_DIR/docker-compose.yml" "$@"
  else
    die "找不到 docker compose"
  fi
}

if (( PRINT_CONFIG )); then
  printf 'repo=%s\n' "$ROOT"
  printf 'compose_dir=%s\n' "$COMPOSE_DIR"
  printf 'compose_project=%s\n' "$COMPOSE_PROJECT"
  printf 'backup_dir=%s\n' "$BACKUP_DIR"
  printf 'prefix=%s\n' "$BACKUP_PREFIX"
  printf 'retention_days=%s\n' "$RETENTION_DAYS"
  printf 'max_count=%s\n' "$MAX_COUNT"
  printf 'max_bytes=%s\n' "$MAX_BYTES"
  printf 'min_keep=%s\n' "$MIN_KEEP"
  printf 'verify_restore=%s\n' "$VERIFY_RESTORE"
  exit 0
fi

command -v docker >/dev/null 2>&1 || die "找不到 docker"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || true)"
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || die "找不到 python3"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

LOCK_DIR="$BACKUP_DIR/.backup.lock"
LOCK_OWNED=0
PARTIAL_DUMP=""
PARTIAL_SHA=""
PARTIAL_MANIFEST=""
FINAL_DUMP=""
FINAL_SHA=""
FINAL_MANIFEST=""
PUBLISHED=0
CONTAINER_ID=""
CONTAINER_DUMP=""
VERIFY_DB=""
LAST_ERROR=0

release_lock() {
  if (( LOCK_OWNED )); then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
    LOCK_OWNED=0
  fi
}

cleanup_verification() {
  local cleanup_status=0
  if [[ -n "$VERIFY_DB" && -n "$CONTAINER_ID" ]]; then
    if docker exec -e VERIFY_DB="$VERIFY_DB" "$CONTAINER_ID" sh -eu -c \
      'dropdb --if-exists --force -U "$POSTGRES_USER" "$VERIFY_DB"' >/dev/null 2>&1; then
      VERIFY_DB=""
    else
      cleanup_status=1
    fi
  fi
  if [[ -n "$CONTAINER_DUMP" && -n "$CONTAINER_ID" ]]; then
    if docker exec -e DUMP_PATH="$CONTAINER_DUMP" "$CONTAINER_ID" sh -eu -c \
      'rm -f "$DUMP_PATH"' >/dev/null 2>&1; then
      CONTAINER_DUMP=""
    else
      cleanup_status=1
    fi
  fi
  return "$cleanup_status"
}

cleanup() {
  local status=$?
  if (( status == 0 && LAST_ERROR != 0 )); then
    status="$LAST_ERROR"
  fi
  trap - EXIT
  set +e
  cleanup_verification
  [[ -n "$PARTIAL_DUMP" ]] && rm -f "$PARTIAL_DUMP"
  [[ -n "$PARTIAL_SHA" ]] && rm -f "$PARTIAL_SHA"
  [[ -n "$PARTIAL_MANIFEST" ]] && rm -f "$PARTIAL_MANIFEST"
  if (( ! PUBLISHED )); then
    [[ -n "$FINAL_SHA" ]] && rm -f "$FINAL_SHA"
    [[ -n "$FINAL_MANIFEST" ]] && rm -f "$FINAL_MANIFEST"
  fi
  release_lock
  exit "$status"
}
trap 'LAST_ERROR=$?' ERR
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid="$(sed -n '1p' "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ "$lock_pid" =~ ^[0-9]+$ ]] && kill -0 "$lock_pid" 2>/dev/null; then
    log "已有备份任务运行（pid ${lock_pid}），本次跳过"
    exit 0
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || die "备份锁存在且无法安全回收：$LOCK_DIR"
  mkdir "$LOCK_DIR" || die "无法创建备份锁：$LOCK_DIR"
fi
LOCK_OWNED=1
printf '%s\n' "$$" >"$LOCK_DIR/pid"

CONTAINER_ID="$(compose ps -q postgres)"
[[ -n "$CONTAINER_ID" ]] || die "Postgres Compose 容器未运行"
[[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_ID")" == "true" ]] \
  || die "Postgres Compose 容器未运行"

DATABASE="$(docker exec "$CONTAINER_ID" sh -eu -c 'printf "%s" "$POSTGRES_DB"')"
DATABASE_USER="$(docker exec "$CONTAINER_ID" sh -eu -c 'printf "%s" "$POSTGRES_USER"')"
[[ -n "$DATABASE" && -n "$DATABASE_USER" ]] || die "容器缺少 POSTGRES_DB/POSTGRES_USER"
database_lower="$(printf '%s' "$DATABASE" | tr '[:upper:]' '[:lower:]')"
if [[ "$database_lower" =~ (^|[-_])(test|tests|ci|tmp|temp|verify|verification)([-_]|$) ]]; then
  die "拒绝备份疑似测试/临时数据库：$DATABASE"
fi

# A killed verification run must not leave its temporary database consuming space forever.
docker exec "$CONTAINER_ID" sh -eu -c '
  psql -X -U "$POSTGRES_USER" -d postgres -Atqc \
    "SELECT datname FROM pg_database WHERE datname LIKE '\''cutagent_backup_verify_%'\''" |
  while IFS= read -r stale_db; do
    [ -z "$stale_db" ] || dropdb --if-exists --force -U "$POSTGRES_USER" "$stale_db"
  done
'

FILE_TIMESTAMP="$(date -u '+%Y%m%dT%H%M%SZ')"
CREATED_AT="$(timestamp)"
BASE_NAME="$BACKUP_PREFIX-$FILE_TIMESTAMP"
FINAL_DUMP="$BACKUP_DIR/$BASE_NAME.dump"
FINAL_SHA="$FINAL_DUMP.sha256"
FINAL_MANIFEST="$FINAL_DUMP.json"
PARTIAL_DUMP="$BACKUP_DIR/.$BASE_NAME.dump.partial"
PARTIAL_SHA="$BACKUP_DIR/.$BASE_NAME.dump.sha256.partial"
PARTIAL_MANIFEST="$BACKUP_DIR/.$BASE_NAME.dump.json.partial"
[[ ! -e "$FINAL_DUMP" && ! -e "$PARTIAL_DUMP" ]] || die "备份文件名冲突：$BASE_NAME"

log "开始备份数据库 ${DATABASE}（Compose project: ${COMPOSE_PROJECT}）"
docker exec "$CONTAINER_ID" sh -eu -c '
  exec pg_dump --format=custom --compress=6 --no-owner --no-privileges \
    --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"
' >"$PARTIAL_DUMP"
[[ -s "$PARTIAL_DUMP" ]] || die "pg_dump 产物为空"
chmod 600 "$PARTIAL_DUMP"

CONTAINER_DUMP="/tmp/$BASE_NAME.dump"
docker cp "$PARTIAL_DUMP" "$CONTAINER_ID:$CONTAINER_DUMP" >/dev/null
docker exec -e DUMP_PATH="$CONTAINER_DUMP" "$CONTAINER_ID" sh -eu -c \
  'pg_restore --list "$DUMP_PATH" >/dev/null'

VERIFY_METHOD="archive_toc_only"
VERIFIED_AT=""
ALEMBIC_REVISION=""
PUBLIC_TABLE_COUNT=""
EMBEDDING_COUNT=""
EMBEDDING_DIMENSION_MIN=""
EMBEDDING_DIMENSION_MAX=""
EMBEDDING_DIMENSION_MISMATCH_COUNT=""
RESTORED_DATABASE_BYTES=""

if (( VERIFY_RESTORE )); then
  VERIFY_METHOD="isolated_pg_restore"
  VERIFY_DB="cutagent_backup_verify_$(date -u '+%Y%m%d_%H%M%S')_$$"
  log "在隔离临时库 $VERIFY_DB 执行恢复校验"
  docker exec -e VERIFY_DB="$VERIFY_DB" "$CONTAINER_ID" sh -eu -c '
    createdb -U "$POSTGRES_USER" "$VERIFY_DB"
  '
  docker exec -e VERIFY_DB="$VERIFY_DB" -e DUMP_PATH="$CONTAINER_DUMP" \
    "$CONTAINER_ID" sh -eu -c '
      pg_restore --exit-on-error --no-owner --no-privileges \
        -U "$POSTGRES_USER" -d "$VERIFY_DB" "$DUMP_PATH"
    '
  verification="$({
    docker exec -e VERIFY_DB="$VERIFY_DB" "$CONTAINER_ID" sh -eu -c '
      psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$VERIFY_DB" \
        -A -t -F "|" -c "
          SELECT
            COALESCE((SELECT version_num FROM public.alembic_version LIMIT 1), '\''unknown'\''),
            (SELECT count(*) FROM pg_catalog.pg_class AS c
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
              WHERE n.nspname = '\''public'\'' AND c.relkind IN ('\''r'\'', '\''p'\'')),
            (SELECT count(*) FROM public.clip_embedding_index),
            COALESCE((SELECT min(vector_dims(embedding))
              FROM public.clip_embedding_index), 0),
            COALESCE((SELECT max(vector_dims(embedding))
              FROM public.clip_embedding_index), 0),
            (SELECT count(*) FROM public.clip_embedding_index
              WHERE vector_dims(embedding) <> embedding_dimension),
            pg_database_size(current_database());"
    '
  } | tr -d '\r\n')"
  IFS='|' read -r ALEMBIC_REVISION PUBLIC_TABLE_COUNT EMBEDDING_COUNT \
    EMBEDDING_DIMENSION_MIN EMBEDDING_DIMENSION_MAX \
    EMBEDDING_DIMENSION_MISMATCH_COUNT RESTORED_DATABASE_BYTES <<<"$verification"
  [[ -n "$ALEMBIC_REVISION" && "$ALEMBIC_REVISION" != "unknown" ]] \
    || die "恢复校验未读到 Alembic revision"
  for numeric_value in "$PUBLIC_TABLE_COUNT" "$EMBEDDING_COUNT" \
    "$EMBEDDING_DIMENSION_MIN" "$EMBEDDING_DIMENSION_MAX" \
    "$EMBEDDING_DIMENSION_MISMATCH_COUNT" "$RESTORED_DATABASE_BYTES"; do
    [[ "$numeric_value" =~ ^[0-9]+$ ]] || die "恢复校验返回了非法统计值"
  done
  (( EMBEDDING_DIMENSION_MISMATCH_COUNT == 0 )) \
    || die "恢复后的 embedding 向量维度与元数据不一致"
  VERIFIED_AT="$(timestamp)"
  cleanup_verification || die "临时恢复库清理失败"
  log "恢复校验通过：revision=$ALEMBIC_REVISION embeddings=$EMBEDDING_COUNT dims=$EMBEDDING_DIMENSION_MIN..$EMBEDDING_DIMENSION_MAX"
else
  cleanup_verification || die "容器临时归档清理失败"
  log "仅完成归档 TOC 校验（已显式关闭完整恢复校验）"
fi

ARCHIVE_BYTES="$(file_size "$PARTIAL_DUMP")"
ARCHIVE_SHA256="$(shasum -a 256 "$PARTIAL_DUMP" | awk '{print $1}')"
POSTGRES_VERSION="$(docker exec "$CONTAINER_ID" sh -eu -c \
  'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SHOW server_version"')"
printf '%s  %s\n' "$ARCHIVE_SHA256" "${FINAL_DUMP##*/}" >"$PARTIAL_SHA"
chmod 600 "$PARTIAL_SHA"

BACKUP_ARCHIVE="${FINAL_DUMP##*/}" \
BACKUP_BYTES="$ARCHIVE_BYTES" \
BACKUP_SHA256="$ARCHIVE_SHA256" \
BACKUP_CREATED_AT="$CREATED_AT" \
BACKUP_DATABASE="$DATABASE" \
BACKUP_COMPOSE_PROJECT="$COMPOSE_PROJECT" \
BACKUP_POSTGRES_VERSION="$POSTGRES_VERSION" \
BACKUP_VERIFY_METHOD="$VERIFY_METHOD" \
BACKUP_VERIFIED_AT="$VERIFIED_AT" \
BACKUP_ALEMBIC_REVISION="$ALEMBIC_REVISION" \
BACKUP_PUBLIC_TABLE_COUNT="$PUBLIC_TABLE_COUNT" \
BACKUP_EMBEDDING_COUNT="$EMBEDDING_COUNT" \
BACKUP_EMBEDDING_DIMENSION_MIN="$EMBEDDING_DIMENSION_MIN" \
BACKUP_EMBEDDING_DIMENSION_MAX="$EMBEDDING_DIMENSION_MAX" \
BACKUP_EMBEDDING_DIMENSION_MISMATCH_COUNT="$EMBEDDING_DIMENSION_MISMATCH_COUNT" \
BACKUP_RESTORED_DATABASE_BYTES="$RESTORED_DATABASE_BYTES" \
"$PYTHON_BIN" - "$PARTIAL_MANIFEST" <<'PY'
import json
import os
import sys


def optional_int(name):
    value = os.environ.get(name, "")
    return int(value) if value else None


payload = {
    "format_version": 1,
    "archive": os.environ["BACKUP_ARCHIVE"],
    "bytes": int(os.environ["BACKUP_BYTES"]),
    "sha256": os.environ["BACKUP_SHA256"],
    "created_at": os.environ["BACKUP_CREATED_AT"],
    "database": os.environ["BACKUP_DATABASE"],
    "compose_project": os.environ["BACKUP_COMPOSE_PROJECT"],
    "postgres_version": os.environ["BACKUP_POSTGRES_VERSION"],
    "verification": {
        "method": os.environ["BACKUP_VERIFY_METHOD"],
        "verified_at": os.environ.get("BACKUP_VERIFIED_AT") or None,
        "alembic_revision": os.environ.get("BACKUP_ALEMBIC_REVISION") or None,
        "public_table_count": optional_int("BACKUP_PUBLIC_TABLE_COUNT"),
        "clip_embedding_count": optional_int("BACKUP_EMBEDDING_COUNT"),
        "embedding_dimension_min": optional_int("BACKUP_EMBEDDING_DIMENSION_MIN"),
        "embedding_dimension_max": optional_int("BACKUP_EMBEDDING_DIMENSION_MAX"),
        "embedding_dimension_mismatch_count": optional_int(
            "BACKUP_EMBEDDING_DIMENSION_MISMATCH_COUNT"
        ),
        "restored_database_bytes": optional_int("BACKUP_RESTORED_DATABASE_BYTES"),
    },
}
with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
    output.write("\n")
PY
chmod 600 "$PARTIAL_MANIFEST"

# Publish sidecars first and the dump last. The retention script discovers only
# final *.dump files, so it never observes a half-published backup set.
mv "$PARTIAL_SHA" "$FINAL_SHA"
PARTIAL_SHA=""
mv "$PARTIAL_MANIFEST" "$FINAL_MANIFEST"
PARTIAL_MANIFEST=""
mv "$PARTIAL_DUMP" "$FINAL_DUMP"
PARTIAL_DUMP=""
chmod 600 "$FINAL_DUMP" "$FINAL_SHA" "$FINAL_MANIFEST"
PUBLISHED=1
log "备份发布成功：${FINAL_DUMP}（${ARCHIVE_BYTES} bytes）"

release_lock
if (( PRUNE_AFTER_BACKUP )); then
  "$PYTHON_BIN" "$SCRIPT_DIR/prune_database_backups.py" \
    --backup-dir "$BACKUP_DIR" \
    --prefix "$BACKUP_PREFIX" \
    --retention-days "$RETENTION_DAYS" \
    --max-count "$MAX_COUNT" \
    --max-bytes "$MAX_BYTES" \
    --min-keep "$MIN_KEEP"
fi
