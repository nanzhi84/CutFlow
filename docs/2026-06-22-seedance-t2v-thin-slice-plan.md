# Seedance 文生/图生视频「极薄链路」实现方案（国内火山引擎方舟）

- 日期：2026-06-22（2026-06-22 更新：参考图列为**第一期必做**，走**路径 A = presign 公网 HTTPS URL**）
- 需求：给系统加一条用 **Seedance 直接一次性生成视频**的链路，**15s / 9:16 / 720p**，**必须支持喂参考图素材**（图生视频 / 参考一致性）。
- 账号：**国内火山引擎方舟（Volcengine Ark，`ark.cn-beijing.volces.com`）**。
- 结论：**不引入 codex 那套统一 CreativePlan / case_reference_assets 表 / 多 artifact kind / 改名请求契约**。本方案是与 `digital_human_v2` / `broll_only_v1` 平级的第三条独立链路，**零 DB 迁移、不碰 case_agent**；唯一契约改动是请求体加 **1 个字段 `reference_asset_ids`**（参考图所需）。

---

## 0. 一句话架构

```
前端 contentMode=seedance + 选参考图  ──►  复用 POST /api/jobs/digital-human-video
                                          （workflow_template_id="seedance_t2v_v1"
                                            + reference_asset_ids=[...]）
                                                  │
                                                  ▼
  ValidateRequest ─► LoadCaseContext ─► SeedanceGenerateVideo ─► ExportSeedanceVideo ─► FinalizeRunReport
   (复用,加voice守卫)   (复用)            (新:拼prompt+解析参考图URI+调provider) (新:精简成片+发布包) (复用)
                                                  │
                                                  ▼
                ProviderGateway.invoke(capability="video.generate", input.references=[{uri,role}])
                                                  │
                                                  ▼
   ArkSeedanceProvider（新插件）：把内部 URI **presign 成阿里云 OSS 公网 HTTPS**（_public_url）
                                  → content 数组带 image_url → 提交火山 → 轮询 → 下载转存
```

5 个工作流节点（3 复用 +2 新）。1 个新 provider 插件。1 个新请求字段。其余是运维数据 + 前端选图器。

**参考图怎么到火山（路径 A，已被 videoretalk 在生产验证）**：素材存在 `s3://cutagent-materials/...`（阿里云 OSS）→ 节点用 `source_artifact_for_asset(asset_id)` 拿 `artifact.uri` → provider 用 `_public_url()` presign 成带签名的公网 HTTPS（2h 有效）→ 放进火山请求的 `content[].image_url.url` → 火山服务器直接 GET 下载。本地 `local://`/MinIO 会 **fail loudly**（符合 no-silent-degrade），真实路径必须是公网可达的阿里云 OSS。

---

## 1. 火山方舟 Seedance API 事实（已联网核实，2026-06）

| 项 | 值 |
|---|---|
| 建任务 | `POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks` → `{"id":"cgt-..."}` |
| 查任务 | `GET .../contents/generations/tasks/{id}` |
| 鉴权 | `Authorization: Bearer <ARK_API_KEY>`（简单 Bearer，对上 SecretStore 单密钥） |
| 请求体 | `model` + `content`（数组）+ **顶层** `ratio`/`resolution`/`duration`/`watermark`/`generate_audio`/`seed` |
| **content：文本** | `{"type":"text","text":"<prompt>"}` |
| **content：参考图** | `{"type":"image_url","image_url":{"url":"<公网https或base64>"},"role":"reference_image"}` |
| **参考图 role** | `reference_image`（主体/场景一致性，**我们用这个**）/ `first_frame` / `last_frame`（首尾帧控制） |
| **参考图 url 形态** | 公网 HTTPS URL（**路径 A 用这个**）或 base64 data URL；多张参考图放多个 image_url 条目 |
| status 机 | `queued`/`running`/`succeeded`/`failed`/`expired`/`cancelled` |
| 成片地址 | `content.video_url`，**仅 24h 有效，provider 内必须立刻下载转存** |
| 约束对得上 | `ratio` 支持 `9:16` ✓ · `resolution` 支持 `720p` ✓ · `duration` 最大 15s ✓ |
| model id | `doubao-seedance-*` 名称或接入点 `ep-xxx`，做 profile.model_id 配置项，**不硬编码** |

