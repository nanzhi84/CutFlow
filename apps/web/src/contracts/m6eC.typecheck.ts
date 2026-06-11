import { api, type DigitalHumanVideoCostEstimateResponse } from "../api/client";

async function assertCostEstimateContract(): Promise<DigitalHumanVideoCostEstimateResponse> {
  return api.jobs.estimateDigitalHumanVideoCost({
    schema_version: "digital_human_video_request.v1",
    case_id: "case_demo",
    script: "成本预估",
    publish_content: "",
    workflow_template_id: "digital_human_v2",
    voice: { voice_id: "voice_sandbox", speed: 1, emotion: "neutral", volume: 1 },
  });
}

void assertCostEstimateContract();
