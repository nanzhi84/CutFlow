# Redis 是可选的跨进程协调层，不是查询缓存

**一句话结论：** CutFlow 里的 Redis 是一个**可选的、短生命周期的跨进程协调层**。业务事实由 PostgreSQL、Temporal 和对象存储持有，Redis 只在多进程/多副本之间共享一小段临时协调状态。它**不是** PostgreSQL 查询缓存，不是视频生产任务的必需存储，不是 Temporal 的队列/状态后端，也不是预算/成本/熔断的事实源。

未配置 `CUTAGENT_REDIS_URL` 时，相关组件按设计退回进程内状态，系统照常工作；启动了 Redis 容器也不代表应用已连接或使用它——是否启用只看 `CUTAGENT_REDIS_URL`。

本文档是 Redis 用途的权威说明。改动 Redis 相关代码或运维配置前先读这里，避免把它误用成通用查询缓存，或在不需要跨进程一致性的场景把它升级为强制依赖。

## 四项真实用途

只有四处运行时代码把 Redis 当协调状态来读写（外加两个非存储触点，见后文）。

### 1. Provider 并发与 QPS 协调

`packages/ai/gateway/provider_limiter.py`。当 `CUTAGENT_REDIS_URL` 存在时，Redis 保存：

- 每个 `concurrency_key` 的短期并发 lease（ZSET，`cutagent:provider:<concurrency_key>:leases`）；
- 每个 `concurrency_key` 的 QPS token bucket（Hash，`cutagent:provider:<concurrency_key>:qps`）；
- lease / QPS key 的 TTL（`PEXPIRE`）和崩溃后自动清理（`ZREMRANGEBYSCORE` 按时间戳剔除过期 lease）。

目的不是缓存 provider 结果，而是让 API 与 worker、或多个进程/副本共享同一份厂商调用额度。获取/释放通过一段原子 Lua 脚本完成（`_ACQUIRE_SCRIPT`）。

**无 Redis 时：**

- `max_inflight` 只在单进程内生效（进程内 `threading.BoundedSemaphore`）；
- API 与 worker 各自持有一套 semaphore，两个进程的并发是叠加的；
- **QPS 完全不执行**——本地 fallback 里没有任何 token bucket。这是模块 docstring 明示的有意设计（"QPS needs shared state and is therefore not enforced without Redis"），不是"QPS 只是不做跨进程协调"：进程内也没有 QPS 限制。

**有 Redis 时：**

- API 与 worker 的总并发共同受限于同一份 lease 集合；
- QPS 在所有接入同一 Redis 的进程间共享；
- Redis 故障时 fail-safe 回退到进程内限制（并发退回 semaphore、QPS 停止执行），记录 degradation，并按 #67 的 30 秒冷却懒重连（下一次经过冷却期的 `slot()` 调用尝试重连，成功即恢复跨进程执行）。

### 2. Run 事件实时 fanout

`packages/core/observability/events.py` 的 `InProcessFanoutHub`。用 Redis Pub/Sub 在多个 API 副本之间广播 run 事件，频道形如：

```text
cutagent:run:<run_id>
```

**注意这是 Pub/Sub channel 而非普通 key**，基于 `SCAN` / `KEYS` 的巡检看不到它。

事件历史与断线 replay 的事实源仍是 **PostgreSQL outbox**（`SqlAlchemyOutboxDispatcher` 派发、`replay_sqlalchemy_outbox` 按 `(created_at, id)` 顺序回放，支持 `after_event_id` cursor）。Redis 只负责实时广播，不负责事件持久化——Redis 挂掉不会丢事件历史，只是跨副本的实时推送退回到本副本进程内 fanout。故障后同样按 #67 的 30 秒冷却懒重连。

### 3. WebSocket stream token

`packages/core/observability/events.py` 的 `EventStreamTokenStore`。短期 WebSocket token 写入 Redis，key 形如：

```text
cutagent:event-token:<token>
```

这样 API 副本 A 签发的 token 可以由副本 B 校验：签发（`/api/runs/{run_id}/events` 返回 token）与 WS 握手校验（`/ws/runs/{run_id}`）是两次独立请求，可能落在不同副本上。token 带 TTL（写入时用 `px` 设过期），不属于长期认证 / session 存储。无 Redis 或 Redis 退化时退回进程内 token 字典，跨副本校验失效，但同副本仍可用。故障后按 #67 的 30 秒冷却懒重连。

### 4. 登录 / 注册防爆破计数

`packages/core/auth/rate_limit.py` 的 `_SlidingWindowLimiter`。用 Redis 共享登录失败和注册尝试的滑动窗口计数（ZSET + `PEXPIRE`），key 为完整复合形态：

