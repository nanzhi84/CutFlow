#!/usr/bin/env bash
#
# Build apps/web as static production assets and publish them to the Singapore
# ECS. Caddy serves /srv/cutagent-dev-web/current for dev.shuying.cyou while
# /api and /ws continue to reverse-proxy to the Mac mini tunnel.
#
# Overridable:
#   CUTAGENT_WEB_DEPLOY_HOST=shuying
#   CUTAGENT_WEB_DEPLOY_ROOT=/srv/cutagent-dev-web
#   CUTAGENT_WEB_PUBLIC_URL=https://dev.shuying.cyou/
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEB_DIR="$ROOT/apps/web"

DEPLOY_HOST="${CUTAGENT_WEB_DEPLOY_HOST:-shuying}"
DEPLOY_ROOT="${CUTAGENT_WEB_DEPLOY_ROOT:-/srv/cutagent-dev-web}"
PUBLIC_URL="${CUTAGENT_WEB_PUBLIC_URL:-https://dev.shuying.cyou/}"
REMOTE_TARBALL="/tmp/cutagent-web-dist.tgz"

log() { printf '▸ %s\n' "$*"; }
ok() { printf '✓ %s\n' "$*"; }

artifact="$(mktemp "${TMPDIR:-/tmp}/cutagent-web-dist.XXXXXX.tgz")"
trap 'rm -f "$artifact"' EXIT

log "building web production bundle"
(cd "$WEB_DIR" && npm run build)

log "packing dist"
tar -C "$WEB_DIR/dist" -czf "$artifact" .

log "uploading to $DEPLOY_HOST:$REMOTE_TARBALL"
scp "$artifact" "$DEPLOY_HOST:$REMOTE_TARBALL"

log "activating release on $DEPLOY_HOST"
ssh "$DEPLOY_HOST" "DEPLOY_ROOT='$DEPLOY_ROOT' REMOTE_TARBALL='$REMOTE_TARBALL' bash -s" <<'REMOTE'
set -euo pipefail

release="$DEPLOY_ROOT/releases/$(date +%Y%m%d%H%M%S)"
install -d "$release"
tar -xzf "$REMOTE_TARBALL" -C "$release"
ln -sfn "$release" "$DEPLOY_ROOT/current"
if id caddy >/dev/null 2>&1; then
  chown -R caddy:caddy "$DEPLOY_ROOT" || true
fi

printf 'release=%s\n' "$release"
find "$release" -maxdepth 2 -type f -printf '%P %s\n' | sort
REMOTE

log "verifying $PUBLIC_URL"
curl -fsS -o /dev/null -w 'status=%{http_code} ttfb=%{time_starttransfer} total=%{time_total}\n' --max-time 20 "$PUBLIC_URL"
ok "web static deploy complete"
