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
| `tests/` | complete | complete | touched narrowly | Case profile test now guards shared material-kind constant behavior. |

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

Final validation:

| Command | Result | Notes |
| --- | --- | --- |
| `uv run --extra dev ruff check .` | pass | Final lint pass. |
| `git diff --check` | pass | No whitespace/error marker issues. |
| `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python -m pytest -q` | pass | Full default suite passed. |
| `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run --extra dev python scripts/export_openapi.py` | pass | OpenAPI export succeeded after clearing local proxy env. |
| `npm run generate:api` | pass | Generated TypeScript schema. |
| `git diff --exit-code apps/web/src/api/openapi.json apps/web/src/api/schema.d.ts` | pass | No generated API drift. |
| `npm run build` | pass | Frontend production build passed. |
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
- Environment failure during final OpenAPI validation: first export inherited local SOCKS proxy env and failed in `httpx`; rerun with proxy vars unset passed.
- Environment failure during full `scripts/ci_gate.sh`: local macOS lacks `timeout`; manual equivalent subcommands were run and recorded above.
- Environment failure during DB integration on the existing dev DB: local auth seed state was dirty (`admin@local.cutagent` login 401); clean temporary DB integration passed.
- Passing baseline: default pytest, frontend build, OpenAPI JSON drift check, generated TypeScript schema drift check.
- Full direct `scripts/ci_gate.sh` remains blocked by missing `timeout`; its DB and Temporal subcommands passed when run manually on a clean temporary database.

## Discovery Rounds

- Round 0: Baseline map and entrypoint scan completed.
- Round 1: completed. Findings accepted and fixed in Batches 1-7.
- Round 2: completed after Batch 7. Python import graph had no zero-inbound candidates except Alembic migration files; duplicate literal scan showed only three intentionally retained/high-risk tuples; tracked build-artifact scan had no matches; stale keyword/config grep had no target matches; frontend unused-symbol gate passed.

## Adversarial Self Review

- Rechecked all deleted frontend wrapper imports; only canonical same-directory `components/ui/ConfirmDialog.tsx -> ./Modal` remains.
- Rechecked generated API files after export/generate; no drift.
- Rechecked that no tracked build/cache artifacts are present.
- Kept migrations, generated clients, Temporal registration, provider registries, seed scripts, fixtures, manual DB/OSS scripts, and external deployment hooks untouched unless there was direct evidence.

## Final Rescan

- Final rescan completed after full validation. No new high-confidence cleanup candidates found.

## Remaining Risks

- Dynamic references are likely in router registration, Temporal activities/workflows, provider registries, object-store config, and generated OpenAPI/schema files.
- Migrations, seeds, fixtures, generated clients, and compatibility shims require stronger evidence before deletion.
