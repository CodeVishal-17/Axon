"use client";

import { Loader2, RotateCcw } from "lucide-react";
import { ApiError, type RepoDetail } from "@/lib/api";
import { useRepo, useRetryIngest } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

type Status = RepoDetail["ingest_status"];

const STATUS_STYLES: Record<Status, string> = {
  pending: "bg-zinc-500/15 text-zinc-300 ring-zinc-500/30",
  ingesting: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  ready: "bg-emerald-500/15 text-emerald-400 ring-emerald-500/30",
  failed: "bg-red-500/15 text-red-400 ring-red-500/30",
};

const COUNT_LABELS: Record<string, string> = {
  code_file: "code files",
  doc: "docs",
  doc_section: "sections",
  issue: "issues",
  pull_request: "PRs",
  person: "people",
};

function StatusBadge({ status }: { status: Status }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ring-1 ring-inset",
        STATUS_STYLES[status],
      )}
    >
      {(status === "pending" || status === "ingesting") && (
        <Loader2 className="size-3 animate-spin" aria-hidden />
      )}
      {status}
    </span>
  );
}

/**
 * Live repository header: name, ingest status (polled until terminal),
 * progress while ingesting, entity counts when ready, and error + retry
 * when failed.
 */
export function RepoHeader({ repoId }: { repoId: string }) {
  const { data: repo, error, isPending } = useRepo(repoId);
  const retry = useRetryIngest(repoId, repo?.full_name);

  if (error) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div className="border-border/60 rounded-lg border border-dashed p-4 text-sm">
        <p className="font-medium">
          {notFound ? "Repository not found" : "Can't reach the Axon API"}
        </p>
        <p className="text-muted-foreground mt-1">
          {notFound
            ? "This repository isn't connected. Head back and connect it first."
            : "Check that the backend is running, then reload this page."}
        </p>
      </div>
    );
  }

  if (isPending || !repo) {
    return (
      <div className="flex flex-col gap-2 pt-2" aria-busy>
        <div className="flex items-center gap-3">
          <Skeleton className="h-7 w-64" />
          <Skeleton className="h-5 w-20 rounded-full" />
        </div>
      </div>
    );
  }

  const counts = Object.entries(repo.entity_counts);
  const job = repo.latest_job;

  return (
    <div className="flex flex-col gap-2 pt-2">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-mono text-xl font-semibold tracking-tight">
          {repo.full_name}
        </h1>
        <StatusBadge status={repo.ingest_status} />
        {repo.last_ingested_sha ? (
          <span className="text-muted-foreground font-mono text-xs">
            @ {repo.last_ingested_sha.slice(0, 7)}
          </span>
        ) : null}
      </div>

      {repo.ingest_status === "pending" ? (
        <p className="text-muted-foreground text-sm">
          Queued — a worker will pick this up momentarily.
        </p>
      ) : null}

      {repo.ingest_status === "ingesting" ? (
        <div className="flex max-w-md flex-col gap-1.5">
          <p className="text-muted-foreground text-sm">
            Scanning repository — building the knowledge graph…
            {job && job.attempts > 1 ? ` (attempt ${job.attempts})` : ""}
          </p>
          <div className="bg-secondary h-1 overflow-hidden rounded-full">
            <div className="animate-indeterminate h-full w-1/3 rounded-full bg-amber-400/80" />
          </div>
        </div>
      ) : null}

      {repo.ingest_status === "failed" ? (
        <div className="border-border/60 flex max-w-xl flex-col gap-2 rounded-md border border-red-500/30 bg-red-500/5 p-3">
          <p className="text-sm font-medium text-red-400">Ingestion failed</p>
          {job?.error ? (
            <p className="text-muted-foreground break-words font-mono text-xs">
              {job.error}
            </p>
          ) : null}
          <div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => retry.mutate()}
              disabled={retry.isPending}
            >
              <RotateCcw
                className={cn("size-3.5", retry.isPending && "animate-spin")}
                aria-hidden
              />
              {retry.isPending ? "Re-queuing…" : "Retry ingestion"}
            </Button>
          </div>
        </div>
      ) : null}

      {repo.ingest_status === "ready" && counts.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {counts.map(([kind, count]) => (
            <span
              key={kind}
              className="bg-secondary/60 text-muted-foreground rounded-full px-2 py-0.5 text-[11px]"
            >
              {count} {COUNT_LABELS[kind] ?? kind}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
