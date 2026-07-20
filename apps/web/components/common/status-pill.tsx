import { CircleCheck, CircleDot, CircleSlash } from "lucide-react";
import type { ClaimStatus, FindingStatus } from "@/lib/api";
import { cn } from "@/lib/utils";

const CLAIM_STYLES: Record<ClaimStatus, string> = {
  contradicted: "bg-red-500/10 text-red-300",
  stale: "bg-amber-500/10 text-amber-200",
  verified: "bg-emerald-500/10 text-emerald-300",
  unchecked: "bg-zinc-500/10 text-zinc-400",
};

/** How the belief currently stands against the code. */
export function ClaimStatusPill({ status }: { status: ClaimStatus }) {
  return (
    <span
      className={cn(
        "rounded px-1.5 py-0.5 text-[10px] font-medium",
        CLAIM_STYLES[status],
      )}
    >
      claim {status}
    </span>
  );
}

const FINDING_META: Record<
  FindingStatus,
  { icon: typeof CircleDot; className: string; label: string }
> = {
  open: { icon: CircleDot, className: "text-sky-400", label: "Open" },
  actioned: { icon: CircleCheck, className: "text-emerald-400", label: "Fix PR opened" },
  dismissed: { icon: CircleSlash, className: "text-zinc-500", label: "Dismissed" },
};

export function FindingStatusIcon({
  status,
  withLabel = false,
}: {
  status: FindingStatus;
  withLabel?: boolean;
}) {
  const { icon: Icon, className, label } = FINDING_META[status];
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs", className)}>
      <Icon className="size-3.5 shrink-0" aria-hidden />
      {withLabel ? label : <span className="sr-only">{label}</span>}
    </span>
  );
}
