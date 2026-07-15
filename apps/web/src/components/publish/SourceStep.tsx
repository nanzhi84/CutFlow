import { Film, Loader2, PackagePlus, Trash2 } from "lucide-react";
import type { CaseListItem, FinishedVideo, PublishPackage } from "../../api/client";
import { DropZone } from "../ui/DropZone";
import { useToast } from "../ui/Toast";
import { useUpload } from "../../hooks/useUpload";
import { formatDuration } from "../../lib/format";
import { PlatformChips } from "./PlatformChips";
import { displayFinishedVideoTitle, type BatchDefaults, type SourcePoolItem } from "./publishModel";

type SourceStepProps = {
  embedded: boolean;
  cases: CaseListItem[];
  selectedCaseId: string | null;
  onCaseChange: (caseId: string) => void;
  videos: FinishedVideo[];
  isVideosLoading: boolean;
  pool: SourcePoolItem[];
  defaults: BatchDefaults;
  onDefaultsChange: (defaults: BatchDefaults) => void;
  onAddFinished: (video: FinishedVideo) => void;
  onAddUpload: (file: File, publishPackage: PublishPackage) => void;
  onRemove: (itemId: string) => void;
  onClear: () => void;
  onCreateBatch: () => void;
  isCreating: boolean;
};

const VIDEO_ACCEPT = "video/*,.mp4,.mov,.mkv,.webm,.avi";

