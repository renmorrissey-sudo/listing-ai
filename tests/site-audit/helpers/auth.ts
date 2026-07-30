import type { Page } from "@playwright/test";
import { readAuditEnv } from "./env";
import { updateState } from "./findings";
import { attachCollectors, recordIssue } from "./page-checks";

/**
 * Read-only login for the audit subscriber account.
 * Does not create accounts, reset passwords, or mutate billing.
 */
export async function loginAsAuditUser(
  page: Page,
  viewport: string
): Promise<boolean> {
  const env = readAuditEnv();
  if (!env.hasAuthCredentials) {
    updateState((s) => {
      s.loginSucceeded = false;
      s.notes.push(
        "Authenticated audit skipped: TOPAI_AUDIT_EMAIL and/or TOPAI_AUDIT_PASSWORD missing"
      );
    });
    await recordIssue(page, {
      title: "Audit credentials not configured",
      severity: "Manual Action Required",
      route: "/login",
      viewport,
      authState: "logged-out",
      reproductionSteps: [
        "Inspect Cursor automation / cloud environment secrets",
        "Confirm TOPAI_AUDIT_EMAIL and TOPAI_AUDIT_PASSWORD are injected for the auditor run",
      ],
      expected:
        "TOPAI_AUDIT_BASE_URL, TOPAI_AUDIT_EMAIL, and TOPAI_AUDIT_PASSWORD are present",
      actual: `Missing: ${env.missingVars.join(", ") || "auth credentials"}`,
      recommendedFix:
        "Add TOPAI_AUDIT_* secrets to the Cursor automation environment (never commit them)",
      recommendedRegressionTest:
        "run-audit.mjs should fail fast with Manual Action Required when auth env is absent",
      screenshot: false,
      classification: "Manual Action Required",
    });
    return false;
  }

  const collectors = attachCollectors(page);
  const response = await page.goto("/login", { waitUntil: "domcontentloaded" });
  await page.fill("#email", env.email!);
  await page.fill("#password", env.password!);
  await Promise.all([
    page.waitForNavigation({ waitUntil: "domcontentloaded", timeout: 45_000 }).catch(() => null),
    page.click('button[type="submit"]'),
  ]);

  const url = page.url();
  const loggedIn =
    /\/(app|dashboard|crm)\b/.test(url) ||
    (await page
      .locator("text=/Log out|Open Tools|Dashboard/i")
      .first()
      .isVisible()
      .catch(() => false));

  updateState((s) => {
    s.loginSucceeded = loggedIn;
  });

  if (!loggedIn) {
    await recordIssue(page, {
      title: "Authenticated login failed",
      severity: "High",
      route: "/login",
      viewport,
      authState: "logged-out",
      reproductionSteps: [
        "Open /login",
        "Submit the configured audit subscriber credentials",
        "Observe final URL and page content",
      ],
      expected: "User reaches /app or authenticated CRM/tools with Log out visible",
      actual: `Final URL: ${url}; status=${response?.status() ?? "n/a"}; console=${collectors.consoleErrors.slice(0, 3).join(" | ") || "none"}`,
      httpStatus: response?.status() ?? null,
      consoleEvidence: collectors.consoleErrors.slice(0, 10),
      networkEvidence: collectors.httpErrors.slice(0, 10),
      suspectedCodeLocation: "app.py:/login, auth.py",
      recommendedFix:
        "Verify audit account exists with active subscription; inspect login error handling",
      recommendedRegressionTest: "tests/site-audit/auth.spec.ts login flow",
    });
  }

  return loggedIn;
}
