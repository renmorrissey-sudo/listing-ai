import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const DEFAULT_AUDIT_ORIGIN = "https://" + "topai" + "realestatetools.com";
const baseURL =
  process.env.TOPAI_AUDIT_BASE_URL?.replace(/\/$/, "") || DEFAULT_AUDIT_ORIGIN;

/**
 * Read-only production audit configuration.
 * Never points mutating helpers at production without explicit env gates.
 */
export default defineConfig({
  testDir: path.join(__dirname, "tests/site-audit"),
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [
    ["list"],
    ["json", { outputFile: "audit-results/playwright-raw.json" }],
    ["./tests/site-audit/reporter.ts"],
  ],
  outputDir: "audit-results/test-artifacts",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    ignoreHTTPSErrors: false,
    actionTimeout: 20_000,
    navigationTimeout: 45_000,
    extraHTTPHeaders: {
      "User-Agent": "TopAI-Site-Auditor/1.0 (+read-only production audit)",
    },
  },
  projects: [
    {
      name: "desktop-1440",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
      },
    },
    {
      name: "tablet-768",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 768, height: 1024 },
        isMobile: false,
        hasTouch: true,
      },
    },
    {
      name: "mobile-390",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
      },
    },
  ],
});
