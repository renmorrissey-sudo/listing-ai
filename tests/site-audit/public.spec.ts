import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import {
  attachCollectors,
  assessPage,
  layoutIssues,
  recordIssue,
  scanForSecrets,
} from "./helpers/page-checks";

const routes = JSON.parse(
  fs.readFileSync(path.join(process.cwd(), "topai-audit-routes.json"), "utf8")
);

function viewportName(projectName: string): string {
  return projectName || "unknown";
}

test.describe("Public routes", () => {
  for (const route of routes.publicRoutes) {
    test(`public ${route.path}`, async ({ page }, testInfo) => {
      const vp = viewportName(testInfo.project.name);
      const collectors = attachCollectors(page);

      if (route.kind === "asset" || route.kind === "health") {
        const res = await page.request.get(route.path);
        const ct = res.headers()["content-type"] || "";
        const status = res.status();
        const body = await res.text();

        if (status !== route.expectStatus) {
          await recordIssue(page, {
            title: `${route.path} unexpected status`,
            severity: "High",
            route: route.path,
            viewport: vp,
            authState: "logged-out",
            reproductionSteps: [`GET ${route.path}`],
            expected: `HTTP ${route.expectStatus}`,
            actual: `HTTP ${status}`,
            httpStatus: status,
            screenshot: false,
          });
        }
        expect(status).toBe(route.expectStatus);
        expect(ct.toLowerCase()).toContain(
          String(route.contentTypeIncludes).toLowerCase()
        );

        const secrets = scanForSecrets(body);
        if (secrets.length) {
          await recordIssue(page, {
            title: `Secret-like material exposed on ${route.path}`,
            severity: "Critical",
            route: route.path,
            viewport: vp,
            authState: "logged-out",
            reproductionSteps: [`GET ${route.path}`, "Inspect response body"],
            expected: "No API keys, tokens, or private material",
            actual: `Matched patterns: ${secrets.join(", ")}`,
            httpStatus: status,
            screenshot: false,
            suspectedCodeLocation: "app.py health/static handlers",
            recommendedFix: "Remove secret material from public responses",
            recommendedRegressionTest: "public.spec.ts secret scan",
          });
        }

        if (route.kind === "health") {
          const json = JSON.parse(body);
          if (json.status !== "ok") {
            await recordIssue(page, {
              title: "/health status is not ok",
              severity: "High",
              route: "/health",
              viewport: vp,
              authState: "logged-out",
              reproductionSteps: ["GET /health"],
              expected: 'status="ok"',
              actual: `status=${json.status}`,
              httpStatus: status,
              screenshot: false,
            });
          }
          if (json.sms_provider !== "telnyx") {
            await recordIssue(page, {
              title: "/health sms_provider is not telnyx",
              severity: "High",
              route: "/health",
              viewport: vp,
              authState: "logged-out",
              reproductionSteps: ["GET /health"],
              expected: 'sms_provider="telnyx"',
              actual: `sms_provider=${json.sms_provider}`,
              httpStatus: status,
              screenshot: false,
              suspectedCodeLocation: "config.SMS_PROVIDER / app.py:/health",
            });
          }
          if (json.telnyx_configured !== true) {
            await recordIssue(page, {
              title: "/health telnyx_configured is not true",
              severity: "High",
              route: "/health",
              viewport: vp,
              authState: "logged-out",
              reproductionSteps: ["GET /health"],
              expected: "telnyx_configured=true",
              actual: `telnyx_configured=${json.telnyx_configured}`,
              httpStatus: status,
              screenshot: false,
            });
          }
          if (json.toll_free_verification_status === "pending") {
            await recordIssue(page, {
              title: "Telnyx toll-free verification still pending",
              severity: "External Dependency",
              route: "/health",
              viewport: vp,
              authState: "logged-out",
              reproductionSteps: ["GET /health"],
              expected:
                "Vendor verification may be pending; site must handle safely",
              actual:
                "toll_free_verification_status=pending; sms_sending_enabled=" +
                String(json.sms_sending_enabled),
              httpStatus: status,
              screenshot: false,
              classification: "External Dependency",
              recommendedFix:
                "Complete Telnyx toll-free verification in vendor dashboard (not a code change)",
              recommendedRegressionTest:
                "Authenticated SMS UI must block send/launch while pending",
            });
          }
          expect(json.status).toBe("ok");
          expect(json.sms_provider).toBe("telnyx");
          expect(json.telnyx_configured).toBe(true);
        }
        return;
      }

      const response = await page.goto(route.path, {
        waitUntil: "domcontentloaded",
      });
      const result = await assessPage(page, collectors, {
        requestedUrl: route.path,
        response,
        viewport: vp,
        authState: "logged-out",
        expectHtml: true,
      });

      const body = await page.locator("body").innerText().catch(() => "");
      const secrets = scanForSecrets(body);
      if (secrets.length) {
        await recordIssue(page, {
          title: `Secret-like material visible on ${route.path}`,
          severity: "Critical",
          route: route.path,
          viewport: vp,
          authState: "logged-out",
          reproductionSteps: [`Open ${route.path}`],
          expected: "No secrets visible",
          actual: `Matched: ${secrets.join(", ")}`,
          httpStatus: result.status,
        });
      }

      if (!result.ok) {
        await recordIssue(page, {
          title: `Public page issue on ${route.path}`,
          severity: result.status && result.status >= 500 ? "High" : "Medium",
          route: route.path,
          viewport: vp,
          authState: "logged-out",
          reproductionSteps: [`Open ${route.path} at viewport ${vp}`],
          expected: "Successful HTML page without error markers or blank content",
          actual: (result.notes || []).join("; ") || "page check failed",
          httpStatus: result.status,
          consoleEvidence: result.consoleErrors.slice(0, 8),
          networkEvidence: result.httpErrors.slice(0, 8),
        });
      }

      const overflow = await layoutIssues(page);
      if (overflow.length && vp.startsWith("mobile")) {
        await recordIssue(page, {
          title: `Mobile layout overflow on ${route.path}`,
          severity: "Medium",
          route: route.path,
          viewport: vp,
          authState: "logged-out",
          reproductionSteps: [`Open ${route.path} at ${vp}`],
          expected: "No horizontal scrolling / clipped primary content",
          actual: overflow.join("; "),
          httpStatus: result.status,
        });
      }

      expect(result.status).toBe(route.expectStatus);
      expect(result.visibleErrors).toEqual([]);
      if (route.titleIncludes) {
        expect(result.title || "").toContain(route.titleIncludes);
      }
    });
  }
});

