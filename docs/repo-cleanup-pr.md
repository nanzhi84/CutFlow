# Maintenance: deep repository hygiene cleanup

## PR Title

Maintenance: deep repository hygiene cleanup

## Summary

Behavior-preserving repository hygiene pass across Python/FastAPI/Temporal and React/Vite code. The cleanup removes dead frontend files and helper properties, consolidates duplicated runtime helpers, fixes stale live docs/config, hardens local validation scripts, and keeps high-risk dynamic/backend contract surfaces intact.

Cleanup docs were intentionally compressed after the user asked to reduce redundant detailed documentation.

## Files Deleted

- `apps/web/src/components/Modal.tsx`
- `apps/web/src/components/State.tsx`
- `apps/web/src/components/Status.tsx`
- `apps/web/src/components/Toast.tsx`
- `apps/web/src/contracts/caseEdit.typecheck.ts`
- `apps/web/src/contracts/m6aA.typecheck.ts`
- `apps/web/src/contracts/m6aR3.typecheck.ts`
- `apps/web/src/contracts/m6aR4.typecheck.ts`
- `apps/web/src/contracts/m6aR5.typecheck.ts`
- `apps/web/src/contracts/m6aR6.typecheck.ts`
- `apps/web/src/contracts/m6eB.typecheck.ts`

## Key Code Changes

- Removed obsolete frontend UI compatibility wrappers and unused typecheck probes.
- Removed unused frontend API/route object-helper properties that normal export scans do not report.
- Consolidated import metadata parsing, performance mappers, artifact mapping, loudness probing, material subject terms, and timeline artifact output helpers.
- Fixed preexisting lint failures.
- Hardened `scripts/export_openapi.py` against inherited proxy env.
- Made `scripts/ci_gate.sh` work on macOS without GNU `timeout`.

## Docs Updated

- Corrected stale migration, route, command, worktree, and env/config references.
- Updated live OpenAPI regeneration guidance.
- Removed stale `apps/web` references to deleted type probes and nonexistent `/publish-center*` route registration.
- Condensed `docs/repo-cleanup-log.md`, `docs/repo-cleanup-inventory.md`, and this PR body to review-critical summaries.

## Dependencies Removed

None.

## Validation Evidence

Passed locally:

- `git diff --check`
- `uv run --extra dev ruff check .`
- `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python -m pytest -q`
- `(cd apps/web && npx tsc -p tsconfig.json --noEmit --noUnusedLocals --noUnusedParameters)`
- `(cd apps/web && npm run build)`
- `(cd apps/web && npx --yes knip --reporter compact)`
- `(cd apps/web && npx --yes knip --production --reporter compact)`
- `(cd apps/web && npm run export:openapi)`
- `(cd apps/web && npm run generate:api)`
- `git diff --exit-code -- apps/web/src/api/openapi.json apps/web/src/api/schema.d.ts`
- Full local `scripts/ci_gate.sh` on macOS with clean temporary DB and local infra.
- Clean temporary DB integration tests.
- Temporal integration tests with shared durable + ephemeral MinIO buckets.
- Runtime duplicate scan with generated files, docs, tests, and migrations excluded.

Remote GitHub Actions must be green before merge.

## Known Preexisting Or Environmental Issues

- Baseline `ruff` failures existed before cleanup and are fixed in this PR.
- Local proxy env previously broke OpenAPI export/import paths; export now clears proxy env.
- Existing local dev DB had dirty auth seed state; clean temp DB and remote CI passed.
- Generated OpenAPI/schema files can look duplicated to clone scanners and are intentionally excluded.

## Risks And Mitigations

- Backend API routes, migrations, generated clients, Temporal registration, provider registries, seed scripts, fixtures, manual ops scripts, and deployment hooks were not speculatively deleted.
- Frontend wrapper removals were checked with targeted reference scans, TypeScript, Knip, build, and pytest probes.
- Consolidations were validated by targeted tests plus full pytest.
- `api.annotations.batch` was kept after TypeScript caught a live cross-line call.

## Reviewer Checklist

- Confirm deleted frontend files/helper properties have no callers.
- Confirm generated API files have no drift.
- Confirm high-risk dynamic/backend contract surfaces were left intact.
- Confirm CI is green on the latest commit.
