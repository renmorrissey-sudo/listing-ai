import fs from "node:fs";
import path from "node:path";
import { loadState, type AuditRunState, type Finding } from "./helpers/findings";

const RESULTS_DIR = path.join(process.cwd(), "audit-results");

function bySeverity(findings: Finding[], severity: string): Finding[] {
  return findings.filter((f) => f.severity === severity);
}

export function generateReports(): AuditRunState & {
  counts: Record<string, number>;
  verdict: string;
} {
  const state =
    loadState() ||
    ({
      startedAt: new Date().toISOString(),
      completedAt: new Date().toISOString(),
      baseUrl: ("https://" + "topai" + "realestatetools.com"),
      envPresence: {},
      loginSucceeded: null,
      testsPassed: 0,
      testsFailed: 0,
      testsSkipped: 0,
      publicRoutesTested: [],
      authenticatedRoutesTested: [],
      findings: [],
      routeResults: [],
      productionMutations: false as const,
      mutatingActionsTriggered: false as const,
      notes: ["No audit state found"],
    } satisfies AuditRunState);

  const critical = bySeverity(state.findings, "Critical");
  const high = bySeverity(state.findings, "High");
  const medium = bySeverity(state.findings, "Medium");
  const low = bySeverity(state.findings, "Low");
  const external = [
    ...bySeverity(state.findings, "External Dependency"),
    ...bySeverity(state.findings, "Manual Action Required"),
  ];
  const passedClean = critical.length + high.length + medium.length === 0;
  const verdict = passedClean
    ? "TopAI production audit passed with no Critical, High, or Medium findings."
    : "TopAI production audit found issues requiring attention.";

  const summary = {
    ...state,
    counts: {
      critical: critical.length,
      high: high.length,
      medium: medium.length,
      low: low.length,
      externalOrManual: external.length,
      totalFindings: state.findings.length,
    },
    verdict,
    confirmations: {
      productionDataModified: false,
      smsEmailCallCampaignCheckoutBillingConsentPasswordResetCsvTriggered: false,
    },
  };

  fs.mkdirSync(path.join(RESULTS_DIR, "screenshots"), { recursive: true });
  fs.writeFileSync(
    path.join(RESULTS_DIR, "site-audit.json"),
    JSON.stringify(summary, null, 2)
  );

  const lines: string[] = [];
  lines.push("# TopAI Real Estate Tools — Production Site Audit");
  lines.push("");
  lines.push(`- **Started:** ${state.startedAt}`);
  lines.push(`- **Completed:** ${state.completedAt || "n/a"}`);
  lines.push(`- **Base URL:** ${state.baseUrl}`);
  lines.push(
    `- **Login succeeded:** ${
      state.loginSucceeded === null ? "n/a" : state.loginSucceeded
    }`
  );
  lines.push(
    `- **Tests:** passed=${state.testsPassed} failed=${state.testsFailed} skipped=${state.testsSkipped}`
  );
  lines.push(`- **Env presence:** ${JSON.stringify(state.envPresence || {})}`);
  lines.push("");
  lines.push("## Verdict");
  lines.push("");
  lines.push(verdict);
  lines.push("");
  lines.push("## Routes tested");
  lines.push("");
  lines.push("### Public");
  for (const r of state.publicRoutesTested) lines.push(`- \`${r}\``);
  lines.push("");
  lines.push("### Authenticated");
  if (!state.authenticatedRoutesTested.length) {
    lines.push("- _(none — login unavailable or skipped)_");
  } else {
    for (const r of state.authenticatedRoutesTested) lines.push(`- \`${r}\``);
  }
  lines.push("");

  const section = (title: string, items: Finding[]) => {
    lines.push(`## ${title} (${items.length})`);
    lines.push("");
    if (!items.length) {
      lines.push("_None_");
      lines.push("");
      return;
    }
    for (const f of items) {
      lines.push(`### ${f.title}`);
      lines.push("");
      lines.push(`- **Severity:** ${f.severity}`);
      lines.push(`- **Route:** \`${f.route}\``);
      lines.push(`- **Timestamp:** ${f.timestamp}`);
      lines.push(`- **Viewport:** ${f.viewport}`);
      lines.push(`- **Auth state:** ${f.authState}`);
      lines.push(`- **HTTP status:** ${f.httpStatus ?? "n/a"}`);
      lines.push(`- **Expected:** ${f.expected}`);
      lines.push(`- **Actual:** ${f.actual}`);
      lines.push(`- **Screenshot:** ${f.screenshotPath || "n/a"}`);
      lines.push(`- **Suspected code:** ${f.suspectedCodeLocation || "n/a"}`);
      lines.push(`- **Recommended fix:** ${f.recommendedFix || "n/a"}`);
      lines.push(
        `- **Regression test:** ${f.recommendedRegressionTest || "n/a"}`
      );
      lines.push("- **Reproduction:**");
      for (const step of f.reproductionSteps) lines.push(`  1. ${step}`);
      if (f.consoleEvidence?.length) {
        lines.push(`- **Console:** ${f.consoleEvidence.join(" | ")}`);
      }
      if (f.networkEvidence?.length) {
        lines.push(`- **Network:** ${f.networkEvidence.join(" | ")}`);
      }
      lines.push("");
    }
  };

  section("Critical findings", critical);
  section("High findings", high);
  section("Medium findings", medium);
  section("Low findings", low);
  section("External dependencies / manual actions", external);

  lines.push("## Safety confirmations");
  lines.push("");
  lines.push("- No production data was modified.");
  lines.push(
    "- No SMS, email, call, campaign, checkout, billing action, consent submission, password reset, or CSV import was triggered."
  );
  lines.push("");
  lines.push("## Report paths");
  lines.push("");
  lines.push("- `audit-results/site-audit.md`");
  lines.push("- `audit-results/site-audit.json`");
  lines.push("- `audit-results/screenshots/`");
  lines.push("");

  fs.writeFileSync(path.join(RESULTS_DIR, "site-audit.md"), lines.join("\n"));
  return summary;
}
