import type { MediaAssetRecord } from "../../api/client";
import { formatRelativeTime } from "../../lib/format";
import { Modal } from "../ui/Modal";
import { FontFaceStyle } from "./FontFaceStyle";
import { fontAssetWeight } from "./fontFamilyModel";
import { annotationStatusLabels, fontFamilyName } from "./libraryModel";

type FontDetailModalProps = {
  asset: MediaAssetRecord | null;
  previewUrl: string | null;
  onClose: () => void;
};

export function FontDetailModal({ asset, previewUrl, onClose }: FontDetailModalProps) {
  const family = asset ? fontFamilyName(asset.id) : "";
  const weight = asset ? fontAssetWeight(asset) : 400;
  return (
    <Modal isOpen={Boolean(asset)} onClose={onClose} title="字体详情" size="lg">
      {asset ? (
        <div className="grid gap-4">
          {previewUrl ? (
            <FontFaceStyle assetId={asset.id} url={previewUrl} weight={weight} />
          ) : null}
          <div>
            <h3 className="text-xl font-semibold text-text-primary">{asset.title}</h3>
            <p className="mt-1 font-mono text-xs text-text-tertiary">{asset.id}</p>
          </div>
          <div className="rounded-[24px] border border-border/80 bg-white/65 p-5">
            <p
              className="text-4xl leading-snug text-text-primary"
              style={previewUrl ? { fontFamily: family, fontWeight: weight } : undefined}
            >
              字幕字体预览
            </p>
            <p
              className="mt-3 text-lg text-text-secondary"
              style={previewUrl ? { fontFamily: family, fontWeight: weight } : undefined}
            >
              适用于口播、产品讲解、直播切片与封面标题。
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            <AnnotationMetric label="标注状态" value={annotationStatusLabels[asset.annotation_status]} />
            <AnnotationMetric label="可用性" value={asset.usable ? "可用" : "不可用"} />
            <AnnotationMetric label="更新时间" value={formatRelativeTime(asset.updated_at ?? asset.created_at)} />
          </div>
        </div>
      ) : null}
    </Modal>
  );
}

function AnnotationMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-border/80 bg-white/65 p-4">
      <p className="text-xs text-text-secondary">{label}</p>
      <p className="mt-2 text-lg font-semibold tabular-nums text-text-primary">{value}</p>
    </div>
  );
}
