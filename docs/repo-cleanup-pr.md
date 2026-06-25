# Maintenance: deep repository hygiene cleanup

## PR Title

Maintenance: deep repository hygiene cleanup

## Executive Summary

This PR performs a behavior-preserving repository hygiene pass: it fixes stale docs/config references, resolves preexisting lint failures, consolidates duplicated constants, removes obsolete frontend compatibility wrappers, and documents the audit/validation trail.

## Files Deleted

- `apps/web/src/components/Modal.tsx`
- `apps/web/src/components/State.tsx`
- `apps/web/src/components/Status.tsx`
- `apps/web/src/components/Toast.tsx`

## Files Added

- `docs/repo-cleanup-log.md`
- `docs/repo-cleanup-inventory.md`
- `docs/repo-cleanup-pr.md`
- `packages/planning/material/subject_terms.py`

## Files Modified

- `.env.example`
- `README.md`
- `AGENTS.md`
- `apps/api/services/cases.py`
- `apps/web/src/App.tsx`
- `apps/web/src/components/AppShell.tsx`
- `apps/web/src/components/RequireAuth.tsx`
- `apps/web/src/components/account/AdminMembersPanel.tsx`
- `apps/web/src/components/account/ProfileSecurityPanel.tsx`
- `apps/web/src/components/account/RegistrationCodesPanel.tsx`
- `apps/web/src/components/annotation/AnnotationEditorModal.tsx`
- `apps/web/src/components/editor-handoff/EditorHandoffActions.tsx`
- `apps/web/src/components/modals/CaseModal.tsx`
- `apps/web/src/components/overview/RecentRunsList.tsx`
- `apps/web/src/components/runs/RunDetailModal.tsx`
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
- `packages/core/contracts/__init__.py`
- `packages/core/contracts/media.py`
- `packages/creative/cases/sqlalchemy_repository.py`
- `packages/production/pipeline/nodes/broll_planning.py`
- `packages/planning/material/broll_pack.py`
- `packages/planning/material/portrait_pack.py`
- `pyproject.toml`
- `scripts/clean_dangling_materials.py`
- `scripts/dev_up.sh`
- `scripts/sync_materials.py`
- `tests/api/test_cases_profile.py`
- `docs/ROADMAP.md`

## Dependencies Removed

None.

## Duplicates Consolidated

- Consolidated Case reusable-material kind allowlist into `packages/core/contracts/media.py::CASE_MATERIAL_ASSET_KINDS`.
- Consolidated planning/material person-centric subject terms into `packages/planning/material/subject_terms.py::PERSON_SUBJECT_TERMS`.
- Removed frontend compatibility wrapper files and imported canonical `components/ui/*` modules directly.

## Docs Updated

- Added cleanup working documents:
  - `docs/repo-cleanup-log.md`
  - `docs/repo-cleanup-inventory.md`
  - `docs/repo-cleanup-pr.md`
- Updated stale Alembic migration range references in `AGENTS.md` and `README.md`.
- Corrected root `AGENTS.md` child-guide naming and package-map drift.
- Fixed README's nonexistent `cost_estimate` router reference and stale `dev_up.sh` web port.
- Fixed README storage backend enum drift by documenting `postgres` as the SQLAlchemy backend alias.
- Clarified `.env.example` so direct runtime env switches are not mislabeled as `Settings` fields.
- Updated roadmap worktree guidance so Codex and Claude worktrees point at their respective tool-owned directories.

## Validation Commands Run

- `uv run --extra dev ruff check .` - failed before cleanup edits on preexisting E402/E702 findings, then passed after Batch 1.
- `uv run --extra dev python -m pytest -q` - passed.
- `(cd apps/web && npm ci && npm run build)` - passed.
- `uv run --extra dev python scripts/export_openapi.py && git diff --exit-code apps/web/src/api/openapi.json` - passed.
- `(cd apps/web && npm run generate:api && git diff --exit-code src/api/schema.d.ts)` - passed.
- `uv run --extra dev python -m pytest -q tests/scripts tests/connectors tests/production/test_broll_planning_node.py tests/temporal/test_temporal_runtime.py` - passed.
- `bash -n scripts/ci_gate.sh scripts/dev_up.sh` - passed.
- `bash -x scripts/dev_up.sh status` - passed; resolved the Codex worktree to the main checkout compose project without starting/stopping services.
- `git grep -n "0001…0011\\|cost_estimate\\|web :5176\\|运行前成本预估" -- README.md AGENTS.md CLAUDE.md docs apps packages tests scripts` - target stale references cleared.
- `uv run --extra dev python -m pytest -q tests/api/test_cases_profile.py tests/contract/test_database_schema.py tests/contract/test_api_contract_matrix.py tests/contract/test_openapi_matrix.py` - passed.
- `uv run --extra dev python -m pytest -q tests/planning tests/production/test_broll_planning_node.py` - passed.
- `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python -m pytest -q tests/production/test_broll_coverage_planning.py tests/production/test_broll_planning_node.py tests/production/test_portrait_planning_node.py` - passed.
- `npx tsc -p tsconfig.json --noEmit --noUnusedLocals --noUnusedParameters` - passed.
- `(cd apps/web && npm run build)` - passed after frontend wrapper deletion.
- `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python -m pytest -q` - passed.
- `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python scripts/export_openapi.py` - passed.
- `git diff --exit-code apps/web/src/api/openapi.json apps/web/src/api/schema.d.ts` - passed.
- `CUTAGENT_RUN_DB_TESTS=1 ... pytest -q tests/integration` against temporary clean DB `cutagent_ci_cleanup_8e2d` - passed.
- `CUTAGENT_RUN_TEMPORAL_TESTS=1 ... pytest -q tests/temporal` against temporary clean DB + shared MinIO - passed.

## Tests Passing

- Default pytest suite.
- Frontend production build.
- OpenAPI and generated TypeScript schema drift checks.
- DB integration tests on a clean temporary database.
- Temporal integration tests with shared MinIO durable + ephemeral buckets.

## Known Preexisting Failures

- Baseline `ruff check .` failed before cleanup edits on E402/E702 findings; this PR fixes those lint failures.
- Direct `scripts/ci_gate.sh` is blocked on this macOS host because `timeout`/`gtimeout` is unavailable; equivalent subcommands were run manually.
- Integration against the existing local dev DB failed because local auth seed state is dirty; the same integration suite passed on a fresh temporary DB.

## Risks and Mitigations

- Dynamic loading surfaces require conservative treatment: routers, Temporal activities/workflows, registries, migrations, seeds, fixtures, generated clients, and deployment hooks are high-risk until proven otherwise.

## Reviewer Checklist

- [ ] Confirm every deleted file has evidence in `docs/repo-cleanup-log.md`.
- [ ] Confirm every consolidation preserves behavior and has validation evidence.
- [ ] Confirm generated API files were only changed through generation commands.
- [ ] Confirm remaining validation failures are classified with evidence.
- [ ] Confirm final two-round rescan found no new high-confidence cleanup candidates.
