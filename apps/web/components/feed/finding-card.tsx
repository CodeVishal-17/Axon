import { GitPullRequest, GitCommitHorizontal, Radar, CircleDot, CircleCheck, CircleSlash } from "lucide-react";
import type { ClaimStatus, FindingKind, FindingOut } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/feed/code-block";
import { SEVERITY_EDGE, SeverityBadge } from "@/components/feed/severity-badge";

const KIND_LABELS: Record<FindingKind, string> = {
  doc_drift: "Doc drift",
  stale_issue: "Stale issue",
  contradiction: "Contradiction",
  silo: "Knowledge silo",
};

const CLAIM_STATUS_STYLES: Record<ClaimStatus, string> = {
  contradicted: "bg-red-500/10 text-red-400",
  stale: "bg-amber-500/10 text-amber-300",
  verified: "bg-emerald-500/10 text-emerald-400",
  unchecked: "bg-zinc-500/10 text-zinc-400",
};

function FindingStatusIcon({ status }: { status: FindingOut["status"] }) {
  if (status === "actioned")
    return <CircleCheck className="size-3.5 text-emerald-400" aria-label="actioned" />;
  if (status === "dismissed")
    return <CircleSlash className="size-3.5 text-zinc-500" aria-label="dismissed" />;
  return <CircleDot className="size-3.5 text-sky-400" aria-label="open" />;
}

function Provenance({ finding }: { finding: FindingOut }) {
  const event = finding.event;
  if (!event) {
    return (
      <span className="text-muted-foreground inline-flex items-center gap-1.5 text-xs">
        <Radar className="size-3.5" aria-hidden />
        Found during repository scan
      </span>
    );
  }
  const icon =
    event.kind === "pr_merged" ? (
      <GitPullRequest className="size-3.5" aria-hidden />
    ) : (
      <GitCommitHorizontal className="size-3.5" aria-hidden />
    );
  const label =
    event.kind === "pr_merged"
      ? `Triggered by PR #${event.external_id}`
      : event.kind === "issue_closed"
        ? `Triggered by issue #${event.external_id} closing`
        : `Triggered by push ${event.external_id ?? ""}`;
  return (
    <span className="text-muted-foreground inline-flex items-center gap-1.5 text-xs">
      {icon}
      {label} · {relativeTime(event.created_at)}
    </span>
  );
}

function anchorLabel(anchor: FindingOut["claim"]["anchor"]): string | null {
  if (!anchor.path) return null;
  if (anchor.start_line == null) return anchor.path;
  const range =
    anchor.end_line != null && anchor.end_line !== anchor.start_line
      ? `${anchor.start_line}–${anchor.end_line}`
      : `${anchor.start_line}`;
  return `${anchor.path}:${range}`;
}

export function FindingCard({ finding }: { finding: FindingOut }) {
  const anchor = anchorLabel(finding.claim.anchor);
  const dimmed = finding.status !== "open";

  return (
    <article
      className={cn(
        "border-border/60 bg-card/60 flex flex-col gap-3 rounded-lg border border-l-2 p-4",
        SEVERITY_EDGE[finding.severity],
        dimmed && "opacity-60",
      )}
    >
      <header className="flex flex-wrap items-center gap-2">
        <SeverityBadge severity={finding.severity} />
        <span className="text-muted-foreground text-xs font-medium">
          {KIND_LABELS[finding.kind]}
        </span>
        <span
          className={cn(
            "rounded-full px-2 py-0.5 text-[11px] font-medium",
            CLAIM_STATUS_STYLES[finding.claim.status],
          )}
        >
          claim {finding.claim.status}
        </span>
        <span className="text-muted-foreground ml-auto inline-flex items-center gap-1.5 text-xs">
          <FindingStatusIcon status={finding.status} />
          {relativeTime(finding.created_at)}
        </span>
      </header>

      <blockquote className="text-[15px] font-medium leading-snug">
        “{finding.claim.statement}”
        {anchor ? (
          <span className="text-muted-foreground ml-2 whitespace-nowrap font-mono text-xs font-normal">
            {anchor}
          </span>
        ) : null}
      </blockquote>

      <p className="text-muted-foreground text-sm leading-relaxed">
        {finding.explanation}
      </p>

      {finding.evidence.quotes?.map((quote, i) => (
        <CodeBlock
          key={i}
          code={quote.text}
          path={quote.path}
          startLine={quote.start_line}
        />
      ))}
      {finding.evidence.diff ? (
        <CodeBlock code={finding.evidence.diff} path="change" variant="diff" />
      ) : null}

      <footer className="flex flex-wrap items-center justify-between gap-3 pt-1">
        <Provenance finding={finding} />
        <div className="flex items-center gap-2">
          {/* Wired to the fix pipeline in T4.1/T4.2 */}
          <Button size="sm" disabled title="Available with the fix pipeline">
            Draft fix PR
          </Button>
          <Button size="sm" variant="ghost" disabled title="Available soon">
            Dismiss
          </Button>
        </div>
      </footer>
    </article>
  );
}