test.describe("Public navigation expectations", () => {
  test("homepage CTAs and legal links", async ({ page }, testInfo) => {
    const vp = viewportName(testInfo.project.name);
    const collectors = attachCollectors(page);
    const response = await page.goto("/", { waitUntil: "domcontentloaded" });
    await assessPage(page, collectors, {
      requestedUrl: "/",
      response,
      viewport: vp,
      authState: "logged-out",
      expectHtml: true,
    });

    const signIn = page.locator('a[href="/login"], a[href^="/login?"]').first();
    await expect(signIn).toBeVisible();

    const subscribe = page.locator('a[href="/subscribe"]').first();
    await expect(subscribe).toBeVisible();

    // View pricing — href may be /pricing or #pricing
    const pricing = page
      .locator('a:has-text("View pricing"), a[href="/pricing"], a[href*="#pricing"]')
      .first();
    if (await pricing.count()) {
      await expect(pricing).toBeVisible();
    } else {
      await recordIssue(page, {
        title: "View pricing CTA missing on homepage",
        severity: "Medium",
        route: "/",
        viewport: vp,
        authState: "logged-out",
        reproductionSteps: ["Open /", "Look for View pricing CTA"],
        expected: "View pricing routes to pricing section or /pricing",
        actual: "No pricing CTA found",
        httpStatus: response?.status() ?? null,
      });
    }

    for (const pathName of ["/terms", "/privacy", "/contact", "/sms-consent"]) {
      const link = page.locator(`a[href="${pathName}"]`).first();
      if ((await link.count()) === 0) {
        await recordIssue(page, {
          title: `Missing public link to ${pathName}`,
          severity: "Medium",
          route: "/",
          viewport: vp,
          authState: "logged-out",
          reproductionSteps: ["Open /", `Find link to ${pathName}`],
          expected: `Link to ${pathName} present in header/footer`,
          actual: "Link not found",
          httpStatus: 200,
        });
      }
    }
  });

  test("discover internal public links", async ({ page }, testInfo) => {
    const vp = viewportName(testInfo.project.name);
    // Run discovery primarily on desktop to avoid triple-network load; still record once per project.
    const seeds: string[] = routes.discoverFromPublic || ["/"];
    const discovered = new Set<string>();

    for (const seed of seeds) {
      await page.goto(seed, { waitUntil: "domcontentloaded" });
      const hrefs = await page.$$eval("a[href]", (as) =>
        as
          .map((a) => (a as HTMLAnchorElement).getAttribute("href") || "")
          .filter(Boolean)
      );
      for (const href of hrefs) {
        if (
          href.startsWith("/") &&
          !href.startsWith("//") &&
          !href.startsWith("/api/") &&
          !href.startsWith("/webhook") &&
          !href.includes("logout")
        ) {
          discovered.add(href.split("?")[0].split("#")[0] || "/");
        }
      }
    }

    // Also try opening mobile nav if present
    await page.goto("/", { waitUntil: "domcontentloaded" });
    const toggles = page.locator(
      'button[aria-label*="menu" i], button.menu, .nav-toggle, #nav-toggle, button:has-text("Menu")'
    );
    if (await toggles.count()) {
      await toggles.first().click().catch(() => null);
      const hrefs = await page.$$eval("a[href]", (as) =>
        as.map((a) => (a as HTMLAnchorElement).getAttribute("href") || "")
      );
      for (const href of hrefs) {
        if (href.startsWith("/") && !href.startsWith("//")) {
          discovered.add(href.split("?")[0].split("#")[0] || "/");
        }
      }
    }

    const publicOnly = [...discovered].filter(
      (p) =>
        !p.startsWith("/crm") &&
        !p.startsWith("/app") &&
        !p.startsWith("/dashboard") &&
        !p.startsWith("/account") &&
        !p.startsWith("/billing") &&
        p !== "/logout"
    );

    for (const p of publicOnly.slice(0, 40)) {
      const collectors = attachCollectors(page);
      const res = await page.goto(p, { waitUntil: "domcontentloaded" });
      const result = await assessPage(page, collectors, {
        requestedUrl: p,
        response: res,
        viewport: vp,
        authState: "logged-out",
        expectHtml: (res?.headers()["content-type"] || "").includes("html"),
      });
      if (result.status && result.status >= 500) {
        await recordIssue(page, {
          title: `Discovered public link returns ${result.status}`,
          severity: "High",
          route: p,
          viewport: vp,
          authState: "logged-out",
          reproductionSteps: [`From homepage navigation, open ${p}`],
          expected: "HTTP 200 HTML",
          actual: `HTTP ${result.status}`,
          httpStatus: result.status,
          consoleEvidence: result.consoleErrors.slice(0, 5),
        });
      } else if (result.status === 404) {
        await recordIssue(page, {
          title: `Broken public link ${p}`,
          severity: "Medium",
          route: p,
          viewport: vp,
          authState: "logged-out",
          reproductionSteps: [`Follow in-page link to ${p}`],
          expected: "Reachable page",
          actual: "HTTP 404",
          httpStatus: 404,
        });
      }
    }
  });

  test("forgot-password page loads without submitting", async ({
    page,
  }, testInfo) => {
    const vp = viewportName(testInfo.project.name);
    const collectors = attachCollectors(page);
    const res = await page.goto("/forgot-password", {
      waitUntil: "domcontentloaded",
    });
    const result = await assessPage(page, collectors, {
      requestedUrl: "/forgot-password",
      response: res,
      viewport: vp,
      authState: "logged-out",
      expectHtml: true,
    });
    await expect(page.locator("#email")).toBeVisible();
    // Intentionally do NOT submit — would send email.
    expect(result.status).toBe(200);
  });

  test("sms-consent loads without submitting", async ({ page }, testInfo) => {
    const vp = viewportName(testInfo.project.name);
    const collectors = attachCollectors(page);
    const res = await page.goto("/sms-consent", {
      waitUntil: "domcontentloaded",
    });
    await assessPage(page, collectors, {
      requestedUrl: "/sms-consent",
      response: res,
      viewport: vp,
      authState: "logged-out",
      expectHtml: true,
    });
    expect(res?.status()).toBe(200);
    // Do not click submit / accept.
  });
});
