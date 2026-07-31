import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { loginAsAuditUser } from "./helpers/auth";
import { readAuditEnv } from "./helpers/env";
import {
  attachCollectors,
  assessPage,
  layoutIssues,
  recordIssue,
  scanForSecrets,
} from "./helpers/page-checks";
import { updateState } from "./helpers/findings";

const routes = JSON.parse(
  fs.readFileSync(path.join(process.cwd(), "topai-audit-routes.json"), "utf8")
);

function vpName(project: string) {
  return project || "unknown";
}

test.describe("Authenticated subscriber / CRM", () => {
  test.describe.configure({ mode: "serial" });

  test("login and core authenticated expectations", async ({
    page,
  }, testInfo) => {
    const vp = vpName(testInfo.project.name);
    const env = readAuditEnv();
    if (!env.hasAuthCredentials) {
      // Records Manual Action Required finding (no password logged).
      await loginAsAuditUser(page, vp);
      test.skip(true, "TOPAI_AUDIT_EMAIL/PASSWORD missing");
    }

    const ok = await loginAsAuditUser(page, vp);
    // Finding already recorded by login helper; skip remainder instead of
    // duplicating a generic Playwright assertion failure.
    test.skip(!ok, "Authenticated login failed — see recorded High/Manual findings");

    // Reach /app
    await page.goto("/app", { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/app/);

    const body = await page.locator("body").innerText();
    const hasOpenToolsOrDashboard =
      /Open Tools|Dashboard/i.test(body) ||
      (await page.locator('a[href="/dashboard"], a[href="/app"]').count()) > 0;
    const hasLogout =
      (await page.locator('a[href="/logout"], button:has-text("Log out"), a:has-text("Log out")').count()) >
      0;
    const hasSignIn =
      (await page.locator('#mkt-sign-in, a:has-text("Sign in")').count()) > 0 &&
      !(await page.locator('a:has-text("Log out")').count());

    if (!hasOpenToolsOrDashboard) {
      await recordIssue(page, {
        title: "Authenticated nav missing Open Tools/Dashboard",
        severity: "Medium",
        route: "/app",
        viewport: vp,
        authState: "authenticated",
        reproductionSteps: ["Log in", "Open /app", "Inspect navigation"],
        expected: "Open Tools or Dashboard visible",
        actual: "Neither label/link found",
        httpStatus: 200,
      });
    }
    if (!hasLogout) {
      await recordIssue(page, {
        title: "Authenticated nav missing Log out",
        severity: "Medium",
        route: "/app",
        viewport: vp,
        authState: "authenticated",
        reproductionSteps: ["Log in", "Open /app"],
        expected: "Log out control visible",
        actual: "Log out not found",
        httpStatus: 200,
      });
    }
    if (hasSignIn) {
      await recordIssue(page, {
        title: "Authenticated user still sees Sign in",
        severity: "Medium",
        route: "/app",
        viewport: vp,
        authState: "authenticated",
        reproductionSteps: ["Log in", "Open /app"],
        expected: "Sign in hidden for authenticated users",
        actual: "Sign in still visible",
        httpStatus: 200,
      });
    }

    // Active subscribers should not see Start trial / Subscribe prompts in app chrome
    const trialOrSubscribe = await page
      .locator('#mkt-start-trial, a:has-text("Start trial"), text=/Start trial/i')
      .count();
    if (trialOrSubscribe > 0) {
      await recordIssue(page, {
        title: "Active subscriber shown Start trial/Subscribe chrome",
        severity: "High",
        route: "/app",
        viewport: vp,
        authState: "authenticated",
        reproductionSteps: ["Log in as active subscriber", "Open /app"],
        expected: "No Start trial / Subscribe checkout prompts",
        actual: "Trial/Subscribe CTA visible",
        httpStatus: 200,
        suspectedCodeLocation: "templates/marketing_header.html / subscriber headers",
        recommendedFix:
          "Hide marketing subscribe CTAs for users with active subscription",
        recommendedRegressionTest: "tests/test_subscriber_navigation.py",
      });
    }

    // /subscribe should redirect away from duplicate checkout
    const collectors = attachCollectors(page);
    const subRes = await page.goto("/subscribe", {
      waitUntil: "domcontentloaded",
    });
    await assessPage(page, collectors, {
      requestedUrl: "/subscribe",
      response: subRes,
      viewport: vp,
      authState: "authenticated",
      expectHtml: true,
    });
    const subUrl = page.url();
    const checkoutForm = await page.locator("#subscribe-form").count();
    if (/\/subscribe\/?$/.test(new URL(subUrl).pathname) && checkoutForm > 0) {
      await recordIssue(page, {
        title: "Active subscriber can open duplicate subscription form",
        severity: "High",
        route: "/subscribe",
        viewport: vp,
        authState: "authenticated",
        reproductionSteps: [
          "Log in as active subscriber",
          "Open /subscribe",
          "Observe whether checkout form is available",
        ],
        expected: "Redirect to tools or billing; no duplicate checkout",
        actual: `Stayed on ${subUrl} with #subscribe-form present`,
        httpStatus: subRes?.status() ?? null,
        suspectedCodeLocation: "app.py:/subscribe, stripe_billing.py",
        recommendedFix:
          "Redirect active subscribers from /subscribe to /app or billing portal",
        recommendedRegressionTest: "tests/test_subscribe_flow.py",
      });
    }

    // Intentionally do not click checkout / billing portal actions that mutate.
  });

  test("authenticated CRM routes render", async ({ page }, testInfo) => {
    const vp = vpName(testInfo.project.name);
    const env = readAuditEnv();
    if (!env.hasAuthCredentials) {
      test.skip(true, "TOPAI_AUDIT_EMAIL/PASSWORD missing");
    }
    const ok = await loginAsAuditUser(page, vp);
    test.skip(!ok, "login failed");

    for (const route of routes.authenticatedRoutes) {
      const collectors = attachCollectors(page);
      const res = await page.goto(route.path, {
        waitUntil: "domcontentloaded",
      });
      const result = await assessPage(page, collectors, {
        requestedUrl: route.path,
        response: res,
        viewport: vp,
        authState: "authenticated",
        expectHtml: true,
      });

      const body = await page.locator("body").innerText().catch(() => "");
      const secrets = scanForSecrets(body);
      if (secrets.length) {
        await recordIssue(page, {
          title: `Secret-like material on ${route.path}`,
          severity: "Critical",
          route: route.path,
          viewport: vp,
          authState: "authenticated",
          reproductionSteps: ["Log in", `Open ${route.path}`],
          expected: "No API keys/tokens/stack traces/private keys",
          actual: `Matched: ${secrets.join(", ")}`,
          httpStatus: result.status,
        });
      }

      if (/twilio/i.test(body) && /sms/i.test(route.path + body.slice(0, 200))) {
        // Flag customer-facing Twilio copy on SMS pages (Medium)
        if (
          route.path.includes("sms") ||
          route.path.includes("#sms") ||
          route.path === "/crm/sms-diagnostics" ||
          route.path === "/crm/sms-campaigns"
        ) {
          await recordIssue(page, {
            title: `Customer-facing Twilio reference on ${route.path}`,
            severity: "Medium",
            route: route.path,
            viewport: vp,
            authState: "authenticated",
            reproductionSteps: ["Log in", `Open ${route.path}`],
            expected: "Telnyx identified as SMS provider; no stale Twilio UX copy",
            actual: "Page text includes Twilio",
            httpStatus: result.status,
            suspectedCodeLocation: "templates/crm_sms_*.html, templates/index.html SMS panel",
            recommendedFix: "Replace customer-facing Twilio labels with Telnyx",
            recommendedRegressionTest: "tests/test_sms_assistant_telnyx_ui.py",
          });
        }
      }

      const expected = route.expectStatus;
      const allowed = Array.isArray(expected) ? expected : [expected];
      if (result.status != null && !allowed.includes(result.status)) {
        await recordIssue(page, {
          title: `Authenticated route unexpected status: ${route.path}`,
          severity:
            route.path === "/crm/dashboard" ? "Low" : result.status >= 500 ? "High" : "Medium",
          route: route.path,
          viewport: vp,
          authState: "authenticated",
          reproductionSteps: ["Log in", `Open ${route.path}`],
          expected: `HTTP ${allowed.join("|")}`,
          actual: `HTTP ${result.status} final=${result.finalUrl}`,
          httpStatus: result.status,
          consoleEvidence: result.consoleErrors.slice(0, 5),
          networkEvidence: result.httpErrors.slice(0, 5),
        });
      }

      if (!result.ok && result.status === 200) {
        await recordIssue(page, {
          title: `Authenticated page render issue: ${route.path}`,
          severity: "High",
          route: route.path,
          viewport: vp,
          authState: "authenticated",
          reproductionSteps: ["Log in", `Open ${route.path}`],
          expected: "Page renders without error markers / blank content",
          actual: (result.notes || []).join("; "),
          httpStatus: result.status,
          consoleEvidence: result.consoleErrors.slice(0, 8),
        });
      }

      const overflow = await layoutIssues(page);
      if (overflow.length && String(vp).startsWith("mobile")) {
        await recordIssue(page, {
          title: `Mobile layout issue on ${route.path}`,
          severity: "Medium",
          route: route.path,
          viewport: vp,
          authState: "authenticated",
          reproductionSteps: ["Log in", `Open ${route.path} at ${vp}`],
          expected: "Usable layout without horizontal overflow",
          actual: overflow.join("; "),
          httpStatus: result.status,
        });
      }
    }

    // Discover CRM nav links
    await page.goto("/crm/leads", { waitUntil: "domcontentloaded" });
    const navHrefs = await page.$$eval(
      'nav a[href], #tool-nav-bar a[href], [aria-label="Main application navigation"] a[href]',
      (as) => as.map((a) => (a as HTMLAnchorElement).getAttribute("href") || "")
    );
    updateState((s) => {
      s.notes.push(`CRM nav discovered ${navHrefs.length} links`);
    });
    for (const href of [...new Set(navHrefs)]
      .filter((h) => h.startsWith("/") && !h.includes("logout"))
      .slice(0, 30)) {
      const collectors = attachCollectors(page);
      const res = await page.goto(href, { waitUntil: "domcontentloaded" });
      const result = await assessPage(page, collectors, {
        requestedUrl: href,
        response: res,
        viewport: vp,
        authState: "authenticated",
        expectHtml: true,
      });
      if (result.status && result.status >= 500) {
        await recordIssue(page, {
          title: `CRM nav target ${href} returns ${result.status}`,
          severity: "High",
          route: href,
          viewport: vp,
          authState: "authenticated",
          reproductionSteps: ["Log in", "Open CRM nav", `Follow ${href}`],
          expected: "HTTP 200",
          actual: `HTTP ${result.status}`,
          httpStatus: result.status,
        });
      }
    }
  });

  test("SMS diagnostics and compliance guards (read-only)", async ({
    page,
  }, testInfo) => {
    const vp = vpName(testInfo.project.name);
    const env = readAuditEnv();
    if (!env.hasAuthCredentials) {
      test.skip(true, "TOPAI_AUDIT_EMAIL/PASSWORD missing");
    }
    const ok = await loginAsAuditUser(page, vp);
    test.skip(!ok, "login failed");

    await page.goto("/crm/sms-diagnostics", { waitUntil: "domcontentloaded" });
    const diagText = await page.locator("body").innerText();

    if (!/telnyx/i.test(diagText)) {
      await recordIssue(page, {
        title: "SMS Diagnostics does not identify Telnyx",
        severity: "High",
        route: "/crm/sms-diagnostics",
        viewport: vp,
        authState: "authenticated",
        reproductionSteps: ["Log in", "Open /crm/sms-diagnostics"],
        expected: "Telnyx shown as provider",
        actual: "Telnyx not mentioned on diagnostics page",
        httpStatus: 200,
        suspectedCodeLocation: "templates/crm_sms_diagnostics.html",
      });
    }

    // AI SMS Assistant panel
    await page.goto("/app#sms", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1000);
    const smsText = await page.locator("body").innerText();
    if (/twilio/i.test(smsText) && !/telnyx/i.test(smsText)) {
      await recordIssue(page, {
        title: "AI SMS Assistant identifies Twilio instead of Telnyx",
        severity: "High",
        route: "/app#sms",
        viewport: vp,
        authState: "authenticated",
        reproductionSteps: ["Log in", "Open /app#sms"],
        expected: "Telnyx identified",
        actual: "Twilio referenced without Telnyx",
        httpStatus: 200,
        suspectedCodeLocation: "templates/index.html SMS status rendering",
        recommendedRegressionTest: "tests/test_sms_assistant_telnyx_ui.py",
      });
    }

    // Compliance: pending verification should disable sending — inspect controls, do not send
    const sendButtons = page.locator(
      'button:has-text("Send"), button#sms-send, button[data-action="send-sms"]'
    );
    const pending =
      /verification[^\n]{0,40}pending|toll-free verification[^\n]{0,40}pending|SMS sending is unavailable/i.test(
        smsText + diagText
      );

    if (pending) {
      await recordIssue(page, {
        title: "Toll-free verification pending (external)",
        severity: "External Dependency",
        route: "/app#sms",
        viewport: vp,
        authState: "authenticated",
        reproductionSteps: ["Log in", "Open SMS assistant / diagnostics"],
        expected: "UI blocks send/launch while pending",
        actual: "Pending verification messaging observed",
        httpStatus: 200,
        classification: "External Dependency",
        screenshot: true,
      });

      // Check send controls disabled
      const count = await sendButtons.count();
      for (let i = 0; i < Math.min(count, 5); i++) {
        const btn = sendButtons.nth(i);
        const disabled = await btn.isDisabled().catch(() => false);
        const ariaDisabled = (await btn.getAttribute("aria-disabled")) === "true";
        if (!disabled && !ariaDisabled && (await btn.isVisible().catch(() => false))) {
          await recordIssue(page, {
            title: "SMS send control enabled while toll-free verification pending",
            severity: "High",
            route: "/app#sms",
            viewport: vp,
            authState: "authenticated",
            reproductionSteps: [
              "Log in",
              "Open /app#sms while toll-free verification is pending",
              "Inspect Send controls (do not click)",
            ],
            expected: "Send disabled until verification completes",
            actual: "Send control appears enabled",
            httpStatus: 200,
            suspectedCodeLocation: "templates/index.html sms send gating",
            recommendedFix:
              "Disable send buttons when toll_free_verification_blocked is true regardless of consent checkbox",
            recommendedRegressionTest: "tests/test_sms_assistant_telnyx_ui.py",
          });
        }
      }
    }

    await page.goto("/crm/sms-campaigns", { waitUntil: "domcontentloaded" });
    const campText = await page.locator("body").innerText();
    const launch = page.locator(
      'button:has-text("Launch"), button:has-text("Schedule"), form[action*="launch"] button'
    );
    if (
      /verification[^\n]{0,40}pending|disabled until toll-free/i.test(campText)
    ) {
      const n = await launch.count();
      for (let i = 0; i < Math.min(n, 5); i++) {
        const btn = launch.nth(i);
        if (
          (await btn.isVisible().catch(() => false)) &&
          !(await btn.isDisabled().catch(() => true))
        ) {
          await recordIssue(page, {
            title: "Campaign launch enabled while toll-free verification pending",
            severity: "High",
            route: "/crm/sms-campaigns",
            viewport: vp,
            authState: "authenticated",
            reproductionSteps: [
              "Log in",
              "Open /crm/sms-campaigns",
              "Inspect Launch/Schedule (do not activate)",
            ],
            expected: "Launch blocked while verification pending",
            actual: "Launch/Schedule control enabled",
            httpStatus: 200,
            suspectedCodeLocation: "templates/crm_sms_campaigns.html / sms_campaigns.py",
          });
        }
      }
    }

    // Consent checkbox cannot override — inspect only
    await page.goto("/app#sms", { waitUntil: "domcontentloaded" });
    const consent = page.locator(
      'input[type="checkbox"][name*="consent" i], #sms-consent, input#consent'
    );
    if ((await consent.count()) && pending) {
      // Check the box in the UI only if it does not submit a network mutation by itself.
      // Prefer evaluating disabled state after checking without sending.
      const box = consent.first();
      if (await box.isEnabled().catch(() => false)) {
        await box.check({ force: true }).catch(() => null);
        await page.waitForTimeout(300);
        const send = page
          .locator('button:has-text("Send"), button#sms-send')
          .first();
        if (
          (await send.count()) &&
          (await send.isVisible().catch(() => false)) &&
          !(await send.isDisabled().catch(() => true))
        ) {
          await recordIssue(page, {
            title: "Consent checkbox appears to override toll-free verification block",
            severity: "Critical",
            route: "/app#sms",
            viewport: vp,
            authState: "authenticated",
            reproductionSteps: [
              "Log in",
              "Open /app#sms while verification pending",
              "Check consent checkbox",
              "Observe Send enabled (do not click Send)",
            ],
            expected: "Consent cannot override toll-free verification block",
            actual: "Send enabled after checking consent",
            httpStatus: 200,
            suspectedCodeLocation: "templates/index.html sms gating (~consent override)",
            recommendedFix:
              "Keep send blocked when toll_free_verification_blocked regardless of consent UI",
            recommendedRegressionTest: "tests/test_sms_assistant_telnyx_ui.py",
          });
        }
      }
    }

    // CSV import page loads without uploading
    await page.goto("/crm/external-leads/import", {
      waitUntil: "domcontentloaded",
    });
    expect(page.url()).toContain("/crm/external-leads/import");
    // Do not upload or commit CSV.
  });
});
