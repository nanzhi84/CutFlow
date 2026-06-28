# Repository Cleanup Log

## Baseline

- Date: 2026-06-25
- Branch: `maintenance/repo-hygiene-deep-cleanup`
- Base commit: `bca3ef57a76ed515d859691142a75e2aed55dd5e`
- Remote comparison: `origin/main...HEAD` = `0 0`
- Baseline status: clean tracked working tree before cleanup edits.
- Goal: one coherent, behavior-preserving repository hygiene PR.

## Baseline Commands

| Command | Result | Notes |
| --- | --- | --- |
| `git status --porcelain=v1 -b` | pass | Started at detached `HEAD`, then switched to cleanup branch. |
| `git fetch origin main` | pass | `origin/main` fetched successfully. |
| `git rev-list --left-right --count origin/main...HEAD` | pass | `0 0`; branch starts aligned to `origin/main`. |
| `uv run --extra dev ruff check .` | fail | Preexisting lint failures: E402/E702 in production pipeline, scripts, connector tests, and Temporal tests. No cleanup edits existed yet. |
| `uv run --extra dev python -m pytest -q` | pass | Default suite passed with third-party `jieba` syntax warnings. |
| `(cd apps/web && npm ci && npm run build)` | pass | Frontend production build passed. |
| `uv run --extra dev python scripts/export_openapi.py && git diff --exit-code apps/web/src/api/openapi.json` | pass | OpenAPI export is committed and stable. |
| `(cd apps/web && npm run generate:api && git diff --exit-code src/api/schema.d.ts)` | pass | Generated TypeScript API schema is committed and stable. |

## Repository Map

Top-level tracked directories:

| Directory | Purpose | Coverage status |
| --- | --- | --- |
| `.github/` | GitHub Actions workflow and local action for ffmpeg setup. | Round 0 scanned. |
| `apps/` | API, worker, web SPA, and connector applications. | Round 0 scanned. |
| `deploy/` | Temporal deployment configuration. | Round 0 scanned. |
| `docs/` | Product/spec/milestone/ops documentation. | Round 0 scanned. |
| `packages/` | Shared domain, AI, media, planning, production, publishing, ops, storage, workflow packages. | Round 0 scanned. |
| `scripts/` | Local and CI operational scripts. | Round 0 scanned. |
| `tests/` | Unit, integration, Temporal, frontend, contract, and domain tests. | Round 0 scanned. |

Top-level tracked files:

- `.env.example`, `.gitignore`, `AGENTS.md`, `CLAUDE.md`, `README.md`, `alembic.ini`, `docker-compose.yml`, `pyproject.toml`, `uv.lock`.

Major entrypoints and surfaces:

- FastAPI app: `apps/api/main.py` -> `apps/api/app.py` -> router modules under `apps/api/routers/`.
- Temporal worker: `python -m apps.worker` -> `apps/worker/main.py`.
- React SPA: `apps/web/src/main.tsx`, `apps/web/src/App.tsx`, `apps/web/src/routes.ts`.
- OceanEngine connector CLI: `apps/connectors/oceanengine/cli.py`.
- Database bootstrap/migration: `scripts/bootstrap_database.py`, `scripts/migrate.py`, Alembic versions under `packages/core/storage/alembic/versions/`.
- Full local gate: `scripts/ci_gate.sh`.
- CI workflows: `.github/workflows/ci.yml`.
- Local demo stack: `docker-compose.yml`, `scripts/dev_up.sh`.

## Directory Coverage Matrix

| Area | Round 1 | Round 2 | Major subtree status | Notes |
| --- | --- | --- | --- | --- |
| `.github/` | complete | complete | no cleanup accepted | CI workflow traced; no dead workflow/action candidate found. |
| `apps/api/` | complete | complete | touched narrowly | Case material count path now imports the shared asset-kind contract. |
| `apps/connectors/` | complete | complete | no cleanup accepted | Optional dependency/import tests traced; no deletion candidate accepted. |
| `apps/web/` | complete | complete | wrappers removed | Obsolete top-level UI compatibility wrappers deleted after direct import rewrite and tsc/build validation. |
| `apps/worker/` | complete | complete | no cleanup accepted | Temporal worker entrypoint traced; dynamic registration kept. |
| `deploy/` | complete | complete | no cleanup accepted | Temporal deployment config traced; no stale config candidate accepted. |
| `docs/` | complete | complete | stale docs fixed | Repository guide, README, roadmap, and cleanup docs updated. |
| `packages/ai/` | complete | complete | no cleanup accepted | Provider/prompt registry surfaces retained as dynamic/high-risk. |
| `packages/core/` | complete | complete | touched narrowly | Shared case material-kind contract added; migrations/storage/auth surfaces retained. |
| `packages/creative/` | complete | complete | touched narrowly | SQLAlchemy case counts now import the shared asset-kind contract. |
| `packages/media/` | complete | complete | no cleanup accepted | Media rendering/env surfaces traced; no deletion candidate accepted. |
| `packages/migrations/` | complete | complete | intentionally skipped with reason | Directory convention only; not Alembic. Actual migrations live under `packages/core/storage/alembic/versions/`. |
| `packages/ops/` | complete | complete | no cleanup accepted | Balance/provider error classification retained as separate high-risk surface. |
| `packages/planning/` | complete | complete | duplicate terms consolidated | Person subject terms shared by b-roll and portrait planning. |
| `packages/production/` | complete | complete | touched narrowly | Preexisting lint import fixed; pipeline/Temporal nodes otherwise retained. |
| `packages/publishing/` | complete | complete | no cleanup accepted | Copy-field tuple retained as schema/validation alignment. |
| `scripts/` | complete | complete | touched narrowly | Lint fixes plus Codex worktree-safe `dev_up.sh` path resolution. |
| `tests/` | complete | complete | touched narrowly | Case profile test now guards shared material-kind constant behavior; later blind-spot pass cleaned static-dead test parameters and hardened Settings env coverage. |

