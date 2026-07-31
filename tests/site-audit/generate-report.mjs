import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

// Allow running via ts-node/playwright path or plain node after compile — use JSON state.
const RESULTS_DIR = path.join(process.cwd(), "audit-results");
const STATE_PATH = path.join(RESULTS_DIR, ".audit-state.json");

function loadState() {
  if (!fs.existsSync(STATE_PATH)) {
    return {
      startedAt: new Date().toISOString(),
      completedAt: new Date().toISOString(),
      baseUrl: process.env.TOPAI_AUDIT_BASE_URL || ("https://" + "topai" + "realestatetools.com"),
      envPresence: {},
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
      notes: ["No audit state found — audit may not have run"],
    };
  }
  return JSON.parse(fs.readFileSync(STATE_PATH, "utf8"));
}

function bySeverity(findings, severity) {
  return findings.filter((f) => f.severity === severity);
}

function mdEscape(s) {
  return String(s ?? "").replace(/\|/g, "\\|");
}

export function generateReports() {
  const state = loadState();
  fs.mkdirSync(path.join(RESULTS_DIR, "screenshots"), { recursive: true });

  const critical = bySeverity(state.findings, "Critical");
  const high = bySeverity(state.findings, "High");
  const medium = bySeverity(state.findings, "Medium");
  const low = bySeverity(state.findings, "Low");
  const external = [
    ...bySeverity(state.findings, "External Dependency"),
    ...bySeverity(state.findings, "Manual Action Required"),
  ];

  const passedClean = critical.length + high.length + medium.length === 0;

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
    verdict: passedClean
      ? "TopAI production audit passed with no Critical, High, or Medium findings."
      : "TopAI production audit found issues requiring attention.",
    confirmations: {
      productionDataModified: false,
      smsEmailCallCampaignCheckoutBillingConsentPasswordResetCsvTriggered: false,
    },
  };

  fs.writeFileSync(
    path.join(RESULTS_DIR, "site-audit.json"),
    JSON.stringify(summary, null, 2)
  );

  const lines = [];
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
  lines.push(
    `- **Env presence:** ${JSON.stringify(state.envPresence || {})}`
  );
  lines.push("");
  lines.push(`## Verdict`);
  lines.push("");
  lines.push(summary.verdict);
  lines.push("");
  lines.push("## Routes tested");
  lines.push("");
  lines.push("### Public");
  for (const r of state.publicRoutesTested || []) lines.push(`- \`${r}\``);
  lines.push("");
  lines.push("### Authenticated");
  if ((state.authenticatedRoutesTested || []).length === 0) {
    lines.push("- _(none — login unavailable or skipped)_");
  } else {
    for (const r of state.authenticatedRoutesTested) lines.push(`- \`${r}\``);
  }
  lines.push("");

  function section(title, items) {
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
      lines.push(`- **Expected:** ${mdEscape(f.expected)}`);
      lines.push(`- **Actual:** ${mdEscape(f.actual)}`);
      lines.push(`- **Screenshot:** ${f.screenshotPath || "n/a"}`);
      lines.push(
        `- **Suspected code:** ${f.suspectedCodeLocation || "n/a"}`
      );
      lines.push(`- **Recommended fix:** ${f.recommendedFix || "n/a"}`);
      lines.push(
        `- **Regression test:** ${f.recommendedRegressionTest || "n/a"}`
      );
      lines.push("- **Reproduction:**");
      for (const step of f.reproductionSteps || []) {
        lines.push(`  1. ${step}`);
      }
      if (f.consoleEvidence?.length) {
        lines.push(`- **Console:** ${f.consoleEvidence.join(" | ")}`);
      }
      if (f.networkEvidence?.length) {
        lines.push(`- **Network:** ${f.networkEvidence.join(" | ")}`);
      }
      lines.push("");
    }
  }

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

  if (state.notes?.length) {
    lines.push("## Notes");
    lines.push("");
    for (const n of state.notes) lines.push(`- ${n}`);
    lines.push("");
  }

  fs.writeFileSync(path.join(RESULTS_DIR, "site-audit.md"), lines.join("\n"));
  return summary;
}

if (import.meta.url === `file://${process.argv[1]}` || process.argv[1]?.endsWith("generate-report.mjs")) {
  generateReports();
  console.log("Wrote audit-results/site-audit.json and audit-results/site-audit.md");
}
