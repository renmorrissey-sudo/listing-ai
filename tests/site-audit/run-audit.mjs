#!/usr/bin/env node
/**
 * TopAI production site audit entrypoint.
 * Never prints password values.
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "../..");
const resultsDir = path.join(root, "audit-results");

function presence(name) {
  const v = process.env[name];
  return v && String(v).trim() ? "PRESENT" : "MISSING";
}

const envPresence = {
  TOPAI_AUDIT_BASE_URL: presence("TOPAI_AUDIT_BASE_URL"),
  TOPAI_AUDIT_EMAIL: presence("TOPAI_AUDIT_EMAIL"),
  TOPAI_AUDIT_PASSWORD: presence("TOPAI_AUDIT_PASSWORD"),
};

if (envPresence.TOPAI_AUDIT_BASE_URL === "MISSING") {
  process.env.TOPAI_AUDIT_BASE_URL = "https://" + "topai" + "realestatetools.com";
  console.log(
    "TOPAI_AUDIT_BASE_URL missing — defaulting to production origin"
  );
  envPresence.TOPAI_AUDIT_BASE_URL = "PRESENT(default)";
}

console.log("Audit env presence:", JSON.stringify(envPresence));
console.log("Base URL host:", new URL(process.env.TOPAI_AUDIT_BASE_URL).host);

fs.mkdirSync(path.join(resultsDir, "screenshots"), { recursive: true });

const startedAt = new Date().toISOString();
const state = {
  startedAt,
  completedAt: null,
  baseUrl: process.env.TOPAI_AUDIT_BASE_URL.replace(/\/$/, ""),
  envPresence,
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

if (envPresence.TOPAI_AUDIT_EMAIL === "MISSING" || envPresence.TOPAI_AUDIT_PASSWORD === "MISSING") {
  state.notes.push(
    "Auth credentials incomplete — authenticated suites will record Manual Action Required and skip login-dependent checks"
  );
}

fs.writeFileSync(
  path.join(resultsDir, ".audit-state.json"),
  JSON.stringify(state, null, 2)
);

const pw = spawnSync(
  "npx",
  ["playwright", "test", "--config=playwright.config.ts"],
  {
    cwd: root,
    stdio: "inherit",
    env: process.env,
    shell: true,
  }
);

// Ensure reports exist even if reporter failed
const gen = spawnSync("node", ["tests/site-audit/generate-report.mjs"], {
  cwd: root,
  stdio: "inherit",
  env: process.env,
});

const exitCode = pw.status === 0 && gen.status === 0 ? 0 : pw.status || gen.status || 1;
console.log(`Audit finished with exit code ${exitCode}`);
process.exit(exitCode);
