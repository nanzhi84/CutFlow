# apps/web

React 18 + Vite 6 + TypeScript 单页控制台（SPA），cutagent 系统的运营前端，通过 `/api` 代理到后端 FastAPI。

## 职责
- 案例从创建、Profile、Agent 跑批、产出/成片到发布的全流程工作台 UI（`pages/studio`、`pages/publish`）。
- 素材库、数据统计、账户中心、设置（Provider/密钥/价目）、提示词运营等控制台页面。
- 封装类型安全的后端 API 客户端（`api/client.ts`、`api/r6.ts`），统一处理 cookie 鉴权、Idempotency-Key、错误码与 requestId。

## 关键文件 / 子目录
- `src/api/client.ts` — `fetchJson`（`credentials: "include"` 带 cookie）+ `api.*` 业务端点封装；错误码映射为中文文案，提取 `request_id`。
- `src/api/r6.ts` — R6 案例 Agent/源绑定相关端点封装，复用 `client.ts` 的 `fetchJson` / `createIdempotencyKey`。
- `src/api/schema.d.ts` — 由 OpenAPI 生成的类型，**严禁手改**。
- `src/api/openapi.json` — 后端导出的 OpenAPI 契约（生成 schema 的源）。
- `src/api/realData.ts` — 真实 vs sandbox/demo 内容判定（`isRealCase`/`isRealVoice`/`isRealAssetCard`/`isRealProviderProfile`/`isRealPriceCatalog`/`isRealPriceItem`）；`client.ts` 用它把 sandbox/demo 项从列表结果中 `.filter()` 掉。
- `src/routes.ts` / `src/App.tsx` — 路由表与 router（`/`、`/studio/:caseId/{profile,agent,outputs,runs,finished-videos,publish}`、`/library/*`、`/analytics/*`、`/account/*`、`/settings`、`/ops/prompts`）。
- `src/components/AppShell.tsx` — 7 项侧栏导航（概览/案例中心/素材库/数据统计/账户中心/设置/提示词）。
- `src/contracts/*.typecheck.ts` — 仅编译期的 API 调用面守卫，由 `tsc -b`（build 的一步）检查，不在运行时执行。

## 约定与要求
- Contract-first：`schema.d.ts` 与 `openapi.json` 是生成产物，改后端 API 后须先 `npm run export:openapi`（跑 `scripts/export_openapi.py`）再 `npm run generate:api`，禁止手改。
- 任何后端调用走 `api/client.ts`（或 `r6.ts`）的封装，不要裸 `fetch`；写操作传 `idempotencyKey`。

## 测试
- 无单测；门禁靠类型与构建：`npm run build`（`tsc -b && vite build`，含 `contracts/*.typecheck.ts`）。CI 见 `.github/workflows/ci.yml` 的 frontend job。

## 注意 / 坑
- CI 两处 `git diff --exit-code` 卡漂移：unit job 跑 `export_openapi.py` 校验 `openapi.json`，frontend job 跑 `generate:api` 校验 `schema.d.ts`——务必提交最新生成结果。
- dev：`npm run dev`（vite，127.0.0.1:5173）。`/api`、`/ws` 代理目标由 `CUTAGENT_API_PROXY_TARGET` 控制（默认 `http://127.0.0.1:8000`）。
