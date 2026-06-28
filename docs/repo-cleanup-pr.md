# Maintenance: deep repository hygiene cleanup

## PR Title

Maintenance: deep repository hygiene cleanup

## Executive Summary

This PR performs a behavior-preserving deep hygiene pass across the repo. It fixes stale docs/config references, resolves baseline lint failures, deletes obsolete frontend wrappers and contract probe files, removes production-unused frontend exports, consolidates duplicate runtime helpers, completes Settings/env sample coverage, cleans static-dead test parameters, hardens the frontend OpenAPI export entrypoint, and records the audit/validation trail.

The aggressive second pass intentionally added a few small canonical helpers, but removed substantially more repeated implementation: tracked changes after that pass were roughly `+166/-1215`, with four new helper files totaling 333 lines.

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

## Files Added

- `docs/repo-cleanup-log.md`
- `docs/repo-cleanup-inventory.md`
- `docs/repo-cleanup-pr.md`
- `packages/core/storage/import_metadata.py`
- `packages/core/storage/performance_mappers.py`
- `packages/media/audio/loudness.py`
- `packages/planning/material/subject_terms.py`
- `packages/production/pipeline/nodes/_timeline_output.py`

## Files Modified

- `.env.example`
- `AGENTS.md`
- `CLAUDE.md`
- `README.md`
- `apps/api/CLAUDE.md`
- `apps/api/services/cases.py`
- `apps/api/services/imports.py`
- `apps/web/CLAUDE.md`
- `apps/web/package.json`
- `apps/web/src/App.tsx`
- `apps/web/src/api/client.ts`
- `apps/web/src/api/r6.ts`
- `apps/web/src/components/AppShell.tsx`
- `apps/web/src/components/RequireAuth.tsx`
- `apps/web/src/components/account/AdminMembersPanel.tsx`
- `apps/web/src/components/account/ProfileSecurityPanel.tsx`
- `apps/web/src/components/account/RegistrationCodesPanel.tsx`
- `apps/web/src/components/annotation/AnnotationEditorModal.tsx`
- `apps/web/src/components/editor-handoff/EditorHandoffActions.tsx`
- `apps/web/src/components/library/libraryModel.ts`
- `apps/web/src/components/modals/CaseModal.tsx`
- `apps/web/src/components/overview/RecentRunsList.tsx`
- `apps/web/src/components/runs/RunDetailModal.tsx`
- `apps/web/src/components/studio-create/batchModel.ts`
- `apps/web/src/components/studio-create/studioCreateModel.ts`
- `apps/web/src/components/ui/Modal.tsx`
- `apps/web/src/hooks/notificationModel.ts`
- `apps/web/src/hooks/useTaskNotifications.ts`
- `apps/web/src/lib/queryClient.ts`
- `apps/web/src/main.tsx`
- `apps/web/src/pages/AnalyticsPage.tsx`
- `apps/web/src/pages/OverviewPage.tsx`
- `apps/web/src/pages/auth/LoginPage.tsx`
- `apps/web/src/pages/auth/RegisterPage.tsx`
- `apps/web/src/pages/ops/PromptManagementPage.tsx`
- `apps/web/src/pages/settings/SettingsPage.tsx`
- `apps/web/src/pages/studio/CaseAgentPage.tsx`
- `apps/web/src/pages/studio/CaseListPage.tsx`
- `apps/web/src/pages/studio/CaseProfilePage.tsx`
- `apps/web/src/pages/studio/RunsPage.tsx`
- `apps/web/src/pages/studio/StudioCreatePage.tsx`
- `apps/web/src/utils/annotationV4.ts`
- `docs/ROADMAP.md`
- `packages/core/contracts/__init__.py`
- `packages/core/contracts/media.py`
- `packages/core/storage/sqlalchemy_uploads.py`
- `packages/creative/cases/sqlalchemy_repository.py`
- `packages/creative/cases/sqlalchemy_rubric.py`
- `packages/media/annotation/bgm.py`
- `packages/media/voice_provider_bridge.py`
- `packages/planning/material/broll_pack.py`
- `packages/planning/material/portrait_pack.py`
- `packages/production/pipeline/_ffmpeg.py`
- `packages/production/pipeline/digital_human.py`
- `packages/production/pipeline/nodes/broll_planning.py`
- `packages/production/pipeline/nodes/broll_timeline_planning.py`
- `packages/production/pipeline/nodes/export_finished_video.py`
- `packages/production/pipeline/nodes/export_seedance_video.py`
- `packages/production/pipeline/nodes/timeline_planning.py`
- `packages/production/sqlalchemy_mappers.py`
- `packages/production/sqlalchemy_repository.py`
- `pyproject.toml`
- `scripts/clean_dangling_materials.py`
- `scripts/dev_up.sh`
- `scripts/export_openapi.py`
- `scripts/sync_materials.py`
- `tests/api/test_cases_profile.py`
- `tests/api/test_annotation_patch.py`
- `tests/conftest.py`
- `tests/contract/test_settings_config.py`
- `tests/creative/test_case_evolution_logic.py`
- `tests/creative/test_reference_extract.py`
- `tests/frontend/test_user_defaults_batch_notify.py`
- `tests/integration/test_parity_mapper.py`
- `tests/production/test_digital_human_ephemeral_gc.py`
- `tests/production/test_sqlalchemy_selection_reservations.py`
- `tests/production/test_yield_funnel_lifecycle.py`
- `tests/providers/test_sqlalchemy_voice_provider_wireup.py`
- `tests/temporal/test_temporal_runtime.py`

## Dependencies Removed

None.

## Duplicates Consolidated

