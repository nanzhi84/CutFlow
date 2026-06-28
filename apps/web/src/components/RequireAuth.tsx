import { Navigate, Outlet, useLocation } from "react-router-dom";
import { LoadingState } from "./ui/State";
import { useAuth } from "../pages/auth/AuthContext";
import { routes } from "../routes";

export function RequireAuth() {
  const auth = useAuth();
  const location = useLocation();

  if (auth.isLoading) {
    return (
      <main className="centerPage">
        <LoadingState label="正在确认登录状态" />
      </main>
    );
  }

  if (!auth.isAuthenticated) {
    return <Navigate to={routes.login()} replace state={{ from: location }} />;
  }

  return <Outlet />;
}
