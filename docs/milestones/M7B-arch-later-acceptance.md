# M7B 架构 later 项验收记录(🟢 项 + 一个 live 发现)

负责:5 个 Opus 子代理(并行 worktree 实现)/ Claude(集成 + 验收)
来源:架构评估报告 `architecture-assessment-2026-06-13.md` 的 🟢 later 项。
合并:`19d2cf3`(pooling)+`15e8a02`(CI)+`9f509ef`(sql-repo)+`c359791`(digital_human)+`47abb15`(contracts)。验收日期:2026-06-13。

## 修了什么(5 项,全部行为保持)

| 项 | 效果 | 风险 |
|---|---|---|
| **DB 连接池 env 化** | pool_size/max_overflow/recycle/timeout 可配,sqlite 路径不变 | 低 |
| **CI ephemeral 修复** | Temporal 集成 job 加 MinIO + ephemeral s3(我 fail-fast 改动的跟进——CI 安全网**本就存在**,报告 agent 漏看了) | 低 |
| **拆 sql-repo god-file** | 1702→1418,抽 18 个 mapper → sqlalchemy_mappers.py(沿用 ops 模式) | 中 |
| **拆 digital_human god-file** | 2448→2188,抽 helper + ffmpeg/render 命令构造 → _helpers.py / render_ops.py | 高 |
| **拆 contracts SSOT** | 2086→330 + 9 个领域子模块,__init__ 全量 re-export | 中高 |

god-file 总量 6236 → 3936(移到新模块)。

## 验收

- **离线**:5 patch disjoint 文件干净 apply;全量套件绿;ruff 清;**openapi 零漂移**(契约拆分保住 API 面);tsc 0。
- **live**:迁 0004 + 重启 worker/API(契约拆分 + 拆解**真实导入无误**);**digital_human 拆解经真 Temporal run 验证行为保持**——同一 demo 在拆解后代码上**端到端成功(15/16 节点,10.62s 音频)**。

## ⚠️ Live 发现的一个 **pre-existing** bug(非本次引入)

验证时一条 run 在 **PortraitTrackBuild** 失败:`render.invalid_timeline: Portrait track duration does not match the plan`。逐项排查证伪了"拆解导致":
- transcode + concat 的 ffmpeg 参数**与拆解前逐字节相同**(git show 对比)。
- 失败/成功**只与 TTS 音频时长相关、与代码无关**:9.74/9.94/10.62s 都过,11.19s(×2)都挂——拆解前/后皆然。
- 即拆解前代码在 11.19s 也会挂(byte-identical 推论)。

**根因(待修,独立 follow-up)**:portrait 轨时长校验容差 `1/fps` 太紧——音频较长(~>11s)时,逐段 transcode(`-t {d:.3f}` ms 量化)+ concat + fps 重采样的累积/量化误差超过一帧,触发严格相等校验。修法应是放宽容差(如几帧)或让 render 保证精确时长。**不是本次 5 项的锅**,本次行为保持。

## 后续
- 修上面 portrait 轨时长校验脆弱性(放宽容差/精确渲染)。
- provider limiter 集群级(Redis 令牌桶)——报告备注项,需 Redis。
