import type { FindingKind } from "@/lib/api";

export const KIND_LABELS: Record<FindingKind, string> = {
  doc_drift: "Doc drift",
  stale_issue: "Stale issue",
  contradiction: "Contradiction",
  silo: "Knowledge silo",
};
