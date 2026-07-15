import { describe, expect, it } from "vitest";

import type { NodeRun } from "../../api/client";
import { buildNodeTimeline, nodeLabel } from "./runModel";

function node(node_id: string, status = "succeeded"): NodeRun {
  return { node_id, status } as NodeRun;
}

describe("timeline assembly node naming", () => {
  it("keeps the two active caption clean-slate sequences at 19 and 20 nodes", () => {
    const main = buildNodeTimeline("digital_human_v2", [], "failed");
    const agent = buildNodeTimeline("digital_human_editing_agent_v2", [], "failed");

    expect(main).toHaveLength(19);
    expect(agent).toHaveLength(20);
    expect(main.map((item) => item.nodeId)).not.toContain("BgmAgentPlanning");
    expect(agent.map((item) => item.nodeId)).toContain("BgmAgentPlanning");
    expect(main.map((item) => item.nodeId)).toContain("CaptionCompositionPlanning");
  });
  it("uses the assembly-validation name for active templates", () => {
    const items = buildNodeTimeline("digital_human_v2", [], "failed");

    expect(items.some((item) => item.nodeId === "TimelineAssemblyValidation")).toBe(true);
    expect(items.some((item) => item.nodeId === "TimelinePlanning")).toBe(false);
    expect(nodeLabel("TimelineAssemblyValidation")).toBe("组装并校验时间线");
  });

  it("shows a removed historical node by its stored raw id", () => {
    const historical = node("TimelinePlanning");
    const items = buildNodeTimeline("digital_human_v2", [historical], "failed");
    expect(items.find((item) => item.nodeId === "TimelineAssemblyValidation")?.node).toBeUndefined();
    expect(items.find((item) => item.nodeId === "TimelinePlanning")?.node).toBe(historical);
    expect(nodeLabel("TimelinePlanning")).toBe("TimelinePlanning");
  });
});
