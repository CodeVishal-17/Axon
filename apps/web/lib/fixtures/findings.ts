/**
 * Truth Feed fixtures — typed as the GENERATED FindingOut, so any backend
 * schema change breaks this file at compile time. Replaced by the real
 * findings API in T2.5; the shapes here are the demo-narrative findings.
 */

import type { FindingOut } from "@/lib/api";

const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();

let seq = 0;
const uid = () => {
  seq += 1;
  return `00000000-0000-4000-8000-${String(seq).padStart(12, "0")}`;
};

type Template = (createdMinutesAgo: number) => FindingOut;

/** The demo's money finding: doc contradicted by a merged PR, with diff. */
const docDrift: Template = (m) => ({
  id: uid(),
  kind: "doc_drift",
  severity: "high",
  status: "open",
  explanation:
    "docs/auth.md states a 24-hour access-token lifetime, but PR #47 reduced TOKEN_TTL_HOURS to 1. Every consumer following the documented lifetime will hold expired tokens.",
  suggested_action: "Update docs/auth.md to describe the 1-hour token lifetime.",
  evidence: {
    quotes: [
      {
        text: 'export const TOKEN_TTL_HOURS = 1; // was 24\nexport function issueToken(user: User): Token {\n  return sign(user, { expiresIn: `${TOKEN_TTL_HOURS}h` });\n}',
        path: "src/auth/token.ts",
        start_line: 12,
        language: "ts",
      },
    ],
    diff: "@@ -10,7 +10,7 @@ import { sign } from './jwt';\n-export const TOKEN_TTL_HOURS = 24;\n+export const TOKEN_TTL_HOURS = 1;\n // Tokens are refreshed by the session middleware.",
  },
  claim: {
    id: uid(),
    statement: "Access tokens expire after 24 hours.",
    claim_type: "behavior",
    status: "contradicted",
    anchor: { path: "docs/auth.md", start_line: 12, end_line: 14 },
  },
  event: {
    id: uid(),
    kind: "pr_merged",
    external_id: "47",
    created_at: minutesAgo(m + 1),
  },
  created_at: minutesAgo(m),
});

/** Long claim statement + long explanation — layout stress test. */
const longClaim: Template = (m) => ({
  id: uid(),
  kind: "contradiction",
  severity: "critical",
  status: "open",
  explanation:
    "The architecture document promises exactly-once delivery semantics across the event pipeline, including consumer retries, idempotent projections, and a deduplication window sized to the maximum consumer lag. The current implementation switched to at-least-once delivery with no deduplication when the outbox worker was replaced, so every downstream projection can now observe duplicates under retry.",
  suggested_action: "Rewrite the delivery-guarantees section of docs/architecture.md.",
  evidence: {
    quotes: [
      {
        text: "def deliver(self, event: Event) -> None:\n    # NOTE: retries may deliver twice; consumers must dedupe themselves\n    for attempt in range(self.max_attempts):\n        try:\n            self.publish(event)\n            return\n        except TransportError:\n            continue",
        path: "pipeline/outbox.py",
        start_line: 88,
        language: "py",
      },
    ],
    diff: null,
  },
  claim: {
    id: uid(),
    statement:
      "The event pipeline guarantees exactly-once delivery end to end: producers write through a transactional outbox, the dispatcher deduplicates on event id within a 24-hour window, and consumer projections are idempotent, so no downstream system will ever observe a duplicate or missing event regardless of retry behavior, redeploys, or transport failures.",
    claim_type: "architecture",
    status: "contradicted",
    anchor: { path: "docs/architecture.md", start_line: 210, end_line: 228 },
  },
  event: null,
  created_at: minutesAgo(m),
});

