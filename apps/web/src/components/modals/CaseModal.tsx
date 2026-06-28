import { Loader2, Plus } from "lucide-react";
import { useState } from "react";
import type { CreateCaseRequest } from "../../api/client";
import { Modal } from "../ui/Modal";
import { ErrorState } from "../ui/State";

// List profile fields are edited as one-per-line text. Newlines and commas both split.
export function parseList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

export function joinList(value: readonly string[] | null | undefined): string {
  return (value ?? []).join("\n");
}

// Slimmed to draft creation only: capture a name, then route into CaseProfilePage
// to complete the full profile. Editing existing cases happens on the profile page.
export function CaseModal({
  isSaving,
  error,
  onClose,
  onCreate,
}: {
  isSaving: boolean;
  error?: unknown;
  onClose: () => void;
  onCreate: (payload: CreateCaseRequest) => void;
}) {
  const [name, setName] = useState("");

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    onCreate({ name: trimmed });
  }

  return (
    <Modal isOpen title="新建案例" onClose={onClose} size="md">
      <form className="formGrid" onSubmit={handleSubmit}>
        <p className="text-sm text-text-secondary">
          先填一个名称建草稿，创建后进入「案例画像」补全行业、卖点与人设等信息。
        </p>
        <label>
          <span>案例名称</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="例如：无忧快喷"
            autoFocus
            required
          />
        </label>

        {error ? <ErrorState error={error} /> : null}
        <div className="formActions">
          <button className="ghostButton" type="button" onClick={onClose} disabled={isSaving}>
            取消
          </button>
          <button className="primaryButton" type="submit" disabled={isSaving || !name.trim()}>
            {isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus size={16} />}
            <span>{isSaving ? "创建中" : "创建草稿"}</span>
          </button>
        </div>
      </form>
    </Modal>
  );
}
