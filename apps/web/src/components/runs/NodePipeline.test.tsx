import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { NodeRun } from "../../api/client";
import { NodePipeline } from "./NodePipeline";

describe("NodePipeline", () => {
  it("keeps a degradation message and removes only its duplicate warning code", () => {
    const node = {
      node_id: "BgmAgentPlanning",
      status: "degraded",
      warnings: ["font.default_used"],
      degradations: [
        {
          code: "font.default_used",
          message: "指定字体不可用，已回退到默认字体。",
          affects_true_yield: false,
        },
      ],
    } as unknown as NodeRun;

    render(
      <NodePipeline
        templateId="digital_human_editing_agent_v2"
        nodes={[node]}
        runStatus="running"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /规划背景音乐/ }));

    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getAllByText("指定字体不可用，已回退到默认字体。")).toHaveLength(1);
  });

  it("keeps degradations with the same code but different messages", () => {
    const node = {
      node_id: "SubtitleAndBgmMix",
      status: "degraded",
      warnings: ["sfx.asset_missing"],
      degradations: [
        {
          code: "sfx.asset_missing",
          message: "缺少转场音效类别。",
          affects_true_yield: false,
        },
        {
          code: "sfx.asset_missing",
          message: "音效资产 sfx-1 无法读取。",
          affects_true_yield: false,
        },
      ],
    } as NodeRun;

    render(
      <NodePipeline
        templateId="digital_human_editing_agent_v2"
        nodes={[node]}
        runStatus="running"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /混合字幕与配乐/ }));

    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("缺少转场音效类别。")).toBeInTheDocument();
    expect(screen.getByText("音效资产 sfx-1 无法读取。")).toBeInTheDocument();
  });

  it("keeps an empty persisted degradation message authoritative", () => {
    const node = {
      node_id: "BgmAgentPlanning",
      status: "degraded",
      warnings: ["font.default_used"],
      degradations: [
        {
          code: "font.default_used",
          message: "",
          affects_true_yield: false,
        },
      ],
    } as unknown as NodeRun;

    render(
      <NodePipeline
        templateId="digital_human_editing_agent_v2"
        nodes={[node]}
        runStatus="running"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /规划背景音乐/ }));

    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.queryByText("未选择字幕字体，已使用默认字体")).not.toBeInTheDocument();
  });

  it("uses the corrected label for a warning-only default font", () => {
    const node = {
      node_id: "BgmAgentPlanning",
      status: "degraded",
      warnings: ["font.default_used"],
      degradations: [],
    } as unknown as NodeRun;

    render(
      <NodePipeline
        templateId="digital_human_editing_agent_v2"
        nodes={[node]}
        runStatus="running"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /规划背景音乐/ }));

    expect(screen.getByText("未选择字幕字体，已使用默认字体")).toBeInTheDocument();
  });
});