## Candidate Inventory

See `docs/repo-cleanup-inventory.md` for the structured candidate list. High-confidence candidates are not accepted until at least two independent evidence sources support the change.

## Cleanup Batches

### Batch 1: Baseline lint hygiene

Files changed:

- `packages/production/pipeline/nodes/broll_planning.py`
- `pyproject.toml`
- `scripts/clean_dangling_materials.py`
- `scripts/sync_materials.py`

Changes:

- Moved one misplaced import in `broll_planning.py` back into the import block.
- Marked intentional import-order exceptions for files that must mutate `sys.path`, gate optional dependencies, or set Temporal env before importing app modules.
- Split one-line semicolon statements in two operational scripts.

Evidence:

- Baseline `ruff check .` failed before cleanup edits on these exact E402/E702 findings.
- `scripts/bootstrap_database.py` and `scripts/export_openapi.py` require `sys.path.insert(0, ROOT)` before importing repository modules.
- `tests/connectors/test_oceanengine_import.py` requires `pytest.importorskip("openpyxl")` before connector imports.
- `tests/temporal/test_temporal_runtime.py` requires Temporal env defaults before importing `apps.api.main`.
- `scripts/clean_dangling_materials.py` and `scripts/sync_materials.py` are tracked but self-referenced operational DB/OSS scripts; because they may be invoked manually outside the repo, they were kept and lint-cleaned rather than deleted.

### Batch 2: Stale repository documentation references

Files changed:

- `AGENTS.md`
- `README.md`

Changes:

- Updated the documented Alembic migration range from stale `0001…0011` to current `0001…0022`, with the current single head `0022_drop_publish_hashtags`.
- Corrected the root agent guide from stale child `AGENTS.md` guidance to the actual per-directory `CLAUDE.md` files.
- Synchronized the root package map with the current root `CLAUDE.md` wording for core storage/secret handling and production workflow variants.
- Removed README's nonexistent `apps/api/routers/cost_estimate.py` reference; job/run endpoints are registered in `apps/api/routers/jobs_runs.py`.
- Corrected README's `scripts/dev_up.sh up` web port from stale `5176` to the script default `8001`.

Evidence:

- `git ls-files packages/core/storage/alembic/versions` shows migrations through `0022_drop_publish_hashtags.py`.
- `CLAUDE.md` and `packages/core/CLAUDE.md` already documented the `0022` head.
- `find apps packages tests -maxdepth 2 -name AGENTS.md -o -name CLAUDE.md` shows child guides are `CLAUDE.md`, not `AGENTS.md`.
- `git ls-files apps/api/routers` has `jobs_runs.py` and no `cost_estimate.py`.
- `scripts/dev_up.sh` sets `WEB_PORT="${CUTAGENT_WEB_PORT:-8001}"`; `apps/web/package.json` separately uses `5173` only for direct `npm run dev`.
- `git grep` after the edit no longer finds `0001…0011`, `cost_estimate`, `web :5176`, or `运行前成本预估` in current repository docs/source, excluding unrelated metric names such as `provider_cost_estimated_total`.

### Batch 3: Case material kind constant consolidation

Files changed:

- `packages/core/contracts/media.py`
- `packages/core/contracts/__init__.py`
- `apps/api/services/cases.py`
- `packages/creative/cases/sqlalchemy_repository.py`
- `tests/api/test_cases_profile.py`

Changes:

- Added `CASE_MATERIAL_ASSET_KINDS` to the core media contract surface.
- Replaced duplicate literal `{"portrait", "broll", "video", "bgm", "font"}` in API and SQLAlchemy case count paths with imports from the shared contract constant.
- Strengthened the existing case profile test to assert both consumers share the same constant object.

Evidence:

- `git grep -n "MATERIAL_ASSET_KINDS"` showed the same reusable-material allowlist duplicated in `apps/api/services/cases.py` and `packages/creative/cases/sqlalchemy_repository.py`.
- The two copies served the same behavior: case material counts in memory and SQLAlchemy modes.
- `packages/core/contracts/media.py` already owns media asset/upload kinds and selection media types, making it the clearest canonical location.
- `git grep` after the edit shows no duplicate literal assignment remains.

### Batch 4: Worktree path drift cleanup

Files changed:

- `scripts/dev_up.sh`
- `docs/ROADMAP.md`

Changes:

- Replaced the `.claude/worktrees`-only main-checkout inference in `scripts/dev_up.sh` with `git rev-parse --git-common-dir`, keeping the old `.claude/worktrees` string fallback for non-Git or legacy layouts.
- Kept the existing preference for a current-worktree `.venv`; when absent, fallback now points at the resolved main checkout `.venv`.
- Updated the roadmap's milestone worktree discipline so Codex worktrees use `.codex/worktrees/<id>/<repo>` and Claude worktrees use `.claude/worktrees/<slug>`.

Evidence:

