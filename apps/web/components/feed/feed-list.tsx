import type { FindingOut } from "@/lib/api";
import { EmptyState } from "@/components/layout/empty-state";
import { FindingCard } from "@/components/feed/finding-card";
import { Skeleton } from "@/components/ui/skeleton";

/** One loading placeholder shaped like a finding card. */
function CardSkeleton() {
  return (
    <div className="border-border/60 bg-card/60 flex flex-col gap-3 rounded-lg border border-l-2 border-l-border p-4">
      <div className="flex items-center gap-2">
        <Skeleton className="h-5 w-16 rounded-full" />
        <Skeleton className="h-4 w-20" />
        <Skeleton className="ml-auto h-4 w-14" />
      </div>
      <Skeleton className="h-5 w-4/5" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-24 w-full rounded-md" />
      <div className="flex justify-end gap-2">
        <Skeleton className="h-8 w-24" />
        <Skeleton className="h-8 w-16" />
      </div>
    </div>
  );
}

/**
 * The Truth Feed. `findings === null` renders the loading state; an empty
 * array renders the (deliberate, styled) empty state.
 */
export function FeedList({ findings }: { findings: FindingOut[] | null }) {
  if (findings === null) {
    return (
      <div className="flex flex-col gap-3" aria-busy>
        <CardSkeleton />
        <CardSkeleton />
        <CardSkeleton />
      </div>
    );
  }

  if (findings.length === 0) {
    return (
      <EmptyState
        title="No open findings"
        description="Axon has nothing to report: every verified claim currently matches the code. New findings appear here the moment reality drifts from the documentation."
      />
    );
  }

  const openCount = findings.filter((f) => f.status === "open").length;
  return (
    <div className="flex flex-col gap-3">
      <p className="text-muted-foreground text-xs">
        {openCount} open finding{openCount === 1 ? "" : "s"}
        {openCount !== findings.length
          ? ` · ${findings.length - openCount} resolved`
          : ""}
      </p>
      {findings.map((finding) => (
        <FindingCard key={finding.id} finding={finding} />
      ))}
    </div>
  );
}
