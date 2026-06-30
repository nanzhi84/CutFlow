# apps/web

React 18 + Vite 6 + TypeScript 单页控制台（SPA），cutagent 系统的运营前端，通过 `/api` 代理到后端 FastAPI。核心依赖：`@tanstack/react-query`、`react-router-dom` v6、`tailwindcss` + `lucide-react`。

## 职责
- 案例从创建、Profile、Agent 跑批、产出/成片到发布的全流程工作台 UI（`pages/studio`、`pages/publish`）。
- 素材库、数据统计、账户中心、设置（Provider/密钥/价目）、提示词运营等控制台页面。
- 封装类型安全的后端 API 客户端（`api/client.ts`、`api/r6.ts`），统一处理 cookie 鉴权、Idempotency-Key、错误码与 requestId。
- 上传统一走浏览器直传对象存储：prepare 拿 presigned PUT，前端直接 PUT 文件，再 complete 让 API 验证并登记产物。

## 关键文件 / 子目录
- `src/api/client.ts` — `fetchJson`（`credentials: "include"` 带 cookie）+ `api.*` 业务端点封装；错误码映射为中文文案，提取 `request_id`；上传相关的 `sha256Hex`/`putToOss` 只做裸 XHR PUT（无 cookie、无 FormData）。
- `src/api/r6.ts` — R6 案例 Agent/源绑定相关端点封装，复用 `client.ts` 的 `fetchJson` / `createIdempotencyKey`。
- `src/api/schema.d.ts` — 由 OpenAPI 生成的类型，**严禁手改**。
- `src/api/openapi.json` — 后端导出的 OpenAPI 契约（生成 schema 的源）。
- `src/api/realData.ts` — 真实 vs sandbox/demo 内容判定（`isRealCase`/`isRealVoice`/`isRealAssetCard`/`isRealProviderProfile`/`isRealPriceCatalog`/`isRealPriceItem`）；`client.ts` 用它把 sandbox/demo 项从列表结果中 `.filter()` 掉。
- `src/routes.ts` / `src/App.tsx` — 路由表与 router（`/login`、`/register`、`/`、`/studio/:caseId/{profile,agent,outputs,publish}`、`/library/*`、`/analytics/*`、`/account/*`、`/settings`、`/ops/prompts`、`/publish-ops`(发布运维页 `PublishOpsPage`)）。
- `src/components/AppShell.tsx` — 8 项侧栏导航（概览/案例中心/素材库/数据统计/账户中心/设置/提示词/发布运维）。
- `src/lib/queryClient.ts` — `createAppQueryClient()` 构造全局 `QueryClient`（在 `main.tsx` 注入 `QueryClientProvider`），统一错误 toast 与重试策略。
- `src/hooks/useUpload.ts` — 直传上传状态机：`preparing → uploading → completing → completed/failed`，失败时 best-effort cancel upload session。
- `src/hooks/useRunEvents.ts` — 跑批实时事件，走 `/ws` WebSocket（连接地址取后端返回的 `stream_url`，dev 下对应 vite 的 `/ws` 代理）。

## 约定与要求
- Contract-first：`schema.d.ts` 与 `openapi.json` 是生成产物，改后端 API 后须先 `npm run export:openapi`（通过 `uv run --extra dev python scripts/export_openapi.py`）再 `npm run generate:api`，禁止手改。
- 任何后端调用走 `api/client.ts`（或 `r6.ts`）的封装，不要裸 `fetch`；写操作传 `idempotencyKey`。
- 文件上传不要改回 API 代理/`FormData`；只通过 `useUpload()` 跑 prepare → `putToOss` → complete。`UploadSession.upload_url` 是 legacy mirror，不是上传地址，上传地址只读 `PrepareUploadResponse.put_url`。

## 测试
- 无单测；门禁靠真实页面/组件类型与构建：`npm run build`（`tsc -b && vite build`）。CI 见 `.github/workflows/ci.yml` 的 frontend job。

## 注意 / 坑
- CI 两处 `git diff --exit-code` 卡漂移：unit job 跑 `export_openapi.py` 校验 `openapi.json`，frontend job 跑 `generate:api` 校验 `schema.d.ts`——务必提交最新生成结果。
- dev：`npm run dev`（vite，127.0.0.1:5173）。`/api`、`/ws` 代理目标由 `CUTAGENT_API_PROXY_TARGET` 控制（默认 `http://127.0.0.1:8000`）。
