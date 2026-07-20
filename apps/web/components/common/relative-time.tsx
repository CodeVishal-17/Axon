import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Relative timestamp with the absolute value available on hover and to
 * assistive tech. Recomputed on each render — the feed polls, so it stays
 * fresh without its own timer (and without extra rerenders).
 */
export function RelativeTime({
  iso,
  className,
  prefix,
}: {
  iso: string;
  className?: string;
  prefix?: string;
}) {
  const absolute = new Date(iso).toLocaleString();
  return (
    <time
      dateTime={iso}
      title={absolute}
      className={cn("text-muted-foreground text-xs", className)}
    >
      {prefix ? `${prefix} ` : ""}
      {relativeTime(iso)}
    </time>
  );
}
