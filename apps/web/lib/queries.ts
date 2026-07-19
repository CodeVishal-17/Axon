"use client";

/**
 * TanStack Query hooks — the frontend's only data-fetching layer for the
 * repo lifecycle. All payload types are the GENERATED API types via lib/api.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ApiError, connectRepo, getRepo, type RepoDetail } from "@/lib/api";

const POLL_MS = 1500;

export function isTerminal(status: RepoDetail["ingest_status"] | undefined) {
  return status === "ready" || status === "failed";
}

/**
 * Live repo state. Polls every 1.5s until the repo reaches a terminal
 * state (ready/failed); a later invalidation (e.g. retry) that returns a
 * non-terminal status automatically resumes polling, because the interval
 * callback is re-evaluated on every fetch result.
 */
export function useRepo(repoId: string) {
  return useQuery({
    queryKey: ["repo", repoId],
    queryFn: () => getRepo(repoId),
    refetchInterval: (query) =>
      isTerminal(query.state.data?.ingest_status) ? false : POLL_MS,
    retry: (failureCount, error) => {
      // A 404 will never heal — stop immediately. Transient errors get 3 tries.
      if (error instanceof ApiError && error.status === 404) return false;
      return failureCount < 3;
    },
  });
}

/**
 * Re-enqueue ingestion for a failed repo. The backend's POST /api/repos is
 * idempotent on full_name and re-enqueues only when the previous ingest
 * failed — exactly the retry semantics this button needs.
 */
export function useRetryIngest(repoId: string, fullName: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => {
      if (!fullName) throw new Error("repository not loaded yet");
      return connectRepo({ full_name: fullName });
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["repo", repoId] }),
  });
}