- Current cleanup worktree path is `/Users/yoryon/.codex/worktrees/8e2d/cutagent-genesis`.
- `git rev-parse --git-common-dir` from this worktree returns `/Users/yoryon/Projects/cutagent-genesis/.git`.
- `git worktree list --porcelain` shows Codex worktrees under `.codex/worktrees/...` and a legacy Claude worktree under `.claude/worktrees/...`.
- The previous `scripts/dev_up.sh` string trim only matched `.claude/worktrees`, so Codex worktrees could not resolve the main checkout by that path rule.
- `docs/ROADMAP.md` said Codex executes milestone worktrees under `.claude/worktrees`, which conflicts with current Codex desktop worktree layout.

### Batch 5: Configuration documentation drift

Files changed:

- `.env.example`
- `README.md`

Changes:

- Corrected `.env.example`'s preamble: most variables map to `Settings`, but a few narrow runtime switches are intentionally read near their direct consumers.
- Documented `CUTAGENT_STORAGE_BACKEND=postgres` in README as the SQLAlchemy backend alias accepted by the code.

Evidence:

- `packages/media/rendering/timeline.py` directly reads `CUTAGENT_RENDER_MAX_INFLIGHT`.
- `packages/production/pipeline/ephemeral_gc.py` directly reads `CUTAGENT_KEEP_FAILED_EPHEMERAL` and `CUTAGENT_EPHEMERAL_FAILED_RETENTION_HOURS`.
- `packages/core/storage/bootstrap.py` treats `{"sqlalchemy", "postgres"}` as SQLAlchemy-enabled backends.
- `.env.example` and `packages/core/CLAUDE.md` already listed `postgres`; README only listed `memory|sqlalchemy`.

### Batch 6: Material person subject terms consolidation

Files changed:

- `packages/planning/material/subject_terms.py`
- `packages/planning/material/broll_pack.py`
- `packages/planning/material/portrait_pack.py`

Changes:

- Added shared `PERSON_SUBJECT_TERMS` under `packages/planning/material/`.
- Replaced duplicate person-centric subject tuples in b-roll person filtering and portrait lip-sync source detection.

Evidence:

- Duplicate literal scan found the exact same person subject tuple in `broll_pack.py` and `portrait_pack.py`.
- Both consumers are in the same pure planning/material domain and already share behavior: b-roll excludes person-centric clips while portrait accepts static person-centric lip-sync sources.

### Batch 7: Frontend compatibility wrapper deletion

Files deleted:

- `apps/web/src/components/Modal.tsx`
- `apps/web/src/components/State.tsx`
- `apps/web/src/components/Status.tsx`
- `apps/web/src/components/Toast.tsx`

Files changed:

- Frontend imports under `apps/web/src/` that previously targeted the deleted wrapper files.
- `apps/web/src/pages/ops/PromptManagementPage.tsx`
- `apps/web/src/pages/settings/SettingsPage.tsx`
- `apps/web/src/components/modals/CaseModal.tsx`

Changes:

- Removed four top-level compatibility wrapper/re-export files.
- Repointed callers to the canonical `apps/web/src/components/ui/*` modules.
- Added explicit `isOpen` to the three old `Modal` wrapper call sites, preserving the previous "rendered means open" behavior.

Evidence:

- `apps/web/src/components/State.tsx`, `Status.tsx`, and `Toast.tsx` were pure re-export files.
- `apps/web/src/components/Modal.tsx` was a thin wrapper over `components/ui/Modal` that only hard-coded `isOpen`.
- The current app already uses `components/ui/*` directly in many newer modules.

### Batch 8: Aggressive duplicate implementation and unused export cleanup

Files deleted:

- `apps/web/src/contracts/caseEdit.typecheck.ts`
- `apps/web/src/contracts/m6aA.typecheck.ts`
- `apps/web/src/contracts/m6aR3.typecheck.ts`
- `apps/web/src/contracts/m6aR4.typecheck.ts`
- `apps/web/src/contracts/m6aR5.typecheck.ts`
- `apps/web/src/contracts/m6aR6.typecheck.ts`
- `apps/web/src/contracts/m6eB.typecheck.ts`

Files added:

- `packages/core/storage/import_metadata.py`
- `packages/core/storage/performance_mappers.py`
- `packages/media/audio/loudness.py`
- `packages/production/pipeline/nodes/_timeline_output.py`

Files changed:

- `README.md`
- `apps/api/services/imports.py`
- `apps/web/CLAUDE.md`
- `apps/web/src/api/client.ts`
- `apps/web/src/api/r6.ts`
- `apps/web/src/components/library/libraryModel.ts`
- `apps/web/src/components/modals/CaseModal.tsx`
- `apps/web/src/components/studio-create/batchModel.ts`
- `apps/web/src/components/studio-create/studioCreateModel.ts`
- `apps/web/src/components/ui/Modal.tsx`
- `apps/web/src/hooks/notificationModel.ts`
- `apps/web/src/hooks/useTaskNotifications.ts`
- `apps/web/src/utils/annotationV4.ts`
- `packages/core/storage/sqlalchemy_uploads.py`
- `packages/creative/cases/sqlalchemy_rubric.py`
- `packages/media/annotation/bgm.py`
- `packages/media/voice_provider_bridge.py`
- `packages/production/pipeline/_ffmpeg.py`
- `packages/production/pipeline/digital_human.py`
- `packages/production/pipeline/nodes/broll_timeline_planning.py`
- `packages/production/pipeline/nodes/export_finished_video.py`
- `packages/production/pipeline/nodes/export_seedance_video.py`
- `packages/production/pipeline/nodes/timeline_planning.py`
- `packages/production/sqlalchemy_mappers.py`
- `packages/production/sqlalchemy_repository.py`
- `tests/creative/test_case_evolution_logic.py`
- `tests/frontend/test_user_defaults_batch_notify.py`
- `tests/integration/test_parity_mapper.py`

