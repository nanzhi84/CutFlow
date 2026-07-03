import { describe, expect, it } from "vitest";
import { buildBatchRequest, parsePastedScripts, summarizeBatchResults } from "./batchModel";

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
});
