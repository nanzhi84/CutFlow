import { describe, expect, it } from "vitest";
import {
  batchLipsyncEnabled,
  batchSubtitleLayerFlags,
  buildBatchRequest,
  parsePastedScripts,
  summarizeBatchResults,
} from "./batchModel";

describe("batchModel", () => {
  it("splits pasted scripts on blank lines and trims empty blocks", () => {
    expect(parsePastedScripts(" 第一条脚本 \n\n\n第二条脚本\n \n第三条脚本 ")).toEqual([
      "第一条脚本",
      "第二条脚本",
      "第三条脚本",
    ]);
  });

  it("builds normalized batch request payloads", () => {
    const request = buildBatchRequest(
      "case_1",
      [
        { script: "  脚本 A  ", title: "  标题 A ", scriptVersionId: "sv_1" },
        { script: "脚本 B", title: "   " },
      ],
      false,
    );

    expect(request).toEqual({
      schema_version: "batch_digital_human_video_request.v1",
      case_id: "case_1",
      items: [
        { script: "脚本 A", title: "标题 A", script_version_id: "sv_1" },
        { script: "脚本 B", title: null, script_version_id: null },
      ],
      use_my_defaults: false,
    });
  });

  it("summarizes created and failed batch results", () => {
    expect(
      summarizeBatchResults([
        { status: "created", job_id: "job_1", run_id: "run_1", index: 0, error: null },
        { status: "created", job_id: "job_2", run_id: "run_2", index: 1, error: null },
        { status: "failed", job_id: null, run_id: null, index: 2, error: "bad script" },
      ]),
    ).toEqual({ created: 2, failed: 1, firstRunId: "run_1" });
  });

  it("disables emphasis-only defaults for deterministic batch runs", () => {
    expect(batchSubtitleLayerFlags("digital_human_v2", true, false, true)).toEqual({
      enabled: false,
      normal_enabled: false,
      emphasis_enabled: false,
    });
  });

  it("rejects emphasis-only defaults for editing-agent batch runs", () => {
    expect(
      batchSubtitleLayerFlags("digital_human_editing_agent_v2", true, false, true),
    ).toEqual({
      enabled: false,
      normal_enabled: false,
      emphasis_enabled: false,
    });
  });

  it("disables all subtitle layers for Seedance or an off panel toggle", () => {
    expect(batchSubtitleLayerFlags("seedance_t2v_v1", true, true, true)).toEqual({
      enabled: false,
      normal_enabled: false,
      emphasis_enabled: false,
    });
    expect(
      batchSubtitleLayerFlags("digital_human_editing_agent_v2", false, true, true),
    ).toEqual({
      enabled: false,
      normal_enabled: false,
      emphasis_enabled: false,
    });
  });

  it("keeps LipSync enabled for both digital-human templates", () => {
    expect(batchLipsyncEnabled("digital_human_v2", "insert")).toBe(true);
    expect(batchLipsyncEnabled("digital_human_editing_agent_v2", "insert")).toBe(true);
    expect(batchLipsyncEnabled("digital_human_editing_agent_v2", "full_coverage")).toBe(
      false,
    );
    expect(batchLipsyncEnabled("seedance_t2v_v1", "insert")).toBe(false);
  });
});