Changes:

- Deleted seven frontend compile-time contract probe files that are not imported by source, test, package scripts, or CI and are superseded by the normal TypeScript/build gates.
- Removed unused frontend exports and payload-builder helpers surfaced by `knip --production`, while preserving `defaultForm` as a private implementation detail and updating probe tests to use the public `loadStoredForm()` entry.
- Shared API request/response helper types between `apps/web/src/api/client.ts` and `apps/web/src/api/r6.ts`.
- Moved repeated import-metadata parsing into `packages/core/storage/import_metadata.py` and reused it from API imports and SQLAlchemy production persistence.
- Moved repeated loudness probing into `packages/media/audio/loudness.py` and reused it from BGM annotation and pipeline ffmpeg helpers.
- Moved repeated performance observation/score row mappers into `packages/core/storage/performance_mappers.py`; production mappers now re-export the canonical helpers for compatibility.
- Added `artifact_to_row` beside `artifact_row_to_contract` and removed duplicate Artifact contract-to-row mapping in media/provider preview and production snapshot sync.
- Shared timeline planning `NodeOutput` artifact construction in `packages/production/pipeline/nodes/_timeline_output.py`.
- Reused export package artifact creation between finished-video and Seedance export nodes.
- Shared digital-human run-running transition/event/funnel recording in one private method.
- Shortened README test env instructions by referring to the already documented manual SQLAlchemy/Temporal/MinIO setup.

Evidence:

- `git diff --shortstat` after this batch showed tracked changes of `+166/-1215`; the four new helper files total 333 lines, so the batch still nets roughly 716 fewer lines before documentation updates.
- `knip --production` identified unused frontend typecheck files/exports; `tsc --noUnusedLocals --noUnusedParameters` and `npm run build` passed after deletion.
- `jscpd` with a lower source threshold found duplicate Python implementations in import metadata, loudness probing, performance mappers, Artifact row mapping, timeline output construction, export package artifact creation, and digital-human run state transition. After consolidation, the same source scan reported zero Python/TypeScript/TSX runtime-code clones at that threshold, excluding docs, tests, generated files, and migrations.
- Repository-wide `rg` checks found no remaining callers of the deleted private performance mapper methods or frontend wrapper/typecheck imports.
- The first full pytest after removing the `defaultForm` export failed two frontend probe tests; tests were corrected to use the public `loadStoredForm()` entry, then the targeted probe tests and full pytest passed.

### Batch 9: Blind-spot static scan and Settings env coverage

Files changed:

- `.env.example`
- `tests/api/test_annotation_patch.py`
- `tests/conftest.py`
- `tests/contract/test_settings_config.py`
- `tests/creative/test_reference_extract.py`
- `tests/production/test_digital_human_ephemeral_gc.py`
- `tests/production/test_sqlalchemy_selection_reservations.py`
- `tests/production/test_yield_funnel_lifecycle.py`
- `tests/providers/test_sqlalchemy_voice_provider_wireup.py`
- `tests/temporal/test_temporal_runtime.py`

Changes:

- Completed `.env.example` coverage for every env var read by `packages/core/config/settings.py`, including DB pool, auth limiter/cookie policy, provider host policy, publishing CDP endpoint, upload, balance, learning, and motion guard settings.
- Added a contract test that parses `.env.example` and fails when a `Settings` env var is missing from the sample config.
- Expanded `clean_env` to clear the full `Settings` env surface and made it autouse so default-value assertions cannot inherit shell env.
- Cleaned vulture-detected test-only unused variables while preserving fake interface signatures where callers may pass keyword args.

Evidence:

- Settings/env comparison found `.env.example` gaps, and a broader `settings.py` scan surfaced undocumented live groups.
- `tests/contract/test_settings_config.py` claimed to clear every Settings env var but omitted multiple groups before this pass.
- Full high-confidence vulture scan now exits cleanly with no dead Python symbol output.
- `npx knip --reporter compact` still reported `package.json: python` at this point; a later blind-spot pass proved the documented `npm run export:openapi` entrypoint was broken on this host and fixed it in Batch 10.

### Batch 10: Frontend OpenAPI export entrypoint hardening

Files changed:

- `AGENTS.md`
- `CLAUDE.md`
- `README.md`
- `apps/api/CLAUDE.md`
- `apps/web/CLAUDE.md`
- `apps/web/package.json`
- `scripts/export_openapi.py`
- `docs/repo-cleanup-log.md`
- `docs/repo-cleanup-inventory.md`
- `docs/repo-cleanup-pr.md`

Changes:

- Replaced the frontend `export:openapi` package script's bare `python` invocation with `uv run --extra dev python`, matching the repo's validation path.
- Made `scripts/export_openapi.py` clear local proxy env before importing the app so OpenAPI schema generation is not affected by host SOCKS proxy settings.
- Added a narrow Knip config for the frontend workspace to ignore the repository-level `uv` binary rather than treating it as an npm dependency.
- Updated live entrypoint docs to use the working OpenAPI export command and fixed README prose that referred to `ci_gate.sh` without the `scripts/` prefix.

Evidence:

- `npm run export:openapi` failed before this batch with `sh: python: command not found`.
- After switching to `uv`, the same script still failed when local proxy env made `httpx` initialize SOCKS support during app import.
- Clearing proxy env inside `scripts/export_openapi.py` made `npm run export:openapi` pass in the same shell without manual `env -u ...` wrapping.
- Full and production Knip scans are clean after adding the narrow `uv` binary ignore.

