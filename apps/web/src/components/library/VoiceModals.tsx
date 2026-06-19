import { CheckCircle2, Loader2, Plus, Wand2 } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type VoiceProfile } from "../../api/client";
import { useUpload } from "../../hooks/useUpload";
import { DropZone } from "../ui/DropZone";
import { Modal } from "../ui/Modal";
import { useToast } from "../ui/Toast";
import { emptyVoiceDraft, uploadStageLabel, VOICE_UPLOAD_ACCEPT } from "./libraryModel";

export function CloneVoiceModal({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const upload = useUpload();
  const [name, setName] = useState("");
  const [providerProfileId, setProviderProfileId] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [error, setError] = useState<string | null>(null);

  const cloneMutation = useMutation({
    mutationFn: async () => {
      const file = files[0];
      if (!name.trim()) throw new Error("请输入音色名称");
      if (!file) throw new Error("请上传一段参考音频");
      const result = await upload.uploadFile({ file, kind: "voice_reference" });
      return api.voices.clone({
        display_name: name.trim(),
        reference_upload_session_id: result.upload_session.id,
        provider_profile_id: providerProfileId.trim() || null,
      });
    },
    onSuccess: async (voice) => {
      await queryClient.invalidateQueries({ queryKey: ["library", "voices"] });
      await queryClient.invalidateQueries({ queryKey: ["voices"] });
      toast.success("音色克隆已提交", `新音色：${voice.display_name}`);
      setName("");
      setProviderProfileId("");
      setFiles([]);
      upload.reset();
      onClose();
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : "音色克隆失败";
      setError(message);
      toast.error("音色克隆失败", message);
    },
  });

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="克隆音色" size="lg">
      <form
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          cloneMutation.mutate();
        }}
      >
        <div className="grid gap-3 md:grid-cols-2">
          <label>
            <span>音色名称</span>
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：温柔讲解女声" />
          </label>
          <label>
            <span>Provider 配置 ID（可选）</span>
            <input value={providerProfileId} onChange={(event) => setProviderProfileId(event.target.value)} placeholder="留空使用默认配置" />
          </label>
        </div>
        <DropZone accept={VOICE_UPLOAD_ACCEPT} maxSize={80} multiple={false} onFilesDrop={(nextFiles) => setFiles(nextFiles)} label="上传参考音频" />
        {upload.status !== "idle" ? (
          <div className="rounded-2xl border border-border/80 bg-white/65 p-3">
            <div className="flex items-center justify-between gap-3 text-sm text-text-secondary">
              <span>上传阶段：{uploadStageLabel(upload.status)}</span>
              <span>{upload.progress}%</span>
            </div>
            <div className="mt-2 h-2 overflow-hidden rounded-full bg-border/70">
              <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${upload.progress}%` }} />
            </div>
          </div>
        ) : null}
        {error ? <p className="text-sm text-status-error">{error}</p> : null}
        <div className="flex justify-end gap-3 border-t border-border/70 pt-4">
          <button className="btn-secondary" type="button" onClick={onClose} disabled={cloneMutation.isPending}>
            取消
          </button>
          <button className="btn-primary" type="submit" disabled={cloneMutation.isPending || !name.trim() || files.length === 0}>
            {cloneMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
            <span>{cloneMutation.isPending ? "提交中" : "创建克隆音色"}</span>
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function DesignVoiceModal({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState(emptyVoiceDraft);
  const [error, setError] = useState<string | null>(null);

  const designMutation = useMutation({
    mutationFn: () => {
      if (!draft.name.trim()) throw new Error("请输入音色名称");
      if (!draft.prompt.trim()) throw new Error("请输入音色描述");
      return api.voices.design({
        display_name: draft.name.trim(),
        prompt: draft.prompt.trim(),
        provider_profile_id: draft.providerProfileId.trim() || null,
      });
    },
    onSuccess: async (voice) => {
      await queryClient.invalidateQueries({ queryKey: ["library", "voices"] });
      await queryClient.invalidateQueries({ queryKey: ["voices"] });
      toast.success("音色设计已提交", `新音色：${voice.display_name}`);
      setDraft(emptyVoiceDraft());
      onClose();
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : "音色设计失败";
      setError(message);
      toast.error("音色设计失败", message);
    },
  });

  const samples = ["温柔甜美的年轻女性声音，适合讲故事", "成熟稳重的男性旁白，适合产品讲解", "活泼开朗的少女声音，适合直播口播"];

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="设计音色" size="lg">
      <form
        className="grid gap-4"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          designMutation.mutate();
        }}
      >
        <div className="rounded-2xl border border-accent/20 bg-accent/10 p-3 text-sm text-accent">
          通过文字描述提交音色设计请求；音频质量取决于所选供应商能力。
        </div>
        <label>
          <span>音色名称</span>
          <input value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} placeholder="例如：沉稳产品旁白" />
        </label>
        <label>
          <span>音色描述</span>
          <textarea value={draft.prompt} onChange={(event) => setDraft((current) => ({ ...current, prompt: event.target.value }))} placeholder="描述声音年龄、性别、语气、场景和节奏。" />
        </label>
        <div className="flex flex-wrap gap-2">
          {samples.map((sample) => (
            <button key={sample} type="button" className="badge bg-white/70 text-text-secondary" onClick={() => setDraft((current) => ({ ...current, prompt: sample }))}>
              {sample}
            </button>
          ))}
        </div>
        <label>
          <span>Provider 配置 ID（可选）</span>
          <input value={draft.providerProfileId} onChange={(event) => setDraft((current) => ({ ...current, providerProfileId: event.target.value }))} placeholder="留空使用默认配置" />
        </label>
        {error ? <p className="text-sm text-status-error">{error}</p> : null}
        <div className="flex justify-end gap-3 border-t border-border/70 pt-4">
          <button className="btn-secondary" type="button" onClick={onClose} disabled={designMutation.isPending}>
            取消
          </button>
          <button className="btn-primary" type="submit" disabled={designMutation.isPending || !draft.name.trim() || !draft.prompt.trim()}>
            {designMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
            <span>{designMutation.isPending ? "提交中" : "提交设计"}</span>
          </button>
        </div>
      </form>
    </Modal>
  );
}

type EditVoiceModalProps = {
  voice: VoiceProfile;
  isOpen: boolean;
  isLoading: boolean;
  onClose: () => void;
  onSubmit: (displayName: string, enabled: boolean) => void;
};

export function EditVoiceModal({ voice, isOpen, isLoading, onClose, onSubmit }: EditVoiceModalProps) {
  const [displayName, setDisplayName] = useState(voice.display_name);
  const [enabled, setEnabled] = useState(voice.enabled);

  useEffect(() => {
    setDisplayName(voice.display_name);
    setEnabled(voice.enabled);
  }, [voice]);

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="编辑音色" size="md">
      <form
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit(displayName.trim(), enabled);
        }}
      >
        <label>
          <span>音色名称</span>
          <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} disabled={isLoading} />
        </label>
        <label className="flex cursor-pointer grid-cols-[auto_minmax(0,1fr)] items-center gap-3 rounded-2xl border border-border/80 bg-white/65 p-3">
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} disabled={isLoading} />
          <span>
            <span className="block text-sm font-semibold text-text-primary">在创作页启用</span>
            <span className="mt-1 block text-xs font-normal text-text-secondary">停用后不会出现在新任务可选音色中。</span>
          </span>
        </label>
        <div className="rounded-2xl border border-status-warning/20 bg-status-warning/10 p-3 text-xs leading-5 text-status-warning">
          修改名称会影响后续选择展示；停用只影响新建任务，不改写历史任务记录。
        </div>
        <div className="flex justify-end gap-3 border-t border-border/70 pt-4">
          <button className="btn-secondary" type="button" onClick={onClose} disabled={isLoading}>
            取消
          </button>
          <button className="btn-primary" type="submit" disabled={isLoading || !displayName.trim()}>
            {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
            <span>{isLoading ? "保存中" : "保存"}</span>
          </button>
        </div>
      </form>
    </Modal>
  );
}
