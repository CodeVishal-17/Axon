"use client";

import { memo } from "react";
import {
  ExternalLink,
  GitPullRequest,
  Loader2,
  MessageSquareText,
  Sparkles,
} from "lucide-react";
import type { PullOut } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { RelativeTime } from "@/components/common/relative-time";
import { cn } from "@/lib/utils";

/** Open pull requests with the review state of each one's current revision. */
export const PullList = memo(function PullList({
  pulls,
  selectedNumber,
  onSelect,
  onReview,
  isRequesting,
}: {
  pulls: PullOut[];
  selectedNumber: number | null;
  onSelect: (pull: PullOut) => void;
  onReview: (pull: PullOut) => void;
  isRequesting: boolean;
}) {
  return (
    <ul className="flex flex-col gap-2">
      {pulls.map((pull) => (
        <PullRow
          key={pull.number}
          pull={pull}
          selected={pull.number === selectedNumber}
          onSelect={onSelect}
          onReview={onReview}
          isRequesting={isRequesting}
        />
      ))}
    </ul>
  );
});

const PullRow = memo(function PullRow({
  pull,
  selected,
  onSelect,
  onReview,
  isRequesting,
}: {
  pull: PullOut;
  selected: boolean;
  onSelect: (pull: PullOut) => void;
  onReview: (pull: PullOut) => void;
  isRequesting: boolean;
}) {
  const reviewed = pull.review_status === "generated" || pull.review_status === "posted";

  return (
    <li
      className={cn(
        "border-border/40 bg-card/40 flex flex-col gap-3 rounded-lg border p-4 transition-colors sm:flex-row sm:items-center sm:justify-between",
        selected && "border-emerald-500/40 bg-emerald-500/[0.03]",
      )}
    >
      <button
        type="button"
        onClick={() => onSelect(pull)}
        className="min-w-0 flex-1 text-left"
      >
        <span className="flex items-center gap-2">
          <GitPullRequest
            className={cn(
              "size-4 shrink-0",
              pull.draft ? "text-muted-foreground" : "text-emerald-400",
            )}
            aria-hidden
          />
          <span className="text-muted-foreground font-mono text-xs">
            #{pull.number}
          </span>
          <span className="truncate text-sm font-medium">{pull.title}</span>
          {pull.draft ? (
            <span className="bg-muted/60 text-muted-foreground rounded px-1.5 py-0.5 text-[10px] uppercase">
              Draft
            </span>
          ) : null}
        </span>
        <span className="text-muted-foreground mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          <span>{pull.author ?? "unknown"}</span>
          <span aria-hidden>·</span>
          {pull.updated_at ? (
            <RelativeTime iso={pull.updated_at} prefix="updated" />
          ) : null}
          <ReviewBadge pull={pull} />
        </span>
      </button>

      <div className="flex shrink-0 items-center gap-2">
        <Button size="sm" variant="ghost" render={<a href={pull.url} target="_blank" rel="noreferrer" />}>
          <ExternalLink className="size-3.5" aria-hidden />
          GitHub
        </Button>
        {pull.review_pending ? (
          <Button size="sm" variant="outline" disabled>
            <Loader2 className="size-3.5 animate-spin" aria-hidden />
            Reviewing…
          </Button>
        ) : (
          <Button
            size="sm"
            variant={reviewed ? "outline" : "default"}
            disabled={isRequesting}
            onClick={() => onReview(pull)}
          >
            <Sparkles className="size-3.5" aria-hidden />
            {reviewed ? "Re-review" : "Review with Axon"}
          </Button>
        )}
      </div>
    </li>
  );
});

function ReviewBadge({ pull }: { pull: PullOut }) {
  if (pull.review_pending) {
    return (
      <span className="text-sky-400 inline-flex items-center gap-1">
        <Loader2 className="size-3 animate-spin" aria-hidden />
        review in progress
      </span>
    );
  }
  if (pull.review_status === "posted") {
    return (
      <span className="text-emerald-400 inline-flex items-center gap-1">
        <MessageSquareText className="size-3" aria-hidden />
        review posted
      </span>
    );
  }
  if (pull.review_status === "generated") {
    return (
      <span className="text-violet-400 inline-flex items-center gap-1">
        <MessageSquareText className="size-3" aria-hidden />
        {pull.review_comment_count} comment
        {pull.review_comment_count === 1 ? "" : "s"} ready
      </span>
    );
  }
  if (pull.review_status === "failed") {
    return <span className="text-amber-400">review failed</span>;
  }
  return null;
}
