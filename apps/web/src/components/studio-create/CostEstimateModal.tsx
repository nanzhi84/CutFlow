import { AlertTriangle, Calculator } from "lucide-react";
import type { DigitalHumanVideoCostEstimateResponse } from "../../api/client";
import { Modal } from "../ui/Modal";

type EstimateLine = DigitalHumanVideoCostEstimateResponse["tts"];

function formatMoney(line: EstimateLine) {
  if (line.unpriced) return "未定价";
  return `${line.estimated_cost.currency} ${line.estimated_cost.amount}`;
}

function formatQuantity(line: EstimateLine) {
  const value = Number(line.quantity);
  if (line.unit === "input_token") return `${value.toLocaleString("zh-CN")} 字符`;
  if (line.unit === "media_second") return `${value.toLocaleString("zh-CN")} 秒`;
  return `${value.toLocaleString("zh-CN")} ${line.unit}`;
}

function EstimateRow({ label, line }: { label: string; line: EstimateLine }) {
  return (
    <div className="grid grid-cols-[1fr_auto] items-center gap-3 rounded-2xl border border-border/70 bg-white/65 p-4">
      <div className="min-w-0">
        <p className="font-medium text-text-primary">{label}</p>
        <p className="mt-1 text-xs text-text-tertiary">{formatQuantity(line)}</p>
      </div>
      <div className={`text-right font-mono text-lg font-semibold ${line.unpriced ? "text-status-warning" : "text-text-primary"}`}>
        {formatMoney(line)}
      </div>
    </div>
  );
}

export function CostEstimateModal({
  isOpen,
  estimate,
  onClose,
}: {
  isOpen: boolean;
  estimate: DigitalHumanVideoCostEstimateResponse | null;
  onClose: () => void;
}) {
  return (
    <Modal isOpen={isOpen} onClose={onClose} title="预估成本" size="md">
      {estimate ? (
        <div className="grid gap-4">
          <div className="flex items-center gap-3 rounded-2xl border border-border/70 bg-white/65 p-4">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent/10 text-accent">
              <Calculator className="h-5 w-5" />
            </span>
            <div>
              <p className="font-semibold text-text-primary">{estimate.tts_characters.toLocaleString("zh-CN")} 字符</p>
              <p className="text-sm text-text-secondary">约 {estimate.estimated_video_seconds.toLocaleString("zh-CN")} 秒视频</p>
            </div>
          </div>
          <EstimateRow label="TTS" line={estimate.tts} />
          <EstimateRow label="视频" line={estimate.video} />
          <EstimateRow label="总成本" line={estimate.total} />
          {estimate.total.unpriced ? (
            <div className="flex items-start gap-2 rounded-2xl border border-status-warning/30 bg-status-warning/10 p-3 text-sm text-status-warning">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>存在未定价项，金额仅汇总已定价部分。</span>
            </div>
          ) : null}
          <div className="flex justify-end">
            <button className="btn-primary" type="button" onClick={onClose}>
              关闭
            </button>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
