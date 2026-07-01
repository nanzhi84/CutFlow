import type { components, operations } from "./schema";
import {
  isRealAssetCard,
  isRealCase,
  isRealPriceCatalog,
  isRealPriceItem,
  isRealProviderProfile,
  isRealVoice,
} from "./realData";

export type JsonRequest<Operation> = Operation extends {
  requestBody: { content: { "application/json": infer Body } };
}
  ? Body
  : never;

export type JsonResponse<Operation> = Operation extends {
  responses: {
    200: { content: { "application/json": infer Body } };
  };
}
  ? Body
  : Operation extends {
        responses: {
          201: { content: { "application/json": infer Body } };
        };
      }
    ? Body
    : Operation extends {
          responses: {
            202: { content: { "application/json": infer Body } };
          };
        }
      ? Body
      : never;

export type QueryParams<Operation> = Operation extends {
  parameters: { query?: infer Query };
}
  ? Query
  : never;

export type ApiError = Error & {
  code?: components["schemas"]["ErrorCode"] | string;
  requestId?: string;
  status: number;
  details?: unknown;
};
export type MaterialUsageRankingReport = components["schemas"]["MaterialUsageRankingReport"];
export type MaterialUsageRankingItem = components["schemas"]["MaterialUsageRankingItem"];

type FetchOptions = Omit<RequestInit, "body"> & {
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  idempotencyKey?: string;
};

const JSON_TYPE = "application/json";

