import { Loader2, RotateCcw, Save, Send, Trash2, Wand2 } from "lucide-react";
import type { PublishBatch, PublishBatchItem, PublishPackage } from "../../api/client";
import { StatusPill } from "../ui/StatusPill";
import { CoverPanel } from "./CoverPanel";
import { PlatformChips } from "./PlatformChips";
import {
  type BatchDefaults,
  type PublishDraft,
  buildDraftFromItem,
  clampTitle,
  formatPublishMode,
  parseTags,
  platformLabel,
  summarizePlatforms,
  titleLength,
  titleLimitForPlatforms,
} from "./publishModel";

type DraftEditorStepProps = {
  batch: PublishBatch;
  packagesById: Map<string, PublishPackage>;
  drafts: Record<string, PublishDraft>;
  defaults: BatchDefaults;
  activeItemId: string | null;
  isSavingItem?: boolean;
  onDefaultsChange: (defaults: BatchDefaults) => void;
  onDraftChange: (itemId: string, patch: Partial<PublishDraft>) => void;
  onResetDraft: (item: PublishBatchItem) => void;
  onSaveItem: (item: PublishBatchItem) => void;
  onDeleteItem: (item: PublishBatchItem) => void;
  onActiveItemChange: (itemId: string) => void;
  onCoverArtifact: (packageId: string, artifactId: string | null) => Promise<void>;
  onNext: () => void;
};

