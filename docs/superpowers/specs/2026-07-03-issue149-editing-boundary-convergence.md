# Issue #149 剪辑窗口职责边界收敛方案

> **历史归档 / Superseded（2026-07-15，issue #209）**：本文仍可作为 issue #149
> 的演进记录，但其中 `digital_human_editing_agent_v1` / `EditingAgentPlanning`
> 拓扑已被 Clean-Slate 删除，不得据此恢复 legacy 链。

> 日期：2026-07-03
> 范围：`digital_human_v2` 与 `digital_human_editing_agent_v1` 两条链路的 planning 层职责收敛
> 性质：设计方案（不含实现），基于对当前代码的逐行核查，独立评估了 issue 评论区（GPT5.5pro 讨论产物）的六层方案

---

## 0. 结论摘要（TL;DR）

1. **Issue 的六个问题全部在代码中坐实**，且实际漂移比 issue 描述的更宽：除了短残片规则，Agent 链还绕过了 B-roll 时长上下限（1.5s–4.0s）、人像切镜节奏（beam packing 的合并逻辑）、`strictness` 策略旋钮，并且失败语义错位（素材不足被报成 `prompt_output_invalid`）。
2. **GPT5.5pro 方案的大方向对**（统一几何政策、Agent 不得自带 materializer、最终帧权威唯一），**但有一个结构性错误**：它把「时间线窗口规划」和「窗口素材填入」拆成两个串行节点，而现有 beam packing 是**联合优化**——切点的选择本身依赖候选素材的容量与得分。硬拆会导致要么跨 artifact 重建 escalation 循环（复杂度爆炸），要么 compiler 内部照旧联合规划（分层沦为装饰）。
3. **本方案的核心命题：层 ≠ 节点，防漂移靠共享库和契约语义，不靠更多节点**。把现有 `plan_boundary_timeline`（联合规划器）正名为 **TimelineCompiler**，一次编译产出两个东西：`timeline_windows`（切镜/窗口权威）+ `default_assignment`（默认素材指派）。deterministic 链直接接受默认指派（输出可证明逐字节不变）；Agent 链在同一批窗口内重新指派（免费获得容量兜底与节奏权威）。两链只在「指派引擎」分叉，几何、落帧、校验全共享。
4. **智能演进**：Agent 的时间线自治按「指派 → 意图 → 草案 → 工具环」四级演进，每一级的输出都只是 compiler 的**约束输入**，最终帧权威永远留在 compiler + materializer。
5. **落地分四阶段**，Phase 0 是一个不动架构、只修真实视频瑕疵的 PR。

（完整验证细节、职责矩阵、契约草案、测试清单见同日 plans/ 下的实现计划；本文件为设计事实源。）

---

## 1. 现状核查摘要

### 1.1 Issue 六问全部坐实

1. MaterialPack 只有 BGM top-k=8，portrait/broll/font 全量进 Agent prompt（`material_pack_planning.py:41`；`_editing_agent.py:169-363`）；预留只锁 top-3（`_RESERVE_TOP_N=3`）。
2. `broll_slots` 是 available windows 非最终计划（`narration_boundary_planning.py:157-161`），传统 BrollPlanning 不消费它。
3. 传统 PortraitPlanning 经 `plan_boundary_timeline()` + escalation ladder 重规划最终主轨。
4. 残片/吸附/起点搜索藏在 `plan_insertions()` 私有路径（`broll_plan.py:539-715`）。
5. Agent `materialize_broll()` 只调 `align_insertions_to_portrait_cuts()`，无残片拒绝、无起点搜索（`_editing_agent.py:688-776`）。
6. Agent portrait 直接用 base `portrait_slots` 落轨（`_editing_agent.py:605-674`）。

### 1.2 新发现的漂移（issue 未覆盖）

