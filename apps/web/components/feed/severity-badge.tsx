import type { FindingSeverity } from "@/lib/api";
import { cn } from "@/lib/utils";

const STYLES: Record<FindingSeverity, string> = {
  critical: "bg-red-500/15 text-red-400 ring-red-500/30",
  high: "bg-orange-500/15 text-orange-400 ring-orange-500/30",
  medium: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  low: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
};

/** Severity accent color, shared by the badge and the card's edge bar. */
export const SEVERITY_EDGE: Record<FindingSeverity, string> = {
  critical: "border-l-red-500",
  high: "border-l-orange-400",
  medium: "border-l-amber-300",
  low: "border-l-sky-400",
};

export function SeverityBadge({ severity }: { severity: FindingSeverity }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ring-1 ring-inset",
        STYLES[severity],
      )}
    >
      {severity}
    </span>
  );
}