```text
cutagent:auth-rate:login:<client_id>:<identifier>
cutagent:auth-rate:register:<client_id>
```

无 Redis 时回退到每个 API 进程各自的内存 bucket。

> **⚠️ 该组件的降级语义与前三项不对称，单列说明，不要套用前三项的统一降级叙述。**
>
> 1. **不进 readiness 503 判定。** `/api/health/ready` 的 required 模式只检查 `event_hub` / `event_tokens` / `provider_limiter` 三个组件（`apps/api/services/core.py` 的 `readiness`）；`_SlidingWindowLimiter` 根本没有 `is_redis_degraded` 接口，因此认证限流退化时 readiness 不会 503。
> 2. **没有 #67 式的 30 秒冷却懒重连。** 一旦 `_redis_failed` 置位，除非 `redis_url` 变化（`_sync_redis_url`）或进程重启，永久停留在进程内 bucket，不会自动重连回 Redis。
> 3. **degrade 时不上报遥测。** 只打一条 `logger.warning`，**不**调用 `record_redis_degraded`，所以 `redis_degraded` 指标不覆盖认证限流（该指标的 `component` 只有 `event_fanout` / `event_token_store` / `provider_limiter` 三个值）。

这三处差异是当前实现的如实记录，不是本文档要修的缺陷；是否补齐重连 / 遥测 / readiness 对齐另行开 issue 决定。认证限流的 Redis / fallback 行为有测试覆盖：`tests/core/test_auth_rate_limit.py`。

### 触点完整性说明

除以上四项协调用途外，运行时还有两个 Redis 触点。它们不持有协调状态，若声称"只有四处代码连 Redis"需一并说明：

- **`/api/health/network` 的诊断 ping**（`apps/api/services/core.py` 的 `network_diagnostics`）：每次请求新建一个 `redis.Redis` 连接、`ping()` 后即弃，只测活、不持有状态。区分三态 `not_configured`（未配 `CUTAGENT_REDIS_URL`）/ `ok` / `failed`。
- **`packages/core/observability/telemetry.py` 的 Prometheus 指标**：`redis_degraded`（Gauge，1=已退化到进程内 fallback）与 `redis_reconnect_attempts_total`（Counter），均带 `component` 标签。这是 degradation 的可观测性，不是数据存储。

另外 `packages/core/config/preflight.py` 有对 Redis 配置的纯校验逻辑（见下面的拓扑硬闸门），只在生产启动前跑一次，不在运行时连 Redis。

## Provider 限流分组原则（capability-scoped）

厂商通常按能力分别限流，因此默认**不应**把同一厂商的所有能力粗暴合并成一个 vendor-wide key。当前 seed（`packages/core/storage/provider_seed.py`）共有 **14 个** `concurrency_key`。

按 `provider_seed.py` 逐个列举，其中 **12 个能力级 key**：

```text
dashscope:llm.chat
dashscope:asr.transcribe
dashscope:vlm.annotation
dashscope:audio.understanding
dashscope:multimodal.embedding
dashscope:lipsync.video

volcengine:tts.speech
volcengine:image.generate
volcengine:video.generate

minimax:tts.speech
runninghub:lipsync.video
openai:image.generate
```

**2 个例外**：`volcengine:billing` 与 `aliyun:billing` 是 capability=`balance.monitor` 的 balance-only profile，key 格式是 `vendor:billing` 而非「厂商:capability」。它们不是调用路径（gateway 不向 `*.billing` 分发能力），只是给余额 poller 一个可查询的账户。因此**不能断言"seed 全部为能力级 key"**，防漂移校验也不能按 `vendor:capability` 模式硬套全部 key。

`lipsync.video` 能力有两个 provider——`runninghub:lipsync.video`（主路 HeyGem）与 `dashscope:lipsync.video`（备路 VideoReTalk）——**各持独立 key**，是「按厂商账号 / 额度边界 + capability 分组」原则的现成例证：同一能力、不同厂商账号，就该分开限流。

需要长期锁定的原则：

1. 默认按「厂商账号 / 额度边界 + capability」分组。
2. 同一账号、同一能力的多个 `ProviderProfile` 使用**同一个** `concurrency_key`。
3. 多账号场景使用不含密钥的稳定 account alias，例如：
   ```text
   dashscope:account-main:llm.chat
   dashscope:account-backup:llm.chat
   ```
4. 只有厂商文档明确说明多个 capability 共用同一并发 / QPS 池时，才允许跨能力共享 key。
5. 未配置 `concurrency_key` 时回退到 `provider_id` 只是安全兜底（保证有界，不至于无限并发），不应作为生产额度建模方案。
6. 预算、账户余额和计费可能是 account-wide（如火山一个账户余额覆盖 TTS / 方舟 Seedance），但这不意味着调用限流也必须 account-wide——两种边界必须分开建模。

