import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PostProcessStep } from "./StudioCreateSteps";
import { loadStoredForm } from "./studioCreateModel";

vi.mock("../../api/client", () => ({
  api: {
    mediaAssets: {
      list: vi.fn().mockResolvedValue({ items: [] }),
      previewUrl: vi.fn(),
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
});
