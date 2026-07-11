import { describe, expect, it } from "vitest";

import type { NodeRun } from "../../api/client";
import { buildNodeTimeline, nodeLabel } from "./runModel";

function node(node_id: string, status = "succeeded"): NodeRun {
  return { node_id, status } as NodeRun;
}

describe("timeline assembly node naming", () => {
  it("uses the assembly-validation name for active templates", () => {
    const items = buildNodeTimeline("digital_human_v2", [], "failed");

    expect(items.some((item) => item.nodeId === "TimelineAssemblyValidation")).toBe(true);
    expect(items.some((item) => item.nodeId === "TimelinePlanning")).toBe(false);
    expect(nodeLabel("TimelineAssemblyValidation")).toBe("组装并校验时间线");
  });

  it("folds a historical TimelinePlanning run into the renamed active node", () => {
    const historical = node("TimelinePlanning");
    const items = buildNodeTimeline("digital_human_v2", [historical], "failed");
    const assemblyItems = items.filter(
      (item) => item.nodeId === "TimelineAssemblyValidation",
    );

    expect(assemblyItems).toHaveLength(1);
    expect(assemblyItems[0]?.node).toBe(historical);
    expect(items.some((item) => item.nodeId === "TimelinePlanning")).toBe(false);
  });
});