**参数风格坑**：Seedance **2.0** 用顶层 JSON 字段（`ratio/resolution/duration`）；**1.x** 用 `content` 文本末尾 `--rt/--rs/--dur` 后缀。本方案默认 2.0 顶层字段，`default_options.param_style` 可切 1.x。落地前用官方文档（https://www.volcengine.com/docs/82379/1520757 ）对一遍真实字段名（参考图 `role`/数量上限/图尺寸限制也以官方为准）。

---

## 2. 改动清单总览

| # | 模块 | 文件 | 性质 |
|---|---|---|---|
| A | Provider 插件 | `packages/ai/providers/seedance.py`（新）+ `__init__.py` 注册 | 新代码（含参考图 presign） |
| B | Sandbox 分支 | `packages/ai/gateway/provider_gateway.py`（SandboxProvider） | 改 |
| C | 出站白名单 | `packages/ai/netpolicy.py` | 改 1 行 |
| D | Profile/价目 seed | `packages/core/storage/provider_seed.py` | 改（可选，也可纯 API arm） |
| **E** | **请求契约** | `packages/core/contracts/jobs.py` 加 `reference_asset_ids` + 重生成 openapi/schema.d.ts | **改 1 字段（唯一契约漂移）** |
| F | 工作流节点 | `nodes/seedance_generate_video.py`+`nodes/export_seedance_video.py`（新）、`node_sequence.py`、`digital_human.py`、`nodes/__init__.py`、`temporal_adapter.py` | 新+接线 |
| G | ValidateRequest 守卫 | `nodes/validate_request.py` | 改 1 处 |
| H | 前端第三态 + 选图器 | `studioCreateModel.ts`、`StudioCreatePage.tsx`、`StudioCreateSteps.tsx` | 改/新 |
| I | 运维 arm | secret + profile + **公网阿里云 OSS** | 运维 |

> ⚠️ 下方骨架签名（`ProviderCall`/`ProviderResult`/`NodeContext`/`common.*`/`_public_url`）来自对真实代码的核实，**提交前用真实文件再核一遍**；火山 endpoint/字段以官方文档为准（已标 `# TODO 核对`）。

---

## A. Provider 插件 `packages/ai/providers/seedance.py`（新建）

仿 `videoretalk.py`「异步提交 → mark_polling → 轮询 → 下载 → store_media_bytes」骨架，**参考图 presign 直接复用 videoretalk 的 `_public_url` 模式**。