- **a) 切镜节奏权威漂移（最根本）**：base `portrait_slots` 是每对相邻安全切点一个 slot（最大切镜密度）；传统链 beam packing 会按节奏/容量合并 chunk。Agent 链直接全切，两链镜头数系统性不同。
- **b) B-roll 时长政策漂移**：传统 1.5s–4.0s；Agent span=整个 narration unit（可 10s+），下限仅 1 帧。
- **c) 失败语义错位**：slot 无 legal 候选时 Agent 烧光 repair 报 `prompt_output_invalid`，真因是素材不足（应为 `material_insufficient_portrait`）。
- **d) 人像唯一性政策分叉**：传统 max_uses=1 硬闸（#102）vs Agent ceil(S/A) 放松（#147），语义存放两处实现、无共同 policy 来源。
- **e) 选择策略输入不对等**：传统 broll 有 keyword 重排+recency demote+freshness jitter；Agent 只拿 MaterialPack 静态分（此项属"指派引擎允许分叉"范围，但 recency 政策归属须唯一）。

---

## 2. 对 GPT5.5pro 六层方案的裁决

**采纳**：统一几何政策为公共契约；Agent 不得自带 materializer；「设计」与「编译/落帧」分离；ledger 只记 final shipped plan；prompt budget 不写死在 prompt builder；稳定候选 ID。

**修正**：
1. 窗口规划与素材填入对人像主轨是**联合优化**（beam packing 的切点可行性由候选容量决定），不可硬拆两节点——封装为编译器双产物。
2. SlotCandidateIndex 不应是节点/artifact（其"陷阱4"自证必须随 windows 动态重算），降为纯函数 shortlist。
3. 不一次引入 6 artifact + 拆 5 节点（中间态=三套权威并存）；层≠节点。
4. PostProcessAssetPlanning 现在不拆（守卫测试即可）。
5. 自治 L1（变体选择）并入 L2（intent）——变体=同一 compiler 跑 N 份 intent。

---

## 3. 目标架构

### 三条公理

1. 一个 artifact 只声明一种权威。
2. 几何与政策是公共库（`packages/planning`），不是任何节点私产。
3. 引擎只在「选择」分叉，「编译」和「落帧」永远共享。

### 权威域

```text
事实域   plan.narration_boundary（切点/停顿事实） · plan.material_pack（候选池+recency）
编译域   TimelineCompiler（=plan_boundary_timeline 正名+escalation ladder）
         → plan.timeline_windows { portrait_windows, broll_windows,
                                    geometry_policy 快照, default_assignment }
选择域   deterministic 引擎（采纳 default_assignment）| LLM 引擎（同窗口内重指派）
         → plan.media_assignment
落帧域   唯一 materializer → plan.portrait / plan.broll（renderer 帧权威）
校验域   TimelinePlanning verify-only（不变）
后处理域 style 只读 windows，禁止写
```

### 关键政策对象

```python
BrollGeometryPolicy: fps=30, min_insert=1.5s, max_insert=4.0s,
                     min_visible_aroll=2.0s, snap_max_frames=15, max_pad=0.15s
PortraitReusePolicy: strict(max_uses=1, #102)，由 TimelineWindowPlanning 统一执行；
                     Agent 不再放宽复用
```

### 智能演进（A0–A3）

A0 指派（Phase 2 即达）→ A1 TimelineIntent（compiler 约束入参）→ A2 TimelineSketch DSL（合法化+accepted/rewritten/rejected 诊断）→ A3 工具环（simulate/compile 工具化）。永久不变量：Agent 输出永远是 intent/sketch/assignment，绝不是帧。

现在只做三条预留：compiler 显式 constraints 纯函数；requested-vs-compiled 诊断从 Phase 1 记录；shortlist 随 windows 动态重算。

---

## 4. 阶段划分

- **Phase 0**：共享几何安全放置进 Agent 路径；人像可行性预检+错误语义对齐；政策对象化；语义注释收紧。零架构变化。
- **Phase 1**：TimelineWindowPlanning 节点（两模板）产出 `plan.timeline_windows`；v2 PortraitPlanning 瘦身为采纳 default_assignment（golden 证明输出不变）；Agent slots 换 compiled windows；shortlist 预算上线。
- **Phase 2**：`plan.media_assignment` 契约；Agent 删自带 materializer；统一落帧库两链共用；同 assignment 双链同帧断言。
- **Phase 3**（不在本次范围）：Intent → Sketch → 工具环。

详细 touch surface、契约字段、测试清单见 plans/2026-07-03-issue149-phase0-2-implementation.md。
