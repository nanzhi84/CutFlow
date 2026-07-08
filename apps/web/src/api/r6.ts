import { createIdempotencyKey, fetchJson } from "./client";
import type { JsonRequest, JsonResponse, QueryParams } from "./client";
import type { components, operations } from "./schema";

const enc = encodeURIComponent;

export type ScorePrediction = components["schemas"]["ScorePrediction"];
export type JianyingDraftResult = components["schemas"]["JianyingDraftPackageArtifact"];
export type ProviderBalanceReport = components["schemas"]["ProviderBalanceReport"];
export type ProviderBalanceItem = components["schemas"]["ProviderBalanceItem"];
export type ProviderUsageMetricsReport = components["schemas"]["ProviderUsageMetricsReport"];
export type ProviderUsageMetricsItem = components["schemas"]["ProviderUsageMetricsItem"];

export const caseAgentApi = {
  generateScript: (
    caseId: string,
    payload: JsonRequest<operations["generate_script_with_memory_api_cases__case_id__scripts_generate_with_memory_post"]>,
  ) =>
    fetchJson<JsonResponse<operations["generate_script_with_memory_api_cases__case_id__scripts_generate_with_memory_post"]>>(
      `/api/cases/${enc(caseId)}/scripts/generate-with-memory`,
      { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("script_memory") },
    ),
  adoptScriptDraft: (
    caseId: string,
    draftId: string,
    payload: JsonRequest<operations["adopt_script_draft_api_cases__case_id__agent_drafts__draft_id__adopt_post"]>,
  ) =>
    fetchJson<JsonResponse<operations["adopt_script_draft_api_cases__case_id__agent_drafts__draft_id__adopt_post"]>>(
      `/api/cases/${enc(caseId)}/agent/drafts/${enc(draftId)}/adopt`,
      { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("script_draft_adopt") },
    ),
};

export const caseRubricApi = {
  rubric: (caseId: string) =>
    fetchJson<JsonResponse<operations["get_rubric_api_cases__case_id__rubric_get"]>>(
      `/api/cases/${enc(caseId)}/rubric`,
    ),
  calibration: (caseId: string) =>
    fetchJson<JsonResponse<operations["calibration_api_cases__case_id__rubric_calibration_get"]>>(
      `/api/cases/${enc(caseId)}/rubric/calibration`,
    ),
  bumpProposal: (caseId: string) =>
    fetchJson<JsonResponse<operations["bump_proposal_api_cases__case_id__rubric_bump_proposal_get"]>>(
      `/api/cases/${enc(caseId)}/rubric/bump-proposal`,
    ),
  acceptBump: (caseId: string, proposalId: string) =>
    fetchJson<JsonResponse<operations["accept_bump_api_cases__case_id__rubric_bump_proposal__proposal_id__accept_post"]>>(
      `/api/cases/${enc(caseId)}/rubric/bump-proposal/${enc(proposalId)}/accept`,
      { method: "POST", idempotencyKey: createIdempotencyKey("rubric_bump_accept") },
    ),
  rejectBump: (
    caseId: string,
    proposalId: string,
    payload: JsonRequest<operations["reject_bump_api_cases__case_id__rubric_bump_proposal__proposal_id__reject_post"]>,
  ) =>
    fetchJson<JsonResponse<operations["reject_bump_api_cases__case_id__rubric_bump_proposal__proposal_id__reject_post"]>>(
      `/api/cases/${enc(caseId)}/rubric/bump-proposal/${enc(proposalId)}/reject`,
      { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("rubric_bump_reject") },
    ),
  predictions: (caseId: string, query: QueryParams<operations["predictions_api_cases__case_id__predictions_get"]> = {}) =>
    fetchJson<JsonResponse<operations["predictions_api_cases__case_id__predictions_get"]>>(
      `/api/cases/${enc(caseId)}/predictions`,
      { query },
    ),
  pendingRetro: (caseId: string) =>
    fetchJson<JsonResponse<operations["pending_retro_api_cases__case_id__pending_retro_get"]>>(
      `/api/cases/${enc(caseId)}/pending-retro`,
    ),
};

export const editorHandoffApi = {
  createJianyingDraft: (
    videoId: string,
    payload: JsonRequest<operations["jianying_draft_api_finished_videos__id__jianying_draft_post"]>,
  ) =>
    fetchJson<JsonResponse<operations["jianying_draft_api_finished_videos__id__jianying_draft_post"]>>(
      `/api/finished-videos/${enc(videoId)}/jianying-draft`,
      { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("jianying_draft") },
    ),
  latestJianyingDraft: (videoId: string) =>
    fetchJson<JsonResponse<operations["latest_jianying_draft_api_finished_videos__id__jianying_draft_latest_get"]>>(
      `/api/finished-videos/${enc(videoId)}/jianying-draft/latest`,
    ),
};

export const providerObservabilityApi = {
  providers: {
    balances: (query: QueryParams<operations["provider_balances_api_providers_balances_get"]> = {}) =>
      fetchJson<JsonResponse<operations["provider_balances_api_providers_balances_get"]>>(
        "/api/providers/balances",
        { query },
      ),
    refreshBalances: () =>
      fetchJson<JsonResponse<operations["refresh_provider_balances_api_providers_balances_refresh_post"]>>(
        "/api/providers/balances/refresh",
        { method: "POST", idempotencyKey: createIdempotencyKey("provider_balance_refresh") },
      ),
  },
  ops: {
    providerUsageMetrics: (
      query: QueryParams<operations["provider_usage_metrics_api_ops_provider_usage_metrics_get"]> = {},
    ) =>
      fetchJson<JsonResponse<operations["provider_usage_metrics_api_ops_provider_usage_metrics_get"]>>(
        "/api/ops/provider-usage-metrics",
        { query },
      ),
  },
};