```python
from __future__ import annotations

import time
from datetime import timedelta

from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_gateway import ProviderCall, ProviderResult, ProviderRuntimeError
from packages.ai.providers.common import option, poll_budget, request, require_secret, response_json
from packages.core.contracts import ArtifactKind, ErrorCode


class ArkSeedanceProvider:
    """国内火山引擎方舟 Seedance 文生/图生视频（capability='video.generate'）。"""

    provider_id = "volcengine.seedance"  # 必须与 ProviderProfile.provider_id / 价目 provider_id 三处一致

    def __init__(self, client) -> None:   # httpx.Client，由 register_real_provider_plugins 注入
        self.client = client

    def invoke_with_context(self, call: ProviderCall, context: ProviderInvocationContext) -> ProviderResult:
        if call.capability_id != "video.generate":
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, f"不支持的能力: {call.capability_id}")

        api_key = require_secret(context)                       # 走 SecretStore，绝不读 env
        base_url = str(option(context, "base_url", "https://ark.cn-beijing.volces.com/api/v3")).rstrip("/")
        model_id = context.profile.model_id
        timeout = float(context.profile.timeout_sec)

        prompt = str(call.input.get("prompt") or "").strip()
        if not prompt:
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "Seedance 生成缺少 prompt")

        duration = int(call.input.get("duration_sec") or option(context, "duration", 15))
        ratio = str(call.input.get("ratio") or option(context, "ratio", "9:16"))
        resolution = str(call.input.get("resolution") or option(context, "resolution", "720p"))
        param_style = str(option(context, "param_style", "json_fields"))  # json_fields(2.0) | prompt_suffix(1.x)

        # ---- 参考图（路径 A）：把内部 URI presign 成火山可下载的公网 HTTPS ----
        references = call.input.get("references") or []          # [{"uri": "s3://...", "role": "reference_image"}, ...]
        content: list[dict] = [{"type": "text", "text": prompt}]
        for ref in references:
            url = self._public_url(context, str(ref.get("uri") or ""))   # 非公网 → fail loudly
            content.append({"type": "image_url",
                            "image_url": {"url": url},
                            "role": str(ref.get("role") or "reference_image")})

        # ---- 组请求体 ----  # TODO 核对火山方舟官方字段名
        if param_style == "prompt_suffix":  # Seedance 1.x：参数拼进首条 text
            content[0]["text"] = f"{prompt} --rt {ratio} --rs {resolution} --dur {duration}"
            body = {"model": model_id, "content": content}
        else:                                # Seedance 2.0（默认）：顶层字段
            body = {"model": model_id, "content": content,
                    "ratio": ratio, "resolution": resolution, "duration": duration, "watermark": False}

        # ---- 提交异步任务 ----
        resp = request(self.client, "POST", f"{base_url}/contents/generations/tasks",
                       headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                       json_body=body, timeout=timeout)
        task_id = str(response_json(resp).get("id") or "")
        if not task_id:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "方舟未返回 task id")
        context.mark_polling(task_id)

        # ---- 轮询（火山 path/字段与 DashScope 不同，照其结构自写）----
        interval, max_attempts = poll_budget(context.profile.default_options,
                                             default_interval=8, default_max_attempts=180,
                                             timeout_minutes=call.input.get("timeout_minutes"))
        payload, attempts = {}, 0
        for attempts in range(1, max_attempts + 1):
            poll = request(self.client, "GET", f"{base_url}/contents/generations/tasks/{task_id}",
                           headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
            payload = response_json(poll)
            status = str(payload.get("status") or "")
            if status == "succeeded":
                break
            if status in {"failed", "expired", "cancelled"}:
                raise ProviderRuntimeError(ErrorCode.provider_remote_failed, f"方舟任务 {status}: {payload}")
            time.sleep(interval)
        else:
            raise ProviderRuntimeError(ErrorCode.provider_timeout, f"方舟任务轮询超时: {task_id}")

        # ---- 取成片 URL（24h 失效！立刻下载）----  # TODO 核对字段路径
        video_url = str((payload.get("content") or {}).get("video_url") or "")
        if not video_url:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "方舟成功但无 video_url")
        video_bytes = request(self.client, "GET", video_url, timeout=timeout).content

        # ---- 落对象存储产 artifact（durable：成片是交付物，发布要下载）----
        artifact = context.store_media_bytes(content=video_bytes, filename="seedance.mp4",
                                             purpose="generated-video", kind=ArtifactKind.video_rendered,
                                             call=call, tier="durable")

        return ProviderResult(
            output={"video_artifact_id": artifact.id, "video_uri": artifact.uri,
                    "external_job_id": task_id, "report": "pass"},
            video_seconds=float(duration),
            raw_usage={"poll_attempts": attempts, "provider_response": payload},
        )

    @staticmethod
    def _public_url(context: ProviderInvocationContext, uri: str) -> str:
        """把内部对象存储 URI presign 成厂商可下载的公网 HTTPS（照抄 videoretalk._public_url:112-132）。
        非公网（本地 LocalObjectStore 的 local:// 签名）→ fail loudly，绝不把死链发给火山。"""
        if not uri:
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "参考图 URI 为空")
        if not uri.startswith(("s3://", "local://")):
            return uri  # 已是外部 http(s) 直链
        signed = context.object_store.signed_url(uri, expires_in=timedelta(hours=2)).url
        if not signed.startswith(("http://", "https://")):
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "Seedance 参考图需要公网可下载 URL，但对象存储产出了非公网签名链接。"
                "真实路径请用 durable 的阿里云 OSS（cutagent-materials/prod），不要本地 MinIO。",
            )
        return signed
```

> `_public_url` 与 videoretalk 完全同款，未来可抽到 `common.py` 共用（目前复制 ~15 行即可，零风险）。

注册（唯一入口）：`packages/ai/providers/__init__.py` import 区加 `from .seedance import ArkSeedanceProvider`，注册元组加 `ArkSeedanceProvider(client),`。

---

## B. Sandbox 分支

`SandboxProvider.invoke`（provider_gateway.py，lipsync.video 分支后、兜底 return 前）加：

```python
if call.capability_id == "video.generate":
    return ProviderResult(
        output={"video_uri": f"sandbox://video/seedance/{uuid4().hex}.mp4",
                "video_artifact_id": None, "external_job_id": f"sandbox-{uuid4().hex[:8]}", "report": "pass"},
        video_seconds=float(call.input.get("duration_sec", 15) or 15))
```

