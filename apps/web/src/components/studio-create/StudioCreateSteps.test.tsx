import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
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
      huaziEnabled: true,
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
    expect(screen.getByRole("heading", { name: "花字" })).toBeVisible();
  });
});
