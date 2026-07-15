import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api, type AuthUser, type LoginRequest } from "../../api/client";
import { routes } from "../../routes";
import { uploadManager } from "../../uploads/manager";

type AuthContextValue = {
  user: AuthUser | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  login: (payload: LoginRequest) => Promise<void>;
  logout: () => Promise<void>;
};

const SESSION_QUERY_KEY = ["auth", "session"] as const;
const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const isPublicAuthPath = location.pathname === routes.login() || location.pathname === routes.register();
  const session = useQuery({
    queryKey: SESSION_QUERY_KEY,
    queryFn: api.auth.session,
    retry: false,
    enabled: !isPublicAuthPath,
  });

  const clearSession = useCallback(() => {
    queryClient.setQueryData(SESSION_QUERY_KEY, null);
    queryClient.removeQueries({ queryKey: SESSION_QUERY_KEY });
  }, [queryClient]);

  useEffect(() => {
    const listener = () => {
      clearSession();
      navigate(routes.login(), { replace: true });
    };
    window.addEventListener("cutagent:unauthorized", listener);
    return () => window.removeEventListener("cutagent:unauthorized", listener);
  }, [clearSession, navigate]);

  const loginMutation = useMutation({
    mutationFn: api.auth.login,
    onSuccess: (data) => {
      queryClient.setQueryData(SESSION_QUERY_KEY, data.session);
    },
  });

  const logoutMutation = useMutation({
    mutationFn: api.auth.logout,
    onSettled: () => {
      clearSession();
      navigate(routes.login(), { replace: true });
    },
  });

  const login = useCallback(
    async (payload: LoginRequest) => {
      await loginMutation.mutateAsync(payload);
    },
    [loginMutation],
  );

  const logout = useCallback(async () => {
    await logoutMutation.mutateAsync();
  }, [logoutMutation]);

  const user = session.data?.user ?? null;
  useEffect(() => {
    uploadManager.setUser(user?.id ?? null);
  }, [user?.id]);
  const value = useMemo(
    () => ({
      user,
      isLoading: session.isLoading || session.isFetching,
      isAuthenticated: Boolean(user),
      login,
      logout,
    }),
    [login, logout, session.isFetching, session.isLoading, user],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return value;
}