> sandbox 不真的下载参考图、也不产真 artifact。默认路径（`sandbox_fallback_allowed()=False`）无真 key 必须 fail loudly。

---

## C. 出站 host 白名单

`packages/ai/netpolicy.py` 的 `DEFAULT_ALLOWED_HOSTS` 加 `"ark.cn-beijing.volces.com"`（备用 `"volces.com"`）。**注意还要确保阿里云 OSS 域名（`oss-cn-shanghai.aliyuncs.com` 等）在白名单里**——presign 出来的参考图 URL 主机是它；它本就是现有 videoretalk 路径在用的，通常已在白名单，确认即可。

---

## D. Profile + 价目 seed（`provider_seed.py`，可选）

仿 `dashscope.videoretalk.prod`：

```python
ProviderProfile(
    id="volcengine.seedance.prod", provider_id="volcengine.seedance",
    model_id="doubao-seedance-2-0-xxx",        # 真实模型名/接入点 ep-，运维定
    capability="video.generate", display_name="火山方舟 Seedance Production",
    environment="prod", secret_ref="volcengine_seedance_prod.secret",
    concurrency_key="volcengine:video.generate", timeout_sec=600,
    options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.video.options"),
    default_options={"base_url": "https://ark.cn-beijing.volces.com/api/v3",
                     "ratio": "9:16", "resolution": "720p", "duration": 15,
                     "param_style": "json_fields", "poll_interval": 8, "poll_max_attempts": 180})
```

- **SAFETY INVARIANT**：只放 `secret_ref`，绝不 seed secret 明文。
- 加 `ProviderPriceCatalog(status="published")` + `ProviderPriceItem(unit="media_second", capability_id="video.generate")`，否则记 `cost.unpriced` 告警。

---

## E. 请求契约：加 `reference_asset_ids`（唯一契约漂移）

`packages/core/contracts/jobs.py` 的 `DigitalHumanVideoRequest` 加：

```python
reference_asset_ids: list[str] = Field(default_factory=list)
"""Seedance 参考图素材 id 列表；其它模板忽略。空 = 纯文生视频。"""
```

- 这是参考图唯一需要的契约改动。**默认空** → 同一字段同时支持纯文生和图生，向后兼容。
- 改完按 contract-first 重生成：`python scripts/export_openapi.py && (cd apps/web && npm run generate:api)`。
- ⚠️ 重生成的 `openapi.json`/`schema.d.ts` key-order 对本地 pydantic/Python 版本敏感（见 memory `OpenAPI drift is env-sensitive`）——**以 CI pinned venv 为准**，本地若与 CI 不一致不要反复 local-regen 去"修"。
- 是否**强制**每个 Seedance job 至少 1 张参考图：默认不强制（空=纯文生）；要强制就在 ValidateRequest 加 `if "SeedanceGenerateVideo" in node_ids and not request.reference_asset_ids: raise ...`，或前端禁用提交。按你需求二选一。

---

## F. 工作流节点

### F1. 新节点 `nodes/seedance_generate_video.py`

```python
from __future__ import annotations

from packages.ai.gateway.provider_gateway import ProviderCall
from packages.core.config.settings import sandbox_fallback_allowed
from packages.core.contracts import ErrorCode
from packages.core.workflow.runtime import NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._errors import NodeExecutionError  # 按真实路径核对


def run(ctx: NodeContext) -> NodeOutput:
    request = ctx.state.request
    case = ctx.repository.cases[request.case_id]
    prompt = (request.script or "").strip() or _compose_prompt_from_case(case)

    # 参考图：asset_id → artifact.uri（内部 URI，由 provider 内 presign 成公网）
    references = []
    for asset_id in (getattr(request, "reference_asset_ids", None) or []):
        artifact = ctx.source_artifact_for_asset(asset_id)        # _node_context.py:96
        if artifact is None:
            raise NodeExecutionError(ErrorCode.artifact_missing, f"参考图素材不存在: {asset_id}")
        references.append({"uri": artifact.uri, "role": "reference_image"})

    profile = ctx.first_available_provider_profile("video.generate", include_sandbox=sandbox_fallback_allowed())
    if profile is None:
        raise NodeExecutionError(ErrorCode.provider_unsupported_option,
            "未配置可用的真实文生视频(Seedance)供应商；请配置 capability=video.generate 并激活 secret。")

    invocation, result = ctx.provider_gateway.invoke(ProviderCall(
        case_id=ctx.run.case_id, run_id=ctx.run.id, node_run_id=ctx.node_run.id,
        provider_profile_id=profile.id, capability_id="video.generate",
        input={"prompt": prompt, "duration_sec": 15, "ratio": "9:16", "resolution": "720p",
               "references": references},
        idempotency_key=f"{ctx.run.id}:{ctx.node_run.id}:seedance"))
    if result is None or invocation.error:
        raise NodeExecutionError(
            (invocation.error.code if invocation.error else ErrorCode.provider_remote_failed),
            "Seedance 视频生成失败", retryable=False)   # 不幂等：失败不自动重试

    artifact = ctx.repository.artifacts[result.output["video_artifact_id"]]
    return NodeOutput(artifacts=[artifact], provider_invocation_ids=[invocation.id])


def _compose_prompt_from_case(case) -> str:
    bits = [case.product, "、".join(case.key_selling_points or []), case.ip_persona, case.brand_voice]
    return "，".join(b for b in bits if b)
```

