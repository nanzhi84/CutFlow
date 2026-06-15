import { api, type PublishAttemptDetail, type PublishBatch, type PublishBatchItem, type PublishPackage } from "../api/client";
import { buildDraftFromItem, clampTitle, titleLimitForPlatforms } from "../components/publish/publishModel";
import { useUpload } from "../hooks/useUpload";

async function assertR4ApiSurface(file: File) {
  const listedPackages = await api.publishing.packages({ limit: 20 });
  const packageFromFinishedVideo = await api.publishing.createPackage({
    source_finished_video_id: "fv_123",
    title: "成片标题",
    description: "发布正文",
  });
  const uploadedCover = await useUpload().uploadFile({ file, kind: "cover_template" });
  const patchedPackage = await api.publishing.patchPackage(packageFromFinishedVideo.id, {
    cover_artifact_id: uploadedCover.artifact.artifact_id,
  });
  const clearedCover = await api.publishing.patchPackage(packageFromFinishedVideo.id, {
    cover_artifact_id: null,
  });
  const batch = await api.publishing.createBatch({
    publish_package_ids: [packageFromFinishedVideo.id],
    platform_targets: ["xiaovmao"],
  });
  const listedBatches = await api.publishing.batches({ limit: 20 });
  const detail = await api.publishing.batch(batch.id);
  const item = detail.items?.[0];
  if (!item) return;
  const patchedItem = await api.publishing.patchItem(item.id, {
    title: clampTitle("一个很长很长的发布标题", [item.platform]),
    description: "正文",
    selected: true,
  });
  const submitted = await api.publishing.submitBatch(detail.id, {
    dry_run: true,
    simulate_publish_failure: false,
    mode: "immediate",
  });
  const attempts = await api.publishing.attempts(submitted.id, { limit: 20 });
  const retried = await api.publishing.retryItem(submitted.id, patchedItem.id);
  const deletedItem = await api.publishing.deleteItem(patchedItem.id);
  const deleted = await api.publishing.deleteBatch(batch.id);

  listedPackages.items[0] satisfies PublishPackage | undefined;
  patchedPackage.cover_artifact?.artifact_id satisfies string | undefined;
  (clearedCover.cover_artifact === null) satisfies boolean;
  listedBatches.items[0] satisfies PublishBatch | undefined;
  attempts.items[0]?.status satisfies "created" | "manual_review_ready" | "scheduled" | "published" | "failed" | undefined;
  retried satisfies PublishBatchItem;
  deletedItem.ok satisfies boolean;
  deleted.ok satisfies boolean;
}

function assertR4DraftModel(item: PublishBatchItem, attemptDetail: PublishAttemptDetail) {
  const draft = buildDraftFromItem(item);
  titleLimitForPlatforms(["douyin", "xiaovmao"]) satisfies number;
  draft.title satisfies string;
  draft.platforms[0] satisfies string | undefined;
  attemptDetail.attempt.adapter_id satisfies string;
}

void assertR4ApiSurface;
void assertR4DraftModel;
