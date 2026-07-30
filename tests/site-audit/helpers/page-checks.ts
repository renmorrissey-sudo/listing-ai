import type { Page, Response } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import {
  addFinding,
  addRouteResult,
  findingId,
  screenshotsDir,
  type Finding,
  type RouteResult,
  type Severity,
} from "./findings";

const ERROR_MARKERS = [
  "Something went wrong",
  "Internal Server Error",
  "Traceback (most recent call last)",
  "Reference Error",
  "Correlation ID",
  "correlation_id",
];

const SECRET_PATTERNS: { name: string; re: RegExp }[] = [
  { name: "stripe_secret", re: /\bsk_live_[A-Za-z0-9]{10,}\b/ },
  { name: "stripe_test_secret", re: /\bsk_test_[A-Za-z0-9]{10,}\b/ },
  { name: "openai_key", re: /\bsk-[A-Za-z0-9]{20,}\b/ },
  { name: "aws_key", re: /\bAKIA[0-9A-Z]{16}\b/ },
  { name: "private_key", re: /-----BEGIN (RSA |EC )?PRIVATE KEY-----/ },
  { name: "telnyx_api_key_like", re: /\bKEY[A-Z0-9]{20,}\b/ },
  { name: "webhook_sig_header", re: /Telnyx-Signature-Ed25519\s*[:=]\s*\S+/i },
];

export type Collectors = {
  consoleErrors: string[];
  pageErrors: string[];
  failedRequests: string[];
  httpErrors: string[];
  responses: { url: string; status: number; contentType: string }[];
};

export function attachCollectors(page: Page): Collectors {
  const collectors: Collectors = {
    consoleErrors: [],
    pageErrors: [],
    failedRequests: [],
    httpErrors: [],
    responses: [],
  };

  page.on("console", (msg) => {
    if (msg.type() === "error") {
      collectors.consoleErrors.push(msg.text());
    }
  });
  page.on("pageerror", (err) => {
    collectors.pageErrors.push(String(err));
  });
  page.on("requestfailed", (req) => {
    collectors.failedRequests.push(
      `${req.method()} ${req.url()} :: ${req.failure()?.errorText || "failed"}`
    );
  });
  page.on("response", (res) => {
    const status = res.status();
    const ct = res.headers()["content-type"] || "";
    collectors.responses.push({ url: res.url(), status, contentType: ct });
    if (status >= 400) {
      collectors.httpErrors.push(`${status} ${res.request().method()} ${res.url()}`);
    }
  });

  return collectors;
}

export async function captureScreenshot(
  page: Page,
  name: string
): Promise<string | null> {
  try {
    const dir = screenshotsDir();
    const file = path.join(dir, `${sanitize(name)}.png`);
    await page.screenshot({ path: file, fullPage: true });
    return path.relative(process.cwd(), file);
  } catch {
    return null;
  }
}

function sanitize(s: string): string {
  return s.replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 120);
}