### F2. 新节点 `nodes/export_seedance_video.py`（精简成片）

不复用 `ExportFinishedVideo`（硬 require `video.final`+`plan.timeline`+`plan.style`，且 `VideoVersion` 要两个非空 plan 外键）。直接产 `FinishedVideo` + 发布包：

```python
def run(ctx: NodeContext) -> NodeOutput:
    state, run, repo = ctx.state, ctx.run, ctx.repository
    video = state.require(ArtifactKind.video_rendered)
    finished = FinishedVideo(
        id=new_id("fv"), case_id=state.request.case_id, run_id=run.id,
        owner_user_id=_resolve_owner_user_id(run, repo),          # 抄 export_finished_video:136-148
        title=state.request.title or "Seedance 短片",
        video_number=next_finished_video_number(
            v.video_number for v in repo.finished_videos.values() if v.case_id == state.request.case_id),
        video_artifact=repo.artifact_ref(video.id), cover_artifact=None,
        duration_sec=15.0, lipsync_provider_id=None, lipsync_fallback_used=False)
    repo.finished_videos[finished.id] = finished
    repo.create_publish_package_from_finished_video(            # 零 timeline/style 依赖，原样复用
        finished, title=finished.title, description=state.request.publish_content)
    repo.create_event("workflow.finished_video.created", ...)    # 抄 export_finished_video:102-122
    return NodeOutput(artifacts=[...])
```

### F3. 接线（同步「四处」——CLAUDE.md 漏写 `_NODE_OUTPUT_KINDS`）

- `node_sequence.py`：`SEEDANCE_T2V_SEQUENCE = ["ValidateRequest","LoadCaseContext","SeedanceGenerateVideo","ExportSeedanceVideo","FinalizeRunReport"]`；`WORKFLOW_TEMPLATE_NODE_COUNTS` 加 `"seedance_t2v_v1": len(...)`
- `digital_human.py`：import 序列；`NODE_HANDLERS` 加两个新节点；**`_NODE_OUTPUT_KINDS`** 加 `"SeedanceGenerateVideo":[ArtifactKind.video_rendered]`、`"ExportSeedanceVideo":[ArtifactKind.publish_package]`；**`_PROVIDER_SIDE_EFFECT_NODES`** 加 `"SeedanceGenerateVideo"`（否则 reuse 静默重放付费调用）；新增 `seedance_t2v_template()` 注册进 `_TEMPLATE_BUILDERS`
- `nodes/__init__.py`：import + `__all__` 加两个新节点
- `temporal_adapter.py` 的 `_node_timeout_seconds`：加 `if node_id == "SeedanceGenerateVideo": return 15 * 60`

---

## G. ValidateRequest voice 守卫（改 1 处）

`nodes/validate_request.py` 把无条件 voice 校验包进 `if "TTS" in node_ids:`（沿用现有 LipSync/Broll 守卫模式）。Seedance 序列无 TTS → 天然跳过。`script` 校验保留（当 prompt 用）。

---

## H. 前端：contentMode 第三态 + 参考图选图器

