import { api } from "../api/client";
import { useUpload } from "../hooks/useUpload";

async function assertR3ApiSurface(file: File) {
  const voices = await api.voices.list({ source: "cloned", enabled: true, limit: 12 });
  const cloned = await api.voices.clone({ display_name: "样例音色", reference_upload_session_id: "upload_123" });
  const preview = await api.voices.preview(cloned.id, { text: "这是音色试听文本。" });
  const patched = await api.voices.patch(cloned.id, { enabled: false });
  const deleted = await api.voices.delete(patched.id);

  const prepared = await api.uploads.prepare({
    kind: "voice_reference",
    filename: file.name,
    content_type: file.type || "application/octet-stream",
    size_bytes: file.size,
    multipart: false,
    stabilize: false,
  });
  const uploaded = await api.uploads.uploadFile(prepared.id, file);
  const completed = await api.uploads.complete({ upload_session_id: uploaded.id, size_bytes: file.size });

  const assets = await api.mediaAssets.list({ kind: "broll", annotation_status: "pending", limit: 24 });
  const created = await api.mediaAssets.create({
    upload_session_id: completed.upload_session.id,
    title: "样例素材",
    kind: "broll",
    tags: ["demo"],
  });
  const detail = await api.mediaAssets.detail(created.id);
  const signed = await api.mediaAssets.previewUrl(detail.asset.id);

  const editor = await api.annotations.get(created.id);
  const updated = await api.annotations.patch(created.id, {
    etag: editor.etag,
    patch: { operations: [{ path: "projection.quality_status", value: "usable" }] },
  });
  const rerun = await api.annotations.rerun(created.id, { force: true });

  voices.items[0]?.display_name satisfies string | undefined;
  preview.audio_artifact.artifact_id satisfies string;
  deleted.ok satisfies boolean;
  assets.items[0]?.asset.annotation_status satisfies "pending" | "annotated" | "annotation_failed" | undefined;
  signed.url satisfies string;
  updated.etag satisfies string;
  rerun.status satisfies "queued" | "running" | "completed" | "failed";
}

function assertUploadHook(file: File) {
  const upload = useUpload();
  upload.status satisfies "idle" | "preparing" | "uploading" | "completing" | "completed" | "failed";
  upload.progress satisfies number;
  upload.uploadFile({ file, kind: "voice_reference" }) satisfies Promise<unknown>;
  upload.reset() satisfies void;
}

void assertR3ApiSurface;
void assertUploadHook;
