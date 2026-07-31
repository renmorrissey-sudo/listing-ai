import type {
  FullConfig,
  FullResult,
  Reporter,
  Suite,
  TestCase,
  TestResult,
} from "@playwright/test/reporter";
import { updateState } from "./helpers/findings";
import { generateReports } from "./generate-report";

class TopAiAuditReporter implements Reporter {
  onBegin(_config: FullConfig, _suite: Suite): void {
    // state initialized by run-audit.mjs
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    updateState((s) => {
      if (result.status === "passed") s.testsPassed += 1;
      else if (result.status === "skipped") s.testsSkipped += 1;
      else s.testsFailed += 1;
      if (result.status === "failed" || result.status === "timedOut") {
        const title = test.title;
        const project = test.parent.project()?.name || "unknown";
        // Avoid duplicate finding noise if specs already recorded; add summary only when none exist for this test
        const already = s.findings.some((f) => f.title.includes(title));
        if (!already) {
          s.findings.push({
            id: `test-fail|${project}|${title}`.slice(0, 160),
            title: `Test failed: ${title}`,
            severity: "High",
            route: "(see test)",
            timestamp: new Date().toISOString(),
            viewport: project,
            authState: "unknown",
            reproductionSteps: [`npx playwright test -g ${JSON.stringify(title)}`],
            expected: "Test passes",
            actual: result.error?.message?.split("\n")[0] || result.status,
            httpStatus: null,
            consoleEvidence: [],
            networkEvidence: [],
            screenshotPath:
              result.attachments.find((a) => a.name === "screenshot")?.path ||
              null,
            suspectedCodeLocation: null,
            recommendedFix: "Inspect failure evidence and regenerate focused regression",
            recommendedRegressionTest: title,
          });
        }
      }
    });
  }

  async onEnd(result: FullResult): Promise<void> {
    updateState((s) => {
      s.completedAt = new Date().toISOString();
      s.notes.push(`Playwright status: ${result.status}`);
    });
    generateReports();
  }
}

export default TopAiAuditReporter;
