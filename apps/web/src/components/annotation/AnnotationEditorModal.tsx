import {
  AlertTriangle,
  CheckCircle2,
  Edit3,
  Eye,
  FileVideo,
  Film,
  Fingerprint,
  Loader2,
  Plus,
  RefreshCw,
  Scissors,
  ShieldAlert,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isApiError, type AnnotationEditorVm, type MediaAssetRecord } from "../../api/client";
import { formatDuration, shortId } from "../../lib/format";
import { toDisplayUrl } from "../../lib/url";
import { readAssetThumbnailUrl, readPreviewUrlMeta } from "../../components/library/libraryModel";
import {
  BGM_ENERGY_PROFILES,
  BGM_ROLES,
  BGM_SECTION_TYPES,
  bgmSegmentsToCanonical,
  canonicalToEvidenceFrames,
  canonicalToBgmSegments,
  canonicalToQualityEvents,
  canonicalToSegments,
  parseQualityEvents,
  readDuration,
  readMeta,
  segmentsToClips,
  type AnnotationEvidenceFrame,
  type AnnotationQualityEvent,
  type AnnotationSegmentQuality,
  type AnnotationTimelineSegment,
  type BgmSegment,
} from "../../utils/annotationV4";
import { Modal } from "../ui/Modal";
import { VideoPlayer, type VideoPlayerQualityEvent, type VideoPlayerSeekRequest, type VideoPlayerSegment } from "../ui/VideoPlayer";
import { useToast } from "../ui/Toast";
import { ErrorState, LoadingState } from "../ui/State";

type AnnotationEditorModalProps = {
  assetId: string | null;
  caseId: string | null;
  onClose: () => void;
};

type AnnotationForm = {
  qualityStatus: string;
  usable: boolean;
  segments: AnnotationTimelineSegment[];
  qualityEvents: AnnotationQualityEvent[];
};

type ClipEmbeddingStatusVm = Awaited<ReturnType<typeof api.mediaAssets.clipEmbeddingStatus>>;
type ClipEmbeddingJobVm = Awaited<ReturnType<typeof api.mediaAssets.clipEmbeddingJobStatus>>;

function isClipEmbeddingJobActive(status: string | undefined): boolean {
  return status === "queued" || status === "running";
}

function embeddingPercent(done: number, total: number) {
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}

const QUALITY_STATUS_LABELS: Record<string, string> = {
  usable: "可用",
  review: "需复核",
  invalid: "不可用",
};

const SPEECH_ALIGNMENT_LABELS: Record<string, string> = { aligned: "动作一致", uncertain: "待确认", mismatch: "不一致" };
const SHOT_SCALE_LABELS: Record<string, string> = {
  extreme_close_up: "大特写",
  close_up: "特写",
  medium: "中景",
  wide: "全景",
  unknown: "未标注",
};
const CAMERA_MOTION_LABELS: Record<string, string> = {
  static: "固定机位",
  stable: "稳定机位",
  handheld: "手持拍摄",
  follow: "跟拍镜头",
  track: "跟随移动",
  push_in: "推进镜头",
  pull_back: "拉远镜头",
  pan: "平移镜头",
  tilt: "俯仰镜头",
  compound: "复合运镜",
  shake: "明显抖动",
  unknown: "未标注",
};
const NARRATIVE_ROLE_LABELS: Record<string, string> = {
  process_proof: "过程证明",
  detail_showcase: "细节展示",
  result_showcase: "结果展示",
  environment_establish: "环境建立",
  transition: "转场衔接",
};
const PROCESS_STAGE_LABELS: Record<string, string> = {
  preparation: "施工前准备",
  process: "施工中",
  inspection: "检查确认",
  result: "结果展示",
  cleanup: "收尾清洁",
};
const ROLE_LABELS: Record<string, string> = {
  hook: "黄金3秒",
  main: "主轨",
  backup: "备选",
  cover: "覆盖镜头",
  avoid: "避用",
};
const BGM_ROLE_LABELS: Record<string, string> = { hook: "开场钩子", climax: "高潮", outro: "收尾", general: "通用铺底" };
const BGM_SECTION_LABELS: Record<string, string> = {
  intro: "前奏",
  stable_bed: "稳定铺底",
  verse: "主歌",
  chorus: "副歌",
  drop: "高潮/Drop",
  bridge: "桥段",
  outro: "尾奏",
  loop: "循环段",
  build: "推进段",
  general: "通用段",
};
const BGM_ENERGY_LABELS: Record<string, string> = {
  stable: "稳定",
  rising: "上升",
  falling: "回落",
  drop: "落点",
  peak: "峰值",
};
const BGM_ROLE_COLORS: Record<BgmSegment["role"], { bar: string; softBg: string; text: string; border: string }> = {
  hook: { bar: "#f97316", softBg: "#fff7ed", text: "#c2410c", border: "#fed7aa" },
  climax: { bar: "#ef4444", softBg: "#fef2f2", text: "#b91c1c", border: "#fecaca" },
  outro: { bar: "#22c55e", softBg: "#f0fdf4", text: "#15803d", border: "#bbf7d0" },
  general: { bar: "#3b82f6", softBg: "#eff6ff", text: "#1d4ed8", border: "#bfdbfe" },
};
const RISK_TIER_LABELS: Record<string, string> = { hard: "硬风险", soft: "软风险" };
const EVENT_TYPE_LABELS: Record<string, string> = {
  camera_drop: "收机下坠",
  shake: "镜头抖动",
  blur: "失焦模糊",
  occlusion: "遮挡/黑屏",
  blooper_laugh: "笑场/NG",
  look_off_camera: "视线离镜",
  exit_frame: "出画",
  retake_pause: "重拍停顿",
  manual_note: "人工备注",
};

function translateToken(value: string | undefined | null, labels: Record<string, string>, fallback = "未标注"): string {
  const token = String(value ?? "").trim().toLowerCase();
  if (!token) return fallback;
  return labels[token] ?? value!;
}

function formatPercent(value?: number): string {
  if (value === undefined || value === null || !Number.isFinite(value)) return "未标注";
  return `${Math.round(value * 100)}%`;
}

function formatWindow(start: number, end: number): string {
  return `${start.toFixed(1)}s – ${end.toFixed(1)}s`;
}

// Voice/voice-reference assets get the 口播-style annotation editor. Visual assets
// are one unified ``video`` bucket now (#133) whose A-roll/B-roll role is a per-clip
// decision, so they already fall through to the B-roll-style editor (as ``video``
// always has); the legacy ``portrait`` asset kind no longer exists.
function isPortraitKind(kind?: MediaAssetRecord["kind"]): boolean {
  return kind === "voice_reference" || kind === "voice";
}

