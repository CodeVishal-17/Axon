"use client";

import { CircleSlash, ExternalLink, GitPullRequest, Loader2 } from "lucide-react";
import type { FindingAction, FindingOut } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Per-finding action state, owned by the feed container. Nothing here is
 * simulated: "queued" means the backend accepted a job, and it only becomes
 * "PR opened" when the finding itself comes back `actioned` from the API.
 */
export type ActionState =
  | { kind: "idle" }
  | { kind: "pending"; action: FindingAction }
  | { kind: "queued" }
  | { kind: "opened"; prUrl?: string | null }
  | { kind: "error"; message: string };

export function FindingActions({
  finding,
  state,
  onAction,
  size = "sm",
}: {
  finding: FindingOut;
  state: ActionState;
  onAction: (action: FindingAction) => void;
  size?: "sm" | "default";
}) {
  const isDismissed = finding.status === "dismissed";
  const prOpened = finding.status === "actioned" || state.kind === "opened";
  const prUrl = state.kind === "opened" ? state.prUrl : null;
  const busy = state.kind === "pending";

  if (prOpened) {
    return (
      <div className="flex items-center gap-2">
        <span className="inline-flex items-center gap-1.5 text-xs text-emerald-400">
          <GitPullRequest className="size-3.5" aria-hidden />
          Fix PR opened
        </span>
        {prUrl ? (
          <Button
            size="sm"
            variant="ghost"
            render={<a href={prUrl} target="_blank" rel="noreferrer" />}
          >
            View PR
            <ExternalLink className="size-3.5" aria-hidden />
          </Button>
        ) : null}
      </div>
    );
  }

  if (state.kind === "queued") {
    return (
      <span
        className="text-muted-foreground inline-flex items-center gap-1.5 text-xs"
        role="status"
      >
        <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden />
        Fix queued — a worker is opening the pull request
      </span>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        size={size}
        onClick={() => onAction("generate_fix")}
        disabled={busy || isDismissed}
      >
        {busy && state.action === "generate_fix" ? (
          <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden />
        ) : (
          <GitPullRequest className="size-3.5" aria-hidden />
        )}
        Draft fix PR
      </Button>
      <Button
        size={size}
        variant="ghost"
        onClick={() => onAction("dismiss")}
        disabled={busy || isDismissed}
      >
        {busy && state.action === "dismiss" ? (
          <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden />
        ) : (
          <CircleSlash className="size-3.5" aria-hidden />
        )}
        Dismiss
      </Button>
      {state.kind === "error" ? (
        <span
          role="alert"
          className={cn("text-xs text-red-400", size === "sm" && "basis-full")}
        >
          {state.message}
        </span>
      ) : null}
    </div>
  );
}