## Validation Log

Baseline validation is recorded above. Targeted validation will be appended after each cleanup batch.

After Batch 1:

| Command | Result | Notes |
| --- | --- | --- |
| `uv run --extra dev ruff check .` | pass | Previously failing lint gate now passes. |
| `uv run --extra dev python -m pytest -q tests/scripts tests/connectors tests/production/test_broll_planning_node.py tests/temporal/test_temporal_runtime.py` | pass | `18` tests passed/skipped as expected; Temporal tests remain gated without env. |
| `bash -n scripts/ci_gate.sh scripts/dev_up.sh` | pass | Shell syntax still valid. |

After Batch 2:

| Command | Result | Notes |
| --- | --- | --- |
| `git grep -n "0001…0011\\|cost_estimate\\|web :5176\\|运行前成本预估" -- README.md AGENTS.md CLAUDE.md docs apps packages tests scripts` | pass | No target stale references remain; unrelated `provider_cost_estimated_total` metric text is not a router/path hit. |
| `git grep -n '改对应代码前先读该目录的 AGENTS.md\\|0001…0011\\|{jobs_runs,cost_estimate}\\|web :5176\\|运行前成本预估' -- README.md AGENTS.md CLAUDE.md apps packages tests docs scripts` | pass | No target stale references remain; exit code `1` is expected for zero matches. |
| `uv run --extra dev ruff check .` | pass | Docs and AGENTS changes did not regress lint. |

After Batch 3:

| Command | Result | Notes |
| --- | --- | --- |
| `uv run --extra dev ruff check .` | pass | Shared constant import style is lint-clean. |
| `uv run --extra dev python -m pytest -q tests/api/test_cases_profile.py tests/contract/test_database_schema.py tests/contract/test_api_contract_matrix.py tests/contract/test_openapi_matrix.py` | pass | `27` relevant API/contract tests passed. |
| `uv run --extra dev python scripts/export_openapi.py && git diff --exit-code apps/web/src/api/openapi.json` | pass | Constant-only contract change did not alter OpenAPI. |
| `(cd apps/web && npm run generate:api && git diff --exit-code src/api/schema.d.ts)` | pass | Generated frontend schema unchanged. |
| `uv run --extra dev python -m pytest -q tests/api/test_cases_profile.py tests/contract/test_openapi_schema.py tests/contract/test_database_schema.py` | command error | `tests/contract/test_openapi_schema.py` does not exist; corrected with the command above. |
| `uv run --extra dev python -m pytest -q tests/api/test_cases_profile.py tests/contract/test_database_schema.py tests/contract/test_contract_matrix.py` | command error | `tests/contract/test_contract_matrix.py` does not exist; corrected with the command above. |

After Batch 4:

| Command | Result | Notes |
| --- | --- | --- |
| `bash -n scripts/dev_up.sh` | pass | Shell syntax remains valid after path helper change. |
| `bash -x scripts/dev_up.sh status` | pass | Read-only status path resolved `COMPOSE_DIR=/Users/yoryon/Projects/cutagent-genesis` from the Codex worktree and reused project `cutagent-genesis`; no services were started or stopped. |
| `git grep -n '\\.claude/worktrees\\|\\.codex/worktrees\\|worktree' -- README.md AGENTS.md CLAUDE.md docs/ROADMAP.md docs/ops docs/milestones apps packages scripts tests` | pass | Current roadmap no longer assigns Codex worktrees to `.claude`; remaining `.claude/worktrees` hit is the intentional legacy fallback in `scripts/dev_up.sh` plus historical milestone notes. |

After Batch 5:

| Command | Result | Notes |
| --- | --- | --- |
| `git grep -n 'postgres.*storage\\|STORAGE_BACKEND\\|storage_backend\\|backend.*postgres' -- packages/core apps scripts tests README.md .env.example AGENTS.md CLAUDE.md` | pass | Confirmed `postgres` is an accepted SQLAlchemy backend alias and README had been the inconsistent doc. |

After Batch 6:

| Command | Result | Notes |
| --- | --- | --- |
| `uv run --extra dev ruff check .` | pass | Shared planning/material constant is lint-clean. |
| `uv run --extra dev python -m pytest -q tests/planning tests/production/test_broll_planning_node.py` | pass | `74` planning and b-roll planning tests passed. |
| `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python -m pytest -q tests/production/test_broll_coverage_planning.py tests/production/test_broll_planning_node.py tests/production/test_portrait_planning_node.py` | pass | `18` production b-roll/portrait planning tests passed after clearing local proxy vars. |
| `git grep -n '_PERSON_SUBJECT_TERMS\\|PERSON_SUBJECT_TERMS' -- packages/planning tests` | pass | Duplicate private tuple is gone; both consumers import the shared constant. |
| `uv run --extra dev python -m pytest -q tests/production/test_broll_coverage_planning.py tests/production/test_broll_planning_node.py tests/production/test_broll_coverage_planning_node.py` | command error | `tests/production/test_broll_coverage_planning_node.py` does not exist; corrected with the commands above. |
| `uv run --extra dev python -m pytest -q tests/production/test_broll_coverage_planning.py tests/production/test_broll_planning_node.py tests/production/test_portrait_planning_node.py` | environment failure | Local SOCKS proxy env pointed httpx at `socks5://127.0.0.1:7897` without `socksio`; rerun with proxy vars unset passed. |

