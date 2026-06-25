import { lazy, Suspense } from "react";
import { Navigate, Route, Routes, useParams } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { RequireAuth } from "./components/RequireAuth";
import { LoadingState } from "./components/ui/State";
import LoginPage from "./pages/auth/LoginPage";
import { routePatterns, routes } from "./routes";

const RegisterPage = lazy(() => import("./pages/auth/RegisterPage"));
const CaseListPage = lazy(() => import("./pages/studio/CaseListPage"));
const CaseAgentPage = lazy(() => import("./pages/studio/CaseAgentPage"));
const CaseProfilePage = lazy(() => import("./pages/studio/CaseProfilePage"));
const StudioCreatePage = lazy(() => import("./pages/studio/StudioCreatePage"));
const RunsPage = lazy(() => import("./pages/studio/RunsPage"));
const SettingsPage = lazy(() => import("./pages/settings/SettingsPage"));
const LibraryLayout = lazy(() => import("./pages/library/LibraryLayout"));
const PublishCenterPage = lazy(() => import("./pages/publish/PublishCenterPage"));
const OverviewPage = lazy(() => import("./pages/OverviewPage"));
const AnalyticsPage = lazy(() => import("./pages/AnalyticsPage"));
const AccountPage = lazy(() => import("./pages/AccountPage"));
const PromptManagementPage = lazy(() => import("./pages/ops/PromptManagementPage"));
const PublishOpsPage = lazy(() => import("./pages/ops/PublishOpsPage"));

export default function App() {
  return (
    <Suspense fallback={<PageFallback />}>
      <Routes>
        <Route path={routePatterns.login} element={<LoginPage />} />
        <Route path={routePatterns.register} element={<RegisterPage />} />
        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route index element={<OverviewPage />} />
            <Route path={routePatterns.studio} element={<CaseListPage />} />
            <Route path={routePatterns.caseStudio} element={<StudioCreatePage />} />
            <Route path={routePatterns.caseProfile} element={<CaseProfilePage />} />
            <Route path={routePatterns.caseAgent} element={<CaseAgentPage />} />
            <Route path={routePatterns.caseOutputs} element={<RunsPage />} />
            <Route path={routePatterns.caseRuns} element={<NavigateToCaseOutputs />} />
            <Route path={routePatterns.caseFinishedVideos} element={<NavigateToCaseOutputs />} />
            <Route path={routePatterns.casePublish} element={<PublishCenterPage />} />
            <Route path={routePatterns.settings} element={<SettingsPage />} />
            <Route path={routePatterns.library} element={<LibraryLayout />} />
            <Route path={routePatterns.analytics} element={<AnalyticsPage />} />
            <Route path={routePatterns.account} element={<AccountPage />} />
            <Route path={routePatterns.promptOps} element={<PromptManagementPage />} />
            <Route path={routePatterns.publishOps} element={<PublishOpsPage />} />
            <Route path={routePatterns.ops} element={<Navigate to={routes.analytics()} replace />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to={routes.studio()} replace />} />
      </Routes>
    </Suspense>
  );
}

function PageFallback() {
  return (
    <main className="centerPage">
      <LoadingState label="正在加载页面" />
    </main>
  );
}

function NavigateToCaseOutputs() {
  const { caseId = "" } = useParams();
  return <Navigate to={caseId ? routes.caseOutputs(caseId) : routes.studio()} replace />;
}
