"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  ArrowUpRight,
  Check,
  Loader2,
  Lock,
  PlusCircle,
} from "lucide-react";
import { connectRepo, type AvailableRepo } from "@/lib/api";
import { useAvailableRepos } from "@/lib/queries";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/layout/empty-state";
import { ErrorState } from "@/components/common/error-state";
import { cn } from "@/lib/utils";

/**
 * Multi-repo connect: lists the repositories the Axon GitHub App is installed
 * on, lets the user select any number of not-yet-connected ones and connect
 * them in a batch, and links out to GitHub to grant access to more.
 */
export function RepoPicker() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const query = useAvailableRepos();
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const connect = useMutation({
    mutationFn: async (fullNames: string[]) => {
      // Sequential: each POST validates + enqueues an ingest; a handful of
      // repos at a time, so no need to parallelize.
      for (const full_name of fullNames) {
        await connectRepo({ full_name });
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      queryClient.invalidateQueries({ queryKey: ["available-repos"] });
      router.push("/dashboard");
    },
  });

  function toggle(fullName: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(fullName)) next.delete(fullName);
      else next.add(fullName);
      return next;
    });
  }

  if (query.isPending) {
    return (
      <div className="flex flex-col gap-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <div
            key={i}
            className="h-14 animate-pulse rounded-lg border border-border/40 bg-card/40"
          />
        ))}
      </div>
    );
  }

  if (query.error) {
    return (
      <ErrorState
        title="Couldn't load your repositories"
        description="Axon couldn't reach GitHub to list your repositories. Try again in a moment."
        onRetry={() => query.refetch()}
      />
    );
  }

  const { repos, install_url } = query.data;
  const connectable = repos.filter((r) => !r.connected);

  if (repos.length === 0) {
    return (
      <EmptyState
        icon={<PlusCircle className="size-6" aria-hidden />}
        title="Install Axon on a repository"
        description="Axon opens fix PRs as a GitHub App, so it only sees the repositories you grant it. Install it on GitHub, then come back to connect."
      >
        {install_url ? (
          <Button className="mt-2" render={<a href={install_url} />}>
            Install Axon on GitHub
            <ArrowUpRight className="size-4" aria-hidden />
          </Button>
        ) : null}
      </EmptyState>
    );
  }

  const count = selected.size;

  return (
    <div className="flex flex-col gap-4">
      <ul className="divide-y divide-border/40 overflow-hidden rounded-lg border border-border/40 bg-card/40">
        {repos.map((repo) => (
          <RepoRow
            key={repo.full_name}
            repo={repo}
            selected={selected.has(repo.full_name)}
            disabled={connect.isPending}
            onToggle={() => toggle(repo.full_name)}
          />
        ))}
      </ul>

      {connect.error ? (
        <p role="alert" className="text-sm text-red-400">
          Couldn&apos;t connect one of the repositories. Please try again.
        </p>
      ) : null}

      <div className="flex flex-col-reverse items-stretch justify-between gap-3 sm:flex-row sm:items-center">
        {install_url ? (
          <a
            href={install_url}
            className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-sm"
          >
            <PlusCircle className="size-3.5" aria-hidden />
            Install on more repositories
            <ArrowUpRight className="size-3.5" aria-hidden />
          </a>
        ) : (
          <span />
        )}
        <Button
          disabled={count === 0 || connect.isPending}
          onClick={() => connect.mutate([...selected])}
        >
          {connect.isPending ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden />
              Connecting…
            </>
          ) : (
            <>
              Connect {count > 0 ? `${count} ` : ""}
              {count === 1 ? "repository" : "repositories"}
              <ArrowRight className="size-4" aria-hidden />
            </>
          )}
        </Button>
      </div>

      {connectable.length === 0 ? (
        <p className="text-muted-foreground text-center text-sm">
          Every installed repository is already connected.
        </p>
      ) : null}
    </div>
  );
}

function RepoRow({
  repo,
  selected,
  disabled,
  onToggle,
}: {
  repo: AvailableRepo;
  selected: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  if (repo.connected) {
    return (
      <li className="flex items-center justify-between gap-3 px-4 py-3">
        <div className="min-w-0">
          <p className="flex items-center gap-2 truncate font-mono text-sm">
            {repo.full_name}
            {repo.private ? (
              <Lock className="text-muted-foreground size-3" aria-hidden />
            ) : null}
          </p>
          <p className="text-muted-foreground text-xs">Connected</p>
        </div>
        {repo.repo_id ? (
          <Link
            href={`/repos/${repo.repo_id}`}
            className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs"
          >
            View feed
            <ArrowRight className="size-3.5" aria-hidden />
          </Link>
        ) : null}
      </li>
    );
  }

  return (
    <li>
      <button
        type="button"
        onClick={onToggle}
        disabled={disabled}
        aria-pressed={selected}
        className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-muted/40 disabled:opacity-50"
      >
        <span
          className={cn(
            "flex size-5 shrink-0 items-center justify-center rounded border transition-colors",
            selected
              ? "border-primary bg-primary text-primary-foreground"
              : "border-border bg-transparent",
          )}
          aria-hidden
        >
          {selected ? <Check className="size-3.5" /> : null}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2 truncate font-mono text-sm">
            {repo.full_name}
            {repo.private ? (
              <Lock className="text-muted-foreground size-3" aria-hidden />
            ) : null}
          </span>
          {repo.description ? (
            <span className="text-muted-foreground block truncate text-xs">
              {repo.description}
            </span>
          ) : null}
        </span>
      </button>
    </li>
  );
}