After Batch 7:

| Command | Result | Notes |
| --- | --- | --- |
| `npx tsc -p tsconfig.json --noEmit --noUnusedLocals --noUnusedParameters` | pass | Frontend type and unused-symbol gate passed after import rewrites. |
| `npm run build` | pass | Frontend production build passed. |
| `rg -n 'components/(Modal|State|Toast|Status)"|\\.\\./(Modal|State|Toast|Status)"|\\./(Modal|State|Toast|Status)"' apps/web/src --glob '*.ts' --glob '*.tsx'` | pass | No old wrapper imports remain; the remaining same-directory `./Modal` hit is canonical `components/ui/ConfirmDialog.tsx` importing `components/ui/Modal.tsx`. |
| `npx tsc -p tsconfig.json --noEmit --noUnusedLocals --noUnusedParameters` | command error | First run found two missed same-directory `./State` imports in `AppShell.tsx` and `RequireAuth.tsx`; fixed and rerun passed. |

After Batch 8:

| Command | Result | Notes |
| --- | --- | --- |
| `uv run --extra dev ruff check .` | pass | Python helper consolidation and repository changes are lint-clean. |
| `npx --yes jscpd --min-lines 20 --min-tokens 120 ...` | pass | Runtime source scan, excluding docs/tests/generated/migrations, reported zero Python/TypeScript/TSX clones at this lower threshold; only README self-overlap remained. |
| `env -u ... uv run --extra dev python -m pytest -q tests/creative/test_case_evolution_logic.py tests/integration/test_parity_mapper.py tests/media/annotation/test_bgm.py tests/production/test_bgm_segment_selection.py tests/production/test_broll_timeline_planning.py tests/production/test_broll_only_template.py tests/production/test_seedance_template.py` | pass | `50` targeted tests passed/skipped as expected. |
| `env -u ... uv run --extra dev python -m pytest -q tests/production/test_digital_human_seed_media.py tests/production/test_digital_human_ephemeral_gc.py tests/workflow/test_broll_only_run.py tests/workflow/test_broll_only_parity.py tests/workflow/test_reuse_plan.py` | pass | `15` runtime/workflow tests passed after digital-human state-transition consolidation. |
| `cd apps/web && npx --yes knip --production --reporter compact && npx tsc -p tsconfig.json --noEmit --noUnusedLocals --noUnusedParameters && npm run build` | pass | Frontend unused-export, type, and production build gates passed after unused export cleanup. |
| `env -u ... uv run --extra dev python scripts/export_openapi.py && cd apps/web && npm run generate:api` | command error | Export and generation succeeded; the trailing `git diff` used root-relative paths after `cd apps/web`. Corrected command below passed. |
| `git diff --exit-code -- apps/web/src/api/openapi.json apps/web/src/api/schema.d.ts` | pass | No generated API/schema drift. |
| `env -u ... uv run --extra dev python -m pytest -q` | introduced failure, then fixed | First full run failed two frontend probe tests because deleting the `defaultForm` export removed a test-used module entry. Tests were updated to use public `loadStoredForm()`; targeted rerun and final full pytest passed. |
| `env -u ... uv run --extra dev python -m pytest -q tests/frontend/test_user_defaults_batch_notify.py::test_form_defaults_round_trip_preserves_preference_blocks tests/frontend/test_user_defaults_batch_notify.py::test_seedance_reference_assets_are_optional` | pass | Corrected frontend probe tests passed. |
| `git diff --check` | pass | No whitespace/error marker issues. |

After Batch 9:

| Command | Result | Notes |
| --- | --- | --- |
| `uv run --extra dev python -m pytest -q tests/contract/test_settings_config.py tests/contract/test_db_pool_config.py` | pass | `28` Settings/DB pool contract tests passed, including `.env.example` coverage. |
| `uv run --extra dev ruff check tests/contract/test_settings_config.py packages/core/config/settings.py` | pass | Settings contract changes are lint-clean. |
| `bash -c 'set -euo pipefail; set -a; source .env.example; set +a; test "$CUTAGENT_STORAGE_BACKEND" = sqlalchemy; test "$CUTAGENT_PUBLISH_ADAPTER" = xiaovmao.cdp'` | pass | `.env.example` remains shell-loadable and keeps key defaults. |
| `git diff --check` | pass | No whitespace/error marker issues. |
| `env -u ... uv run --extra dev python -m pytest -q tests/api/test_annotation_patch.py tests/creative/test_reference_extract.py tests/production/test_digital_human_ephemeral_gc.py tests/production/test_sqlalchemy_selection_reservations.py tests/production/test_yield_funnel_lifecycle.py tests/providers/test_sqlalchemy_voice_provider_wireup.py tests/temporal/test_temporal_runtime.py` | pass | `43` touched-test suites passed/skipped after static-dead variable cleanup. |
| `uv run --extra dev ruff check tests/api/test_annotation_patch.py tests/conftest.py tests/contract/test_settings_config.py tests/creative/test_reference_extract.py tests/production/test_digital_human_ephemeral_gc.py tests/production/test_sqlalchemy_selection_reservations.py tests/production/test_yield_funnel_lifecycle.py tests/providers/test_sqlalchemy_voice_provider_wireup.py tests/temporal/test_temporal_runtime.py` | pass | Touched Python test files are lint-clean. |
| `uvx --from vulture vulture apps packages scripts tests --min-confidence 90 --exclude 'apps/web/node_modules,apps/web/dist,.venv'` | pass | High-confidence dead-symbol scan returned no output. |
| Settings/env comparison script | pass | `SETTINGS_ENV_NOT_IN_EXAMPLE` and `SETTINGS_FILE_ENV_NOT_IN_EXAMPLE` were both empty after the fix. |
| Python import graph scan | pass | No new zero-inbound source modules; only external/manual scripts remain as expected. |
| `npx --yes jscpd apps packages scripts --min-lines 20 --min-tokens 120 ...` | pass | Runtime source scan analyzed `372` files and still reported zero clones. |