### H1. contentMode 第三态（同前，5 处）
1. `studioCreateModel.ts:8` 联合类型加 `"seedance"`
2. `studioCreateModel.ts:~92` `loadStoredForm` 持久化白名单加 `seedance`（**否则刷新静默回落，且不报错**）
3. `studioCreateModel.ts:~133` `contentModeLabel` 加一支
4. `studioCreateModel.ts:~118` `validateStep` step2 voice 校验加 `&& form.contentMode!=="seedance"`
5. `StudioCreatePage.tsx buildJobPayload`：`isSeedance` 标志；`workflow_template_id` 三路；`lipsync.enabled`/`broll.enabled` 在 seedance 强制 false

### H2. 参考图选图器（新）
- **FormState 加字段**：`seedanceReferenceAssetIds: string[]`（`studioCreateModel.ts` defaultForm + 持久化集合）
- **选图 UI**：在 Seedance 分支的步骤里加一个多选面板，从**已有素材库**列图（复用现有上传/素材列表的 api，挑 `kind in {portrait, image, broll}` 的图片素材）。选中存 `seedanceReferenceAssetIds`。素材库已存在，这里只是加 picker + 多选状态。
- **payload**：`buildJobPayload` 里 `reference_asset_ids: isSeedance ? form.seedanceReferenceAssetIds : []`
- **可选强制**：若要"必须选至少 1 张"，在 `validateStep` 对 seedance 加 `if (isSeedance && refs.length === 0) return "请至少选择一张参考图"`
- UI 三目（`SubmitStep`/`ConfigSummary`）改三路，seedance 显示「Seedance · 15s 9:16 720p · N 张参考图」

验证：`cd apps/web && npm run build`（tsc 逼出遗漏分支）。

---

## I. 运维 arm（数据 + 基础设施）

1. **secret**：`LocalSecretStore.put("<火山方舟真key>", secret_ref="volcengine_seedance_prod.secret")`，或 API `CreateSecretRequest` + `PATCH /provider-profiles/{id}`。
2. **profile**：若没 seed（D），经 API 建 enabled `video.generate` profile。
3. **公网阿里云 OSS（路径 A 的硬前提）**：参考图素材必须存在阿里云 OSS 后端的桶（`cutagent-materials`/`cutagent-prod`），其 presign 出来的是公网 HTTPS。**本地 MinIO/`local://` 不可达，会 fail loudly**——本地联调要么把素材上传到阿里云 OSS、要么临时退 base64（见风险 §3.2）。

---

## 3. 关键正确性 / 上线风险

1. **参考图必须公网可达（路径 A 硬前提）**：素材在阿里云 OSS → presign 公网 HTTPS ✓。本地 MinIO 不行（fail loudly，符合预期）。这是参考图唯一可能"卡基础设施"的点，已被 videoretalk→DashScope 在生产验证同款路径可行。
2. **【必做冒烟测试】火山是否接受带签名查询参数的 URL**：presigned URL 形如 `https://...aliyuncs.com/x.jpg?Signature=...&Expires=...`。上线前用一张真图打一次火山 i2v 接口确认能下载；**若火山拒绝带 query 的签名链，立刻切 base64 内联（路径 B）**作为兜底（`image_url.url` 也接受 `data:image/...;base64,...`，单图几百 KB~2MB 在请求体限制内；大图先压）。这是参考图唯一的"未知数"，但低风险且有现成兜底。
3. **24h URL**：火山成片 URL 仅 24h——provider 内同步下载 store_media_bytes 已覆盖。
4. **异步轮询超时**：`SeedanceGenerateVideo` 在 `_node_timeout_seconds` 加特判（15min），靠 worker 后台 heartbeat 续命。
5. **不幂等 → 不重试**：`retryable=False` + 进 `_PROVIDER_SIDE_EFFECT_NODES`，防 resume 重复扣费。
6. **durable OSS**：成片 `tier="durable"`，否则被 ephemeral GC 清掉、发布下载不到。
7. **worker 独立进程**：改完 `packages/ai/providers/*` 与 `packages/production/*` 必须重启 worker。
8. **host 白名单**：火山 host + 阿里云 OSS host 都要在白名单。
9. **计价**：`video_seconds=15.0` + `media_second` 价目，否则 `cost.unpriced` 告警。

---

## 4. 测试计划

