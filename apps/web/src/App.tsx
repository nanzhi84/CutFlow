import { Navigate, Route, Routes, useParams } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { RequireAuth } from "./components/RequireAuth";
import LoginPage from "./pages/auth/LoginPage";
import RegisterPage from "./pages/auth/RegisterPage";
import CaseListPage from "./pages/studio/CaseListPage";
import CaseAgentPage from "./pages/studio/CaseAgentPage";
import StudioCreatePage from "./pages/studio/StudioCreatePage";
import RunsPage from "./pages/studio/RunsPage";
import SettingsPage from "./pages/settings/SettingsPage";
import LibraryLayout from "./pages/library/LibraryLayout";
import PublishCenterPage from "./pages/publish/PublishCenterPage";
import PlaceholderPage from "./pages/PlaceholderPage";
import OverviewPage from "./pages/OverviewPage";
import AnalyticsPage from "./pages/AnalyticsPage";
import AccountPage from "./pages/AccountPage";
import PromptManagementPage from "./pages/ops/PromptManagementPage";
import { routePatterns, routes } from "./routes";

export default function App() {
  return (
    <Routes>
      <Route path={routePatterns.login} element={<LoginPage />} />
      <Route path={routePatterns.register} element={<RegisterPage />} />
        <Route element={<RequireAuth />}>
        <Route element={<AppShell />}>
          <Route index element={<OverviewPage />} />
          <Route path={routePatterns.studio} element={<CaseListPage />} />
          <Route path={routePatterns.caseStudio} element={<StudioCreatePage />} />
          <Route path={routePatterns.caseAgent} element={<CaseAgentPage />} />
          <Route path={routePatterns.caseOutputs} element={<RunsPage />} />
          <Route path={routePatterns.caseRuns} element={<NavigateToCaseOutputs />} />
          <Route path={routePatterns.caseFinishedVideos} element={<NavigateToCaseOutputs />} />
          <Route path={routePatterns.casePublish} element={<PublishCenterPage />} />
          <Route path={routePatterns.publishCenter} element={<PublishCenterPage />} />
          <Route path={routePatterns.publishCenterBatch} element={<PublishCenterPage />} />
          <Route path={routePatterns.settings} element={<SettingsPage />} />
          <Route path={routePatterns.library} element={<LibraryLayout />} />
          <Route path={routePatterns.analytics} element={<AnalyticsPage />} />
          <Route path={routePatterns.account} element={<AccountPage />} />
          <Route path={routePatterns.promptOps} element={<PromptManagementPage />} />
          <Route path={routePatterns.ops} element={<Navigate to={routes.analytics()} replace />} />
        </Route>
      </Route>
      <Route path="*" element={<Navigate to={routes.studio()} replace />} />
    </Routes>
  );
}

function NavigateToCaseOutputs() {
  const { caseId = "" } = useParams();
  return <Navigate to={caseId ? routes.caseOutputs(caseId) : routes.studio()} replace />;
}