After Batch 10:

| Command | Result | Notes |
| --- | --- | --- |
| `npm run export:openapi` from `apps/web` | pass | Frontend package script now exports OpenAPI through `uv run --extra dev python` and succeeds despite inherited local proxy env. |
| `npm run export:openapi && git diff --exit-code -- src/api/openapi.json` from `apps/web` | pass | Export script produced no OpenAPI drift. |
| `cd apps/web && npx --yes knip --reporter compact` | pass | Full frontend Knip scan is clean after replacing the broken `python` script entry and narrowly ignoring repo-level `uv`. |
| `cd apps/web && npx --yes knip --production --reporter compact` | pass | Production frontend Knip scan remains clean. |
| `.env.example` reverse reference scan | pass | No sample `CUTAGENT_*` variables exist only in `.env.example`. |
| `uvx --from deptry deptry ...` | reviewed findings | No accepted dependency removal: findings were package/module-name mapping noise, dev tools, FastAPI runtime multipart dependency, transitive `botocore`, and optional `fontTools` fallback. |
| `uvx --from vulture vulture apps packages scripts tests --min-confidence 80 ...` | pass | Lower-confidence dead-symbol scan returned no output. |
| tracked build-artifact scan | pass | No tracked build/cache artifacts found. |

Final validation:

| Command | Result | Notes |
| --- | --- | --- |
| `uv run --extra dev ruff check .` | pass | Final lint pass. |
| `git diff --check` | pass | No whitespace/error marker issues. |
| `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python -m pytest -q` | pass | Full default suite passed. |
| `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python -m pytest -q` | pass | Re-run after Batch 8 and the frontend probe correction; full default suite passed again. |
| `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python scripts/export_openapi.py` | pass | OpenAPI export succeeded after clearing local proxy env. |
| `npm run export:openapi` from `apps/web` | pass | Re-run after Batch 10; package script succeeds without manual proxy env clearing. |
| `npm run generate:api` | pass | Generated TypeScript schema. |
| `git diff --exit-code apps/web/src/api/openapi.json apps/web/src/api/schema.d.ts` | pass | No generated API drift. |
| `npm run build` | pass | Frontend production build passed. |
| `cd apps/web && npx --yes knip --reporter compact` | pass | Full frontend Knip scan clean after Batch 10. |
| `cd apps/web && npx --yes knip --production --reporter compact && npx tsc -p tsconfig.json --noEmit --noUnusedLocals --noUnusedParameters && npm run build` | pass | Re-run after Batch 8; no production-unused frontend exports remain. |
| `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python -m pytest -q` | pass | Re-run after Batch 10; full default suite passed. |
| `scripts/ci_gate.sh` with `PYTHON_BIN=.venv/bin/python` | environment failure | macOS environment lacks GNU `timeout`/`gtimeout`; script failed before tests at line 26. Equivalent subcommands below were run manually. |
| `CUTAGENT_RUN_DB_TESTS=1 CUTAGENT_STORAGE_BACKEND=sqlalchemy CUTAGENT_DATABASE_URL=postgresql+psycopg://cutagent:cutagent@localhost:55432/cutagent .venv/bin/python scripts/bootstrap_database.py` | pass | Existing local dev DB migrated to 0022 but inserted 0 seed rows; not used for final integration verdict because it was not a clean CI DB. |
| Same integration command against existing local dev DB | environment failure | Failed on admin login because the existing local DB had non-CI auth state; classified as dirty local data, not a code regression. |
| `CREATE DATABASE cutagent_ci_cleanup_8e2d` then bootstrap against that DB | pass | Fresh temporary DB migrated from empty to 0022 and inserted 118 seed rows plus 3 demo media source artifacts. |
| `CUTAGENT_RUN_DB_TESTS=1 ... CUTAGENT_DATABASE_URL=postgresql+psycopg://cutagent:cutagent@localhost:55432/cutagent_ci_cleanup_8e2d .venv/bin/python -m pytest -q tests/integration` | pass | `38` integration tests passed on the clean temporary DB. |
| MinIO bucket pre-create for `cutagent-local` and `cutagent-ephemeral` | pass | Both buckets already existed. |
| `CUTAGENT_RUN_TEMPORAL_TESTS=1 ... CUTAGENT_DATABASE_URL=postgresql+psycopg://cutagent:cutagent@localhost:55432/cutagent_ci_cleanup_8e2d ... .venv/bin/python -m pytest -q tests/temporal` | pass | `11` Temporal tests passed with shared MinIO durable + ephemeral buckets. |
| `DROP DATABASE IF EXISTS cutagent_ci_cleanup_8e2d` | pass | Temporary CI database removed after validation. |

## Failure Classification

