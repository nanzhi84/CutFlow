# M6h 施工简报：剪映草稿真包

负责：Codex（执行）/ Claude（结构验收）/ 用户（剪映桌面端最终确认）
分支：`feat/m6h-jianying-draft`
背景：当前 jianying-draft / editor-handoff 产出 sandbox://*.zip 空壳。本批照原版生成真实剪映草稿包。

## 验收特殊性（必须知晓）
剪映草稿是剪映桌面软件的私有工程格式。**验收官（Linux server）只能验结构**：draft JSON schema、
轨道数、字幕轨条数、媒体 staging、zip 可解压；**"剪映软件真能打开并正确显示"需用户在剪映桌面端确认**。
因此简报要求严格照搬原版已上线验证的 JSON 结构，并写强结构断言测试。

## 原版参考（只读 `/home/nanzhi/projects/digital-human-Cutagent/backend/app/services/jianying_draft_service.py`，1632 行）
- `detect_draft_root` 草稿根探测、`_safe_draft_name`、`_now_us` 微秒时间基
- 轨道构建：视频主轨 + 音频轨 + 字幕轨（`_parse_srt_entries` SRT→字幕轨）+ 贴纸/overlay 分道 + 花字
- `_stage_media_file` 媒体 staging（拷贝/链接到草稿目录）
- draft_content.json / draft_meta_info.json 等剪映 JSON 结构
- 严格照搬 JSON 字段/结构/时间基，不要自创格式。

## 改动清单
- A `packages/production`（或 packages/media/rendering）实现真 JianyingDraftBuilder：从 FinishedVideo +
  timeline plan + subtitle.ass/SRT + 素材，构建剪映草稿目录结构 + draft JSON，打包 zip 落 ObjectStore。
- B `JianyingDraftPackageArtifact` 填真字段（draft_uri 真 zip、draft_name、tracks_summary 真轨道统计）；
  editor-handoff 同样真包（manifest + assets 真清单）。
- C 端点 /api/finished-videos/{id}/jianying-draft 返回真包；前端 EditorHandoffActions 展示真轨道摘要 + 下载真 zip。
- D 结构断言测试：zip 可解压、draft_content.json 含预期轨道（视频/音频/字幕）、字幕条数=narration units 数、
  时间基微秒一致；用 M6b 合成素材 + 一条 sandbox run 的产物构造。

## 边界
- 不要求剪映软件端到端验证（用户做）；真 portrait+HeyGem 完整片等用户素材；M6c 冻结。

## Verification（sandbox 内）
- 全量 pytest 绿（基线含 M6g）；剪映包结构断言测试绿；tsc/build 绿；OpenAPI 同步。

## 验收门
1. 验收官：jianying-draft 端点产真 zip，解压后 draft JSON 结构/轨道/字幕条数断言通过；下载真包。
2. 用户（剪映桌面端）：下载草稿包导入剪映，确认视频/音频/字幕轨正确显示、可编辑。
3. 全量 + DB + Temporal 三套绿。

---

## 验收记录（2026-06-12，验收官：Claude）

**判定：结构通过并合入**（merge 见 git log），**剪映桌面端打开待用户确认**。证据：全量 + tsc/build 绿；结构断言测试覆盖 zip 解压、draft_content.json 视频/音频/字幕轨、字幕条数=narration units、微秒时间基；draft JSON 字段照 pyJianYingDraft/原版形状（canvas_config/materials/tracks/segments + draft_meta_info/root_meta_info）。jianying-draft 端点产真 zip（local:// 或 s3://，含 sha256/manifest/assets/tracks_summary），前端展示真轨道摘要+下载真包。

验收边界（如实）：Linux server 无法验"剪映软件真能打开"，需用户在剪映/CapCut 桌面端导入草稿目录确认轨道显示可编辑。

**主线 milestone 至此基本清空**（M1-M6h）。剩余：① 完整真口播片端到端（等用户真 portrait 素材 + HeyGem live）；② parity 零散前端项（Broll 插入点预览、素材使用排行榜、任务详情 Agent 透视深化）；③ M6c 发布（用户冻结）。
