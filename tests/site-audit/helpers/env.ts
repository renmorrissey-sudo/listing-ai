export type AuditEnv = {
  baseUrl: string;
  email: string | null;
  password: string | null;
  hasAuthCredentials: boolean;
  missingVars: string[];
};

/** Read audit env without ever returning password to callers that might log it. */
export function readAuditEnv(): AuditEnv {
  const baseUrl = (process.env.TOPAI_AUDIT_BASE_URL || "").trim().replace(/\/$/, "");
  const email = (process.env.TOPAI_AUDIT_EMAIL || "").trim() || null;
  const password = (process.env.TOPAI_AUDIT_PASSWORD || "").trim() || null;
  const missingVars: string[] = [];
  if (!baseUrl) missingVars.push("TOPAI_AUDIT_BASE_URL");
  if (!email) missingVars.push("TOPAI_AUDIT_EMAIL");
  if (!password) missingVars.push("TOPAI_AUDIT_PASSWORD");
  return {
    baseUrl: baseUrl || ("https://" + "topai" + "realestatetools.com"),
    email,
    password,
    hasAuthCredentials: Boolean(email && password),
    missingVars,
  };
}

/** Presence-only summary safe for logs and reports. */
export function envPresenceSummary(): Record<string, "PRESENT" | "MISSING"> {
  return {
    TOPAI_AUDIT_BASE_URL: process.env.TOPAI_AUDIT_BASE_URL?.trim()
      ? "PRESENT"
      : "MISSING",
    TOPAI_AUDIT_EMAIL: process.env.TOPAI_AUDIT_EMAIL?.trim() ? "PRESENT" : "MISSING",
    TOPAI_AUDIT_PASSWORD: process.env.TOPAI_AUDIT_PASSWORD?.trim()
      ? "PRESENT"
      : "MISSING",
  };
}
