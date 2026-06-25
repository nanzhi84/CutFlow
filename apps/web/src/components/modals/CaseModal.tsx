import { Loader2, Plus } from "lucide-react";
import { useState } from "react";
import type { CreateCaseRequest, PatchCaseRequest } from "../../api/client";
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

// Profile payload builders. The full case editor lives in CaseProfilePage; these
// helpers stay here so the locked case-edit contract (CreateCaseRequest /
// PatchCaseRequest field coverage) keeps a single typed source of truth.
type CaseProfileFields = {
  name: string;
  description: string;
  industry: string;
  product: string;
  target_audience: string;
  key_selling_points: string;
  ip_persona: string;
  brand_voice: string;
  strategy_tags: string;
  brand_keywords: string;
  competitor_names: string;
};

export function buildCreatePayload(form: CaseProfileFields): CreateCaseRequest {
  return {
    name: form.name.trim(),
    description: form.description.trim() || null,
    industry: form.industry.trim() || null,
    product: form.product.trim() || null,
    target_audience: form.target_audience.trim() || null,
    key_selling_points: parseList(form.key_selling_points),
    ip_persona: form.ip_persona.trim() || null,
    brand_voice: form.brand_voice.trim() || null,
    strategy_tags: parseList(form.strategy_tags),
    brand_keywords: parseList(form.brand_keywords),
    competitor_names: parseList(form.competitor_names),
  };
}

export function buildPatchPayload(form: CaseProfileFields): PatchCaseRequest {
  return {
    name: form.name.trim(),
    description: form.description.trim() || null,
    industry: form.industry.trim() || null,
    product: form.product.trim() || null,
    target_audience: form.target_audience.trim() || null,
    key_selling_points: parseList(form.key_selling_points),
    ip_persona: form.ip_persona.trim() || null,
    brand_voice: form.brand_voice.trim() || null,
    strategy_tags: parseList(form.strategy_tags),
    brand_keywords: parseList(form.brand_keywords),
    competitor_names: parseList(form.competitor_names),
  };
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
