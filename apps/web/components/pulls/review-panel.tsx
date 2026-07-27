"use client";

import {
  BookOpenCheck,
  Code2,
  ExternalLink,
  Loader2,
  Send,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import type { PullOut, ReviewCommentOut, ReviewLens } from "@/lib/api";
import { usePostPrReview, usePrReview } from "@/lib/queries";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { cn } from "@/lib/utils";

const LENS_META: Record<
  ReviewLens,
  { label: string; icon: typeof Code2; className: string; blurb: string }
> = {
  code: {
    label: "Code review",
    icon: Code2,
    className: "text-sky-400",
    blurb: "Correctness, edge cases, and risk in the change itself.",
  },
  truth: {
    label: "Truth maintenance",
    icon: BookOpenCheck,
    className: "text-emerald-400",
    blurb:
      "Where this change would make Axon's verified documentation untrue.",
  },
};

const SEVERITY_STYLES: Record<string, string> = {
  critical: "bg-red-500/15 text-red-300",
  high: "bg-orange-500/15 text-orange-300",
  medium: "bg-amber-500/15 text-amber-200",
  low: "bg-zinc-500/15 text-zinc-400",
};

/**
 * The generated review for one pull request: summary, then comments grouped
 * by lens, then the explicit publish action. Presentation + the two
 * review-scoped mutations; the list container owns selection.
 */
export function ReviewPanel({
  repoId,
  pull,
  onOpenChange,
}: {
  repoId: string;
  pull: PullOut | null;
  onOpenChange: (open: boolean) => void;
}) {
  // Poll while a review is being generated so the panel fills in by itself.
  const query = usePrReview(repoId, pull?.number ?? null, {
    active: Boolean(pull?.review_pending),
  });
  const postReview = usePostPrReview(repoId);
  const review = query.data ?? null;

  const codeComments = (review?.comments ?? []).filter((c) => c.lens === "code");
  const truthComments = (review?.comments ?? []).filter((c) => c.lens === "truth");

  return (
    <Sheet open={pull !== null} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="data-[side=right]:sm:max-w-2xl gap-0 overflow-y-auto bg-background"
      >
        {pull ? (
          <>
            <SheetHeader className="border-border/60 border-b bg-popover/80 pr-12 backdrop-blur-sm">
              <SheetTitle className="text-base leading-snug">
                <span className="text-muted-foreground font-mono text-xs">
                  #{pull.number}
                </span>{" "}
                {pull.title}
              </SheetTitle>
              <SheetDescription>
                {pull.author ?? "unknown"} · {pull.head_sha.slice(0, 7)}
              </SheetDescription>
            </SheetHeader>

            <div className="flex flex-col gap-6 p-4">
              {query.isPending ? (
                <PanelSkeleton />
              ) : !review ? (
                <PendingState pending={Boolean(pull.review_pending)} />
              ) : review.status === "failed" ? (
                <FailedState reason={review.error} />
              ) : (
                <>
                  <section>
                    <SectionLabel>Summary</SectionLabel>
                    <p className="text-sm leading-relaxed">{review.summary}</p>
                  </section>

                  {review.comments.length === 0 ? (
                    <p className="text-muted-foreground text-sm">
                      Axon found nothing worth flagging in this diff.
                    </p>
                  ) : (
                    <>
                      <LensSection lens="truth" comments={truthComments} />
                      <LensSection lens="code" comments={codeComments} />
                    </>
                  )}

                  <footer className="border-border/60 flex flex-col gap-2 border-t pt-4">
                    {review.status === "posted" && review.review_url ? (
                      <Button
                        variant="outline"
                        render={
                          <a
                            href={review.review_url}
                            target="_blank"
                            rel="noreferrer"
                          />
                        }
                      >
                        <ExternalLink className="size-4" aria-hidden />
                        View review on GitHub
                      </Button>
                    ) : (
                      <Button
                        disabled={postReview.isPending}
                        onClick={() => postReview.mutate(pull.number)}
                      >
                        {postReview.isPending ? (
                          <>
                            <Loader2 className="size-4 animate-spin" aria-hidden />
                            Posting…
                          </>
                        ) : (
                          <>
                            <Send className="size-4" aria-hidden />
                            Post review to GitHub
                          </>
                        )}
                      </Button>
                    )}
                    {postReview.error ? (
                      <p role="alert" className="text-sm text-red-400">
                        Couldn&apos;t post the review. Please try again.
                      </p>
                    ) : (
                      <p className="text-muted-foreground text-xs">
                        Nothing is posted to GitHub until you click. Comments
                        publish as the Axon app.
                      </p>
                    )}
                  </footer>
                </>
              )}
            </div>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function LensSection({
  lens,
  comments,
}: {
  lens: ReviewLens;
  comments: ReviewCommentOut[];
}) {
  if (comments.length === 0) return null;
  const meta = LENS_META[lens];
  const Icon = meta.icon;
  return (
    <section className="flex flex-col gap-2">
      <SectionLabel>
        <span className={cn("inline-flex items-center gap-1.5", meta.className)}>
          <Icon className="size-3.5" aria-hidden />
          {meta.label}
        </span>
        <span className="text-muted-foreground ml-2 normal-case">
          {comments.length}
        </span>
      </SectionLabel>
      <p className="text-muted-foreground -mt-1 text-xs">{meta.blurb}</p>
      <ul className="flex flex-col gap-2">
        {comments.map((comment, i) => (
          <li
            key={`${comment.path}-${comment.line}-${i}`}
            className="border-border/40 bg-card/40 rounded-lg border p-3"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-muted-foreground truncate font-mono text-xs">
                {comment.path}:{comment.line}
              </span>
              <span
                className={cn(
                  "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium uppercase",
                  SEVERITY_STYLES[comment.severity] ?? SEVERITY_STYLES.low,
                )}
              >
                {comment.severity}
              </span>
            </div>
            <p className="mt-2 text-sm leading-relaxed">{comment.body}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-muted-foreground mb-2 text-xs font-semibold tracking-wide uppercase">
      {children}
    </h3>
  );
}

function PanelSkeleton() {
  return (
    <div className="flex flex-col gap-3">
      {Array.from({ length: 3 }).map((_, i) => (
        <div
          key={i}
          className="border-border/40 bg-card/40 h-16 animate-pulse rounded-lg border"
        />
      ))}
    </div>
  );
}

function PendingState({ pending }: { pending: boolean }) {
  return (
    <div className="flex flex-col items-center gap-3 py-12 text-center">
      {pending ? (
        <>
          <Loader2 className="size-5 animate-spin text-sky-400" aria-hidden />
          <p className="text-sm font-medium">Axon is reviewing this pull request</p>
          <p className="text-muted-foreground max-w-sm text-sm">
            Reading the diff and checking it against the claims your
            documentation makes. This usually takes a few seconds.
          </p>
        </>
      ) : (
        <>
          <Sparkles className="text-muted-foreground size-5" aria-hidden />
          <p className="text-sm font-medium">No review yet</p>
          <p className="text-muted-foreground max-w-sm text-sm">
            Click “Review with Axon” to check this pull request for code issues
            and for documentation it would make untrue.
          </p>
        </>
      )}
    </div>
  );
}

function FailedState({ reason }: { reason: string | null | undefined }) {
  return (
    <div className="flex flex-col items-center gap-3 py-12 text-center">
      <TriangleAlert className="size-5 text-amber-400" aria-hidden />
      <p className="text-sm font-medium">Axon couldn&apos;t review this pull request</p>
      {reason ? (
        <p className="text-muted-foreground max-w-sm text-sm">{reason}</p>
      ) : null}
    </div>
  );
}