配置覆盖示例（`CUTAGENT_PROVIDER_LIMITS` 是可选的 per-key 覆盖 JSON）：

```bash
CUTAGENT_REDIS_URL=redis://127.0.0.1:6379/0
CUTAGENT_PROVIDER_MAX_INFLIGHT=4
CUTAGENT_PROVIDER_MAX_QPS=4
CUTAGENT_PROVIDER_LIMITS='{
  "dashscope:llm.chat": {"max_inflight": 2, "max_qps": 1},
  "dashscope:asr.transcribe": {"max_inflight": 3, "max_qps": 2},
  "volcengine:video.generate": {"max_inflight": 1, "max_qps": 1}
}'
```

以上数值只是格式示例，**不是**厂商生产额度承诺。具体上限必须来自实际厂商配额。

## 数据所有权边界

| 数据 / 能力 | 权威后端 |
| --- | --- |
| Case、Job、Run、ProviderInvocation、预算、成本、告警 | PostgreSQL |
| 工作流调度、重试、恢复 | Temporal |
| 视频、图片、音频和派生媒体 | OSS / MinIO |
| Prompt / Profile / Secret 元数据 | PostgreSQL / SecretStore |
| 实时跨进程 fanout、短期 stream token、限流 lease/QPS、短期防爆破计数 | Redis（可选） |

Redis 只持有最后一行那类**短期、可重建**的协调状态；Redis 故障不会造成任何业务事实丢失。

## 拓扑与启用条件

| 运行拓扑 | Redis 建议 | 原因 |
| --- | --- | --- |
| 单 API、无跨进程 provider 调用 | 可不启用 | 进程内 fanout / token / 认证限流足够 |
| 单 API + 单 worker，低并发 | 可选 | 若不要求严格共享 provider 额度，可保守拆分 API / worker 配额 |
| 单 API + 单 worker，要求严格共享 provider QPS / 并发 | 建议启用 | API 与 worker 是两个独立进程，无 Redis 时并发叠加、QPS 不执行 |
| 多 API 副本（开发 / 测试环境） | 建议启用；依赖跨副本语义时 required | stream token、fanout、认证限流需要共享 |
| **多副本生产（`CUTAGENT_ENV=production` 且 `CUTAGENT_REPLICA_COUNT > 1`）** | **强制**：production preflight 未配 `CUTAGENT_REDIS_URL` 时 **fail-closed 拒绝启动**（API / worker 均是） | 硬闸门，不是"建议"。见下 |
| 多 worker / 多 provider 调用进程 | 建议启用 | 防止每个进程各自放大厂商并发 / QPS |
| 高可用生产且 Redis 是必要协调依赖 | 设 `CUTAGENT_REDIS_REQUIRED=1` | Redis 退化时 readiness 503 摘流，避免静默丢失跨进程保证 |

**Production preflight 硬闸门。** `packages/core/config/preflight.py` 的 `validate_startup_settings` 只在 `CUTAGENT_ENV=production` 下生效，其中一条：`replica_count > 1` 且没有 `redis_url` 时产出 `redis_required` finding。API / worker 生产启动会把非空 findings 变成 fail-closed 启动失败，`scripts/preflight.py` 也会非零退出。`deploy/production.env.example` 明文标注 "required when `CUTAGENT_REPLICA_COUNT > 1`"。所以多副本生产缺 Redis 不是"降级运行"，而是**根本起不来**。

其他必须说明的运维事实：

- 本地宿主机进程用 `redis://127.0.0.1:6379/0`；Docker 内应用用 `redis://redis:6379/0`（服务名解析）。
- **启动 Redis 容器 ≠ 应用已启用 Redis。** 是否启用只看应用进程有没有 `CUTAGENT_REDIS_URL`。实证：`docker-compose.yml` 的 `worker` 服务本身**不设** `CUTAGENT_REDIS_URL`（只设 storage / database / temporal 相关 env），即便同一 compose 里 `redis` 容器在跑；`scripts/dev_up.sh` 拉起 `redis` 容器（在 `INFRA_SERVICES` 里）但只把 `.env.local` 的变量透传给应用进程，而 `.env.example` 里 `CUTAGENT_REDIS_URL` 默认是注释掉的。容器存在与应用启用是两回事。
- Redis key 多数带 TTL 或调用结束后删除，空闲时 `DBSIZE=0` 可以是正常状态，不代表故障。
- Pub/Sub channel（`cutagent:run:<run_id>`）不是普通 key，`SCAN` / `KEYS` 看不到。
- Redis 不保存 secret，也不保存媒体字节。

## 三种状态与各组件降级行为