export function DraftEditorStep({
  batch,
  packagesById,
  drafts,
  defaults,
  activeItemId,
  isSavingItem = false,
  onDefaultsChange,
  onDraftChange,
  onResetDraft,
  onSaveItem,
  onDeleteItem,
  onActiveItemChange,
  onCoverArtifact,
  onNext,
}: DraftEditorStepProps) {
  const items = batch.items ?? [];
  const activeItem = items.find((item) => item.id === activeItemId) ?? items[0] ?? null;
  const activeDraft = activeItem ? drafts[activeItem.id] ?? buildDraftFromItem(activeItem) : null;
  const selectedCount = Object.values(drafts).filter((draft) => draft.selected).length;

  function applyDefaultsToSelected() {
    items.forEach((item) => {
      const draft = drafts[item.id] ?? buildDraftFromItem(item);
      if (!draft.selected) return;
      const tags = parseTags(defaults.tagsInput);
      onDraftChange(item.id, {
        title: clampTitle(defaults.titlePrefix ? `${defaults.titlePrefix}${draft.title}` : draft.title, [item.platform]),
        description: defaults.description || draft.description,
        tagsInput: tags.join(" "),
        location: defaults.location,
        scheduleMode: defaults.scheduleMode,
        scheduledAt: defaults.scheduledAt,
      });
    });
  }

  return (
    <section className="grid gap-4">
      <details className="card grid gap-4" open>
        <summary className="cursor-pointer list-none text-lg font-semibold text-text-primary [&::-webkit-details-marker]:hidden">
          批次默认设置
        </summary>
        <div className="grid gap-4 border-t border-border/70 pt-4">
          <label>
            <span>平台 chips</span>
            <PlatformChips
              value={defaults.platforms}
              onChange={(platforms) => onDefaultsChange({ ...defaults, platforms })}
            />
          </label>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <label>
              <span>发布时间</span>
              <select
                value={defaults.scheduleMode}
                onChange={(event) => onDefaultsChange({ ...defaults, scheduleMode: event.target.value as BatchDefaults["scheduleMode"] })}
              >
                <option value="immediate">立即</option>
                <option value="scheduled">定时</option>
              </select>
            </label>
            <label>
              <span>定时时间</span>
              <input
                type="datetime-local"
                value={defaults.scheduledAt}
                disabled={defaults.scheduleMode !== "scheduled"}
                onChange={(event) => onDefaultsChange({ ...defaults, scheduledAt: event.target.value })}
              />
            </label>
            <label>
              <span>标签</span>
              <input value={defaults.tagsInput} onChange={(event) => onDefaultsChange({ ...defaults, tagsInput: event.target.value })} placeholder="#树影 #短视频" />
            </label>
            <label>
              <span>地区</span>
              <input value={defaults.location} onChange={(event) => onDefaultsChange({ ...defaults, location: event.target.value })} placeholder="不显示位置" />
            </label>
          </div>
          <div className="grid gap-3 md:grid-cols-[220px_minmax(0,1fr)_auto]">
            <label>
              <span>标题前缀</span>
              <input value={defaults.titlePrefix} onChange={(event) => onDefaultsChange({ ...defaults, titlePrefix: event.target.value })} placeholder="例如：树影案例 · " />
            </label>
            <label>
              <span>默认正文</span>
              <input value={defaults.description} onChange={(event) => onDefaultsChange({ ...defaults, description: event.target.value })} placeholder="为空则保留单条正文" />
            </label>
            <button className="btn-secondary self-end" type="button" onClick={applyDefaultsToSelected} disabled={selectedCount === 0}>
              <RotateCcw className="h-4 w-4" />
              应用默认到选中
            </button>
          </div>
        </div>
      </details>

      <div className="card grid gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-text-primary">逐条草稿编辑</h2>
            <p className="mt-1 text-sm text-text-secondary">可逐条调整标题、正文和发布时间。</p>
          </div>
          <button className="btn-primary" type="button" disabled={items.length === 0} onClick={onNext}>
            <Send className="h-4 w-4" />
            下一步发布
          </button>
        </div>

        <div className="flex flex-wrap gap-2">
          {items.map((item, index) => {
            const draft = drafts[item.id] ?? buildDraftFromItem(item);
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => onActiveItemChange(item.id)}
                className={`rounded-full border px-3 py-1.5 text-sm transition ${
                  activeItem?.id === item.id ? "border-accent/30 bg-accent/15 text-accent" : "border-border/75 bg-white/65 text-text-secondary"
                }`}
              >
                <span className="mr-1 font-mono text-xs">{index + 1}</span>
                {draft.title || item.title || item.id.slice(0, 8)}
                {!draft.selected ? <span className="ml-1 text-xs text-text-tertiary">· 跳过</span> : null}
              </button>
            );
          })}
        </div>

        {activeItem && activeDraft ? (
          <div className="grid gap-4">
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border/80 bg-white/60 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill status={activeItem.status} />
                <span className="badge bg-surface-hover text-text-secondary">{platformLabel(activeItem.platform)}</span>
                <span className="text-xs text-text-tertiary">{formatPublishMode(activeDraft.scheduleMode, activeDraft.scheduledAt)}</span>
              </div>
              <div className="flex flex-wrap gap-2">
                <button className="btn-secondary min-h-9 px-3" type="button" onClick={() => onDraftChange(activeItem.id, { selected: !activeDraft.selected })}>
                  {activeDraft.selected ? "跳过" : "恢复"}
                </button>
                <button className="btn-secondary min-h-9 px-3" type="button" onClick={() => onResetDraft(activeItem)}>
                  <RotateCcw className="h-4 w-4" />
                  重置编辑
                </button>
                <button className="btn-secondary min-h-9 px-3" type="button" disabled title="暂未开放">
                  <Wand2 className="h-4 w-4" />
                  重试生成
                </button>
                <button className="btn-secondary min-h-9 px-3 hover:border-status-error/30 hover:text-status-error" type="button" onClick={() => onDeleteItem(activeItem)}>
                  <Trash2 className="h-4 w-4" />
                  删除
                </button>
              </div>
            </div>

            <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_280px]">
              <div className="grid gap-4">
                <label>
                  <span className="flex items-center justify-between gap-3">
                    <span>标题</span>
                    <span className={titleLength(activeDraft.title) >= titleLimitForPlatforms([activeItem.platform]) ? "text-status-error" : "text-text-tertiary"}>
                      {titleLength(activeDraft.title)}/{titleLimitForPlatforms([activeItem.platform])}
                    </span>
                  </span>
                  <input
                    value={activeDraft.title}
                    onChange={(event) => onDraftChange(activeItem.id, { title: clampTitle(event.target.value, [activeItem.platform]) })}
                  />
                </label>
                <label>
                  <span>正文</span>
                  <textarea value={activeDraft.description} onChange={(event) => onDraftChange(activeItem.id, { description: event.target.value })} />
                </label>
                <div className="flex flex-wrap gap-2 text-xs">
                  <span className="rounded-full bg-white/70 px-2.5 py-1 text-text-secondary">{summarizePlatforms([activeItem.platform])}</span>
                  {activeDraft.tagsInput ? <span className="rounded-full bg-white/70 px-2.5 py-1 text-text-secondary">{activeDraft.tagsInput}</span> : null}
                  {activeDraft.location ? <span className="rounded-full bg-white/70 px-2.5 py-1 text-text-secondary">{activeDraft.location}</span> : null}
                </div>
                <button className="btn-primary w-fit" type="button" disabled={isSavingItem} onClick={() => onSaveItem(activeItem)}>
                  {isSavingItem ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                  保存单条
                </button>
              </div>
              <div className="rounded-2xl border border-border/80 bg-white/55 p-4 text-sm leading-6 text-text-secondary">
                <p className="font-semibold text-text-primary">发布后果</p>
                <p className="mt-2">保存只更新当前草稿。全自动发布仅生成内部发布记录，不会真正触达外部平台。</p>
                <p className="mt-2">删除会从当前批次移除此平台条目；不会删除成片或上传文件。</p>
              </div>
            </div>

            <CoverPanel
              item={activeItem}
              draft={activeDraft}
              publishPackage={packagesById.get(activeItem.publish_package_id)}
              onDraftChange={(patch) => onDraftChange(activeItem.id, patch)}
              onCoverArtifact={onCoverArtifact}
            />
          </div>
        ) : (
          <div className="rounded-2xl border border-dashed border-border/80 bg-white/50 p-8 text-center text-sm text-text-secondary">
            这个批次暂无草稿条目。
          </div>
        )}
      </div>
    </section>
  );
}