export function createIdempotencyKey(prefix = "request") {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}_${crypto.randomUUID()}`;
  }
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function buildUrl(path: string, query?: FetchOptions["query"]) {
  const search = new URLSearchParams();
  Object.entries(query ?? {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== "") {
      search.set(key, String(value));
    }
  });
  const suffix = search.toString();
  return suffix ? `${path}?${suffix}` : path;
}

function readErrorMessage(code: string | undefined, fallback: string) {
  if (code === "auth.invalid_credentials") {
    return "邮箱、用户名或密码不正确";
  }
  if (code === "auth.unauthorized") {
    return "登录已失效，请重新登录";
  }
  if (code === "auth.forbidden") {
    return "当前账号没有权限执行此操作";
  }
  return fallback;
}

function getObject(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

async function parseError(response: Response): Promise<ApiError> {
  const payload = getObject(await response.json().catch(() => undefined));
  const rawError = getObject(payload?.error);
  const code = typeof rawError?.code === "string" ? rawError.code : undefined;
  const message =
    typeof rawError?.message === "string"
      ? readErrorMessage(code, rawError.message)
      : readErrorMessage(code, `请求失败（${response.status}）`);
  const requestId =
    (typeof rawError?.request_id === "string" && rawError.request_id) ||
    (typeof payload?.request_id === "string" && payload.request_id) ||
    response.headers.get("X-Request-Id") ||
    undefined;
  const error = new Error(message) as ApiError;
  error.status = response.status;
  error.code = code;
  error.requestId = requestId;
  error.details = rawError?.details ?? payload;
  return error;
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof Error && "status" in error;
}

export async function fetchJson<T>(path: string, options: FetchOptions = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", JSON_TYPE);
  if (options.body !== undefined && !(options.body instanceof FormData)) {
    headers.set("Content-Type", JSON_TYPE);
  }
  if (options.idempotencyKey) {
    headers.set("Idempotency-Key", options.idempotencyKey);
  }

  const response = await fetch(buildUrl(path, options.query), {
    ...options,
    credentials: "include",
    headers,
    body:
      options.body === undefined
        ? undefined
        : options.body instanceof FormData
          ? options.body
          : JSON.stringify(options.body),
  });

  if (!response.ok) {
    if (response.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("cutagent:unauthorized"));
    }
    throw await parseError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return response.json() as Promise<T>;
}

const enc = encodeURIComponent;

/** SHA-256 hex of a file. Browser-direct uploads send it so the API can verify
 * the object server-side (it never sees the bytes during upload). */
export async function sha256Hex(file: File): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/** PUT a file directly to a presigned OSS URL: raw body, the signed Content-Type,
 * NO cookies / NO FormData. Reports progress via the XHR upload events. */
export function putToOss(
  url: string,
  file: File,
  contentType: string,
  onProgress?: (loaded: number, total: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("Content-Type", contentType);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded, event.total);
    };
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300
        ? resolve()
        : reject(new Error(`对象存储上传失败（HTTP ${xhr.status}）`));
    xhr.onerror = () => reject(new Error("对象存储上传网络错误"));
    xhr.send(file);
  });
}

export const api = {
  auth: {
    register: (payload: JsonRequest<operations["register_api_auth_register_post"]>) =>
      fetchJson<JsonResponse<operations["register_api_auth_register_post"]>>("/api/auth/register", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("register"),
      }),
    login: (payload: JsonRequest<operations["login_api_auth_login_post"]>) =>
      fetchJson<JsonResponse<operations["login_api_auth_login_post"]>>("/api/auth/login", {
        method: "POST",
        body: payload,
      }),
    session: () =>
      fetchJson<JsonResponse<operations["session_api_auth_session_get"]>>("/api/auth/session"),
    logout: () =>
      fetchJson<JsonResponse<operations["logout_api_auth_logout_post"]>>("/api/auth/logout", {
        method: "POST",
        idempotencyKey: createIdempotencyKey("logout"),
      }),
    updateMe: (payload: JsonRequest<operations["update_me_api_auth_me_patch"]>) =>
      fetchJson<JsonResponse<operations["update_me_api_auth_me_patch"]>>("/api/auth/me", {
        method: "PATCH",
        body: payload,
        idempotencyKey: createIdempotencyKey("auth_me"),
      }),
    changePassword: (payload: JsonRequest<operations["change_password_api_auth_me_change_password_post"]>) =>
      fetchJson<JsonResponse<operations["change_password_api_auth_me_change_password_post"]>>(
        "/api/auth/me/change-password",
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("change_password") },
      ),
    users: (query: QueryParams<operations["list_users_api_auth_users_get"]> = {}) =>
      fetchJson<JsonResponse<operations["list_users_api_auth_users_get"]>>("/api/auth/users", { query }),
    createUser: (payload: JsonRequest<operations["create_user_api_auth_users_post"]>) =>
      fetchJson<JsonResponse<operations["create_user_api_auth_users_post"]>>("/api/auth/users", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("auth_user"),
      }),
    patchUser: (userId: string, payload: JsonRequest<operations["patch_user_api_auth_users__user_id__patch"]>) =>
      fetchJson<JsonResponse<operations["patch_user_api_auth_users__user_id__patch"]>>(
        `/api/auth/users/${enc(userId)}`,
        { method: "PATCH", body: payload, idempotencyKey: createIdempotencyKey("auth_user_patch") },
      ),
    registrationCodes: (query: QueryParams<operations["registration_codes_api_auth_registration_codes_get"]> = {}) =>
      fetchJson<JsonResponse<operations["registration_codes_api_auth_registration_codes_get"]>>(
        "/api/auth/registration-codes",
        { query },
      ),
    createRegistrationCode: (
      payload: JsonRequest<operations["create_registration_code_api_auth_registration_codes_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["create_registration_code_api_auth_registration_codes_post"]>>(
        "/api/auth/registration-codes",
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("registration_code") },
      ),
    patchRegistrationCode: (
      codeId: string,
      payload: JsonRequest<operations["patch_registration_code_api_auth_registration_codes__code_id__patch"]>,
    ) =>
      fetchJson<JsonResponse<operations["patch_registration_code_api_auth_registration_codes__code_id__patch"]>>(
        `/api/auth/registration-codes/${enc(codeId)}`,
        { method: "PATCH", body: payload, idempotencyKey: createIdempotencyKey("registration_code_patch") },
      ),
  },
  me: {
    getGenerationDefaults: () =>
      fetchJson<JsonResponse<operations["get_generation_defaults_api_auth_me_generation_defaults_get"]>>(
        "/api/auth/me/generation-defaults",
      ),
    putGenerationDefaults: (
      payload: JsonRequest<operations["put_generation_defaults_api_auth_me_generation_defaults_put"]>,
    ) =>
      fetchJson<JsonResponse<operations["put_generation_defaults_api_auth_me_generation_defaults_put"]>>(
        "/api/auth/me/generation-defaults",
        { method: "PUT", body: payload, idempotencyKey: createIdempotencyKey("generation_defaults") },
      ),
  },
  cases: {
    list: (query: QueryParams<operations["list_cases_api_cases_get"]> = {}) =>
      fetchJson<JsonResponse<operations["list_cases_api_cases_get"]>>("/api/cases", { query }).then((res) => ({
        ...res,
        items: res.items.filter(isRealCase),
      })),
    create: (payload: JsonRequest<operations["create_case_api_cases_post"]>) =>
      fetchJson<JsonResponse<operations["create_case_api_cases_post"]>>("/api/cases", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("case"),
      }),
    detail: (caseId: string) =>
      fetchJson<JsonResponse<operations["case_detail_api_cases__case_id__get"]>>(`/api/cases/${enc(caseId)}`),
    patch: (caseId: string, payload: JsonRequest<operations["patch_case_api_cases__case_id__patch"]>) =>
      fetchJson<JsonResponse<operations["patch_case_api_cases__case_id__patch"]>>(`/api/cases/${enc(caseId)}`, {
        method: "PATCH",
        body: payload,
        idempotencyKey: createIdempotencyKey("case_patch"),
      }),
    delete: (caseId: string) =>
      fetchJson<JsonResponse<operations["delete_case_api_cases__case_id__delete"]>>(`/api/cases/${enc(caseId)}`, {
        method: "DELETE",
        idempotencyKey: createIdempotencyKey("case_delete"),
      }),
    runs: (caseId: string, query: QueryParams<operations["case_run_cards_api_cases__case_id__runs_get"]> = {}) =>
      fetchJson<JsonResponse<operations["case_run_cards_api_cases__case_id__runs_get"]>>(
        `/api/cases/${enc(caseId)}/runs`,
        { query },
      ),
  },
  creative: {
    extractReference: (payload: JsonRequest<operations["reference_extract_api_creative_reference_extract_post"]>) =>
      fetchJson<JsonResponse<operations["reference_extract_api_creative_reference_extract_post"]>>(
        "/api/creative/reference-extract",
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("reference_extract") },
      ),
    referenceExtractorStatus: () =>
      fetchJson<JsonResponse<operations["reference_extractor_status_api_creative_reference_extractor_status_get"]>>(
        "/api/creative/reference-extractor/status",
      ),
    importReferenceCookies: (
      payload: JsonRequest<operations["import_reference_cookies_api_creative_reference_extractor_import_cookies_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["import_reference_cookies_api_creative_reference_extractor_import_cookies_post"]>>(
        "/api/creative/reference-extractor/import-cookies",
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("reference_cookies") },
      ),
    testReferenceCookies: (
      payload: JsonRequest<operations["test_reference_cookies_api_creative_reference_extractor_test_cookies_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["test_reference_cookies_api_creative_reference_extractor_test_cookies_post"]>>(
        "/api/creative/reference-extractor/test-cookies",
        { method: "POST", body: payload },
      ),
  },
  prompts: {
    list: (query: QueryParams<operations["list_prompts_api_prompts_get"]> = {}) =>
      fetchJson<JsonResponse<operations["list_prompts_api_prompts_get"]>>("/api/prompts", { query }),
    create: (payload: JsonRequest<operations["create_prompt_api_prompts_post"]>) =>
      fetchJson<JsonResponse<operations["create_prompt_api_prompts_post"]>>("/api/prompts", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("prompt_template"),
      }),
    versions: (
      templateId: string,
      query: QueryParams<operations["prompt_versions_api_prompts__template_id__versions_get"]> = {},
    ) =>
      fetchJson<JsonResponse<operations["prompt_versions_api_prompts__template_id__versions_get"]>>(
        `/api/prompts/${enc(templateId)}/versions`,
        { query },
      ),
    createVersion: (
      templateId: string,
      payload: JsonRequest<operations["create_prompt_version_api_prompts__template_id__versions_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["create_prompt_version_api_prompts__template_id__versions_post"]>>(
        `/api/prompts/${enc(templateId)}/versions`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("prompt_version") },
      ),
    approveVersion: (
      templateId: string,
      versionId: string,
      payload: JsonRequest<operations["approve_prompt_version_api_prompts__template_id__versions__version_id__approve_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["approve_prompt_version_api_prompts__template_id__versions__version_id__approve_post"]>>(
        `/api/prompts/${enc(templateId)}/versions/${enc(versionId)}/approve`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("prompt_approve") },
      ),
    publishVersion: (
      templateId: string,
      versionId: string,
      payload: JsonRequest<operations["publish_prompt_version_api_prompts__template_id__versions__version_id__publish_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["publish_prompt_version_api_prompts__template_id__versions__version_id__publish_post"]>>(
        `/api/prompts/${enc(templateId)}/versions/${enc(versionId)}/publish`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("prompt_publish") },
      ),
    rollback: (
      templateId: string,
      payload: JsonRequest<operations["rollback_prompt_api_prompts__template_id__rollback_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["rollback_prompt_api_prompts__template_id__rollback_post"]>>(
        `/api/prompts/${enc(templateId)}/rollback`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("prompt_rollback") },
      ),
    bindings: (query: QueryParams<operations["prompt_bindings_api_prompts_bindings_get"]> = {}) =>
      fetchJson<JsonResponse<operations["prompt_bindings_api_prompts_bindings_get"]>>("/api/prompts/bindings", {
        query,
      }),
    createBinding: (payload: JsonRequest<operations["create_prompt_binding_api_prompts_bindings_post"]>) =>
      fetchJson<JsonResponse<operations["create_prompt_binding_api_prompts_bindings_post"]>>(
        "/api/prompts/bindings",
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("prompt_binding") },
      ),
    patchBinding: (
      bindingId: string,
      payload: JsonRequest<operations["patch_prompt_binding_api_prompts_bindings__binding_id__patch"]>,
    ) =>
      fetchJson<JsonResponse<operations["patch_prompt_binding_api_prompts_bindings__binding_id__patch"]>>(
        `/api/prompts/bindings/${enc(bindingId)}`,
        { method: "PATCH", body: payload, idempotencyKey: createIdempotencyKey("prompt_binding_patch") },
      ),
  },
  voices: {
    list: (query: QueryParams<operations["list_voices_api_voices_get"]> = {}) =>
      fetchJson<JsonResponse<operations["list_voices_api_voices_get"]>>("/api/voices", { query }).then((res) => ({
        ...res,
        items: res.items.filter(isRealVoice),
      })),
    sync: (payload: JsonRequest<operations["sync_voices_api_voices_sync_post"]> = {}) =>
      fetchJson<JsonResponse<operations["sync_voices_api_voices_sync_post"]>>("/api/voices/sync", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("voice_sync"),
      }),
    clone: (payload: JsonRequest<operations["clone_voice_api_voices_clone_post"]>) =>
      fetchJson<JsonResponse<operations["clone_voice_api_voices_clone_post"]>>("/api/voices/clone", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("voice_clone"),
      }),
    preview: (voiceId: string, payload: JsonRequest<operations["voice_preview_api_voices__voice_id__preview_post"]>) =>
      fetchJson<JsonResponse<operations["voice_preview_api_voices__voice_id__preview_post"]>>(
        `/api/voices/${enc(voiceId)}/preview`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("voice_preview") },
      ),
    refreshStatus: (voiceId: string) =>
      fetchJson<JsonResponse<operations["refresh_voice_status_api_voices__voice_id__refresh_status_post"]>>(
        `/api/voices/${enc(voiceId)}/refresh-status`,
        { method: "POST", idempotencyKey: createIdempotencyKey("voice_refresh_status") },
      ),
    patch: (voiceId: string, payload: JsonRequest<operations["patch_voice_api_voices__voice_id__patch"]>) =>
      fetchJson<JsonResponse<operations["patch_voice_api_voices__voice_id__patch"]>>(`/api/voices/${enc(voiceId)}`, {
        method: "PATCH",
        body: payload,
        idempotencyKey: createIdempotencyKey("voice_patch"),
      }),
    delete: (voiceId: string) =>
      fetchJson<JsonResponse<operations["delete_voice_api_voices__voice_id__delete"]>>(`/api/voices/${enc(voiceId)}`, {
        method: "DELETE",
        idempotencyKey: createIdempotencyKey("voice_delete"),
      }),
  },
  uploads: {
    prepare: (payload: JsonRequest<operations["prepare_upload_api_uploads_prepare_post"]>) =>
      fetchJson<JsonResponse<operations["prepare_upload_api_uploads_prepare_post"]>>("/api/uploads/prepare", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("upload_prepare"),
      }),
    complete: (payload: JsonRequest<operations["complete_upload_api_uploads_complete_post"]>) =>
      fetchJson<JsonResponse<operations["complete_upload_api_uploads_complete_post"]>>("/api/uploads/complete", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("upload_complete"),
      }),
    cancel: (uploadSessionId: string) =>
      fetchJson<JsonResponse<operations["cancel_upload_api_uploads__upload_session_id__cancel_post"]>>(
        `/api/uploads/${enc(uploadSessionId)}/cancel`,
        { method: "POST", idempotencyKey: createIdempotencyKey("upload_cancel") },
      ),
  },
  mediaAssets: {
    list: (query: QueryParams<operations["list_media_assets_api_media_assets_get"]> = {}) =>
      fetchJson<JsonResponse<operations["list_media_assets_api_media_assets_get"]>>("/api/media/assets", { query }).then(
        (res) => ({ ...res, items: res.items.filter(isRealAssetCard) }),
      ),
    usageRanking: (
      kind: "portrait" | "broll" | "bgm" | "font",
      query: QueryParams<operations["material_usage_ranking_api_library_assets__kind__usage_ranking_get"]> = {},
    ) =>
      fetchJson<JsonResponse<operations["material_usage_ranking_api_library_assets__kind__usage_ranking_get"]>>(
        `/api/library/assets/${enc(kind)}/usage-ranking`,
        { query },
      ),
    batchStabilize: (payload: JsonRequest<operations["batch_stabilize_assets_api_media_assets_batch_stabilize_post"]>) =>
      fetchJson<JsonResponse<operations["batch_stabilize_assets_api_media_assets_batch_stabilize_post"]>>(
        "/api/media/assets/batch-stabilize",
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("media_stabilize") },
      ),
    delete: (assetId: string) =>
      fetchJson<JsonResponse<operations["delete_media_asset_api_media_assets__asset_id__delete"]>>(
        `/api/media/assets/${enc(assetId)}`,
        { method: "DELETE", idempotencyKey: createIdempotencyKey("media_delete") },
      ),
    autoMatchReplace: (payload: JsonRequest<operations["auto_match_replace_api_media_assets_auto_match_replace_post"]>) =>
      fetchJson<JsonResponse<operations["auto_match_replace_api_media_assets_auto_match_replace_post"]>>(
        "/api/media/assets/auto-match-replace",
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("media_auto_replace") },
      ),
    replaceSource: (
      assetId: string,
      payload: JsonRequest<operations["replace_asset_source_api_media_assets__asset_id__replace_source_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["replace_asset_source_api_media_assets__asset_id__replace_source_post"]>>(
        `/api/media/assets/${enc(assetId)}/replace-source`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("media_replace_source") },
      ),
    previewUrl: (assetId: string) =>
      fetchJson<JsonResponse<operations["media_asset_preview_api_media_assets__asset_id__preview_url_get"]>>(
        `/api/media/assets/${enc(assetId)}/preview-url`,
      ),
  },
  annotations: {
    get: (assetId: string) =>
      fetchJson<JsonResponse<operations["get_annotation_api_annotations__asset_id__get"]>>(
        `/api/annotations/${enc(assetId)}`,
      ),
    patch: (assetId: string, payload: JsonRequest<operations["patch_annotation_api_annotations__asset_id__patch"]>) =>
      fetchJson<JsonResponse<operations["patch_annotation_api_annotations__asset_id__patch"]>>(
        `/api/annotations/${enc(assetId)}`,
        { method: "PATCH", body: payload, idempotencyKey: createIdempotencyKey("annotation_patch") },
      ),
    trim: (assetId: string, payload: JsonRequest<operations["trim_annotation_api_annotations__asset_id__trim_post"]>) =>
      fetchJson<JsonResponse<operations["trim_annotation_api_annotations__asset_id__trim_post"]>>(
        `/api/annotations/${enc(assetId)}/trim`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("annotation_trim") },
      ),
    rerun: (assetId: string, payload: JsonRequest<operations["rerun_annotation_api_annotations__asset_id__rerun_post"]>) =>
      fetchJson<JsonResponse<operations["rerun_annotation_api_annotations__asset_id__rerun_post"]>>(
        `/api/annotations/${enc(assetId)}/rerun`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("annotation_rerun") },
      ),
    batch: (payload: JsonRequest<operations["batch_annotation_api_annotations_batch_post"]>) =>
      fetchJson<JsonResponse<operations["batch_annotation_api_annotations_batch_post"]>>(
        "/api/annotations/batch",
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("annotation_batch") },
      ),
  },
  jobs: {
    createDigitalHumanVideo: (
      payload: JsonRequest<operations["create_digital_human_job_api_jobs_digital_human_video_post"]>,
      idempotencyKey = createIdempotencyKey("video_job"),
    ) =>
      fetchJson<JsonResponse<operations["create_digital_human_job_api_jobs_digital_human_video_post"]>>(
        "/api/jobs/digital-human-video",
        { method: "POST", body: payload, idempotencyKey },
      ),
    createDigitalHumanVideoBatch: (
      payload: JsonRequest<operations["create_digital_human_batch_api_jobs_digital_human_video_batch_post"]>,
      idempotencyKey = createIdempotencyKey("video_batch"),
    ) =>
      fetchJson<JsonResponse<operations["create_digital_human_batch_api_jobs_digital_human_video_batch_post"]>>(
        "/api/jobs/digital-human-video/batch",
        { method: "POST", body: payload, idempotencyKey },
      ),
  },
  runs: {
    detail: (runId: string) =>
      fetchJson<JsonResponse<operations["run_detail_api_runs__run_id__get"]>>(`/api/runs/${enc(runId)}`),
    cancel: (runId: string, payload: JsonRequest<operations["cancel_run_api_runs__run_id__cancel_post"]>) =>
      fetchJson<JsonResponse<operations["cancel_run_api_runs__run_id__cancel_post"]>>(
        `/api/runs/${enc(runId)}/cancel`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("cancel_run") },
      ),
    retry: (runId: string, payload: JsonRequest<operations["retry_run_api_runs__run_id__retry_post"]>) =>
      fetchJson<JsonResponse<operations["retry_run_api_runs__run_id__retry_post"]>>(
        `/api/runs/${enc(runId)}/retry`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("retry_run") },
      ),
    resume: (runId: string, payload: JsonRequest<operations["resume_run_api_runs__run_id__resume_post"]>) =>
      fetchJson<JsonResponse<operations["resume_run_api_runs__run_id__resume_post"]>>(
        `/api/runs/${enc(runId)}/resume`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("resume_run") },
      ),
    events: (runId: string) =>
      fetchJson<JsonResponse<operations["run_events_api_runs__run_id__events_get"]>>(`/api/runs/${enc(runId)}/events`),
    delete: (runId: string) =>
      fetchJson<JsonResponse<operations["delete_run_record_api_runs__run_id__delete"]>>(`/api/runs/${enc(runId)}`, {
        method: "DELETE",
        idempotencyKey: createIdempotencyKey("delete_run"),
      }),
  },
  finishedVideos: {
    list: (
      caseId: string,
      query: QueryParams<operations["case_finished_videos_api_cases__case_id__finished_videos_get"]> = {},
    ) =>
      fetchJson<JsonResponse<operations["case_finished_videos_api_cases__case_id__finished_videos_get"]>>(
        `/api/cases/${enc(caseId)}/finished-videos`,
        { query },
      ),
    previewUrl: (id: string) =>
      fetchJson<JsonResponse<operations["finished_video_preview_api_finished_videos__id__preview_url_get"]>>(
        `/api/finished-videos/${enc(id)}/preview-url`,
      ),
    download: (id: string) =>
      fetchJson<JsonResponse<operations["finished_video_download_api_finished_videos__id__download_get"]>>(
        `/api/finished-videos/${enc(id)}/download`,
      ),
    delete: (id: string) =>
      fetchJson<JsonResponse<operations["delete_finished_video_api_finished_videos__id__delete"]>>(
        `/api/finished-videos/${enc(id)}`,
        { method: "DELETE", idempotencyKey: createIdempotencyKey("delete_video") },
      ),
  },
  publishing: {
    packages: (query: QueryParams<operations["publish_packages_api_publish_packages_get"]> = {}) =>
      fetchJson<JsonResponse<operations["publish_packages_api_publish_packages_get"]>>("/api/publish/packages", {
        query,
      }),
    createPackage: (payload: JsonRequest<operations["create_publish_package_api_publish_packages_post"]>) =>
      fetchJson<JsonResponse<operations["create_publish_package_api_publish_packages_post"]>>("/api/publish/packages", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("publish_package"),
      }),
    patchPackage: (
      packageId: string,
      payload: JsonRequest<operations["patch_publish_package_api_publish_packages__package_id__patch"]>,
    ) =>
      fetchJson<JsonResponse<operations["patch_publish_package_api_publish_packages__package_id__patch"]>>(
        `/api/publish/packages/${enc(packageId)}`,
        { method: "PATCH", body: payload, idempotencyKey: createIdempotencyKey("publish_package_patch") },
      ),
    batches: (query: QueryParams<operations["publish_batches_api_publish_batches_get"]> = {}) =>
      fetchJson<JsonResponse<operations["publish_batches_api_publish_batches_get"]>>("/api/publish/batches", {
        query,
      }),
    createBatch: (payload: JsonRequest<operations["create_publish_batch_api_publish_batches_post"]>) =>
      fetchJson<JsonResponse<operations["create_publish_batch_api_publish_batches_post"]>>("/api/publish/batches", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("publish_batch"),
      }),
    batch: (batchId: string) =>
      fetchJson<JsonResponse<operations["publish_batch_detail_api_publish_batches__batch_id__get"]>>(
        `/api/publish/batches/${enc(batchId)}`,
      ),
    attempts: (
      batchId: string,
      query: QueryParams<operations["publish_batch_attempts_api_publish_batches__batch_id__attempts_get"]> = {},
    ) =>
      fetchJson<JsonResponse<operations["publish_batch_attempts_api_publish_batches__batch_id__attempts_get"]>>(
        `/api/publish/batches/${enc(batchId)}/attempts`,
        { query },
      ),
    deleteBatch: (batchId: string) =>
      fetchJson<JsonResponse<operations["delete_publish_batch_api_publish_batches__batch_id__delete"]>>(
        `/api/publish/batches/${enc(batchId)}`,
        { method: "DELETE", idempotencyKey: createIdempotencyKey("publish_batch_delete") },
      ),
    submitBatch: (
      batchId: string,
      payload: JsonRequest<operations["submit_publish_batch_api_publish_batches__batch_id__submit_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["submit_publish_batch_api_publish_batches__batch_id__submit_post"]>>(
        `/api/publish/batches/${enc(batchId)}/submit`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("publish_submit") },
      ),
    retryItem: (batchId: string, itemId: string) =>
      fetchJson<JsonResponse<operations["retry_publish_item_api_publish_batches__batch_id__items__item_id__retry_publish_post"]>>(
        `/api/publish/batches/${enc(batchId)}/items/${enc(itemId)}/retry-publish`,
        { method: "POST", idempotencyKey: createIdempotencyKey("publish_retry") },
      ),
    generateCover: (
      batchId: string,
      itemId: string,
      payload: JsonRequest<operations["generate_publish_cover_api_publish_batches__batch_id__items__item_id__generate_cover_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["generate_publish_cover_api_publish_batches__batch_id__items__item_id__generate_cover_post"]>>(
        `/api/publish/batches/${enc(batchId)}/items/${enc(itemId)}/generate-cover`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("publish_cover") },
      ),
    previewCoverFrame: (
      batchId: string,
      itemId: string,
      payload: JsonRequest<operations["preview_publish_cover_frame_api_publish_batches__batch_id__items__item_id__preview_cover_frame_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["preview_publish_cover_frame_api_publish_batches__batch_id__items__item_id__preview_cover_frame_post"]>>(
        `/api/publish/batches/${enc(batchId)}/items/${enc(itemId)}/preview-cover-frame`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("publish_cover_preview") },
      ),
    patchItem: (itemId: string, payload: JsonRequest<operations["patch_publish_item_api_publish_items__item_id__patch"]>) =>
      fetchJson<JsonResponse<operations["patch_publish_item_api_publish_items__item_id__patch"]>>(
        `/api/publish/items/${enc(itemId)}`,
        { method: "PATCH", body: payload, idempotencyKey: createIdempotencyKey("publish_item_patch") },
      ),
    deleteItem: (itemId: string) =>
      fetchJson<JsonResponse<operations["delete_publish_item_api_publish_items__item_id__delete"]>>(
        `/api/publish/items/${enc(itemId)}`,
        { method: "DELETE", idempotencyKey: createIdempotencyKey("publish_item_delete") },
      ),
  },
  publishOps: {
    listClients: (query: QueryParams<operations["list_clients_api_publish_clients_get"]> = {}) =>
      fetchJson<JsonResponse<operations["list_clients_api_publish_clients_get"]>>("/api/publish/clients", {
        query,
      }),
    createClient: (payload: JsonRequest<operations["create_client_api_publish_clients_post"]>) =>
      fetchJson<JsonResponse<operations["create_client_api_publish_clients_post"]>>("/api/publish/clients", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("publish_client"),
      }),
    listAccounts: (query: QueryParams<operations["list_accounts_api_publish_accounts_get"]> = {}) =>
      fetchJson<JsonResponse<operations["list_accounts_api_publish_accounts_get"]>>("/api/publish/accounts", {
        query,
      }),
    listCaseTargets: (caseId: string) =>
      fetchJson<JsonResponse<operations["list_case_targets_api_cases__case_id__publish_targets_get"]>>(
        `/api/cases/${enc(caseId)}/publish-targets`,
      ),
    setCaseTargets: (
      caseId: string,
      payload: JsonRequest<operations["set_case_targets_api_cases__case_id__publish_targets_put"]>,
    ) =>
      fetchJson<JsonResponse<operations["set_case_targets_api_cases__case_id__publish_targets_put"]>>(
        `/api/cases/${enc(caseId)}/publish-targets`,
        { method: "PUT", body: payload, idempotencyKey: createIdempotencyKey("case_publish_targets") },
      ),
    createAccount: (payload: JsonRequest<operations["create_account_api_publish_accounts_post"]>) =>
      fetchJson<JsonResponse<operations["create_account_api_publish_accounts_post"]>>("/api/publish/accounts", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("publish_account"),
      }),
    patchAccount: (
      accountId: string,
      payload: JsonRequest<operations["patch_account_api_publish_accounts__account_id__patch"]>,
    ) =>
      fetchJson<JsonResponse<operations["patch_account_api_publish_accounts__account_id__patch"]>>(
        `/api/publish/accounts/${enc(accountId)}`,
        { method: "PATCH", body: payload, idempotencyKey: createIdempotencyKey("publish_account_patch") },
      ),
    deleteAccount: (accountId: string) =>
      fetchJson<JsonResponse<operations["delete_account_api_publish_accounts__account_id__delete"]>>(
        `/api/publish/accounts/${enc(accountId)}`,
        { method: "DELETE", idempotencyKey: createIdempotencyKey("publish_account_delete") },
      ),
    beginLogin: (accountId: string) =>
      fetchJson<JsonResponse<operations["begin_account_login_api_publish_accounts__account_id__login_post"]>>(
        `/api/publish/accounts/${enc(accountId)}/login`,
        { method: "POST", idempotencyKey: createIdempotencyKey("publish_login") },
      ),
    cancelLogin: (accountId: string, loginId: string) =>
      fetchJson<JsonResponse<operations["cancel_account_login_api_publish_accounts__account_id__login__login_id__delete"]>>(
        `/api/publish/accounts/${enc(accountId)}/login/${enc(loginId)}`,
        { method: "DELETE", idempotencyKey: createIdempotencyKey("publish_login_cancel") },
      ),
    validateSession: (accountId: string) =>
      fetchJson<JsonResponse<operations["validate_account_session_api_publish_accounts__account_id__session_validate_post"]>>(
        `/api/publish/accounts/${enc(accountId)}/session:validate`,
        { method: "POST", idempotencyKey: createIdempotencyKey("publish_session_validate") },
      ),
  },
  providers: {
    profiles: (query: QueryParams<operations["provider_profiles_api_providers_profiles_get"]> = {}) =>
      fetchJson<JsonResponse<operations["provider_profiles_api_providers_profiles_get"]>>("/api/providers/profiles", {
        query,
      }).then((res) => ({ ...res, items: res.items.filter(isRealProviderProfile) })),
    createProfile: (payload: JsonRequest<operations["create_provider_profile_api_providers_profiles_post"]>) =>
      fetchJson<JsonResponse<operations["create_provider_profile_api_providers_profiles_post"]>>(
        "/api/providers/profiles",
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("provider_profile") },
      ),
    patchProfile: (
      profileId: string,
      payload: JsonRequest<operations["patch_provider_profile_api_providers_profiles__profile_id__patch"]>,
    ) =>
      fetchJson<JsonResponse<operations["patch_provider_profile_api_providers_profiles__profile_id__patch"]>>(
        `/api/providers/profiles/${enc(profileId)}`,
        { method: "PATCH", body: payload, idempotencyKey: createIdempotencyKey("provider_profile") },
      ),
    testProfile: (
      profileId: string,
      payload: JsonRequest<operations["test_provider_profile_api_providers_profiles__profile_id__test_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["test_provider_profile_api_providers_profiles__profile_id__test_post"]>>(
        `/api/providers/profiles/${enc(profileId)}/test`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("provider_test") },
      ),
    capabilities: () =>
      fetchJson<JsonResponse<operations["provider_capabilities_api_providers_capabilities_get"]>>(
        "/api/providers/capabilities",
      ),
    usage: (query: QueryParams<operations["provider_usage_api_providers_usage_get"]> = {}) =>
      fetchJson<JsonResponse<operations["provider_usage_api_providers_usage_get"]>>("/api/providers/usage", {
        query,
      }),
    priceCatalogs: (query: QueryParams<operations["price_catalogs_api_providers_price_catalogs_get"]> = {}) =>
      fetchJson<JsonResponse<operations["price_catalogs_api_providers_price_catalogs_get"]>>(
        "/api/providers/price-catalogs",
        { query },
      ).then((res) => ({ ...res, items: res.items.filter(isRealPriceCatalog) })),
    priceCatalogItems: (
      catalogId: string,
      query: QueryParams<operations["price_catalog_items_api_providers_price_catalogs__catalog_id__items_get"]> = {},
    ) =>
      fetchJson<JsonResponse<operations["price_catalog_items_api_providers_price_catalogs__catalog_id__items_get"]>>(
        `/api/providers/price-catalogs/${enc(catalogId)}/items`,
        { query },
      ).then((res) => ({ ...res, items: res.items.filter(isRealPriceItem) })),
    upsertPriceCatalog: (payload: JsonRequest<operations["upsert_price_catalog_api_providers_price_catalogs_post"]>) =>
      fetchJson<JsonResponse<operations["upsert_price_catalog_api_providers_price_catalogs_post"]>>(
        "/api/providers/price-catalogs",
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("price_catalog") },
      ),
    approvePriceCatalog: (
      catalogId: string,
      payload: JsonRequest<operations["approve_price_catalog_api_providers_price_catalogs__catalog_id__approve_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["approve_price_catalog_api_providers_price_catalogs__catalog_id__approve_post"]>>(
        `/api/providers/price-catalogs/${enc(catalogId)}/approve`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("price_approve") },
      ),
    publishPriceCatalog: (
      catalogId: string,
      payload: JsonRequest<operations["publish_price_catalog_api_providers_price_catalogs__catalog_id__publish_post"]>,
    ) =>
      fetchJson<JsonResponse<operations["publish_price_catalog_api_providers_price_catalogs__catalog_id__publish_post"]>>(
        `/api/providers/price-catalogs/${enc(catalogId)}/publish`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("price_publish") },
      ),
  },
  secrets: {
    list: () => fetchJson<JsonResponse<operations["list_secrets_api_secrets_get"]>>("/api/secrets"),
    create: (payload: JsonRequest<operations["create_secret_api_secrets_post"]>) =>
      fetchJson<JsonResponse<operations["create_secret_api_secrets_post"]>>("/api/secrets", {
        method: "POST",
        body: payload,
        idempotencyKey: createIdempotencyKey("secret"),
      }),
    rotate: (secretId: string, payload: JsonRequest<operations["rotate_secret_api_secrets__secret_id__rotate_post"]>) =>
      fetchJson<JsonResponse<operations["rotate_secret_api_secrets__secret_id__rotate_post"]>>(
        `/api/secrets/${enc(secretId)}/rotate`,
        { method: "POST", body: payload, idempotencyKey: createIdempotencyKey("secret_rotate") },
      ),
    disable: (secretId: string, payload: JsonRequest<operations["disable_secret_api_secrets__secret_id__disable_patch"]>) =>
      fetchJson<JsonResponse<operations["disable_secret_api_secrets__secret_id__disable_patch"]>>(
        `/api/secrets/${enc(secretId)}/disable`,
        { method: "PATCH", body: payload, idempotencyKey: createIdempotencyKey("secret_disable") },
      ),
  },
  ops: {
    dashboard: (query: QueryParams<operations["ops_dashboard_api_ops_dashboard_get"]> = {}) =>
      fetchJson<JsonResponse<operations["ops_dashboard_api_ops_dashboard_get"]>>("/api/ops/dashboard", { query }),
    costRollups: (query: QueryParams<operations["cost_rollups_api_ops_cost_rollups_get"]> = {}) =>
      fetchJson<JsonResponse<operations["cost_rollups_api_ops_cost_rollups_get"]>>("/api/ops/cost-rollups", {
        query,
      }),
    yieldFunnel: (query: QueryParams<operations["yield_funnel_api_ops_yield_funnel_get"]> = {}) =>
      fetchJson<JsonResponse<operations["yield_funnel_api_ops_yield_funnel_get"]>>("/api/ops/yield-funnel", {
        query,
      }),
  },
  health: {
    // /api/health/network is an operational-only endpoint (include_in_schema=False),
    // so it has no generated OpenAPI type — the response shape is hand-written here.
    // It returns 200 (ok) or 503 (degraded); BOTH carry the same diagnostics body,
    // so a 503 is a valid result to render (which hop failed), not a transport error.
    // Any other status falls back to the shared error handling.
    network: async (): Promise<NetworkDiagnostics> => {
      const response = await fetch("/api/health/network", {
        credentials: "include",
        headers: { Accept: JSON_TYPE },
      });
      if (response.ok || response.status === 503) {
        return (await response.json()) as NetworkDiagnostics;
      }
      throw await parseError(response);
    },
  },
} as const;

/** One segment of the Web→VPS→Mac→OSS topology probe (`/api/health/network`). */
export type NetworkHop = {
  status: "ok" | "failed" | "skipped" | "not_configured" | string;
  latency_ms?: number;
  backend?: string;
  runtime?: string;
  address?: string;
  endpoint?: string;
  error?: string;
};

export type NetworkDiagnostics = {
  status: "ok" | "degraded" | string;
  hops: Record<string, NetworkHop>;
  request_id: string;
};

export type AuthUser = components["schemas"]["AuthUser"];
export type LoginRequest = components["schemas"]["LoginRequest"];
export type RegistrationCodePreview = components["schemas"]["RegistrationCodePreview"];
export type CaseListItem = components["schemas"]["CaseListItem"];
export type CaseDetail = components["schemas"]["CaseDetail"];
export type CreateCaseRequest = components["schemas"]["CreateCaseRequest"];
export type PatchCaseRequest = components["schemas"]["PatchCaseRequest"];
export type PromptTemplateView = components["schemas"]["PromptTemplateView"];
export type PromptBindingView = components["schemas"]["PromptBindingView"];
export type NodeRun = components["schemas"]["NodeRun"];
export type RunCard = components["schemas"]["RunCard"];
export type RunDetailResponse = components["schemas"]["RunDetailResponse"];
export type RunConfigSummary = components["schemas"]["RunConfigSummary"];
export type VoiceProfile = components["schemas"]["VoiceProfile"];
export type UploadKind = components["schemas"]["UploadKind"];
export type UploadSession = components["schemas"]["UploadSession"];
export type CompleteUploadResponse = components["schemas"]["CompleteUploadResponse"];
export type MediaAssetCard = components["schemas"]["MediaAssetCard"];
export type MediaAssetRecord = components["schemas"]["MediaAssetRecord"];
export type SignedUrlResponse = components["schemas"]["SignedUrlResponse"];
export type AnnotationEditorVm = components["schemas"]["AnnotationEditorVm"];
export type FinishedVideo = components["schemas"]["FinishedVideo"];
export type ArtifactRef = components["schemas"]["ArtifactRef"];
export type PublishCoverResult = components["schemas"]["PublishCoverResult"];
export type PreviewCoverFrameResult = components["schemas"]["PreviewCoverFrameResult"];
export type PublishAttempt = components["schemas"]["PublishAttempt"];
export type PublishBatch = components["schemas"]["PublishBatchVm"];
export type PublishBatchItem = components["schemas"]["PublishBatchItemVm"];
export type PublishPackage = components["schemas"]["PublishPackage"];
export type PublishClient = components["schemas"]["Client"];
export type PublishAccount = components["schemas"]["PublishAccount"];
export type CreateClientRequest = components["schemas"]["CreateClientRequest"];
export type CreatePublishAccountRequest = components["schemas"]["CreatePublishAccountRequest"];
export type PatchPublishAccountRequest = components["schemas"]["PatchPublishAccountRequest"];
export type PublishPlatform = "douyin" | "shipinhao" | "kuaishou" | "xiaohongshu";
export type PublishLoginState = "logged_in" | "logged_out" | "unknown";
export type ProviderProfile = components["schemas"]["ProviderProfile"];
export type SecretPreview = components["schemas"]["SecretPreview"];
export type CostRollup = components["schemas"]["CostRollup"];
export type OpsDashboardVm = components["schemas"]["OpsDashboardVm"];
export type ProviderUsageReport = components["schemas"]["ProviderUsageReport"];
export type YieldFunnelEvent = components["schemas"]["YieldFunnelEvent"];
export type YieldFunnelResponse = components["schemas"]["YieldFunnelResponse"];
