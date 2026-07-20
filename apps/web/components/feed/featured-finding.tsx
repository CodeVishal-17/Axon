"use client";

import { memo } from "react";
import { ArrowUpRight } from "lucide-react";
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

/**
 * The headline finding — the one a reader should look at first (highest
 * severity, then most recent). Same primitives as FindingCard, more room:
 * full explanation and full evidence, so the top of the feed reads as an
 * argument rather than a list item.
 */
export const FeaturedFinding = memo(function FeaturedFinding({
  finding,
  state,
  isNew = false,
  wasPromoted = false,
  onOpen,
  onAction,
}: {
  finding: FindingOut;
  state: ActionState;
  isNew?: boolean;
  wasPromoted?: boolean;
  onOpen: (findingId: string) => void;
  onAction: (findingId: string, action: FindingAction) => void;
}) {
  const anchor = anchorLabel(finding.claim.anchor);

  return (
    <article
      className={cn(
        "group border-border/60 bg-card/60 relative flex flex-col gap-3 rounded-xl border border-l-[3px] p-5",
        "transition-colors duration-150 motion-reduce:transition-none",
        (isNew || wasPromoted) &&
          "animate-featured-arrival motion-reduce:animate-none",
        "hover:bg-card/80 focus-within:border-border",
        SEVERITY_EDGE[finding.severity],
      )}
    >
      <button
        type="button"
        onClick={() => onOpen(finding.id)}
        className="focus-visible:ring-ring/60 absolute inset-0 z-0 rounded-xl focus-visible:ring-2 focus-visible:outline-none"
      >
        <span className="sr-only">Open finding: {finding.claim.statement}</span>
      </button>

      <header className="pointer-events-none relative z-10 flex flex-wrap items-center gap-2">
        {isNew ? <span className="sr-only">New finding received.</span> : null}
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

      <div className="pointer-events-none relative z-10 flex flex-col gap-2">
        <h2 className="text-lg leading-snug font-medium text-balance">
          {finding.claim.statement}
          <ArrowUpRight
            className="text-muted-foreground ml-1 inline size-4 align-text-top opacity-0 transition-opacity group-hover:opacity-100 motion-reduce:transition-none"
            aria-hidden
          />
        </h2>
        {anchor ? (
          <p className="text-muted-foreground font-mono text-[11px]">{anchor}</p>
        ) : null}
        <p className="text-muted-foreground text-sm leading-relaxed">
          {finding.explanation}
        </p>
      </div>

      <div className="pointer-events-none relative z-10">
        <Evidence evidence={finding.evidence} maxLines={8} />
      </div>

      <footer className="relative z-10 flex flex-wrap items-center justify-between gap-3 pt-1">
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