- Case reusable-material kind allowlist into `CASE_MATERIAL_ASSET_KINDS`.
- Planning/material person-centric subject terms into `PERSON_SUBJECT_TERMS`.
- Import metadata parsing into `packages/core/storage/import_metadata.py`.
- Performance observation/score row mappers into `packages/core/storage/performance_mappers.py`.
- Artifact contract-to-row mapping into `artifact_to_row`.
- Loudness probing into `packages/media/audio/loudness.py`.
- Timeline-planning artifact `NodeOutput` construction into `_timeline_output.py`.
- Finished-video and Seedance export package artifact creation.
- Digital-human run-running transition/event/funnel recording.
- Frontend API helper types shared between `client.ts` and `r6.ts`.

## Docs Updated

- Added `docs/repo-cleanup-log.md`, `docs/repo-cleanup-inventory.md`, and this PR body.
- Updated stale Alembic migration range references.
- Corrected root child-guide naming from `AGENTS.md` to `CLAUDE.md`.
- Fixed README's nonexistent router reference, stale `dev_up.sh` web port, and storage backend enum drift.
- Clarified `.env.example` direct runtime env switches.
- Completed `.env.example` coverage for every env var read by `Settings` and added a contract guard to keep it covered.
- Updated roadmap worktree guidance for Codex vs Claude worktree locations.
- Removed README test-env duplication by referring to the manual setup block.
- Updated live contract-regeneration docs to use `uv run --extra dev python scripts/export_openapi.py`.
- Fixed README prose to reference `scripts/ci_gate.sh` instead of a nonexistent root `ci_gate.sh`.
- Removed stale `apps/web/CLAUDE.md` references to deleted typecheck probes.

## Validation Commands Run

- `uv run --extra dev ruff check .`
- `git diff --check`
- `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python -m pytest -q`
- `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python scripts/export_openapi.py`
- `(cd apps/web && npm run generate:api)`
- `(cd apps/web && npm run export:openapi)`
- `git diff --exit-code -- apps/web/src/api/openapi.json apps/web/src/api/schema.d.ts`
- `(cd apps/web && npx --yes knip --reporter compact)`
- `(cd apps/web && npx --yes knip --production --reporter compact && npx tsc -p tsconfig.json --noEmit --noUnusedLocals --noUnusedParameters && npm run build)`
- `uvx --from vulture vulture apps packages scripts tests --min-confidence 90 --exclude 'apps/web/node_modules,apps/web/dist,.venv'`
- `bash -c 'set -euo pipefail; set -a; source .env.example; set +a; test "$CUTAGENT_STORAGE_BACKEND" = sqlalchemy; test "$CUTAGENT_PUBLISH_ADAPTER" = xiaovmao.cdp'`
- Settings/env comparison script confirming no `Settings` env vars are missing from `.env.example`.
- `.env.example` reverse reference scan confirming no sample `CUTAGENT_*` variables exist only in the sample file.
- Deptry scan reviewed for dependency issues; no dependency removal accepted because findings were aliases, runtime/tool deps, transitive deps, or optional fallbacks.
- Targeted Python suites for imports, mappers, BGM/loudness, timeline/export nodes, digital-human runtime, workflow reuse, and frontend probes.
- DB integration tests on clean temporary database `cutagent_ci_cleanup_8e2d`.
- Temporal tests against the same clean DB plus shared MinIO durable/ephemeral buckets.
- Runtime source duplicate scan with `jscpd --min-lines 20 --min-tokens 120`, excluding docs/tests/generated/migrations.

## Tests Passing

- Default pytest suite.
- Python lint.
- Frontend `knip --production`.
- Frontend full `knip`.
- Frontend TypeScript unused-symbol gate.
- Frontend production build.
- OpenAPI and generated TypeScript schema drift checks.
- Settings/env coverage contract tests.
- Full high-confidence vulture dead-symbol scan.
- DB integration tests on a clean temporary database.
- Temporal integration tests with shared MinIO durable + ephemeral buckets.

## Known Preexisting Failures

- Baseline `ruff check .` failed before cleanup edits on E402/E702 findings; this PR fixes those failures.
- `npm run export:openapi` failed before the final blind-spot pass because the script used bare `python`, and then because local SOCKS proxy env polluted app import; this PR fixes both issues.
- Direct `scripts/ci_gate.sh` is blocked on this macOS host because GNU `timeout`/`gtimeout` is unavailable; equivalent subcommands were run manually.
- Integration against the existing local dev DB failed because local auth seed state is dirty; the same suite passed on a fresh temporary DB.

## Risks and Mitigations

- Migrations, generated clients, Temporal registration, provider registries, seed scripts, fixtures, manual DB/OSS scripts, and deployment hooks were treated as high-risk and left untouched unless direct evidence supported a safe change.
- The removed frontend typecheck probes had no package/CI/import references and were covered by `tsc`, frontend build, `knip`, and full pytest.
- The `export:openapi` package script is retained and fixed; Knip's `uv` binary is ignored narrowly because it is a repository-level Python tool, not an npm dependency.
- Mapper/helper consolidations were validated with targeted mapper/import/media/workflow tests plus full pytest.
- A first full pytest caught an unsafe `defaultForm` export deletion; the test now uses the public `loadStoredForm()` entry and the full suite passes.

## Reviewer Checklist

- [ ] Confirm deleted files have evidence in `docs/repo-cleanup-log.md`.
- [ ] Confirm consolidations preserve behavior and have targeted validation.
- [ ] Confirm generated API files have no drift.
- [ ] Confirm high-risk dynamic surfaces were not speculatively deleted.
- [ ] Confirm final rescan found no new high-confidence runtime cleanup candidates.
