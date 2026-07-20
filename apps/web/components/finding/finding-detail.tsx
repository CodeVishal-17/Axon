"use client";

import type { FindingAction, FindingOut } from "@/lib/api";
import { KIND_LABELS } from "@/components/finding/kind-labels";
import { Evidence } from "@/components/finding/evidence";
import { Provenance, anchorLabel } from "@/components/finding/provenance";
import {
  FindingActions,
  type ActionState,
} from "@/components/finding/finding-actions";
import { ClaimStatusPill, FindingStatusIcon } from "@/components/common/status-pill";
import { RelativeTime } from "@/components/common/relative-time";
import { SeverityBadge } from "@/components/common/severity-badge";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

/**
 * Full finding view. Presentation only — the container owns the data and
 * the action state, so this re-renders exactly when its finding changes.
 */
export function FindingDetail({
  finding,
  state,
  onAction,
  onOpenChange,
}: {
  finding: FindingOut | null;
  state: ActionState;
  onAction: (action: FindingAction) => void;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Sheet open={finding !== null} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="data-[side=right]:sm:max-w-2xl gap-0 overflow-y-auto"
      >
        {finding ? (
          <>
            <SheetHeader className="border-border/60 border-b pr-12">
              <div className="flex flex-wrap items-center gap-2">
                <SeverityBadge severity={finding.severity} />
                <span className="text-muted-foreground text-xs font-medium">
                  {KIND_LABELS[finding.kind]}
                </span>
                <ClaimStatusPill status={finding.claim.status} />
                <FindingStatusIcon status={finding.status} withLabel />
              </div>
              <SheetTitle className="pt-2 text-base leading-snug font-medium">
                {finding.claim.statement}
              </SheetTitle>
              <SheetDescription className="sr-only">
                Finding detail: evidence, provenance, and available actions.
              </SheetDescription>
              {anchorLabel(finding.claim.anchor) ? (
                <p className="text-muted-foreground pt-1 font-mono text-xs">
                  {anchorLabel(finding.claim.anchor)}
                </p>
              ) : null}
            </SheetHeader>

            <div className="flex flex-col gap-5 p-4">
              <section className="flex flex-col gap-1.5">
                <h3 className="text-muted-foreground text-[11px] font-semibold tracking-wide uppercase">
                  What changed
                </h3>
                <p className="text-sm leading-relaxed">{finding.explanation}</p>
              </section>

              <section className="flex flex-col gap-1.5">
                <h3 className="text-muted-foreground text-[11px] font-semibold tracking-wide uppercase">
                  Evidence from the code
                </h3>
                <Evidence evidence={finding.evidence} />
              </section>

              {finding.suggested_action ? (
                <section className="flex flex-col gap-1.5">
                  <h3 className="text-muted-foreground text-[11px] font-semibold tracking-wide uppercase">
                    Suggested action
                  </h3>
                  <p className="text-muted-foreground text-sm leading-relaxed">
                    {finding.suggested_action}
                  </p>
                </section>
              ) : null}

              <section className="border-border/60 flex flex-col gap-3 border-t pt-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Provenance finding={finding} />
                  <RelativeTime iso={finding.created_at} prefix="detected" />
                </div>
                <FindingActions
                  finding={finding}
                  state={state}
                  onAction={onAction}
                  size="default"
                />
              </section>
            </div>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
