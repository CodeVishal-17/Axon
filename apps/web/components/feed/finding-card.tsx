"use client";

import { memo } from "react";
import type { FindingAction, FindingOut } from "@/lib/api";
import { KIND_LABELS } from "@/components/finding/kind-labels";
import { Evidence } from "@/components/finding/evidence";
import { Provenance, anchorLabel } from "@/components/finding/provenance";
import {
  FindingActions,
  type ActionState,
} from "@/components/finding/finding-actions";
import { RelativeTime } from "@/components/common/relative-time";
import { SEVERITY_EDGE, SeverityBadge } from "@/components/common/severity-badge";
import { ClaimStatusPill, FindingStatusIcon } from "@/components/common/status-pill";
import { cn } from "@/lib/utils";

export type FindingCardProps = {
  finding: FindingOut;
  state: ActionState;
  isNew?: boolean;
  onOpen: (findingId: string) => void;
  onAction: (findingId: string, action: FindingAction) => void;
};

/**
 * One row of the Truth Feed.
 *
 * The whole card is a single keyboard-focusable target (a stretched button
 * under the content) so it behaves like a Linear/GitHub list row; the
 * action buttons float above it, so there is no invalid nested-button
 * markup and Tab order stays natural.
 *
 * Memoized on identity — the feed polls every few seconds, and unchanged
 * findings must not re-render (or re-tokenize their evidence).
 */
export const FindingCard = memo(function FindingCard({
  finding,
  state,
  isNew = false,
  onOpen,
  onAction,
}: FindingCardProps) {
  const anchor = anchorLabel(finding.claim.anchor);
  const dimmed = finding.status === "dismissed";
  const firstQuote = finding.evidence.quotes?.[0];

  return (
    <article
      className={cn(
        "group border-border/60 bg-card/40 relative flex flex-col gap-2.5 rounded-lg border border-l-2 p-4",
        "transition-colors duration-150 motion-reduce:transition-none",
        isNew && "animate-finding-arrival motion-reduce:animate-none",
        "hover:bg-card/70 focus-within:border-border focus-within:bg-card/70",
        SEVERITY_EDGE[finding.severity],
        dimmed && "opacity-55",
      )}
    >
      {/* Stretched primary control: opens the detail view. */}
      <button
        type="button"
        onClick={() => onOpen(finding.id)}
        className="focus-visible:ring-ring/60 absolute inset-0 z-0 rounded-lg focus-visible:ring-2 focus-visible:outline-none"
      >
        <span className="sr-only">
          Open finding: {finding.claim.statement}
        </span>
      </button>

      <header className="pointer-events-none relative z-10 flex flex-wrap items-center gap-2">
        <SeverityBadge severity={finding.severity} />
        <span className="text-muted-foreground text-xs font-medium">
          {KIND_LABELS[finding.kind]}
        </span>
        <ClaimStatusPill status={finding.claim.status} />
        <span className="ml-auto flex items-center gap-2">
          <FindingStatusIcon status={finding.status} />
          <RelativeTime iso={finding.created_at} />
        </span>
      </header>

      <div className="pointer-events-none relative z-10 flex flex-col gap-1.5">
        <h3 className="text-[15px] leading-snug font-medium text-balance">
          {finding.claim.statement}
        </h3>
        {anchor ? (
          <p className="text-muted-foreground font-mono text-[11px]">{anchor}</p>
        ) : null}
        <p className="text-muted-foreground line-clamp-2 text-sm leading-relaxed">
          {finding.explanation}
        </p>
      </div>

      {firstQuote ? (
        <div className="pointer-events-none relative z-10">
          <Evidence
            evidence={{ quotes: [firstQuote], diff: null }}
            maxLines={3}
          />
        </div>
      ) : null}

      <footer className="relative z-10 flex flex-wrap items-center justify-between gap-3 pt-0.5">
        <span className="pointer-events-none">
          <Provenance finding={finding} />
        </span>
        <FindingActions
          finding={finding}
          state={state}
          onAction={(action) => onAction(finding.id, action)}
        />
      </footer>
    </article>
  );
});
