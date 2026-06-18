import {
  AlertTriangle,
  CheckCircle2,
  Edit3,
  Eye,
  FileVideo,
  Film,
  Loader2,
  Plus,
  RefreshCw,
  Scissors,
  ShieldAlert,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isApiError, type AnnotationEditorVm, type MediaAssetRecord } from "../../api/client";
import { formatDuration, shortId } from "../../lib/format";
import { toDisplayUrl } from "../../lib/url";
import { readAssetThumbnailUrl, readPreviewUrlMeta } from "../../components/library/libraryModel";
import {
  canonicalToEvidenceFrames,
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
} from "../../utils/annotationV4";
import { Modal } from "../ui/Modal";
import { VideoPlayer, type VideoPlayerQualityEvent, type VideoPlayerSegment } from "../ui/VideoPlayer";
import { useToast } from "../ui/Toast";
import { ErrorState, LoadingState } from "../State";

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

function isPortraitKind(kind?: MediaAssetRecord["kind"]): boolean {
  return kind === "portrait" || kind === "voice_reference" || kind === "voice";
}

export function AnnotationEditorModal({ assetId, caseId, onClose }: AnnotationEditorModalProps) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [rerunPreview, setRerunPreview] = useState(false);
  const [activeSegmentId, setActiveSegmentId] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewPlayable, setPreviewPlayable] = useState<boolean | undefined>(undefined);
  const [form, setForm] = useState<AnnotationForm>({ qualityStatus: "usable", usable: true, segments: [], qualityEvents: [] });

  const editorQuery = useQuery({
    queryKey: ["library", "annotation", assetId],
    queryFn: () => api.annotations.get(assetId!),
    enabled: Boolean(assetId),
  });

  const editor = editorQuery.data ?? null;
  const isPortrait = isPortraitKind(editor?.asset.kind);

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
    setEditing(false);
    setRerunPreview(false);
    setActiveSegmentId(null);
  }, [editor]);

  const displaySegments = editing ? form.segments : readSegments;
  const displayEvents = editing ? form.qualityEvents : readEvents;

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
    () => displaySegments.map((segment, index) => toPlayerSegment(segment, index)),
    [displaySegments],
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
                <span className="badge bg-surface-hover text-text-secondary">{isPortrait ? "口播 / 数字人" : "B-roll"}</span>
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
              <button className="btn-secondary" type="button" onClick={() => trimMutation.mutate()} disabled={trimMutation.isPending}>
                {trimMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Scissors className="h-4 w-4" />}
                <span>{trimMutation.isPending ? "裁剪中" : "裁剪无效"}</span>
              </button>
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
                    onSegmentClick={(segment) => segment.id && setActiveSegmentId(segment.id)}
                    onTimeUpdate={(time) => {
                      const hit = displaySegments.find((segment) => time >= segment.start && time <= segment.end);
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

              <div className="grid gap-3 sm:grid-cols-3">
                <AnnotationMetric label="有效时长" value={formatDuration(validDuration)} tone="ok" />
                <AnnotationMetric label="无效时长" value={formatDuration(invalidDuration)} tone="warn" />
                <AnnotationMetric label="总时长" value={formatDuration(totalDuration > 0 ? totalDuration : undefined)} />
              </div>

              <p className="text-xs text-text-tertiary">
                {playerSegments.length > 0
                  ? `时间轴叠加 ${playerSegments.length} 个片段${playerEvents.length > 0 ? ` · ${playerEvents.length} 个质量事件` : ""}${evidenceFrames.length > 0 ? ` · ${evidenceFrames.length} 个证据帧` : ""}，点击可跳转并高亮。`
                  : "该标注暂无可视化片段。"}
              </p>
            </div>

            <div className="max-h-[72vh] overflow-y-auto pr-1">
              {editing ? (
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
                  onSelectSegment={setActiveSegmentId}
                />
              )}
            </div>
          </div>
        </div>
      ) : null}
    </Modal>
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
  onSelectSegment: (id: string | null) => void;
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
                onSelect={() => onSelectSegment(segment.segment_id || null)}
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
        {quality.voiceover_cover_ok ? <Signal tone="good" label="适合盖旁白" /> : null}
        {quality.voiceover_only ? <Signal tone="warn" label="仅盖旁白" /> : null}
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
                <SelectField
                  label="盖旁白"
                  value={boolSelect(segment.quality?.voiceover_cover_ok)}
                  options={[["", "未标注"], ["true", "适合盖旁白"], ["false", "不适合"]]}
                  onChange={(v) => updateQuality(index, { voiceover_cover_ok: parseBoolSelect(v) ?? undefined })}
                />
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
