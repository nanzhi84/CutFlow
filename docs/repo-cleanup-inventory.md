# Repository Cleanup Inventory

Condensed inventory of accepted cleanup and intentionally retained high-risk items.

## Accepted Deletions

### Dead Files

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

### Dead Functions And Exports

- Frontend payload/type helpers in modal, batch, notification, and annotation modules.
- Duplicate/unused API type exports in `apps/web/src/api/client.ts` and `apps/web/src/api/r6.ts`.
- Frontend object-helper properties with no repository callers:
  - `caseAgentApi.drafts`
  - `caseAgentApi.adoptDraft`
  - `editorHandoffApi.createEditorHandoff`
  - `routePatterns.overview`
  - `routes.caseRuns`
  - `api.auth.me`
  - `api.mediaAssets.create`
  - `api.mediaAssets.detail`
  - `api.finishedVideos.delete`

## Accepted Consolidations

- `CASE_MATERIAL_ASSET_KINDS`
- `PERSON_SUBJECT_TERMS`
- `packages/core/storage/import_metadata.py`
- `packages/core/storage/performance_mappers.py`
- `packages/core/storage/sqlalchemy_uploads.py::artifact_to_row`
- `packages/media/audio/loudness.py`
- `packages/production/pipeline/nodes/_timeline_output.py`
- Finished-video and Seedance package artifact creation.
- Digital-human run-running transition/funnel recording.

## Docs And Config Cleanup

- Live repository docs now match current migration head, child guide names, web port, storage backend aliases, contract regeneration command, and worktree layout.
- `.env.example` coverage was brought in line with live `Settings` env reads.
- `apps/web/CLAUDE.md` no longer references deleted typecheck probes or nonexistent `/publish-center*` routing.
- Cleanup docs were compressed after the user requested key summaries instead of detailed transcripts.
- `scripts/export_openapi.py` clears local proxy env before app import.
- `scripts/ci_gate.sh` now works on macOS without GNU `timeout`.

## Dependencies Removed

None. Deptry findings were reviewed but rejected as aliases, runtime/tool dependencies, transitive dependencies, or optional fallbacks.

## Generated Or Build Artifacts

No tracked build/cache artifact was accepted for deletion. Local ignored artifacts such as `.venv/`, `apps/web/node_modules/`, `apps/web/dist/`, caches, and `__pycache__/` remain ignored-local state.

## Intentionally Retained High-Risk Items

- Alembic migrations.
- Generated OpenAPI files: `apps/web/src/api/openapi.json`, `apps/web/src/api/schema.d.ts`.
- Temporal workflow/activity registration.
- Provider registries and prompt bindings.
- Seed scripts and fixtures.
- Manual DB/OSS operational scripts.
- Deployment and CI hooks.
- Backend API routes whose current frontend wrapper is unused but whose contract may still be public or tested.
- Compatibility values needed for historical persisted data.

## False Positives Kept

- `api.annotations.batch`: cross-line frontend call in `TemplatesTab.tsx`, caught by TypeScript.
- `normalize_dsn` duplication in manual ops scripts: kept local to avoid widening high-risk script coupling.
- Generated OpenAPI/schema duplicate blocks: generated contract output, not runtime duplication.