- **Provider 单测**（`tests/providers/`，`httpx.MockTransport`）：
  - 纯文生：POST `/tasks`→`{"id":"cgt-1"}` → GET running → GET succeeded+`content.video_url` → GET video_url 返 fixture mp4；断言 succeeded、`external_job_id=="cgt-1"`、`artifact.media_info.media_type=="video"`、`video_seconds==15.0`。
  - **图生（参考图）**：profile 用 S3 mock OSS（`signed_url` 返 `https://oss.example/...`）；`call.input.references=[{"uri":"s3://...","role":"reference_image"}]`；断言提交 body 的 `content` 含一条 `image_url.url` 是 `https://...`。
  - **fail-loud**：LocalObjectStore（`signed_url` 返 `local://`）→ `_public_url` 抛 `provider_unsupported_option`（参考图死链不外发）。
  - 失败/超时/缺 secret 各一条。
- **Workflow 节点测**：`seedance_t2v_v1` 序列=5、`_NODE_OUTPUT_KINDS` 命中、`SeedanceGenerateVideo` 在 `_PROVIDER_SIDE_EFFECT_NODES` 有 `idempotency_key`；节点把 `reference_asset_ids` 解析进 `call.input.references`。
- **契约测**：`DigitalHumanVideoRequest` 带/不带 `reference_asset_ids` 都能反序列化；空列表=纯文生。
- **端到端（sandbox）**：`CUTAGENT_ALLOW_SANDBOX_FALLBACK=1` 跑通出 `FinishedVideo`+发布包；关 fallback 且无 profile → 显式失败。
- **前端**：`npm run build` 通过。
- 本地 pytest 用 `CUTAGENT_SECRET_STORE_DIR=<空目录>` 避免真实 secret 污染假失败。

---

## 5. 实施顺序

1. **Provider 插件（含参考图 presign）+ sandbox + netpolicy + 单测**（含图生 + fail-loud 用例，可独立验证）。
2. **请求契约 `reference_asset_ids` + 重生成 openapi/schema.d.ts**。
3. **两个新节点 + 接线四处 + temporal timeout + ValidateRequest 守卫 + 节点测**。
4. **provider_seed profile/价目**（或纯运维 arm）。
5. **前端第三态 5 处 + 参考图选图器 + `npm run build`**。
6. **运维**：arm secret + 确认素材在公网阿里云 OSS。
7. **冒烟测试**：真图打火山 i2v，确认 presigned URL 可被下载（不行切 base64）。
8. **重启 worker**，用真实 case + 参考图跑一条 ≤15s 成片，确认进成片页 + 发布包。

---

## 6. 明确不做（YAGNI）

- ❌ CreativePlan / CreativeShot / SeedancePromptPack / SeedanceReferenceBinding / CreativeCompliance 全套契约
- ❌ **`case_reference_assets` 整张表 + reference-assets CRUD + 授权状态机 + provider_asset_id 预上传**：那是"把带角色/授权的素材**持久绑定**到 case、并预上传到厂商换 id"的产品功能。我们的做法——请求里传 `reference_asset_ids`、provider 内直接 presign 公网 URL 喂给火山——**不需要预上传、不需要绑定表**。等真要"一次绑定多次复用、不每次重选"时再加，是 UX 便利不是瓶颈。
- ❌ `ScriptDraft`/`ScriptVersion` 加 `creative_plan_artifact_id`
- ❌ `creative.plan`/`seedance.prompt_pack`/`seedance.generation_report` artifact kind（复用 `video.rendered` + `FinishedVideo` + 现成 run report）
- ❌ 4 个新 prompt 组 + `creative_plan.output` schema
- ❌ 改名 `CaseVideoGenerationRequest`/新 `/api/jobs/case-video`（复用 `digital-human-video` 端点）
- ❌ `BuildSeedanceStylePlan`/`ResolveCreativePlan`/`ResolveSeedanceReferences` 节点

## 7. Phase 2 可选扩展

- **base64 兜底路径（路径 B）**：若火山不接受签名 URL，或某环境无公网 OSS——provider 内把素材 bytes 读出转 base64 data URL 填 `image_url.url`（大图先压）。已在风险 §3.2 备好。
- **首尾帧控制**：`role` 支持 `first_frame`/`last_frame`——把请求字段从 `list[str]` 升级为 `list[{asset_id, role}]`（再一次小契约改动）。
- **后包装模式**：Seedance 只出画面，复用 `SubtitleAndBgmMix` 二次配 BGM/字幕（注意帧数精确校验坑）。
- **第二家厂商**：再加 provider plugin + profile，不动链路。
- **per-case 参考素材持久绑定**：真有"复用素材集"需求时，再考虑轻量绑定（仍不必是 codex 那张 9 字段表）。