/** Long evidence block — scroll containment stress test. */
const longEvidence: Template = (m) => ({
  id: uid(),
  kind: "doc_drift",
  severity: "medium",
  status: "open",
  explanation:
    "The setup guide lists eight environment variables; the config module now requires thirteen, and three of the documented names were renamed.",
  suggested_action: "Regenerate the environment table in docs/setup.md.",
  evidence: {
    quotes: [
      {
        text: Array.from({ length: 30 }, (_, i) => {
          const names = [
            "DATABASE_URL", "REDIS_URL", "SECRET_KEY", "API_ORIGIN",
            "SMTP_HOST", "SMTP_PORT", "OAUTH_CLIENT_ID", "OAUTH_SECRET",
            "FEATURE_FLAGS", "LOG_LEVEL",
          ];
          const name = names[i % names.length];
          return `    ${name.padEnd(24)}= Field(${i % 3 === 0 ? "..." : `"default_${i}"`})  # required since v2.${i}`;
        }).join("\n"),
        path: "app/config.py",
        start_line: 41,
        language: "py",
      },
    ],
    diff: null,
  },
  claim: {
    id: uid(),
    statement: "Eight environment variables are required to run the service.",
    claim_type: "process",
    status: "stale",
    anchor: { path: "docs/setup.md", start_line: 33, end_line: 52 },
  },
  event: {
    id: uid(),
    kind: "push",
    external_id: "9f2c1ab",
    created_at: minutesAgo(m + 3),
  },
  created_at: minutesAgo(m),
});

const staleIssue: Template = (m) => ({
  id: uid(),
  kind: "stale_issue",
  severity: "low",
  status: "open",
  explanation:
    "Issue #31 tracks a crash in the CSV exporter, but the exporter module it references was deleted four months ago when exports moved to the reporting service.",
  suggested_action: "Close issue #31 as obsolete with a pointer to the reporting service.",
  evidence: {
    quotes: [
      {
        text: "Issue #31: \"Exporter crashes on files > 2GB\"\nReferences: src/export/csv_writer.py — file no longer exists (removed in 9f2c1ab).",
        path: null,
        start_line: null,
        language: null,
      },
    ],
    diff: null,
  },
  claim: {
    id: uid(),
    statement: "The CSV exporter crashes on files larger than 2GB.",
    claim_type: "status",
    status: "stale",
    anchor: { path: null, start_line: null, end_line: null },
  },
  event: null,
  created_at: minutesAgo(m),
});

const silo: Template = (m) => ({
  id: uid(),
  kind: "silo",
  severity: "medium",
  status: "open",
  explanation:
    "97% of commits to the payments reconciliation module in the last six months came from a single engineer, and no document describes its retry model. Bus factor: 1.",
  suggested_action: "Schedule a knowledge-transfer session and draft docs/payments.md.",
  evidence: {
    quotes: [
      {
        text: "payments/reconcile.py   61 commits — alice (97%)\npayments/ledger.py      18 commits — alice (94%)\ndocs coverage: none",
        path: null,
        start_line: null,
        language: null,
      },
    ],
    diff: null,
  },
  claim: {
    id: uid(),
    statement: "Knowledge of payments reconciliation is held by a single engineer.",
    claim_type: "process",
    status: "verified",
    anchor: { path: null, start_line: null, end_line: null },
  },
  event: null,
  created_at: minutesAgo(m),
});

const TEMPLATES: Template[] = [docDrift, longClaim, longEvidence, staleIssue, silo];

export function singleFinding(): FindingOut[] {
  seq = 0;
  return [docDrift(42)];
}

export function manyFindings(count = 50): FindingOut[] {
  seq = 0;
  const severities: FindingOut["severity"][] = ["critical", "high", "medium", "low"];
  const statuses: FindingOut["status"][] = ["open", "actioned", "dismissed"];
  return Array.from({ length: count }, (_, i) => {
    const finding = TEMPLATES[i % TEMPLATES.length](15 + i * 37);
    return {
      ...finding,
      severity: i < 5 ? finding.severity : severities[i % severities.length],
      status: i === 7 ? statuses[1] : i === 11 ? statuses[2] : finding.status,
    };
  });
}

export const emptyFindings: FindingOut[] = [];
