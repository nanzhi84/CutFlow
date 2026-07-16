import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PostProcessStep } from "./StudioCreateSteps";
import { loadStoredForm } from "./studioCreateModel";
import { api, type MediaAssetRecord } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    mediaAssets: {
      list: vi.fn().mockResolvedValue({ items: [] }),
      previewUrl: vi.fn().mockResolvedValue({ url: "" }),
    },
  },
}));

describe("PostProcessStep caption layout", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("keeps normal and emphasis controls stacked at wide desktop widths", () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const form = {
      ...loadStoredForm(),
      contentMode: "editing_agent" as const,
      normalSubtitleEnabled: true,
      emphasisEnabled: true,
    };

    render(
      <QueryClientProvider client={client}>
        <PostProcessStep form={form} setField={vi.fn()} />
      </QueryClientProvider>,
    );

    const groups = screen.getByTestId("caption-style-groups");
    expect(groups).toHaveClass("grid", "gap-3");
    expect(groups.className).not.toContain("grid-cols-2");
    expect(screen.getByRole("heading", { name: "普通字幕" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "强调文字" })).toBeVisible();
    expect(screen.getByTestId("fixed-caption-band-preview")).toHaveTextContent("字幕内强调");
    expect(screen.getByTestId("fixed-caption-band-preview")).not.toHaveTextContent("整句强调");
    expect(screen.getByTestId("fixed-caption-band-preview")).toHaveAttribute(
      "data-line-height-ratio",
      "1.12",
    );
    expect(screen.getByTestId("fixed-caption-band-preview")).toHaveAttribute(
      "data-anchor-x",
      "0.5",
    );
    expect(screen.getByTestId("fixed-caption-band-baseline")).toHaveAttribute("x", "540");
    expect(screen.getByTestId("fixed-caption-band-baseline")).toHaveAttribute(
      "y",
      "1612.8",
    );
    fireEvent.click(screen.getByRole("button", { name: "整句强调" }));
    expect(screen.getByTestId("fixed-caption-band-preview")).toHaveTextContent(
      "整句强调仍在固定字幕带",
    );
    expect(screen.getByTestId("fixed-caption-band-preview")).not.toHaveTextContent(
      "高端定制也能",
    );
  });

  it("disables emphasis when the normal caption band is off", () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const form = {
      ...loadStoredForm(),
      normalSubtitleEnabled: false,
      emphasisEnabled: true,
    };

    render(
      <QueryClientProvider client={client}>
        <PostProcessStep form={form} setField={vi.fn()} />
      </QueryClientProvider>,
    );

    expect(screen.getByRole("switch", { name: "字幕内强调" })).toBeDisabled();
    expect(screen.queryByTestId("fixed-caption-band-preview")).not.toBeInTheDocument();
  });

  it("shows one family option and switches the concrete face with weight buttons", async () => {
    const regular = {
      id: "serif_regular",
      title: "Noto Serif CJK SC Regular",
      kind: "font",
      tags: ["family:Noto Serif CJK SC", "weight:400"],
      annotation_status: "annotated",
      usable: true,
    } as MediaAssetRecord;
    const bold = {
      ...regular,
      id: "serif_bold",
      title: "Noto Serif CJK SC Bold",
      tags: ["family:Noto Serif CJK SC", "weight:700"],
    } as MediaAssetRecord;
    vi.mocked(api.mediaAssets.list).mockResolvedValueOnce({
      items: [{ asset: regular }, { asset: bold }],
    } as never);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const setField = vi.fn();
    const form = {
      ...loadStoredForm(),
      normalSubtitleEnabled: true,
      emphasisEnabled: true,
      subtitleFontId: regular.id,
      emphasisFontId: bold.id,
    };

    render(
      <QueryClientProvider client={client}>
        <PostProcessStep form={form} setField={setField} />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getAllByRole("option", { name: "Noto Serif CJK SC" })).toHaveLength(2);
    });
    const emphasisWeights = screen.getByRole("group", { name: "强调文字字重" });
    expect(within(emphasisWeights).getByRole("button", { name: "加粗 · 700" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    fireEvent.click(within(emphasisWeights).getByRole("button", { name: "常规 · 400" }));
    expect(setField).toHaveBeenCalledWith("emphasisFontId", regular.id);
  });
});
