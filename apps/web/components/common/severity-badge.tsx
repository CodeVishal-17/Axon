import type { FindingSeverity } from "@/lib/api";
import { cn } from "@/lib/utils";

const STYLES: Record<FindingSeverity, string> = {
  critical: "bg-red-500/12 text-red-300 ring-red-500/25",
  high: "bg-orange-500/12 text-orange-300 ring-orange-500/25",
  medium: "bg-amber-500/12 text-amber-200 ring-amber-500/25",
  low: "bg-sky-500/12 text-sky-300 ring-sky-500/25",
};

/** Severity accent for a card's left edge — the feed's scan-line signal. */
export const SEVERITY_EDGE: Record<FindingSeverity, string> = {
  critical: "border-l-red-500/70",
  high: "border-l-orange-400/70",
  medium: "border-l-amber-300/70",
  low: "border-l-sky-400/70",
};

export function SeverityBadge({
  severity,
  className,
}: {
  severity: FindingSeverity;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide uppercase ring-1 ring-inset",
        STYLES[severity],
        className,
      )}
    >
      {severity}
    </span>
  );
}
