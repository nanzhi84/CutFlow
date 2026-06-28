# Repository Cleanup Log

Condensed after the final documentation cleanup request. This file keeps the review-critical facts and removes the earlier command-by-command transcript.

## Scope

- Branch: `maintenance/repo-hygiene-deep-cleanup`
- PR: `https://github.com/nanzhi84/cutagent-genesis/pull/61`
- Base: branch started aligned with `origin/main` (`0 0`).
- Goal: behavior-preserving repository hygiene cleanup: delete dead frontend files/helpers, consolidate duplicate runtime helpers, fix stale docs/config, and leave high-risk dynamic surfaces untouched.

## Repository Coverage

| Area | Status | Notes |
| --- | --- | --- |
| `.github/` | clean | CI workflow traced; no workflow/action deletion accepted. |
| `apps/api/` | changed and validated | Shared constants/helpers imported where safe; backend API contracts kept intact. |
| `apps/connectors/` | clean | CLI/import tests traced; no deletion accepted. |
| `apps/web/` | changed and validated | Deleted compatibility wrappers/type probes; pruned unused helper exports/object properties; updated live guide. |
| `apps/worker/` | clean | Temporal worker entrypoint left untouched. |
| `deploy/` | clean | Temporal config traced; no cleanup accepted. |
| `docs/` | changed and compressed | Stale live docs fixed; cleanup docs condensed to key evidence. Historical milestone/audit docs left as records. |
| `packages/` | changed and validated | Duplicate storage/media/planning/production helpers consolidated; migrations/registries/seeds left untouched. |
| `scripts/` | changed and validated | `dev_up.sh`, `export_openapi.py`, and `ci_gate.sh` hardened; manual ops scripts kept. |
| `tests/` | changed and validated | Static-dead test parameters removed; focused probe tests updated. |

## Cleanup Summary

### Deleted Files

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

### Dead Frontend Helpers Removed

- Production-unused payload/type helpers from modal, batch, notification, and annotation modules.
- Unused API type exports from `apps/web/src/api/client.ts` and `apps/web/src/api/r6.ts`.
- Object-helper properties missed by normal export scans:
  - `caseAgentApi.drafts`
  - `caseAgentApi.adoptDraft`
  - `editorHandoffApi.createEditorHandoff`
  - `routePatterns.overview`
  - `routes.caseRuns`
  - `api.auth.me`
  - `api.mediaAssets.create`
  - `api.mediaAssets.detail`
  - `api.finishedVideos.delete`

`api.annotations.batch` was tested as a candidate and kept because TypeScript found a live cross-line call in `TemplatesTab.tsx`.

### Consolidations

- Case material-kind allowlist -> `CASE_MATERIAL_ASSET_KINDS`.
- Person subject terms -> `PERSON_SUBJECT_TERMS`.
- Import metadata parsing -> `packages/core/storage/import_metadata.py`.
- Performance mappers -> `packages/core/storage/performance_mappers.py`.
- Artifact contract-to-row mapping -> `artifact_to_row`.
- Loudness probing -> `packages/media/audio/loudness.py`.
- Timeline output artifact creation -> `_timeline_output.py`.
- Finished-video / Seedance package artifact creation.
- Digital-human run-running transition/funnel recording.

### Config And Docs Fixed

- Corrected migration range, child guide naming, router references, dev web port, storage backend docs, and worktree guidance.
- Fixed live OpenAPI export docs and `apps/web` package script to use `uv run --extra dev python`.
- Cleared proxy env inside `scripts/export_openapi.py` before app import.
- Made `scripts/ci_gate.sh` macOS-compatible via `timeout` / `gtimeout` / Python fallback.
- Removed stale live doc claims about deleted frontend type probes and `/publish-center*` route registration.
- Compressed cleanup documentation to summary form after the user requested less redundant documentation.

## Evidence Methods

- Repository-wide `rg` / `git grep` references.
- Frontend Knip full and production scans.
- Targeted object-property reference scans for `api.*`, `caseAgentApi.*`, `editorHandoffApi.*`, and `routes.*`.
- TypeScript unused-symbol checking and production build.
- Python `ruff`, `vulture`, import/reference review, and targeted pytest suites.
- OpenAPI/schema drift checks.
- DB and Temporal integration tests on clean temporary databases.
- Runtime duplicate scan with generated files, migrations, docs, and tests excluded.
- Remote GitHub Actions checks.

## Validation

Passed locally during the final sweep:

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
- `scripts/ci_gate.sh` on macOS with a clean temporary database and local infra.
- DB integration and Temporal integration tests on clean temporary DBs.

Remote CI has been checked after pushes; the latest pushed commit must be verified green before merge handoff.

## Failure Classification

- Preexisting `ruff` failures were fixed.
- Local proxy env caused some `httpx`/OpenAPI subprocess failures; reruns with proxy env cleared passed, and `export_openapi.py` now clears proxy env itself.
- Existing local dev DB had dirty auth seed state; integration passed on a fresh temporary DB and in remote CI.
- `scripts/ci_gate.sh` previously failed on macOS due missing GNU `timeout`; fixed with fallback.
- `jscpd` reports generated OpenAPI/schema self-duplicates if generated files are included; generated files are intentionally excluded from runtime duplicate decisions.
- `api.annotations.batch` was a false-positive deletion candidate and was kept after TypeScript caught the live cross-line call.

## Final Rescan

Multiple discovery rounds were completed. The last two rounds after the object-helper cleanup included:

- Direct reference scans for removed frontend helper properties.
- Full and production Knip.
- Vulture at confidence 80.
- Tracked build/cache artifact scan.
- `.env.example` reverse scan.
- Runtime `jscpd` with generated files excluded.

No new high-confidence runtime cleanup candidate remained.

## Remaining Risks

- Migrations, generated clients, Temporal workflow/activity registration, provider registries, seed scripts, fixtures, manual DB/OSS scripts, deployment hooks, and public backend API routes were intentionally treated as high-risk.
- Backend endpoints can remain valuable even when current frontend wrappers are unused; this cleanup only removes frontend wrapper properties unless backend deadness is proven separately.