export async function assessPage(
  page: Page,
  collectors: Collectors,
  opts: {
    requestedUrl: string;
    response: Response | null;
    viewport: string;
    authState: RouteResult["authState"];
    expectHtml?: boolean;
  }
): Promise<RouteResult> {
  const status = opts.response?.status() ?? null;
  const contentType = opts.response?.headers()["content-type"] || null;
  let title: string | null = null;
  let bodyText = "";
  let visibleErrors: string[] = [];

  try {
    title = await page.title();
  } catch {
    title = null;
  }

  try {
    bodyText = await page.locator("body").innerText({ timeout: 5000 });
  } catch {
    bodyText = "";
  }

  for (const marker of ERROR_MARKERS) {
    if (bodyText.includes(marker)) visibleErrors.push(marker);
  }

  const isBlank =
    Boolean(opts.expectHtml) &&
    (!bodyText || bodyText.replace(/\s+/g, "").length < 20);

  const looksLikeRawJson =
    Boolean(opts.expectHtml) &&
    /^\s*[\{\[]/.test(bodyText) &&
    !bodyText.includes("<html") &&
    (contentType || "").includes("json");

  // Detect raw JSON rendered in HTML context (browser shows JSON text)
  const rawJsonInBrowser =
    Boolean(opts.expectHtml) &&
    (await page.evaluate(() => {
      const t = document.body?.innerText?.trim() || "";
      return (
        (t.startsWith("{") || t.startsWith("[")) &&
        document.body?.children.length <= 1 &&
        !document.querySelector("nav, header, main, form")
      );
    }).catch(() => false));

  const result: RouteResult = {
    requestedUrl: opts.requestedUrl,
    finalUrl: page.url(),
    status,
    contentType,
    title,
    viewport: opts.viewport,
    authState: opts.authState,
    visibleErrors,
    consoleErrors: [...collectors.consoleErrors],
    pageErrors: [...collectors.pageErrors],
    failedRequests: [...collectors.failedRequests],
    httpErrors: [...collectors.httpErrors],
    ok: true,
    notes: [],
  };

  if (status !== null && status >= 400) {
    result.ok = false;
    result.notes?.push(`HTTP ${status}`);
  }
  if (isBlank) {
    result.ok = false;
    result.notes?.push("blank page");
  }
  if (looksLikeRawJson || rawJsonInBrowser) {
    result.ok = false;
    result.notes?.push("raw JSON displayed");
  }
  if (visibleErrors.length) {
    result.ok = false;
    result.notes?.push(`visible errors: ${visibleErrors.join(", ")}`);
  }
  if (collectors.pageErrors.length) {
    result.ok = false;
    result.notes?.push("uncaught JS errors");
  }

  addRouteResult(result);
  return result;
}

export async function recordIssue(
  page: Page,
  opts: {
    title: string;
    severity: Severity;
    route: string;
    viewport: string;
    authState: Finding["authState"];
    reproductionSteps: string[];
    expected: string;
    actual: string;
    httpStatus?: number | null;
    consoleEvidence?: string[];
    networkEvidence?: string[];
    suspectedCodeLocation?: string | null;
    recommendedFix?: string | null;
    recommendedRegressionTest?: string | null;
    classification?: string;
    screenshot?: boolean;
  }
): Promise<void> {
  let screenshotPath: string | null = null;
  if (opts.screenshot !== false) {
    screenshotPath = await captureScreenshot(
      page,
      `${opts.severity}_${opts.viewport}_${opts.route}`
    );
  }
  addFinding({
    id: findingId(opts.severity, opts.route, opts.title, opts.viewport),
    title: opts.title,
    severity: opts.severity,
    route: opts.route,
    timestamp: new Date().toISOString(),
    viewport: opts.viewport,
    authState: opts.authState,
    reproductionSteps: opts.reproductionSteps,
    expected: opts.expected,
    actual: opts.actual,
    httpStatus: opts.httpStatus ?? null,
    consoleEvidence: opts.consoleEvidence || [],
    networkEvidence: opts.networkEvidence || [],
    screenshotPath,
    suspectedCodeLocation: opts.suspectedCodeLocation || null,
    recommendedFix: opts.recommendedFix || null,
    recommendedRegressionTest: opts.recommendedRegressionTest || null,
    classification: opts.classification,
  });
}

export function scanForSecrets(text: string): string[] {
  const hits: string[] = [];
  for (const { name, re } of SECRET_PATTERNS) {
    if (re.test(text)) hits.push(name);
  }
  return hits;
}

export async function layoutIssues(
  page: Page
): Promise<string[]> {
  return page.evaluate(() => {
    const issues: string[] = [];
    const doc = document.documentElement;
    if (doc.scrollWidth > window.innerWidth + 2) {
      issues.push(
        `horizontal overflow: scrollWidth=${doc.scrollWidth} innerWidth=${window.innerWidth}`
      );
    }
    return issues;
  });
}

export function writeJson(filePath: string, data: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2));
}
