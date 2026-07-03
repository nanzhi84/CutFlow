import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NetworkDiagnosticsPanel } from "./NetworkDiagnosticsPanel";

const mocks = vi.hoisted(() => ({
  network: vi.fn(),
  pageVisible: vi.fn(() => true),
}));

vi.mock("../../api/client", () => ({
  api: {
    health: {
      network: mocks.network,
    },
  },
}));

vi.mock("../../hooks/usePageVisible", () => ({
  usePageVisible: mocks.pageVisible,
}));

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <NetworkDiagnosticsPanel />
    </QueryClientProvider>,
  );
}

describe("NetworkDiagnosticsPanel", () => {
  beforeEach(() => {
    mocks.network.mockReset();
    mocks.pageVisible.mockReturnValue(true);
  });

  it("renders degraded network payloads as diagnostics instead of a generic error", async () => {
    mocks.network.mockResolvedValue({
      status: "failed",
      checked_at: "2026-07-03T00:00:00Z",
      hops: {
        postgres: { status: "ok", latency_ms: 12, backend: "sqlalchemy" },
        oss: { status: "failed", error: "OSS timeout", backend: "s3" },
        temporal: { status: "not_configured", runtime: "local" },
      },
    });

    renderPanel();

    expect(await screen.findByText("链路降级")).toBeInTheDocument();
    expect(screen.getByText("Postgres 数据库")).toBeInTheDocument();
    expect(screen.getByText("对象存储 (OSS)")).toBeInTheDocument();
    expect(screen.getByText("OSS timeout")).toBeInTheDocument();
    expect(screen.getByText("未配置")).toBeInTheDocument();
  });
});
