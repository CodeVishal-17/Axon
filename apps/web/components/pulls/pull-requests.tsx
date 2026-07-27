"use client";

import { useCallback, useState } from "react";
import { GitPullRequest } from "lucide-react";
import type { PullOut } from "@/lib/api";
import { usePulls, useRequestPrReview } from "@/lib/queries";
import { EmptyState } from "@/components/layout/empty-state";
import { ErrorState } from "@/components/common/error-state";
import { PullList } from "@/components/pulls/pull-list";
import { ReviewPanel } from "@/components/pulls/review-panel";

/**
 * Pull Requests container: owns fetching, review requests, and selection.
 * Everything below it is presentational.
 */
export function PullRequests({ repoId }: { repoId: string }) {
  const [selected, setSelected] = useState<PullOut | null>(null);
  const query = usePulls(repoId);
  const requestReview = useRequestPrReview(repoId);

  const handleReview = useCallback(
    (pull: PullOut) => {
      setSelected(pull);
      requestReview.mutate(pull.number);
    },
    [requestReview],
  );

  if (query.isPending) {
    return (
      <div className="flex flex-col gap-2">
        {Array.from({ length: 3 }).map((_, i) => (
          <div
            key={i}
            className="border-border/40 bg-card/40 h-20 animate-pulse rounded-lg border"
          />
        ))}
      </div>
    );
  }

  if (query.error) {
    return (
      <ErrorState
        title="Couldn't load pull requests"
        description="Axon couldn't reach GitHub for this repository's open pull requests."
        onRetry={() => query.refetch()}
      />
    );
  }

  const pulls = query.data.items;

  if (pulls.length === 0) {
    return (
      <EmptyState
        icon={<GitPullRequest className="size-6" aria-hidden />}
        title="No open pull requests"
        description="When someone opens a pull request, Axon can review it — for code issues and for documentation it would make untrue."
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <PullList
        pulls={pulls}
        selectedNumber={selected?.number ?? null}
        onSelect={setSelected}
        onReview={handleReview}
        isRequesting={requestReview.isPending}
      />
      <ReviewPanel
        repoId={repoId}
        pull={selected}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
      />
    </div>
  );
}
