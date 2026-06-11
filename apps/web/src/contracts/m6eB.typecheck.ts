import { api, type PromptBindingView, type PromptTemplateView, type PromptVersionView } from "../api/client";
import PromptManagementPage from "../pages/ops/PromptManagementPage";
import { routes } from "../routes";

async function assertPromptManagementContracts(templateId: string, versionId: string, bindingId: string) {
  const list: { items: PromptTemplateView[] } = await api.prompts.list({ limit: 10 });
  const versions: { items: PromptVersionView[] } = await api.prompts.versions(templateId);
  const binding: PromptBindingView = await api.prompts.createBinding({
    prompt_template_id: templateId,
    prompt_version_id: versionId,
    case_id: "case_demo",
    node_id: "ResolveCreativeIntent",
    priority: 10,
  });
  await api.prompts.approveVersion(templateId, versionId, { reason: "typecheck" });
  await api.prompts.publishVersion(templateId, versionId, { reason: "typecheck" });
  await api.prompts.rollback(templateId, { target_version_id: versionId, reason: "typecheck" });
  await api.prompts.patchBinding(bindingId || binding.binding.id, { enabled: false });
  return [routes.promptOps(), list.items, versions.items, PromptManagementPage];
}

void assertPromptManagementContracts("prompt_creative_intent", "prompt_creative_intent_v1", "prompt_binding_global_intent");
