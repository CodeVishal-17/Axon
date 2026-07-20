"use client";

import type { FindingStatus } from "@/lib/api";
import { cn } from "@/lib/utils";

const TABS: { value: FindingStatus; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "actioned", label: "Fix opened" },
  { value: "dismissed", label: "Dismissed" },
];

/**
 * Feed controls: which slice of the feed to show, plus an honest live
 * indicator — it pulses only while a request is actually in flight.
 */
export function FeedToolbar({
  status,
  onStatusChange,
  total,
  isFetching,
  isLive,
  hasNewData = false,
}: {
  status: FindingStatus;
  onStatusChange: (status: FindingStatus) => void;
  total: number | null;
  isFetching: boolean;
  isLive: boolean;
  hasNewData?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <div
        role="tablist"
        aria-label="Finding status"
        className="border-border/60 flex rounded-md border p-0.5"
      >
        {TABS.map((tab) => {
          const selected = tab.value === status;
          return (
            <button
              key={tab.value}
              role="tab"
              type="button"
              aria-selected={selected}
              onClick={() => onStatusChange(tab.value)}
              className={cn(
                "focus-visible:ring-ring/60 rounded px-2.5 py-1 text-xs transition-colors focus-visible:ring-2 focus-visible:outline-none motion-reduce:transition-none",
                selected
                  ? "bg-secondary text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      {total !== null ? (
        <p className="text-muted-foreground text-xs" aria-live="polite">
          {total} finding{total === 1 ? "" : "s"}
        </p>
      ) : null}

      <span className="text-muted-foreground ml-auto inline-flex items-center gap-1.5 text-[11px]">
        <span
          className={cn(
            "inline-block size-1.5 rounded-full",
            hasNewData
              ? "bg-emerald-300 animate-arrival-pulse motion-reduce:animate-none"
              : isFetching
              ? "bg-emerald-400 animate-pulse motion-reduce:animate-none"
              : isLive
                ? "bg-emerald-400/50"
                : "bg-muted-foreground/40",
          )}
          aria-hidden
        />
        {isFetching ? "Syncing…" : hasNewData ? "Updated" : isLive ? "Live" : "Idle"}
      </span>
    </div>
  );
}