export function AnnotationEditorModal({ assetId, caseId, onClose }: AnnotationEditorModalProps) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [rerunPreview, setRerunPreview] = useState(false);
  const [activeSegmentId, setActiveSegmentId] = useState<string | null>(null);
  const [seekRequest, setSeekRequest] = useState<VideoPlayerSeekRequest | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewPlayable, setPreviewPlayable] = useState<boolean | undefined>(undefined);
  const [form, setForm] = useState<AnnotationForm>({ qualityStatus: "usable", usable: true, segments: [], qualityEvents: [] });
  const [bgmForm, setBgmForm] = useState<BgmSegment[]>([]);
  const [embeddingJobId, setEmbeddingJobId] = useState<string | null>(null);

  const editorQuery = useQuery({
    queryKey: ["library", "annotation", assetId],
    queryFn: () => api.annotations.get(assetId!),
    enabled: Boolean(assetId),
  });

  const editor = editorQuery.data ?? null;
  const isPortrait = isPortraitKind(editor?.asset.kind);
  const isBgm = editor?.asset.kind === "bgm";

  const embeddingStatusQuery = useQuery({
    queryKey: ["library", "annotation", "clip-embedding-status", caseId, assetId],
    queryFn: () => api.mediaAssets.clipEmbeddingStatus({ case_id: caseId!, asset_id: assetId!, namespace: "all" }),
    enabled: Boolean(caseId && assetId && editor?.asset.kind === "video"),
    refetchInterval: embeddingJobId ? 1_500 : 5_000,
  });

  const embeddingJobQuery = useQuery({
    queryKey: ["library", "annotation", "clip-embedding-job", embeddingJobId],
    queryFn: () => api.mediaAssets.clipEmbeddingJobStatus(embeddingJobId!),
    enabled: Boolean(embeddingJobId),
    refetchInterval: embeddingJobId ? 1_500 : false,
  });

  useEffect(() => {
    if (!assetId) {
      setPreviewUrl(null);
      setPreviewPlayable(undefined);
      return;
    }
    let cancelled = false;
    setPreviewLoading(true);
    setPreviewUrl(null);
    setPreviewPlayable(undefined);
    api.mediaAssets
      .previewUrl(assetId)
      .then((response) => {
        if (cancelled) return;
        setPreviewUrl(toDisplayUrl(response.url));
        setPreviewPlayable(readPreviewUrlMeta(response).playable);
      })
      .catch(() => {
        if (!cancelled) setPreviewUrl(null);
      })
      .finally(() => {
        if (!cancelled) setPreviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [assetId]);

  const canonical = editor?.canonical;
  const projection = editor?.projection ?? {};
  const readSegments = useMemo<AnnotationTimelineSegment[]>(
    () => (canonical ? canonicalToSegments(canonical) : []),
    [canonical],
  );
  const readEvents = useMemo<AnnotationQualityEvent[]>(
    () => (canonical ? canonicalToQualityEvents(canonical) : []),
    [canonical],
  );
  const totalDuration = useMemo(() => (canonical ? readDuration(canonical) : 0), [canonical]);
  const bgmSegments = useMemo(() => canonical ? canonicalToBgmSegments(canonical) : [], [canonical]);
  const bgmReport = (projection.bgm ?? {}) as Record<string, unknown>;

  useEffect(() => {
    if (!editor) return;
    const proj = editor.projection ?? {};
    const projEvents = parseQualityEvents(proj.quality_events);
    setForm({
      qualityStatus: readJsonString(proj, "quality_status") || (editor.asset.usable ? "usable" : "review"),
      usable: typeof proj.usable === "boolean" ? proj.usable : editor.asset.usable,
      segments: canonicalToSegments(editor.canonical),
      qualityEvents: projEvents.length > 0 ? projEvents : canonicalToQualityEvents(editor.canonical),
    });
    setBgmForm(canonicalToBgmSegments(editor.canonical));
    setEditing(false);
    setRerunPreview(false);
    setActiveSegmentId(null);
    setSeekRequest(null);
  }, [editor]);

  const displaySegments = editing ? form.segments : readSegments;
  const displayEvents = editing ? form.qualityEvents : readEvents;
  const displayBgmSegments = editing ? bgmForm : bgmSegments;

  const seekToBgmSegment = useCallback((segment: BgmSegment) => {
    setActiveSegmentId(segment.segment_id || null);
    setSeekRequest((current) => ({
      time: Math.max(0, segment.start),
      key: (current?.key ?? 0) + 1,
    }));
  }, []);

  const seekToVisualSegment = useCallback((segment: AnnotationTimelineSegment) => {
    setActiveSegmentId(segment.segment_id || null);
    setSeekRequest((current) => ({
      time: Math.max(0, segment.start),
      key: (current?.key ?? 0) + 1,
    }));
  }, []);

  const invalidDuration = useMemo(
    () => readInvalidSegments(projection).reduce((sum, item) => sum + Math.max(0, item.end_sec - item.start_sec), 0),
    [projection],
  );
  const validDuration = useMemo(() => {
    const explicit = readJsonNumber(projection, "valid_duration_sec") ?? readJsonNumber(projection, "usable_duration_sec");
    if (explicit !== undefined) return explicit;
    if (totalDuration > 0) return Math.max(0, totalDuration - invalidDuration);
    return undefined;
  }, [projection, totalDuration, invalidDuration]);

  const playerSegments = useMemo<VideoPlayerSegment[]>(
    () => (isBgm ? displayBgmSegments.map((segment, index) => toBgmPlayerSegment(segment, index)) : displaySegments.map((segment, index) => toPlayerSegment(segment, index))),
    [displayBgmSegments, displaySegments, isBgm],
  );
  const playerEvents = useMemo<VideoPlayerQualityEvent[]>(
    () => displayEvents.map((event, index) => toPlayerEvent(event, index)),
    [displayEvents],
  );
  const evidenceFrames = useMemo<AnnotationEvidenceFrame[]>(
    () => (canonical ? canonicalToEvidenceFrames(canonical) : []),
    [canonical],
  );

  // Annotation version badge ("annotation_v4" -> "标注 v4"); thumbnail poster; playability gate.
  const annotationVersionLabel = useMemo(() => formatAnnotationVersion(readMeta(canonical).annotation_version), [canonical]);
  const thumbnailUrl = editor ? readAssetThumbnailUrl(editor.asset) : null;
  const canPlay = Boolean(previewUrl) && previewPlayable !== false;
  const bgmCoverage = useMemo(() => displayBgmSegments.reduce((sum, segment) => sum + Math.max(0, segment.end - segment.start), 0), [displayBgmSegments]);
  const bgmCoverageRatio = totalDuration > 0 ? Math.min(1, bgmCoverage / totalDuration) : 0;

  const patchMutation = useMutation({
    mutationFn: async () => {
      if (!assetId || !editor) throw new Error("标注未加载");
      const clips = segmentsToClips(form.segments, isPortrait);
      const events = form.qualityEvents.map((event, index) => ({
        event_id: event.event_id || `manual_event_${index + 1}`,
        event_type: event.event_type || "manual_note",
        start: event.start,
        end: event.end,
        description: event.description ?? "",
        risk_tier: event.risk_tier ?? "soft",
        confidence: event.confidence,
      }));
      return api.annotations.patch(assetId, {
        etag: editor.etag,
        patch: {
          operations: [
            { op: "replace", path: "/projection/quality_status", value: form.qualityStatus },
            { op: "replace", path: "/projection/usable", value: form.usable },
            { op: "replace", path: "/projection/quality_events", value: events },
            { op: "replace", path: "/canonical/clips", value: clips },
            { op: "replace", path: "/canonical/quality_events", value: events },
          ],
        },
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["library", "annotation", assetId] });
      await queryClient.invalidateQueries({ queryKey: ["library", "media", caseId] });
      toast.success("标注已保存", "结构化标注已更新。");
      setEditing(false);
    },
    onError: (error) => {
      if (isApiError(error) && (error.status === 409 || error.code === "artifact.schema_mismatch")) {
        toast.error("标注版本冲突", "服务器标注已更新，请刷新后重新编辑。");
        return;
      }
      toast.error("标注保存失败", error);
    },
  });

  const rerunMutation = useMutation({
    mutationFn: () => {
      if (!assetId) throw new Error("标注未加载");
      return api.annotations.rerun(assetId, { force: true });
    },
    onSuccess: async (response) => {
      await queryClient.invalidateQueries({ queryKey: ["library", "annotation", assetId] });
      await queryClient.invalidateQueries({ queryKey: ["library", "media", caseId] });
      toast.success("重新分析已提交", response.run_id ? `运行 ID：${shortId(response.run_id)}` : "已返回完成状态");
      setRerunPreview(false);
    },
    onError: (error) => toast.error("重新分析失败", error),
  });

  const trimMutation = useMutation({
    mutationFn: () => {
      if (!assetId) throw new Error("标注未加载");
      return api.annotations.trim(assetId, {});
    },
    onSuccess: async (response) => {
      await queryClient.invalidateQueries({ queryKey: ["library", "annotation", assetId] });
      await queryClient.invalidateQueries({ queryKey: ["library", "media", caseId] });
      toast.success("裁剪完成", `有效时长 ${formatDuration(response.valid_duration_sec)}`);
    },
    onError: (error) => toast.error("裁剪失败", error),
  });

  const embeddingJobActive = Boolean(
    isClipEmbeddingJobActive(embeddingJobQuery.data?.status) || (embeddingJobId && !embeddingJobQuery.data),
  );

  const embeddingMutation = useMutation({
    mutationFn: () => {
      if (!caseId || !assetId) throw new Error("素材未加载");
      const pendingCount = embeddingStatusQuery.data?.pending_count ?? 1;
      return api.mediaAssets.indexClipEmbeddings({
        schema_version: "clip_embedding_index_request.v1",
        case_id: caseId,
        asset_ids: [assetId],
        namespace: "all",
        provider_profile_id: "dashscope.multimodal_embedding.prod",
        limit: Math.max(1, pendingCount),
        force: false,
      });
    },
    onSuccess: async (response) => {
      setEmbeddingJobId(response.job_id);
      await queryClient.invalidateQueries({ queryKey: ["library", "annotation", "clip-embedding-status", caseId, assetId] });
      toast.success("单素材嵌入已开始", `任务 ${shortId(response.job_id)} · 排队 ${response.queued_count} 条`);
    },
    onError: (error) => toast.error("单素材嵌入失败", error),
  });

  useEffect(() => {
    const job = embeddingJobQuery.data;
    if (!job || !embeddingJobId || !["succeeded", "failed"].includes(job.status)) return;
    void queryClient.invalidateQueries({ queryKey: ["library", "annotation", "clip-embedding-status", caseId, assetId] });
    void queryClient.invalidateQueries({ queryKey: ["library", "clip-embeddings", caseId] });
    if (job.status === "failed") {
      toast.warning("单素材嵌入结束，有失败项", `新增 ${job.indexed_now_count} 条，失败 ${job.failed_count} 条`);
    } else {
      toast.success("单素材嵌入完成", `新增 ${job.indexed_now_count} 条，剩余 ${job.remaining_count} 条`);
    }
    setEmbeddingJobId(null);
  }, [assetId, caseId, embeddingJobId, embeddingJobQuery.data, queryClient, toast]);

  return (
    <Modal isOpen={Boolean(assetId)} onClose={onClose} title="标注编辑器" size="3xl">
      {editorQuery.isLoading ? (
        <div className="grid min-h-[360px] place-items-center">
          <LoadingState label="加载标注" />
        </div>
      ) : null}
      {editorQuery.error ? <ErrorState error={editorQuery.error} /> : null}

      {editor ? (
        <div className="grid gap-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-lg font-semibold text-text-primary">{editor.asset.title}</h3>
                <span className="badge bg-surface-hover text-text-secondary">{isBgm ? "BGM" : isPortrait ? "口播 / 数字人" : "B-roll"}</span>
                {annotationVersionLabel ? <span className="badge bg-accent/12 text-accent">{annotationVersionLabel}</span> : null}
              </div>
              <p className="mt-1 font-mono text-xs text-text-tertiary">
                {shortId(editor.asset.id, 14)} · 版本标识 {shortId(editor.etag, 14)}
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button className="btn-secondary" type="button" onClick={() => setRerunPreview(true)} disabled={rerunMutation.isPending}>
                {rerunMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                <span>{rerunMutation.isPending ? "分析中" : "重新分析"}</span>
              </button>
              {!isBgm ? (
                <button className="btn-secondary" type="button" onClick={() => trimMutation.mutate()} disabled={trimMutation.isPending}>
                  {trimMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Scissors className="h-4 w-4" />}
                  <span>{trimMutation.isPending ? "裁剪中" : "裁剪无效"}</span>
                </button>
              ) : null}
              <button className="btn-primary" type="button" onClick={() => setEditing((value) => !value)}>
                {editing ? <Eye className="h-4 w-4" /> : <Edit3 className="h-4 w-4" />}
                <span>{editing ? "查看只读" : "手动编辑"}</span>
              </button>
            </div>
          </div>

          {rerunPreview ? (
            <div className="rounded-2xl border border-status-warning/25 bg-status-warning/10 p-4">
              <h4 className="text-sm font-semibold text-status-warning">重新分析预览</h4>
              <p className="mt-2 text-sm text-status-warning">
                将基于当前素材重新生成标注结果。确认覆盖后，现有人工编辑可能被新结果替换；放弃会保留当前版本标识与编辑内容。
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <button className="btn-primary min-h-9 px-3" type="button" onClick={() => rerunMutation.mutate()} disabled={rerunMutation.isPending}>
                  {rerunMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                  <span>{rerunMutation.isPending ? "覆盖中" : "确认覆盖"}</span>
                </button>
                <button className="btn-secondary min-h-9 px-3" type="button" onClick={() => setRerunPreview(false)} disabled={rerunMutation.isPending}>
                  放弃
                </button>
              </div>
            </div>
          ) : null}

          <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)]">
            <div className="grid content-start gap-4">
              <div className="relative">
                {canPlay && previewUrl ? (
                  <VideoPlayer
                    src={previewUrl}
                    poster={thumbnailUrl ?? undefined}
                    className="aspect-video w-full"
                    segments={playerSegments}
                    qualityEvents={playerEvents}
                    evidenceFrames={evidenceFrames}
                    durationHint={totalDuration > 0 ? totalDuration : undefined}
                    activeSegmentId={activeSegmentId}
                    seekRequest={seekRequest}
                    segmentBarsInteractive={false}
                    onSegmentClick={(segment) => segment.id && setActiveSegmentId(segment.id)}
                    onTimeUpdate={(time) => {
                      const active = isBgm ? displayBgmSegments : displaySegments;
                      const hit = active.find((segment) => time >= segment.start && time <= segment.end);
                      setActiveSegmentId(hit?.segment_id ?? null);
                    }}
                  />
                ) : (
                  <div className="grid aspect-video w-full place-items-center overflow-hidden rounded-2xl border border-dashed border-border bg-[#151913] text-sm text-white/70">
                    {!previewLoading && thumbnailUrl ? (
                      <img src={thumbnailUrl} alt={editor.asset.title} className="aspect-video w-full object-cover opacity-80" />
                    ) : (
                      <div className="flex flex-col items-center gap-2">
                        {previewLoading ? <Loader2 className="h-7 w-7 animate-spin opacity-80" /> : <FileVideo className="h-8 w-8 opacity-70" />}
                        <span>{previewLoading ? "加载视频预览…" : "素材预览暂不可用（待真实媒体接入）"}</span>
                      </div>
                    )}
                  </div>
                )}
              </div>

              {isBgm ? (
                <div className="grid gap-3 sm:grid-cols-3">
                  <AnnotationMetric label="标注覆盖" value={formatPercent(bgmCoverageRatio)} tone={bgmCoverageRatio >= 0.95 ? "ok" : "warn"} />
                  <AnnotationMetric label="音乐段落" value={`${displayBgmSegments.length} 段`} />
                  <AnnotationMetric label="总时长" value={formatDuration(totalDuration > 0 ? totalDuration : undefined)} />
                </div>
              ) : (
                <div className="grid gap-3 sm:grid-cols-3">
                  <AnnotationMetric label="有效时长" value={formatDuration(validDuration)} tone="ok" />
                  <AnnotationMetric label="无效时长" value={formatDuration(invalidDuration)} tone="warn" />
                  <AnnotationMetric label="总时长" value={formatDuration(totalDuration > 0 ? totalDuration : undefined)} />
                </div>
              )}

              <p className="text-xs text-text-tertiary">
                {playerSegments.length > 0
                  ? isBgm
                    ? `进度条叠加 ${playerSegments.length} 个音乐段落，点击可跳转并高亮。`
                    : `时间轴叠加 ${playerSegments.length} 个片段${playerEvents.length > 0 ? ` · ${playerEvents.length} 个质量事件` : ""}${evidenceFrames.length > 0 ? ` · ${evidenceFrames.length} 个证据帧` : ""}，点击可跳转并高亮。`
                  : isBgm ? "该 BGM 暂无音乐段落。" : "该标注暂无可视化片段。"}
              </p>

              {!isBgm ? (
                <EmbeddingStatusPanel
                  status={embeddingStatusQuery.data}
                  loading={embeddingStatusQuery.isFetching || embeddingJobQuery.isFetching}
                  error={embeddingStatusQuery.error || embeddingJobQuery.error}
                  active={embeddingJobActive || embeddingMutation.isPending}
                  onEmbed={() => embeddingMutation.mutate()}
                />
              ) : null}
            </div>

            <div className="max-h-[72vh] overflow-y-auto pr-1">
              {isBgm ? (
                editing ? (
                  <BgmAnnotationForm
                    assetId={assetId ?? editor.asset.id}
                    caseId={caseId}
                    editor={editor}
                    segments={bgmForm}
                    setSegments={setBgmForm}
                    duration={totalDuration}
                    onCancel={() => {
                      setBgmForm(bgmSegments);
                      setEditing(false);
                    }}
                    onSaved={() => setEditing(false)}
                  />
                ) : (
                  <BgmStructurePanel
                    bgmReport={bgmReport}
                    segments={displayBgmSegments}
                    totalDuration={totalDuration > 0 ? totalDuration : undefined}
                    activeSegmentId={activeSegmentId}
                    onSelectSegment={seekToBgmSegment}
                  />
                )
              ) : editing ? (
                <StructuredAnnotationForm
                  form={form}
                  setForm={setForm}
                  isPortrait={isPortrait}
                  duration={totalDuration}
                  pending={patchMutation.isPending}
                  onSubmit={() => patchMutation.mutate()}
                  onCancel={() => setEditing(false)}
                />
              ) : (
                <ReadonlyStructurePanel
                  segments={readSegments}
                  events={readEvents}
                  isPortrait={isPortrait}
                  activeSegmentId={activeSegmentId}
                  onSelectSegment={seekToVisualSegment}
                />
              )}
            </div>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}

function EmbeddingStatusPanel({
  status,
  loading,
  error,
  active,
  onEmbed,
}: {
  status?: ClipEmbeddingStatusVm;
  loading: boolean;
  error: unknown;
  active: boolean;
  onEmbed: () => void;
}) {
  const candidateCount = status?.candidate_count ?? 0;
  const indexedCount = status?.indexed_count ?? 0;
  const pendingCount = status?.pending_count ?? 0;
  const percent = embeddingPercent(indexedCount, candidateCount);
  const hasError = Boolean(error);
  const isComplete = candidateCount > 0 && pendingCount === 0 && !hasError;
  const isEmpty = candidateCount === 0 && !loading && !hasError;
  const canEmbed = !active && !hasError && pendingCount > 0;
  const tone = hasError ? "danger" : isComplete ? "good" : isEmpty ? "neutral" : "warn";
  const title = hasError
    ? "嵌入状态读取失败"
    : loading && !status
      ? "读取嵌入状态"
      : isComplete
        ? "视频嵌入已齐"
        : isEmpty
          ? "暂无可嵌入片段"
          : "视频嵌入待补";
  const detail = status
    ? `${indexedCount}/${candidateCount} 条 clip 已入库${pendingCount > 0 ? ` · 待补 ${pendingCount}` : ""}`
    : hasError
      ? "稍后会自动重试。"
      : "正在计算当前素材的 clip 索引状态。";
  const toneClass =
    tone === "good"
      ? "border-status-success/25 bg-status-success/10 text-status-success"
      : tone === "danger"
        ? "border-status-error/25 bg-status-error/10 text-status-error"
        : tone === "warn"
          ? "border-status-warning/25 bg-status-warning/10 text-status-warning"
          : "border-border/80 bg-white/65 text-text-secondary";

  return (
    <section className="grid gap-3 rounded-2xl border border-border/80 bg-white/65 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Fingerprint className="h-4 w-4 shrink-0 text-accent" />
          <div className="min-w-0">
            <h4 className="text-sm font-semibold text-text-primary">Clip 视频嵌入</h4>
            <p className="mt-0.5 truncate text-xs text-text-tertiary">
              {status ? `${status.embedding_model} · ${status.embedding_dimension} 维` : "qwen3-vl-embedding"}
            </p>
          </div>
        </div>
        <button
          className={isComplete ? "btn-secondary min-h-9 px-3 py-2 text-xs" : "btn-primary min-h-9 px-3 py-2 text-xs"}
          type="button"
          disabled={!canEmbed}
          onClick={onEmbed}
          title="只为当前素材的待补 clip 生成视频嵌入"
        >
          {active || loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Fingerprint className="h-3.5 w-3.5" />}
          <span>{active ? "嵌入中" : isComplete ? "已齐全" : pendingCount > 0 ? `生成嵌入 (${pendingCount})` : "无片段"}</span>
        </button>
      </div>
      <div className={`flex flex-wrap items-center justify-between gap-2 rounded-xl border px-3 py-2 text-sm font-semibold ${toneClass}`}>
        <span>{title}</span>
        {hasError ? <AlertTriangle className="h-4 w-4 shrink-0" /> : <span className="font-mono">{percent}%</span>}
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-surface-hover">
        <div
          className={`h-full rounded-full transition-all duration-500 ${hasError ? "bg-status-error" : isComplete ? "bg-status-success" : "bg-accent"}`}
          style={{ width: `${percent}%` }}
        />
      </div>
      <div className="grid grid-cols-3 gap-2 text-center">
        <div className="rounded-xl border border-border/70 bg-white/55 px-2 py-2">
          <p className="text-[11px] text-text-tertiary">候选</p>
          <p className="mt-0.5 text-sm font-semibold text-text-primary">{candidateCount}</p>
        </div>
        <div className="rounded-xl border border-border/70 bg-white/55 px-2 py-2">
          <p className="text-[11px] text-text-tertiary">已入库</p>
          <p className="mt-0.5 text-sm font-semibold text-text-primary">{indexedCount}</p>
        </div>
        <div className="rounded-xl border border-border/70 bg-white/55 px-2 py-2">
          <p className="text-[11px] text-text-tertiary">待补</p>
          <p className={`mt-0.5 text-sm font-semibold ${pendingCount > 0 ? "text-status-warning" : "text-text-primary"}`}>{pendingCount}</p>
        </div>
      </div>
      <p className="text-xs leading-5 text-text-secondary">{detail}</p>
    </section>
  );
}

function ReadonlyStructurePanel({
  segments,
  events,
  isPortrait,
  activeSegmentId,
  onSelectSegment,
}: {
  segments: AnnotationTimelineSegment[];
  events: AnnotationQualityEvent[];
  isPortrait: boolean;
  activeSegmentId: string | null;
  onSelectSegment: (segment: AnnotationTimelineSegment) => void;
}) {
  return (
    <div className="grid gap-4">
      <section className="grid gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Film className="h-4 w-4 text-accent" />
          <span>结构化片段</span>
          <span className="text-xs font-normal text-text-tertiary">共 {segments.length} 段</span>
        </div>
        {segments.length === 0 ? (
          <p className="rounded-2xl border border-border/80 bg-white/65 p-4 text-sm text-text-secondary">暂无结构化片段。</p>
        ) : (
          <div className="grid gap-3">
            {segments.map((segment, index) => (
              <SegmentCard
                key={segment.segment_id || index}
                segment={segment}
                index={index}
                isPortrait={isPortrait}
                active={Boolean(activeSegmentId) && segment.segment_id === activeSegmentId}
                onSelect={() => onSelectSegment(segment)}
              />
            ))}
          </div>
        )}
      </section>

      <section className="grid gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
          <ShieldAlert className="h-4 w-4 text-status-warning" />
          <span>质量事件</span>
          <span className="text-xs font-normal text-text-tertiary">共 {events.length} 个</span>
        </div>
        {events.length === 0 ? (
          <p className="rounded-2xl border border-border/80 bg-white/65 p-4 text-sm text-text-secondary">未捕捉到质量事件。</p>
        ) : (
          <div className="grid gap-2">
            {events.map((event, index) => (
              <QualityEventRow key={event.event_id || index} event={event} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function SegmentCard({
  segment,
  index,
  isPortrait,
  active,
  onSelect,
}: {
  segment: AnnotationTimelineSegment;
  index: number;
  isPortrait: boolean;
  active: boolean;
  onSelect: () => void;
}) {
  const role = segment.usable_roles?.[0];
  const quality = segment.quality ?? {};
  const summary = segment.summary || segment.retrieval_sentence || "未生成片段摘要";
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`grid gap-3 rounded-2xl border bg-white/70 p-4 text-left transition-all ${
        active ? "border-accent/45 shadow-glow" : "border-border/80 hover:border-accent/25"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-full bg-accent px-2.5 py-1 text-xs font-semibold text-white">{formatWindow(segment.start, segment.end)}</span>
        <span className="badge bg-surface-hover text-text-secondary">片段 {index + 1}</span>
        {role ? <span className="badge bg-accent/12 text-accent">{translateToken(role, ROLE_LABELS, role)}</span> : null}
        <span className="ml-auto text-xs text-text-tertiary">
          时长 {segment.duration.toFixed(1)}s · 置信度 {formatPercent(segment.confidence)}
        </span>
      </div>

      <p className="text-sm leading-6 text-text-primary">{summary}</p>

      {segment.keywords && segment.keywords.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {segment.keywords.map((keyword) => (
            <span key={keyword} className="badge bg-surface-hover text-text-tertiary">
              {keyword}
            </span>
          ))}
        </div>
      ) : null}

      <div className="flex flex-wrap gap-1.5">
        {isPortrait ? (
          <>
            <Signal tone={segment.gaze_to_camera === false ? "danger" : "good"} label={boolLabel(segment.gaze_to_camera, "直视镜头", "视线偏离", "视线未标注")} />
            <Signal tone={segment.mouth_visible === false ? "danger" : "good"} label={boolLabel(segment.mouth_visible, "嘴型清晰", "嘴型不可见", "嘴型未标注")} />
            <Signal tone={segment.mouth_moving === false ? "warn" : "good"} label={boolLabel(segment.mouth_moving, "持续口播", "嘴部未动", "口播未标注")} />
            {segment.speech_action_alignment ? (
              <Signal tone={segment.speech_action_alignment === "aligned" ? "good" : "warn"} label={`动作一致性：${translateToken(segment.speech_action_alignment, SPEECH_ALIGNMENT_LABELS)}`} />
            ) : null}
            {segment.speaker_intent ? <Signal tone="neutral" label={`意图：${segment.speaker_intent}`} /> : null}
            {segment.gesture_type ? <Signal tone="neutral" label={`动作：${segment.gesture_type}`} /> : null}
            {segment.retake_cue && segment.retake_cue !== "none" ? <Signal tone="danger" label={`重来信号：${segment.retake_cue}`} /> : null}
          </>
        ) : (
          <>
            {segment.process_stage ? <Signal tone="neutral" label={`工序：${translateToken(segment.process_stage, PROCESS_STAGE_LABELS)}`} /> : null}
            {segment.action ? <Signal tone="neutral" label={`动作：${segment.action}`} /> : null}
            {segment.narrative_role ? <Signal tone="neutral" label={`叙事：${translateToken(segment.narrative_role, NARRATIVE_ROLE_LABELS)}`} /> : null}
            {segment.camera_motion ? <Signal tone="neutral" label={`运镜：${translateToken(segment.camera_motion, CAMERA_MOTION_LABELS)}`} /> : null}
            {segment.shot_scale ? <Signal tone="neutral" label={`景别：${translateToken(segment.shot_scale, SHOT_SCALE_LABELS)}`} /> : null}
            {segment.contains_face !== null && segment.contains_face !== undefined ? (
              <Signal tone={segment.contains_face ? "warn" : "good"} label={segment.contains_face ? "含人脸" : "不含人脸"} />
            ) : null}
          </>
        )}
        {quality.lip_sync_safe ? <Signal tone="good" label="适合对口型" /> : null}
        {isPortrait && quality.voiceover_only ? <Signal tone="warn" label="不适合数字人口型" /> : null}
        {!isPortrait && quality.voiceover_cover_ok ? <Signal tone="good" label="适合做 B-roll" /> : null}
        {!isPortrait && quality.voiceover_only ? <Signal tone="warn" label="只做 B-roll，勿对口型" /> : null}
      </div>
    </button>
  );
}

function QualityEventRow({ event }: { event: AnnotationQualityEvent }) {
  const hard = (event.risk_tier ?? "").toLowerCase() === "hard";
  return (
    <div className={`grid gap-1.5 rounded-2xl border p-3 ${hard ? "border-status-error/30 bg-status-error/5" : "border-status-warning/30 bg-status-warning/5"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <AlertTriangle className={`h-4 w-4 shrink-0 ${hard ? "text-status-error" : "text-status-warning"}`} />
        <span className="text-sm font-semibold text-text-primary">{translateToken(event.event_type, EVENT_TYPE_LABELS, "质量事件")}</span>
        <span className={`badge ${hard ? "badge-error" : "badge-warning"}`}>{translateToken(event.risk_tier, RISK_TIER_LABELS, "未分级")}</span>
        <span className="ml-auto font-mono text-xs text-text-tertiary">{formatWindow(event.start, event.end)}</span>
        {event.confidence !== undefined ? <span className="text-xs text-text-tertiary">置信度 {formatPercent(event.confidence)}</span> : null}
      </div>
      {event.description ? <p className="text-xs leading-5 text-text-secondary">{event.description}</p> : null}
    </div>
  );
}

function BgmStructurePanel({
  bgmReport,
  segments,
  totalDuration,
  activeSegmentId,
  onSelectSegment,
}: {
  bgmReport: Record<string, unknown>;
  segments: BgmSegment[];
  totalDuration?: number;
  activeSegmentId?: string | null;
  onSelectSegment?: (segment: BgmSegment) => void;
}) {
  const summaryItems = [
    ["bpm", "BPM"],
    ["tempo_bucket", "速度"],
    ["genre", "风格"],
    ["loudness_lufs", "响度 LUFS"],
    ["mood", "情绪"],
  ]
    .map(([key, label]) => ({ key, label, value: bgmReport[key] }))
    .filter((item) => item.value !== undefined && item.value !== null && String(item.value).trim().length > 0);
  const coverage = segments.reduce((sum, segment) => sum + Math.max(0, segment.end - segment.start), 0);
  const coverageRatio = totalDuration && totalDuration > 0 ? Math.min(1, coverage / totalDuration) : 0;

  return (
    <div className="grid gap-4">
      {summaryItems.length > 0 ? (
        <section className="grid gap-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <Film className="h-4 w-4 text-accent" />
            <span>BGM 摘要</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {summaryItems.map((item) => (
              <span key={item.key} className="badge bg-surface-hover text-text-secondary">
                {item.label}: {String(item.value)}
              </span>
            ))}
          </div>
        </section>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2">
        <AnnotationMetric label="标注覆盖" value={formatPercent(coverageRatio)} tone={coverageRatio >= 0.95 ? "ok" : "warn"} />
        <AnnotationMetric label="音乐段落" value={`${segments.length} 段`} />
      </div>

      <section className="grid gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Film className="h-4 w-4 text-accent" />
          <span>音乐段落</span>
          <span className="text-xs font-normal text-text-tertiary">共 {segments.length} 段</span>
        </div>
        {segments.length === 0 ? (
          <p className="rounded-2xl border border-border/80 bg-white/65 p-4 text-sm text-text-secondary">暂无音乐段落。</p>
        ) : (
          <div className="grid gap-3">
            <BgmSegmentTimeline segments={segments} totalDuration={totalDuration} activeSegmentId={activeSegmentId} onSelectSegment={onSelectSegment} />
            {segments.map((item, index) => {
              const roleColor = BGM_ROLE_COLORS[item.role];
              const sectionLabel = translateToken(item.section_type, BGM_SECTION_LABELS, "通用段");
              const energyLabel = translateToken(item.energy_profile, BGM_ENERGY_LABELS, "稳定");
              const active = Boolean(activeSegmentId) && item.segment_id === activeSegmentId;
              return (
                <button
                  key={`bgm-segment-card-${item.start}-${item.end}-${index}`}
                  type="button"
                  onClick={() => onSelectSegment?.(item)}
                  className={`grid gap-3 rounded-2xl border bg-white/70 p-4 text-left transition-all ${
                    active ? "border-accent/45 shadow-glow" : "border-border/80 hover:border-accent/25"
                  }`}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-full px-2.5 py-1 text-xs font-semibold" style={{ backgroundColor: roleColor.softBg, color: roleColor.text }}>
                      {formatWindow(item.start, item.end)}
                    </span>
                    <span className="badge bg-surface-hover text-text-secondary">时长 {Math.max(0, item.end - item.start).toFixed(1)}s</span>
                    <span className="badge" style={{ backgroundColor: roleColor.softBg, color: roleColor.text, borderColor: roleColor.border }}>
                      {BGM_ROLE_LABELS[item.role]}
                    </span>
                    <span className="badge bg-surface-hover text-text-secondary">{sectionLabel}</span>
                    <span className="badge bg-surface-hover text-text-secondary">能量 {energyLabel}</span>
                    {item.section_label ? <span className="badge bg-surface-hover text-text-secondary">结构 {item.section_label}</span> : null}
                    {item.repeat_group ? <span className="badge bg-surface-hover text-text-secondary">重复 {item.repeat_group}</span> : null}
                    {item.loopable ? <span className="badge bg-status-success/10 text-status-success">可循环</span> : null}
                    {item.energy !== undefined ? <span className="badge bg-surface-hover text-text-secondary">energy {formatPercent(item.energy)}</span> : null}
                    {item.drop_anchor_sec !== undefined ? <span className="badge bg-surface-hover text-text-secondary">drop {item.drop_anchor_sec.toFixed(1)}s</span> : null}
                  </div>
                  {item.script_fit && item.script_fit.length > 0 ? (
                    <div className="flex flex-wrap gap-1.5">
                      {item.script_fit.map((tag) => (
                        <span key={`script-fit-${item.segment_id ?? index}-${tag}`} className="badge bg-accent/10 text-accent">
                          {tag}
                        </span>
                      ))}
                    </div>
                  ) : null}
                  {item.scene_fit && item.scene_fit.length > 0 ? (
                    <div className="flex flex-wrap gap-1.5">
                      {item.scene_fit.map((tag) => (
                        <span key={tag} className="badge bg-surface-hover text-text-tertiary">
                          {tag}
                        </span>
                      ))}
                    </div>
                  ) : null}
                  {item.reason ? <p className="text-sm leading-6 text-text-secondary">{item.reason}</p> : null}
                </button>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

function BgmSegmentTimeline({
  segments,
  totalDuration,
  activeSegmentId,
  onSelectSegment,
}: {
  segments: BgmSegment[];
  totalDuration?: number;
  activeSegmentId?: string | null;
  onSelectSegment?: (segment: BgmSegment) => void;
}) {
  const duration = Math.max(totalDuration || 0, ...segments.map((segment) => segment.end), 0);
  if (duration <= 0) return null;
  return (
    <div className="grid gap-1.5">
      <div className="relative h-12 overflow-hidden rounded-lg border border-border bg-surface-hover">
        {segments.map((segment, index) => {
          const roleColor = BGM_ROLE_COLORS[segment.role];
          const left = Math.max(0, Math.min(100, (segment.start / duration) * 100));
          const width = Math.max(0.35, Math.min(100 - left, ((segment.end - segment.start) / duration) * 100));
          const active = Boolean(activeSegmentId) && segment.segment_id === activeSegmentId;
          return (
            <button
              key={`bgm-timeline-${segment.segment_id ?? index}`}
              type="button"
              onClick={() => onSelectSegment?.(segment)}
              className={`absolute top-1 h-10 overflow-hidden border-r border-white/80 px-2 py-1 text-left text-[11px] font-semibold leading-4 transition-all ${
                active ? "ring-2 ring-accent" : "hover:ring-1 hover:ring-accent/40"
              }`}
              style={{
                left: `${left}%`,
                width: `${width}%`,
                backgroundColor: roleColor.softBg,
                color: roleColor.text,
              }}
              title={`${formatWindow(segment.start, segment.end)} ${translateToken(segment.section_type, BGM_SECTION_LABELS, "通用段")}`}
            >
              <span className="block truncate">{segment.section_label || translateToken(segment.section_type, BGM_SECTION_LABELS, "通用段")}</span>
              <span className="block truncate font-normal">{formatWindow(segment.start, segment.end)}</span>
            </button>
          );
        })}
      </div>
      <div className="flex justify-between text-[11px] text-text-tertiary">
        <span>0:00</span>
        <span>{formatDuration(duration)}</span>
      </div>
    </div>
  );
}

function BgmAnnotationForm({
  assetId,
  caseId,
  editor,
  segments,
  setSegments,
  duration,
  onCancel,
  onSaved,
}: {
  assetId: string;
  caseId: string | null;
  editor: AnnotationEditorVm;
  segments: BgmSegment[];
  setSegments: Dispatch<SetStateAction<BgmSegment[]>>;
  duration: number;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const roleOptions = BGM_ROLES.map((role) => [role, BGM_ROLE_LABELS[role] ?? role] as [string, string]);
  const sectionOptions = BGM_SECTION_TYPES.map((type) => [type, BGM_SECTION_LABELS[type] ?? type] as [string, string]);
  const energyOptions = BGM_ENERGY_PROFILES.map((profile) => [profile, BGM_ENERGY_LABELS[profile] ?? profile] as [string, string]);

  const updateSegment = (index: number, patch: Partial<BgmSegment>) =>
    setSegments((current) => current.map((item, i) => (i === index ? { ...item, ...patch } : item)));

  const removeSegment = (index: number) => setSegments((current) => current.filter((_, i) => i !== index));

  const addSegment = () => {
    const end = Math.min(Math.max(duration || 1, 0.5), 1);
    setSegments((current) => [
      ...current,
      {
        segment_id: `manual_bgm_${Date.now()}`,
        start: 0,
        end,
        duration: end,
        role: "general",
        section_type: "general",
        energy_profile: "stable",
        loopable: false,
        script_fit: [],
        avoid_script: [],
        scene_fit: [],
        source: "manual",
      },
    ]);
  };

  const patchMutation = useMutation({
    mutationFn: async () =>
      api.annotations.patch(assetId, {
        etag: editor.etag,
        patch: {
          operations: [
            { op: "replace", path: "/canonical/bgm_segments", value: bgmSegmentsToCanonical(segments) },
            { op: "replace", path: "/projection/usable", value: true },
          ],
        },
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["library", "annotation", assetId] });
      await queryClient.invalidateQueries({ queryKey: ["library", "media", caseId] });
      toast.success("标注已保存", "BGM 音乐段落已更新。");
      onSaved();
    },
    onError: (error) => {
      if (isApiError(error) && (error.status === 409 || error.code === "artifact.schema_mismatch")) {
        toast.error("标注版本冲突", "服务器标注已更新，请刷新后重新编辑。");
        return;
      }
      toast.error("标注保存失败", error);
    },
  });

  return (
    <form
      className="grid gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        patchMutation.mutate();
      }}
    >
      <div className="grid gap-3">
        <div className="flex items-center justify-between gap-3">
          <h4 className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <Film className="h-4 w-4 text-accent" /> 音乐段落
          </h4>
          <button className="btn-secondary min-h-9 px-3" type="button" onClick={addSegment}>
            <Plus className="h-4 w-4" />
            <span>新增段落</span>
          </button>
        </div>
        <div className="grid gap-3">
          {segments.map((item, index) => (
            <div key={`bgm-edit-${index}`} className="grid gap-3 rounded-2xl border border-border/80 bg-white/65 p-3">
              <div className="grid gap-2 sm:grid-cols-[1fr_1fr_1fr_auto]">
                <NumberField label="开始(s)" value={item.start} step={0.1} max={duration || undefined} onChange={(v) => updateSegment(index, { start: v })} />
                <NumberField label="结束(s)" value={item.end} step={0.1} max={duration || undefined} onChange={(v) => updateSegment(index, { end: v })} />
                <SelectField
                  label="角色"
                  value={item.role}
                  options={roleOptions}
                  onChange={(v) => updateSegment(index, { role: v as BgmSegment["role"] })}
                />
                <button className="icon-button mt-5 self-start" type="button" onClick={() => removeSegment(index)} aria-label="删除段落">
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>

              <div className="grid gap-2 sm:grid-cols-3">
                <SelectField
                  label="结构段落"
                  value={item.section_type ?? "general"}
                  options={sectionOptions}
                  onChange={(v) => updateSegment(index, { section_type: v as BgmSegment["section_type"] })}
                />
                <SelectField
                  label="能量走势"
                  value={item.energy_profile ?? "stable"}
                  options={energyOptions}
                  onChange={(v) => updateSegment(index, { energy_profile: v as BgmSegment["energy_profile"] })}
                />
                <TextField label="结构标签" value={item.section_label ?? ""} onChange={(v) => updateSegment(index, { section_label: v })} />
              </div>

              <div className="grid gap-2 sm:grid-cols-3">
                <label className="grid gap-1.5">
                  <span className="text-[11px] font-medium text-text-tertiary">drop_anchor_sec(s)</span>
                  <input
                    className="input"
                    type="number"
                    step={0.1}
                    min={0}
                    max={duration || undefined}
                    value={item.drop_anchor_sec ?? ""}
                    onChange={(event) => {
                      const value = event.target.value;
                      const next = Number(value);
                      updateSegment(index, { drop_anchor_sec: value === "" || !Number.isFinite(next) ? undefined : next });
                    }}
                  />
                </label>
                <TextField label="情绪" value={item.mood ?? ""} onChange={(v) => updateSegment(index, { mood: v })} />
                <TextField label="适配场景（逗号分隔）" value={(item.scene_fit ?? []).join(", ")} onChange={(v) => updateSegment(index, { scene_fit: splitTokens(v) })} />
              </div>
              <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
                <TextField label="适配脚本（逗号分隔）" value={(item.script_fit ?? []).join(", ")} onChange={(v) => updateSegment(index, { script_fit: splitTokens(v) })} />
                <TextField label="避用脚本（逗号分隔）" value={(item.avoid_script ?? []).join(", ")} onChange={(v) => updateSegment(index, { avoid_script: splitTokens(v) })} />
                <label className="mt-5 flex h-10 items-center gap-2 rounded-lg border border-border bg-white px-3 text-xs font-medium text-text-secondary">
                  <input type="checkbox" checked={Boolean(item.loopable)} onChange={(event) => updateSegment(index, { loopable: event.target.checked })} />
                  <span>可循环</span>
                </label>
              </div>
            </div>
          ))}
          {segments.length === 0 ? <p className="text-sm text-text-secondary">暂无音乐段落，点击「新增段落」添加。</p> : null}
        </div>
      </div>

      <div className="rounded-2xl border border-status-warning/20 bg-status-warning/10 p-3 text-xs leading-5 text-status-warning">
        保存会携带当前版本标识；若服务端标注已被更新，将提示版本冲突并要求刷新后重试。
      </div>
      <div className="flex justify-end gap-3 border-t border-border/70 pt-4">
        <button className="btn-secondary" type="button" onClick={onCancel} disabled={patchMutation.isPending}>
          取消编辑
        </button>
        <button className="btn-primary" type="submit" disabled={patchMutation.isPending}>
          {patchMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
          <span>{patchMutation.isPending ? "保存中" : "保存标注"}</span>
        </button>
      </div>
    </form>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Structured edit form — per-segment + per-event add/remove/field edit. No JSON.
// ─────────────────────────────────────────────────────────────────────────────

function StructuredAnnotationForm({
  form,
  setForm,
  isPortrait,
  duration,
  pending,
  onSubmit,
  onCancel,
}: {
  form: AnnotationForm;
  setForm: Dispatch<SetStateAction<AnnotationForm>>;
  isPortrait: boolean;
  duration: number;
  pending: boolean;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  const updateSegment = (index: number, patch: Partial<AnnotationTimelineSegment>) =>
    setForm((current) => ({
      ...current,
      segments: current.segments.map((segment, i) => (i === index ? { ...segment, ...patch } : segment)),
    }));

  const updateQuality = (index: number, patch: Partial<AnnotationSegmentQuality>) =>
    setForm((current) => ({
      ...current,
      segments: current.segments.map((segment, i) => (i === index ? { ...segment, quality: { ...segment.quality, ...patch } } : segment)),
    }));

  const removeSegment = (index: number) =>
    setForm((current) => ({ ...current, segments: current.segments.filter((_, i) => i !== index) }));

  const addSegment = () => {
    const end = Math.min(Math.max(duration || 1, 0.5), 1);
    setForm((current) => ({
      ...current,
      segments: [
        ...current.segments,
        {
          segment_id: `manual_${Date.now()}`,
          level: "editable_clip",
          start: 0,
          end,
          duration: end,
          confidence: 0.8,
          summary: "",
          retrieval_sentence: "",
          keywords: [],
          usable_roles: isPortrait ? ["main"] : ["cover"],
          quality: { lip_sync_safe: isPortrait, voiceover_cover_ok: !isPortrait },
        },
      ],
    }));
  };

  const updateEvent = (index: number, patch: Partial<AnnotationQualityEvent>) =>
    setForm((current) => ({
      ...current,
      qualityEvents: current.qualityEvents.map((event, i) => (i === index ? { ...event, ...patch } : event)),
    }));

  const removeEvent = (index: number) =>
    setForm((current) => ({ ...current, qualityEvents: current.qualityEvents.filter((_, i) => i !== index) }));

  const addEvent = () =>
    setForm((current) => ({
      ...current,
      qualityEvents: [
        ...current.qualityEvents,
        {
          event_id: `manual_event_${Date.now()}`,
          event_type: "manual_note",
          start: 0,
          end: Math.min(duration || 1, 0.5),
          description: "",
          risk_tier: "soft",
          confidence: 0.8,
        },
      ],
    }));

  return (
    <form
      className="grid gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="grid gap-1.5">
          <span className="text-xs font-medium text-text-secondary">质量状态</span>
          <select className="input" value={form.qualityStatus} onChange={(event) => setForm((current) => ({ ...current, qualityStatus: event.target.value }))}>
            {Object.entries(QUALITY_STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex cursor-pointer items-center gap-3 self-end rounded-2xl border border-border/80 bg-white/65 p-3">
          <input type="checkbox" checked={form.usable} onChange={(event) => setForm((current) => ({ ...current, usable: event.target.checked }))} />
          <span>
            <span className="block text-sm font-semibold text-text-primary">允许生产链路复用</span>
            <span className="mt-0.5 block text-xs font-normal text-text-secondary">关闭后素材会被标记为不可用。</span>
          </span>
        </label>
      </div>

      <div className="grid gap-3">
        <div className="flex items-center justify-between gap-3">
          <h4 className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <Film className="h-4 w-4 text-accent" /> 结构化片段
          </h4>
          <button className="btn-secondary min-h-9 px-3" type="button" onClick={addSegment}>
            <Plus className="h-4 w-4" />
            <span>新增片段</span>
          </button>
        </div>
        <div className="grid gap-3">
          {form.segments.map((segment, index) => (
            <div key={segment.segment_id || index} className="grid gap-3 rounded-2xl border border-border/80 bg-white/65 p-3">
              <div className="grid gap-2 sm:grid-cols-[1fr_1fr_1fr_auto]">
                <NumberField label="开始(s)" value={segment.start} step={0.1} max={duration || undefined} onChange={(v) => updateSegment(index, { start: v })} />
                <NumberField label="结束(s)" value={segment.end} step={0.1} max={duration || undefined} onChange={(v) => updateSegment(index, { end: v })} />
                <NumberField label="置信度" value={segment.confidence ?? 0.8} step={0.01} min={0} max={1} onChange={(v) => updateSegment(index, { confidence: v })} />
                <button className="icon-button mt-5 self-start" type="button" onClick={() => removeSegment(index)} aria-label="删除片段">
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>

              <div className="grid gap-2 sm:grid-cols-2">
                <TextareaField label="画面动作摘要" value={segment.summary ?? ""} onChange={(v) => updateSegment(index, { summary: v })} />
                <TextareaField label="检索描述" value={segment.retrieval_sentence ?? ""} onChange={(v) => updateSegment(index, { retrieval_sentence: v })} />
              </div>

              <div className="grid gap-2 sm:grid-cols-2">
                <TextField
                  label="用途角色（逗号分隔）"
                  value={(segment.usable_roles ?? []).join(", ")}
                  placeholder={isPortrait ? "hook, main, avoid" : "cover, avoid"}
                  onChange={(v) => updateSegment(index, { usable_roles: splitTokens(v) })}
                />
                <TextField label="关键词（逗号分隔）" value={(segment.keywords ?? []).join(", ")} onChange={(v) => updateSegment(index, { keywords: splitTokens(v) })} />
              </div>

              <div className="grid gap-2 sm:grid-cols-2">
                <SelectField
                  label="对口型"
                  value={boolSelect(segment.quality?.lip_sync_safe)}
                  options={[["", "未标注"], ["true", "适合对口型"], ["false", "不适合"]]}
                  onChange={(v) => updateQuality(index, { lip_sync_safe: parseBoolSelect(v) ?? undefined })}
                />
                {!isPortrait ? (
                  <SelectField
                    label="B-roll 覆盖"
                    value={boolSelect(segment.quality?.voiceover_cover_ok)}
                    options={[["", "未标注"], ["true", "适合做 B-roll"], ["false", "不适合"]]}
                    onChange={(v) => updateQuality(index, { voiceover_cover_ok: parseBoolSelect(v) ?? undefined })}
                  />
                ) : null}
              </div>

              {isPortrait ? (
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  <SelectField label="视线" value={boolSelect(segment.gaze_to_camera)} options={[["", "未标注"], ["true", "直视镜头"], ["false", "视线偏离"]]} onChange={(v) => updateSegment(index, { gaze_to_camera: parseBoolSelect(v) })} />
                  <SelectField label="嘴型" value={boolSelect(segment.mouth_visible)} options={[["", "未标注"], ["true", "嘴型清晰"], ["false", "嘴型不可见"]]} onChange={(v) => updateSegment(index, { mouth_visible: parseBoolSelect(v) })} />
                  <SelectField label="口播" value={boolSelect(segment.mouth_moving)} options={[["", "未标注"], ["true", "持续口播"], ["false", "嘴部未动"]]} onChange={(v) => updateSegment(index, { mouth_moving: parseBoolSelect(v) })} />
                  <SelectField
                    label="动作一致性"
                    value={segment.speech_action_alignment ?? ""}
                    options={[["", "未标注"], ["aligned", "动作一致"], ["uncertain", "待确认"], ["mismatch", "不一致"]]}
                    onChange={(v) => updateSegment(index, { speech_action_alignment: v })}
                  />
                  <TextField label="表达意图" value={segment.speaker_intent ?? ""} onChange={(v) => updateSegment(index, { speaker_intent: v })} />
                  <TextField label="动作类型" value={segment.gesture_type ?? ""} onChange={(v) => updateSegment(index, { gesture_type: v })} />
                </div>
              ) : (
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  <TextField label="工序阶段" value={segment.process_stage ?? ""} onChange={(v) => updateSegment(index, { process_stage: v })} />
                  <TextField label="动作" value={segment.action ?? ""} onChange={(v) => updateSegment(index, { action: v })} />
                  <TextField label="叙事用途" value={segment.narrative_role ?? ""} onChange={(v) => updateSegment(index, { narrative_role: v })} />
                  <TextField label="运镜" value={segment.camera_motion ?? ""} onChange={(v) => updateSegment(index, { camera_motion: v })} />
                  <TextField label="景别" value={segment.shot_scale ?? ""} onChange={(v) => updateSegment(index, { shot_scale: v })} />
                  <SelectField label="人脸" value={boolSelect(segment.contains_face)} options={[["", "未标注"], ["true", "含人脸"], ["false", "不含人脸"]]} onChange={(v) => updateSegment(index, { contains_face: parseBoolSelect(v) })} />
                </div>
              )}
            </div>
          ))}
          {form.segments.length === 0 ? <p className="text-sm text-text-secondary">暂无片段，点击「新增片段」添加。</p> : null}
        </div>
      </div>

      <div className="grid gap-3">
        <div className="flex items-center justify-between gap-3">
          <h4 className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <ShieldAlert className="h-4 w-4 text-status-warning" /> 质量事件
          </h4>
          <button className="btn-secondary min-h-9 px-3" type="button" onClick={addEvent}>
            <Plus className="h-4 w-4" />
            <span>新增事件</span>
          </button>
        </div>
        <div className="grid gap-3">
          {form.qualityEvents.map((event, index) => (
            <div key={event.event_id || index} className="grid gap-2 rounded-2xl border border-border/80 bg-white/65 p-3">
              <div className="grid gap-2 sm:grid-cols-[1.2fr_1fr_1fr_1fr_1fr_auto]">
                <TextField label="类型" value={event.event_type ?? ""} onChange={(v) => updateEvent(index, { event_type: v })} />
                <NumberField label="开始(s)" value={event.start} step={0.1} max={duration || undefined} onChange={(v) => updateEvent(index, { start: v })} />
                <NumberField label="结束(s)" value={event.end} step={0.1} max={duration || undefined} onChange={(v) => updateEvent(index, { end: v })} />
                <SelectField label="风险" value={event.risk_tier ?? ""} options={[["", "未分级"], ["soft", "软风险"], ["hard", "硬风险"]]} onChange={(v) => updateEvent(index, { risk_tier: v })} />
                <NumberField label="置信度" value={event.confidence ?? 0.8} step={0.01} min={0} max={1} onChange={(v) => updateEvent(index, { confidence: v })} />
                <button className="icon-button mt-5 self-start" type="button" onClick={() => removeEvent(index)} aria-label="删除事件">
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
              <TextareaField label="事件说明" value={event.description ?? ""} onChange={(v) => updateEvent(index, { description: v })} />
            </div>
          ))}
          {form.qualityEvents.length === 0 ? <p className="text-sm text-text-secondary">暂无质量事件。</p> : null}
        </div>
      </div>

      <div className="rounded-2xl border border-status-warning/20 bg-status-warning/10 p-3 text-xs leading-5 text-status-warning">
        保存会携带当前版本标识；若服务端标注已被更新，将提示版本冲突并要求刷新后重试。
      </div>
      <div className="flex justify-end gap-3 border-t border-border/70 pt-4">
        <button className="btn-secondary" type="button" onClick={onCancel} disabled={pending}>
          取消编辑
        </button>
        <button className="btn-primary" type="submit" disabled={pending}>
          {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
          <span>{pending ? "保存中" : "保存标注"}</span>
        </button>
      </div>
    </form>
  );
}

function AnnotationMetric({ label, value, tone }: { label: string; value: string; tone?: "ok" | "warn" }) {
  const valueClass = tone === "ok" ? "text-status-success" : tone === "warn" ? "text-status-warning" : "text-text-primary";
  return (
    <div className="rounded-2xl border border-border/80 bg-white/65 p-4">
      <p className="text-xs text-text-secondary">{label}</p>
      <p className={`mt-2 text-lg font-semibold tabular-nums ${valueClass}`}>{value}</p>
    </div>
  );
}

type SignalTone = "good" | "warn" | "danger" | "neutral";

function Signal({ tone, label }: { tone: SignalTone; label: string }) {
  const toneClass: Record<SignalTone, string> = {
    good: "bg-status-success/15 text-status-success",
    warn: "bg-status-warning/15 text-status-warning",
    danger: "bg-status-error/15 text-status-error",
    neutral: "bg-surface-hover text-text-secondary",
  };
  return <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium ${toneClass[tone]}`}>{label}</span>;
}

function NumberField({
  label,
  value,
  step,
  min = 0,
  max,
  onChange,
}: {
  label: string;
  value: number;
  step: number;
  min?: number;
  max?: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="grid gap-1.5">
      <span className="text-[11px] font-medium text-text-tertiary">{label}</span>
      <input className="input" type="number" step={step} min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} />
    </label>
  );
}

function TextField({ label, value, placeholder, onChange }: { label: string; value: string; placeholder?: string; onChange: (value: string) => void }) {
  return (
    <label className="grid gap-1.5">
      <span className="text-[11px] font-medium text-text-tertiary">{label}</span>
      <input className="input" value={value} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function TextareaField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="grid gap-1.5">
      <span className="text-[11px] font-medium text-text-tertiary">{label}</span>
      <textarea className="input min-h-[72px]" value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function SelectField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: Array<[string, string]>;
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-1.5">
      <span className="text-[11px] font-medium text-text-tertiary">{label}</span>
      <select className="input" value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map(([optionValue, optionLabel]) => (
          <option key={optionValue} value={optionValue}>
            {optionLabel}
          </option>
        ))}
      </select>
    </label>
  );
}

function toPlayerSegment(segment: AnnotationTimelineSegment, index: number): VideoPlayerSegment {
  return {
    id: segment.segment_id || `seg-${index}`,
    start: segment.start,
    end: segment.end,
    label: segment.summary || segment.retrieval_sentence || segment.segment_id || `片段 ${index + 1}`,
    role: segment.usable_roles?.[0],
  };
}

function toBgmPlayerSegment(segment: BgmSegment, index: number): VideoPlayerSegment {
  return {
    id: segment.segment_id || `bgm-seg-${index}`,
    start: segment.start,
    end: segment.end,
    label: segment.mood || segment.reason || segment.segment_id || `音乐段落 ${index + 1}`,
    role: segment.role,
  };
}

function toPlayerEvent(event: AnnotationQualityEvent, index: number): VideoPlayerQualityEvent {
  return {
    id: event.event_id || `qe-${index}`,
    start: event.start,
    end: event.end,
    label: event.description || event.event_type || "质量事件",
    risk_tier: event.risk_tier,
  };
}

function formatAnnotationVersion(version?: string): string | null {
  const token = String(version ?? "").trim();
  if (!token) return null;
  const match = /^annotation_(.+)$/i.exec(token);
  return `标注 ${match ? match[1] : token}`;
}

function boolLabel(value: boolean | null | undefined, onTrue: string, onFalse: string, unknown: string): string {
  if (value === true) return onTrue;
  if (value === false) return onFalse;
  return unknown;
}

function boolSelect(value: boolean | null | undefined): string {
  if (value === true) return "true";
  if (value === false) return "false";
  return "";
}

function parseBoolSelect(value: string): boolean | null {
  if (value === "true") return true;
  if (value === "false") return false;
  return null;
}

function splitTokens(value: string): string[] {
  return value
    .split(/[,，]/)
    .map((token) => token.trim())
    .filter((token) => token.length > 0);
}

function readJsonString(source: AnnotationEditorVm["projection"], key: string): string | undefined {
  const value = source[key];
  return typeof value === "string" ? value : undefined;
}

function readJsonNumber(source: AnnotationEditorVm["projection"], key: string): number | undefined {
  const value = source[key];
  return typeof value === "number" ? value : undefined;
}

type InvalidSegment = { start_sec: number; end_sec: number };

function readInvalidSegments(source: AnnotationEditorVm["projection"]): InvalidSegment[] {
  const raw = source.invalid_segments;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((value) => {
      const record = typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
      const start = Number(record.start_sec ?? record.start ?? 0);
      const end = Number(record.end_sec ?? record.end ?? start);
      return { start_sec: Number.isFinite(start) ? start : 0, end_sec: Number.isFinite(end) ? end : 0 };
    })
    .filter((value) => value.end_sec >= value.start_sec);
}
