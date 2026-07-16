import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth } from "./AuthContext";

const mocks = vi.hoisted(() => ({
  session: vi.fn(),
  setUploadUser: vi.fn(),
}));

vi.mock("../../api/client", () => ({
  api: {
    auth: {
      session: mocks.session,
      login: vi.fn(),
      logout: vi.fn(),
    },
  },
}));

vi.mock("../../uploads/manager", () => ({
  uploadManager: {
    setUser: mocks.setUploadUser,
  },
}));

const SESSION_QUERY_KEY = ["auth", "session"] as const;
const SESSION = {
  user: {
    id: "usr_test",
    version: 1,
    schema_version: "v1",
    email: "wzm@example.test",
    display_name: "wzm",
    role: "admin",
    status: "active",
  },
  session_id: "ses_test",
  expires_at: "2026-07-17T00:00:00Z",
  request_id: "req_test",
};

function AuthStateProbe() {
  const auth = useAuth();
  return <div>{auth.isLoading ? "blocking" : `ready:${auth.user?.display_name ?? "anonymous"}`}</div>;
}

function renderAuth(client: QueryClient) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/"]}>
        <AuthProvider>
          <AuthStateProbe />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AuthProvider loading state", () => {
  beforeEach(() => {
    mocks.session.mockReset();
    mocks.setUploadUser.mockReset();
  });

  it("keeps the authenticated app visible during a background session refresh", () => {
    mocks.session.mockReturnValue(new Promise(() => undefined));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(SESSION_QUERY_KEY, SESSION);

    renderAuth(client);

    expect(screen.getByText("ready:wzm")).toBeInTheDocument();
    expect(mocks.session).toHaveBeenCalledOnce();
  });

  it("still blocks protected content during the initial session request", () => {
    mocks.session.mockReturnValue(new Promise(() => undefined));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    renderAuth(client);

    expect(screen.getByText("blocking")).toBeInTheDocument();
  });
});
