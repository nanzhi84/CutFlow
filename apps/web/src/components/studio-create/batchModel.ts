import type { components } from "../../api/schema";

export type BatchDigitalHumanVideoRequest = components["schemas"]["BatchDigitalHumanVideoRequest"];
type BatchItem = components["schemas"]["BatchItem"];
export type BatchItemResult = components["schemas"]["BatchItemResult"];

/** A single script row from a batch entry point (pasted list or candidate pool). */
export type BatchScriptInput = {
  script: string;
  title?: string | null;
  scriptVersionId?: string | null;
};

export type BatchWorkflowTemplate =
  | "digital_human_v2"
  | "digital_human_editing_agent_v2"
  | "seedance_t2v_v1";

export type BatchSubtitleLayerFlags = {
  enabled: boolean;
  normal_enabled: boolean;
  emphasis_enabled: boolean;
};

/** LipSync is available on both digital-human templates, never on Seedance/full coverage. */
export function batchLipsyncEnabled(
  workflowTemplate: BatchWorkflowTemplate,
  brollMode: "insert" | "full_coverage",
): boolean {
  return workflowTemplate !== "seedance_t2v_v1" && brollMode !== "full_coverage";
}

/** Apply the selected workflow's real subtitle capabilities to saved defaults. */
export function batchSubtitleLayerFlags(
  workflowTemplate: BatchWorkflowTemplate,
  panelEnabled: boolean,
  requestedNormal: boolean,
  requestedEmphasis: boolean,
): BatchSubtitleLayerFlags {
  const supportsSubtitles = workflowTemplate !== "seedance_t2v_v1";
  const normalEnabled = supportsSubtitles && panelEnabled && requestedNormal;
  const emphasisEnabled =
    workflowTemplate === "digital_human_editing_agent_v2" &&
    panelEnabled &&
    requestedEmphasis;
  return {
    enabled: normalEnabled || emphasisEnabled,
    normal_enabled: normalEnabled,
    emphasis_enabled: emphasisEnabled,
  };
}

/**
 * Split a pasted multi-script blob into individual scripts. Scripts are
 * separated by one or more blank lines; surrounding whitespace is trimmed and
 * empty blocks dropped.
 */
export function parsePastedScripts(raw: string): string[] {
  return raw
    .split(/\n\s*\n+/)
    .map((block) => block.trim())
    .filter((block) => block.length > 0);
}

/** Map UI script rows into the contract `BatchItem[]` shape (trimmed, normalized). */
function scriptInputsToBatchItems(inputs: BatchScriptInput[]): BatchItem[] {
  return inputs.map((input) => {
    const title = input.title?.trim();
    return {
      script: input.script.trim(),
      title: title ? title : null,
      script_version_id: input.scriptVersionId ?? null,
    };
  });
}

/**
 * Build the full batch request payload. `useMyDefaults` controls whether the
 * server merges each item over the caller's saved generation defaults; per-item
 * overrides (if any) still win at the service layer.
 */
export function buildBatchRequest(
  caseId: string,
  inputs: BatchScriptInput[],
  useMyDefaults: boolean,
): BatchDigitalHumanVideoRequest {
  return {
    schema_version: "batch_digital_human_video_request.v1",
    case_id: caseId,
    items: scriptInputsToBatchItems(inputs),
    use_my_defaults: useMyDefaults,
  };
}

/** Human summary of a batch response for toast / inline status. */
export function summarizeBatchResults(results: BatchItemResult[]): {
  created: number;
  failed: number;
  firstRunId: string | null;
} {
  let created = 0;
  let failed = 0;
  let firstRunId: string | null = null;
  for (const result of results) {
    if (result.status === "created" || result.status === "queued") {
      created += 1;
      if (!firstRunId && result.run_id) firstRunId = result.run_id;
    } else {
      failed += 1;
    }
  }
  return { created, failed, firstRunId };
}

export const BATCH_MAX_ITEMS = 50;
