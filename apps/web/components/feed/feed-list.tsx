"use client";

import type { FindingAction, FindingOut } from "@/lib/api";
import { FeaturedFinding } from "@/components/feed/featured-finding";
import { FindingCard } from "@/components/feed/finding-card";
import type { ActionState } from "@/components/finding/finding-actions";

const IDLE: ActionState = { kind: "idle" };

/**
 * Presentation only: headline finding on top, the rest beneath. All data,
 * polling, and mutation state live in the container.
 */
export function FeedList({
  findings,
  actionStates,
  onOpen,
  onAction,
}: {
  findings: FindingOut[];
  actionStates: Record<string, ActionState>;
  onOpen: (findingId: string) => void;
  onAction: (findingId: string, action: FindingAction) => void;
}) {
  const [featured, ...rest] = findings;
  if (!featured) return null;

  return (
    <div className="flex flex-col gap-4">
      <FeaturedFinding
        finding={featured}
        state={actionStates[featured.id] ?? IDLE}
        onOpen={onOpen}
        onAction={onAction}
      />

      {rest.length > 0 ? (
        <>
          <div className="flex items-center gap-3">
            <h2 className="text-muted-foreground text-[11px] font-semibold tracking-wide uppercase">
              More findings
            </h2>
            <span className="bg-border/60 h-px flex-1" aria-hidden />
          </div>
          <ul className="flex flex-col gap-2.5">
            {rest.map((finding) => (
              <li key={finding.id}>
                <FindingCard
                  finding={finding}
                  state={actionStates[finding.id] ?? IDLE}
                  onOpen={onOpen}
                  onAction={onAction}
                />
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
