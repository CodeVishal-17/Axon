import { Skeleton } from "@/components/ui/skeleton";

function CardSkeleton({ featured = false }: { featured?: boolean }) {
  return (
    <div
      className={
        featured
          ? "border-border/60 border-l-border bg-card/60 flex flex-col gap-3 rounded-xl border border-l-[3px] p-5"
          : "border-border/60 border-l-border bg-card/40 flex flex-col gap-2.5 rounded-lg border border-l-2 p-4"
      }
    >
      <div className="flex items-center gap-2">
        <Skeleton className="h-4 w-14 rounded" />
        <Skeleton className="h-3.5 w-20" />
        <Skeleton className="ml-auto h-3.5 w-16" />
      </div>
      <Skeleton className={featured ? "h-5 w-4/5" : "h-4 w-3/4"} />
      <Skeleton className="h-3.5 w-full" />
      <Skeleton className={featured ? "h-28 w-full rounded-md" : "h-16 w-full rounded-md"} />
      <div className="flex items-center justify-between gap-2">
        <Skeleton className="h-3.5 w-40" />
        <Skeleton className="h-8 w-32" />
      </div>
    </div>
  );
}

/** Mirrors the real layout so the feed doesn't jump when data lands. */
export function FeedSkeleton() {
  return (
    <div className="flex flex-col gap-4" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading findings…</span>
      <CardSkeleton featured />
      <CardSkeleton />
      <CardSkeleton />
    </div>
  );
}