export function SourceStep({
  embedded,
  cases,
  selectedCaseId,
  onCaseChange,
  videos,
  isVideosLoading,
  pool,
  defaults,
  onDefaultsChange,
  onAddFinished,
  onAddUpload,
  onRemove,
  onClear,
  onCreateBatch,
  isCreating,
}: SourceStepProps) {
  const toast = useToast();
  const upload = useUpload();
  const poolIds = new Set(pool.map((item) => item.id));

  async function uploadFiles(files: File[]) {
    if (!selectedCaseId && !embedded) {
      toast.warning("请选择案例", "外部视频会按案例归档；不指定案例会影响后续复盘。");
    }
    for (const file of files) {
      try {
        const result = await upload.uploadFile({
          file,
          kind: "publish_video",
          caseId: selectedCaseId,
          metadata: { title: file.name, description: "" },
        });
        if (!result.publish_package) {
          toast.error("上传完成但未生成发布包", "请检查上传类型是否为 publish_video。");
          continue;
        }
        onAddUpload(file, result.publish_package);
        toast.success("已加入批次池", file.name);
      } catch (error) {
        toast.error("外部视频上传失败", error);
      }
    }
  }

  return (
    <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
      <div className="grid gap-4">
        {!embedded ? (
          <div className="card grid gap-3">
            <div>
              <h2 className="text-lg font-semibold text-text-primary">案例范围</h2>
              <p className="mt-1 text-sm text-text-secondary">选择案例后，可从该案例成片创建发布批次。</p>
            </div>
            <select value={selectedCaseId ?? ""} onChange={(event) => onCaseChange(event.target.value)}>
              <option value="">请选择案例</option>
              {cases.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
        ) : null}

        <div className="card grid gap-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold text-text-primary">从成片创建批次</h2>
              <p className="mt-1 text-sm text-text-secondary">勾选后即刻加入下方批次池，保留成片标题作为草稿标题。</p>
            </div>
            {isVideosLoading ? <Loader2 className="h-5 w-5 animate-spin text-text-tertiary" /> : null}
          </div>
          <div className="grid max-h-[min(520px,calc(100vh-18rem))] gap-2 overflow-y-auto pr-1">
            {videos.map((video) => {
              const active = poolIds.has(`finished:${video.id}`);
              const displayTitle = displayFinishedVideoTitle(video);
              return (
                <button
                  key={video.id}
                  type="button"
                  className={`grid gap-2 rounded-2xl border p-3 text-left transition ${
                    active ? "border-accent/30 bg-accent/10" : "border-border/75 bg-white/55 hover:bg-white/85"
                  }`}
                  onClick={() => onAddFinished(video)}
                >
                  <span className="flex items-center gap-3">
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-white/70 text-accent">
                      <Film className="h-4 w-4" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-semibold text-text-primary">{displayTitle}</span>
                      <span className="mt-0.5 block text-xs text-text-tertiary">
                        {formatDuration(video.duration_sec)} · QC {video.qc_status}
                      </span>
                    </span>
                    {active ? <span className="badge-success shrink-0">已入池</span> : null}
                  </span>
                </button>
              );
            })}
            {!isVideosLoading && videos.length === 0 ? (
              <div className="rounded-2xl border border-dashed border-border/80 bg-white/50 p-6 text-center">
                <Film className="mx-auto h-8 w-8 text-text-tertiary" />
                <p className="mt-3 text-sm font-medium text-text-primary">还没有可发布成片</p>
                <p className="mt-1 text-xs text-text-secondary">生产成功后成片会显示在这里。</p>
              </div>
            ) : null}
          </div>
        </div>

        <div className="card grid gap-4">
          <div>
            <h2 className="text-lg font-semibold text-text-primary">外部视频上传</h2>
            <p className="mt-1 text-sm text-text-secondary">上传后会生成发布包并加入批次池。</p>
          </div>
          <DropZone
            accept={VIDEO_ACCEPT}
            multiple
            maxSize={200}
            onFilesDrop={(_files, details) => void uploadFiles(details.acceptedFiles)}
            label="拖拽外部视频到此处"
          />
          {upload.status !== "idle" ? (
            <div className="rounded-2xl border border-border/80 bg-white/65 p-3 text-sm text-text-secondary">
              上传阶段：{upload.status} · {upload.progress}%
            </div>
          ) : null}
        </div>
      </div>

      <aside className="card grid content-start gap-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-text-primary">批次池</h2>
            <p className="mt-1 text-sm text-text-secondary">创建前可移出单条或清空。</p>
          </div>
          {pool.length > 0 ? (
            <button className="btn-ghost min-h-9 px-3 text-xs" type="button" onClick={onClear}>
              清空
            </button>
          ) : null}
        </div>
        <div className="rounded-2xl border border-border/80 bg-white/60 p-3">
          <div className="flex items-center justify-between text-sm">
            <span className="text-text-secondary">总计</span>
            <span className="font-semibold text-text-primary">{pool.length} 条</span>
          </div>
          <div className="mt-3 grid gap-2">
            {pool.map((item) => (
              <div key={item.id} className="flex items-center gap-2 rounded-xl bg-white/70 px-3 py-2 text-sm">
                <span className="min-w-0 flex-1 truncate text-text-primary">{item.title}</span>
                <span className="badge bg-surface-hover text-text-tertiary">{item.type === "finished" ? "成片" : "外部"}</span>
                <button className="rounded-lg p-1 text-text-tertiary hover:bg-status-error/10 hover:text-status-error" type="button" onClick={() => onRemove(item.id)} title="移出批次池">
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ))}
            {pool.length === 0 ? <p className="py-5 text-center text-sm text-text-tertiary">先勾选成片或上传视频。</p> : null}
          </div>
        </div>

        <div className="grid gap-3 border-t border-border/70 pt-4">
          <label>
            <span>发布平台</span>
            <PlatformChips
              value={defaults.platforms}
              onChange={(platforms) => onDefaultsChange({ ...defaults, platforms })}
            />
          </label>
          <button className="btn-primary w-full" type="button" disabled={pool.length === 0 || isCreating} onClick={onCreateBatch}>
            {isCreating ? <Loader2 className="h-4 w-4 animate-spin" /> : <PackagePlus className="h-4 w-4" />}
            <span>{isCreating ? "创建中" : "创建批次"}</span>
          </button>
          <p className="rounded-2xl border border-status-info/25 bg-status-info/10 p-3 text-xs leading-5 text-status-info">
            创建批次后先编辑草稿；最终发布会在确认步骤选择小V猫账号后提交。
          </p>
        </div>
      </aside>
    </section>
  );
}