Redis 有三种运行态，降级行为**按组件区分**：

**A. 未配置（`CUTAGENT_REDIS_URL` 空）。** 四个组件全部走进程内状态：provider 限流只在单进程内、QPS 不执行；fanout 只在本副本；stream token 只在本副本；认证限流各进程独立计数。单进程 / 单副本部署这是正确且够用的。

**B. 已配置且健康。** 四个组件跨进程共享协调状态，多副本 / API+worker 拿到一致的并发额度、实时广播、token 校验和防爆破计数。

**C. 已配置但 degraded（Redis 连接失败）。** 每个组件**先** fail-safe 回退到自己的进程内 fallback（单个请求不会因 Redis 挂掉而硬失败），**再**按各自的重连 / 上报策略处理：

| 组件 | fallback 行为 | 30s 懒重连（#67） | 上报 `redis_degraded` 遥测 | 计入 readiness 503 |
| --- | --- | --- | --- | --- |
| provider limiter | 进程内 semaphore；QPS 停止执行 | 是 | 是（`provider_limiter`） | 是（required 模式） |
| event fanout | 本副本进程内 fanout | 是 | 是（`event_fanout`） | 是（`event_hub`，required 模式） |
| stream token | 本副本进程内 token 字典 | 是 | 是（`event_token_store`） | 是（`event_tokens`，required 模式） |
| auth rate limit | 各进程内存 bucket | **否**（需 URL 变化 / 重启） | **否**（只 log warning） | **否**（不在判定内） |

> 表中遥测的 `component` 标签名（`event_fanout` / `event_token_store` / `provider_limiter`）与 readiness `redis_degradations` 列表里的组件名（`event_hub` / `event_tokens` / `provider_limiter`）不完全一致——前者是 telemetry 里的命名，后者是 `app.state` 上的属性名，指的是同一批组件。

## `CUTAGENT_REDIS_REQUIRED` 语义

`CUTAGENT_REDIS_REQUIRED=1` **只改变 readiness / 摘流语义，不把 Redis 变成持久化事实源**。它做的是：当 Redis required 时，若纳入 readiness 判定的 Redis 组件退化到进程内 fallback，`/api/health/ready` 返回 **503**，让编排器把这个副本摘流，而不是让它带着"跨副本保证已静默失效"的状态继续服务。

纳入 readiness 判定的组件（`apps/api/services/core.py` 的 `readiness`）**只有三个**：

- `event_hub`（event fanout）
- `event_tokens`（stream token store）
- `provider_limiter`

**认证限流不在其中**（见上文不对称说明）。即便 `CUTAGENT_REDIS_REQUIRED=1`，认证限流退化也不会让 readiness 503。

注意 required 模式下底层组件**仍然**保持进程内 fallback 继续服务单个请求（fail-safe，不 fail-open），503 只是给编排器的摘流信号，不是让请求硬失败。默认 `CUTAGENT_REDIS_REQUIRED` 关闭时就是纯降级在原地（degrade-in-place）。

## 非目标

本文档 / 本设计明确**不做**：PostgreSQL 查询结果缓存、ORM / query cache、Prompt / ProviderProfile / 预算结果缓存、把预算 / 成本 / 熔断状态迁到 Redis、把 Temporal workflow / job 状态迁到 Redis、用 Redis 保存媒体文件或 provider 返回的大对象、强制所有 provider 调用迁入 worker、强制本地开发必须启用 Redis、在没有厂商额度证据时重新划分现有 `concurrency_key`。

## 相关代码与历史 issue

- `packages/ai/gateway/provider_limiter.py` — provider 并发 / QPS 限流
- `packages/core/observability/events.py` — event fanout + stream token store + SQL outbox
- `packages/core/observability/telemetry.py` — `redis_degraded` / `redis_reconnect_attempts_total` 指标
- `packages/core/auth/rate_limit.py` — 登录 / 注册防爆破限流
- `packages/core/config/settings.py` — `redis_url` / `redis_required` / `replica_count`
- `packages/core/config/preflight.py` — 多副本生产 Redis 硬闸门
- `packages/core/storage/provider_seed.py` — 14 个 `concurrency_key` seed
- `apps/api/services/core.py` — `/api/health/ready`（readiness）与 `/api/health/network`（分段诊断）
- CI：`.github/workflows/ci.yml` 的 `redis-coordination` job 跑 `tests/observability/test_redis_coordination.py` + `test_redis_reconnect.py`，且 `assert_no_pytest_skips.py` 禁止静默 skip（#146）
- 历史：#67（reconnect / degradation / readiness）· #70（生产预检 / 多副本 / CI）· #87（Redis required readiness follow-up）· #146（coordination 测试不得静默 skip）
