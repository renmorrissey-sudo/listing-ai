import fs from "node:fs";
import path from "node:path";

export type Severity =
  | "Critical"
  | "High"
  | "Medium"
  | "Low"
  | "External Dependency"
  | "Manual Action Required"
  | "Info";

export type Finding = {
  id: string;
  title: string;
  severity: Severity;
  route: string;
  timestamp: string;
  viewport: string;
  authState: "authenticated" | "logged-out" | "unknown";
  reproductionSteps: string[];
  expected: string;
  actual: string;
  httpStatus: number | null;
  consoleEvidence: string[];
  networkEvidence: string[];
  screenshotPath: string | null;
  suspectedCodeLocation: string | null;
  recommendedFix: string | null;
  recommendedRegressionTest: string | null;
  classification?: string;
};

export type RouteResult = {
  requestedUrl: string;
  finalUrl: string;
  status: number | null;
  contentType: string | null;
  title: string | null;
  viewport: string;
  authState: "authenticated" | "logged-out" | "unknown";
  visibleErrors: string[];
  consoleErrors: string[];
  pageErrors: string[];
  failedRequests: string[];
  httpErrors: string[];
  ok: boolean;
  notes?: string[];
};

export type AuditRunState = {
  startedAt: string;
  completedAt: string | null;
  baseUrl: string;
  envPresence: Record<string, "PRESENT" | "MISSING">;
  loginSucceeded: boolean | null;
  testsPassed: number;
  testsFailed: number;
  testsSkipped: number;
  publicRoutesTested: string[];
  authenticatedRoutesTested: string[];
  findings: Finding[];
  routeResults: RouteResult[];
  productionMutations: false;
  mutatingActionsTriggered: false;
  notes: string[];
};

const RESULTS_DIR = path.join(process.cwd(), "audit-results");
const STATE_PATH = path.join(RESULTS_DIR, ".audit-state.json");

export function ensureResultsDirs(): void {
  fs.mkdirSync(path.join(RESULTS_DIR, "screenshots"), { recursive: true });
  fs.mkdirSync(path.join(RESULTS_DIR, "test-artifacts"), { recursive: true });
}

export function loadState(): AuditRunState | null {
  if (!fs.existsSync(STATE_PATH)) return null;
  return JSON.parse(fs.readFileSync(STATE_PATH, "utf8")) as AuditRunState;
}

export function saveState(state: AuditRunState): void {
  ensureResultsDirs();
  fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 2));
}

export function initState(partial: Partial<AuditRunState>): AuditRunState {
  const state: AuditRunState = {
    startedAt: partial.startedAt || new Date().toISOString(),
    completedAt: null,
    baseUrl: partial.baseUrl || "https://topairealestatetools.com",
    envPresence: partial.envPresence || {},
    loginSucceeded: null,
    testsPassed: 0,
    testsFailed: 0,
    testsSkipped: 0,
    publicRoutesTested: [],
    authenticatedRoutesTested: [],
    findings: [],
    routeResults: [],
    productionMutations: false,
    mutatingActionsTriggered: false,
    notes: [],
  };
  saveState(state);
  return state;
}

export function updateState(mutator: (s: AuditRunState) => void): AuditRunState {
  const state = loadState() || initState({});
  mutator(state);
  saveState(state);
  return state;
}

export function addFinding(finding: Finding): void {
  updateState((s) => {
    if (!s.findings.some((f) => f.id === finding.id)) {
      s.findings.push(finding);
    }
  });
}

export function addRouteResult(result: RouteResult): void {
  updateState((s) => {
    s.routeResults.push(result);
    const pathOnly = safePath(result.requestedUrl);
    if (result.authState === "authenticated") {
      if (!s.authenticatedRoutesTested.includes(pathOnly)) {
        s.authenticatedRoutesTested.push(pathOnly);
      }
    } else if (!s.publicRoutesTested.includes(pathOnly)) {
      s.publicRoutesTested.push(pathOnly);
    }
  });
}

export function safePath(url: string): string {
  try {
    const u = new URL(url, "https://topairealestatetools.com");
    return `${u.pathname}${u.hash || ""}`;
  } catch {
    return url;
  }
}

export function findingId(
  severity: string,
  route: string,
  title: string,
  viewport: string
): string {
  const slug = `${severity}|${route}|${title}|${viewport}`
    .toLowerCase()
    .replace(/[^a-z0-9|/_#-]+/g, "-")
    .slice(0, 160);
  return slug;
}

export function screenshotsDir(): string {
  ensureResultsDirs();
  return path.join(RESULTS_DIR, "screenshots");
}
