import { CheckSquare, Loader2, RefreshCw, Sparkles, Trash2 } from "lucide-react";

type TemplateBatchActionBarProps = {
  selectedCount: number;
  totalCount: number;
  isStabilizing: boolean;
  isAnnotating: boolean;
  isDeleting: boolean;
  onSelectAll: () => void;
  onStabilize: () => void;
  onAnnotate: () => void;
  onDelete: () => void;
  onClear: () => void;
};

export function TemplateBatchActionBar({
  selectedCount,
  totalCount,
  isStabilizing,
  isAnnotating,
  isDeleting,
  onSelectAll,
  onStabilize,
  onAnnotate,
  onDelete,
  onClear,
}: TemplateBatchActionBarProps) {
  const allSelected = totalCount > 0 && selectedCount >= totalCount;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border/80 bg-white/65 p-3">
      <span className="text-sm text-text-secondary">已选择 {selectedCount} / {totalCount} 个素材</span>
      <div className="flex flex-wrap gap-2">
        <button className="btn-secondary min-h-9 px-3" type="button" disabled={totalCount === 0 || allSelected} onClick={onSelectAll}>
          <CheckSquare className="h-4 w-4" />
          <span>全选当前</span>
        </button>
        <button className="btn-secondary min-h-9 px-3" type="button" disabled={selectedCount === 0 || isAnnotating} onClick={onAnnotate}>
          {isAnnotating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
          <span>{isAnnotating ? "标注中" : "批量标注"}</span>
        </button>
        <button className="btn-secondary min-h-9 px-3" type="button" disabled={selectedCount === 0 || isStabilizing} onClick={onStabilize}>
          {isStabilizing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          <span>{isStabilizing ? "增稳中" : "批量增稳"}</span>
        </button>
        <button
          className="btn-secondary min-h-9 px-3 text-status-error hover:border-status-error/30"
          type="button"
          disabled={selectedCount === 0 || isDeleting}
          onClick={onDelete}
        >
          {isDeleting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
          <span>{isDeleting ? "删除中" : "批量删除"}</span>
        </button>
        <button className="btn-ghost min-h-9 px-3" type="button" onClick={onClear}>
          清空选择
        </button>
      </div>
    </div>
  );
}