- Resolved by Batch 1: baseline `ruff check .` E402/E702 failures.
- Command error during Batch 2 stale-reference verification: an earlier grep pattern used unescaped backticks in a zsh command string, causing command substitution noise before the grep ran. Re-run with single quotes is recorded as passing above.
- Command error during Batch 4 helper verification: an attempted process-substitution probe sourced empty content instead of the script prefix; replaced with `bash -x scripts/dev_up.sh status`, which exercises the real read-only path.
- Command error during Batch 6 production-node validation: guessed nonexistent test filename; corrected after `rg --files tests`.
- Environment failure during Batch 6 production-node validation: local proxy env caused `httpx` SOCKS import failure; unsetting proxy env vars made the same tests pass.
- Command error during Batch 7 frontend validation: first tsc run found two missed wrapper imports; fixed and rerun passed.
- Introduced failure during Batch 8 full pytest: removing the `defaultForm` export broke two frontend probe tests. Tests were updated to use public `loadStoredForm()` instead of a production-unused export; targeted and full pytest reruns passed.
- Command error during Batch 8 OpenAPI validation: generation succeeded but the trailing diff command used root-relative paths after `cd apps/web`; corrected root-level diff passed.
- Command error during Batch 9 lint validation: an attempted `ruff check .env.example ...` treated `.env.example` as Python; corrected by linting Python files and shell-sourcing `.env.example`.
- Environment failure during Batch 9 touched-test validation: inherited local SOCKS proxy env caused an `httpx`/`socksio` import error; rerun with proxy vars unset passed.
- Command error during final Batch 9 rescan: bare `python` is not present in this shell and an initial `jscpd --pattern` invocation matched zero files. Re-ran via `uv run --extra dev python` and with explicit `jscpd apps packages scripts` path arguments; both corrected commands passed.
- Introduced discovery during Batch 10: the previously retained `npm run export:openapi` entrypoint was actually broken on this host due missing bare `python`, then due inherited SOCKS proxy env. Fixed by using `uv run --extra dev python` and clearing proxy env inside the export script.
- Command error during Batch 10 stale-reference search: a shell command included unescaped backticks, so zsh attempted command substitution and scanned noisy output. Re-run with single-quoted patterns was used for the actual `ci_gate.sh` check.
- Reviewed deptry findings during Batch 10: no dependency was removed because reported issues were package/module aliasing (`argon2`, `cv2`), expected runtime/tool dependencies (`python-multipart`, `pytest`, `ruff`), transitive `botocore`, or explicitly optional `fontTools`.
- Environment failure during final OpenAPI validation: first export inherited local SOCKS proxy env and failed in `httpx`; rerun with proxy vars unset passed.
- Environment failure during full `scripts/ci_gate.sh`: local macOS lacks `timeout`; manual equivalent subcommands were run and recorded above.
- Environment failure during DB integration on the existing dev DB: local auth seed state was dirty (`admin@local.cutagent` login 401); clean temporary DB integration passed.
- Passing baseline: default pytest, frontend build, OpenAPI JSON drift check, generated TypeScript schema drift check.
- Full direct `scripts/ci_gate.sh` remains blocked by missing `timeout`; its DB and Temporal subcommands passed when run manually on a clean temporary database.

## Discovery Rounds

- Round 0: Baseline map and entrypoint scan completed.
- Round 1: completed. Findings accepted and fixed in Batches 1-7.
- Round 2: completed after Batch 7. Python import graph had no zero-inbound candidates except Alembic migration files; duplicate literal scan showed only three intentionally retained/high-risk tuples; tracked build-artifact scan had no matches; stale keyword/config grep had no target matches; frontend unused-symbol gate passed.
- Round 3: completed after Batch 8. Old frontend wrapper/typecheck/private-mapper references had no matches, `knip --production` was clean, tracked build-artifact scan had no matches, and low-threshold runtime `jscpd` found zero clones after excluding docs/tests/generated/migrations/README.
- Round 4: repeated the same post-Batch-8 discovery set. It again found no old references, no frontend production-unused exports, no tracked build artifacts, and zero low-threshold runtime source clones.
- Round 5: completed after Batch 9. Full high-confidence vulture scan found no dead Python symbols/variables, Settings/env coverage scan found no `.env.example` gaps, Python import graph found no new source-module islands, tracked build-artifact scan had no matches, and low-threshold runtime `jscpd` still found zero clones.
- Round 6: completed after Batch 10. Full/prod frontend Knip scans were clean, vulture at confidence 80 returned no output, `.env.example` reverse scan found no orphan sample variables, tracked build-artifact scan had no matches, and low-threshold runtime `jscpd` reported zero clones across `373` files.
- Round 7: repeated the post-Batch-10 set. It again found clean full/prod Knip, no vulture output, no orphan sample env vars, no tracked build/cache artifacts, and zero runtime clones.

## Adversarial Self Review

- Rechecked all deleted frontend wrapper imports; only canonical same-directory `components/ui/ConfirmDialog.tsx -> ./Modal` remains.
- Rechecked generated API files after export/generate; no drift.
- Rechecked that no tracked build/cache artifacts are present.
- Rechecked `apps/web/package.json`'s `export:openapi` script after proving the old bare-`python` command unsafe; the package script now runs successfully and Knip is clean.
- Kept migrations, generated clients, Temporal registration, provider registries, seed scripts, fixtures, manual DB/OSS scripts, and external deployment hooks untouched unless there was direct evidence.

## Final Rescan

- Final rescan completed after full validation, after Batch 8, after Batch 9, and through two consecutive post-Batch-10 discovery rounds with no new high-confidence cleanup candidates.

## Remaining Risks

- Dynamic references are likely in router registration, Temporal activities/workflows, provider registries, object-store config, and generated OpenAPI/schema files.
- Migrations, seeds, fixtures, generated clients, and compatibility shims require stronger evidence before deletion.
