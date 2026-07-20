import { GitCommitHorizontal, GitPullRequest, Radar, MessageSquare } from "lucide-react";
import type { FindingOut } from "@/lib/api";
import { RelativeTime } from "@/components/common/relative-time";

/** Where this finding came from — the event that changed reality, or the
 *  at-rest scan that first noticed. */
export function Provenance({ finding }: { finding: FindingOut }) {
  const event = finding.event;

  if (!event) {
    return (
      <span className="text-muted-foreground inline-flex items-center gap-1.5 text-xs">
        <Radar className="size-3.5 shrink-0" aria-hidden />
        Found during repository scan
      </span>
    );
  }

  const { icon: Icon, label } =
    event.kind === "pr_merged"
      ? { icon: GitPullRequest, label: `Triggered by PR #${event.external_id}` }
      : event.kind === "issue_closed"
        ? { icon: MessageSquare, label: `Triggered by issue #${event.external_id} closing` }
        : {
            icon: GitCommitHorizontal,
            label: `Triggered by ${event.kind === "simulated" ? "simulated " : ""}push ${
              event.external_id?.slice(0, 7) ?? ""
            }`.trim(),
          };

  return (
    <span className="text-muted-foreground inline-flex items-center gap-1.5 text-xs">
      <Icon className="size-3.5 shrink-0" aria-hidden />
      {label}
      <span aria-hidden>·</span>
      <RelativeTime iso={event.created_at} />
    </span>
  );
}

/** `docs/auth.md:12–14` — where the belief is written down. */
export function anchorLabel(anchor: FindingOut["claim"]["anchor"]): string | null {
  if (!anchor.path) return null;
  if (anchor.start_line == null) return anchor.path;
  const range =
    anchor.end_line != null && anchor.end_line !== anchor.start_line
      ? `${anchor.start_line}–${anchor.end_line}`
      : `${anchor.start_line}`;
  return `${anchor.path}:${range}`;
}
