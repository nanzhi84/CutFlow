export const routePatterns = {
  login: "/login",
  register: "/register",
  studio: "/studio",
  caseStudio: "/studio/:caseId",
  caseProfile: "/studio/:caseId/profile",
  caseAgent: "/studio/:caseId/agent",
  caseOutputs: "/studio/:caseId/outputs",
  caseRuns: "/studio/:caseId/runs",
  caseFinishedVideos: "/studio/:caseId/finished-videos",
  casePublish: "/studio/:caseId/publish",
  settings: "/settings",
  library: "/library/*",
  analytics: "/analytics/*",
  account: "/account/*",
  promptOps: "/ops/prompts",
  publishOps: "/publish-ops",
  ops: "/ops/*",
} as const;

const segment = (value: string) => encodeURIComponent(value);

export const routes = {
  login: () => "/login",
  register: () => "/register",
  overview: () => "/",
  studio: () => "/studio",
  caseStudio: (caseId: string) => `/studio/${segment(caseId)}`,
  caseProfile: (caseId: string) => `/studio/${segment(caseId)}/profile`,
  caseAgent: (caseId: string) => `/studio/${segment(caseId)}/agent`,
  caseOutputs: (caseId: string) => `/studio/${segment(caseId)}/outputs`,
  casePublish: (caseId: string) => `/studio/${segment(caseId)}/publish`,
  settings: (tab?: "providers" | "secrets" | "prices") => (tab ? `/settings?tab=${tab}` : "/settings"),
  library: () => "/library",
  analytics: () => "/analytics",
  account: () => "/account",
  promptOps: () => "/ops/prompts",
  publishOps: () => "/publish-ops",
} as const;
