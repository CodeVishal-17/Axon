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
import {
  ApiError,
  actionFinding,
  connectRepo,
  getAvailableRepos,
  getDashboard,
  getMe,
  getRepo,
  listFindings,
  type FindingAction,
  type FindingPage,
  type FindingStatus,
  type RepoDetail,
} from "@/lib/api";

const POLL_MS = 1500;
/** Feed cadence: brisk while work is in flight, calm when settled. */
const FEED_POLL_ACTIVE_MS = 4000;
const FEED_POLL_IDLE_MS = 12000;

export function isTerminal(status: RepoDetail["ingest_status"] | undefined) {
  return status === "ready" || status === "failed";
}

/**
 * The signed-in user, or `null` when signed out. A 401 is the normal
 * signed-out state, so it resolves to `null` rather than erroring; other
 * failures surface. Cached generously — identity rarely changes mid-session.
 */
export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await getMe();
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },
    staleTime: 60_000,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 401) return false;
      return failureCount < 2;
    },
  });
}

/** The per-user dashboard rollup. Only meaningful when signed in. */
export function useDashboard(enabled = true) {
  return useQuery({
    queryKey: ["dashboard"],
    queryFn: getDashboard,
    enabled,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 401) return false;
      return failureCount < 2;
    },
  });
}

/** Repos the signed-in user can connect (Axon App installations). */
export function useAvailableRepos(enabled = true) {
  return useQuery({
    queryKey: ["available-repos"],
    queryFn: getAvailableRepos,
    enabled,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 401) return false;
      return failureCount < 2;
    },
  });
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

export const findingsKey = (repoId: string, status: FindingStatus) =>
  ["findings", repoId, status] as const;

/**
 * The Truth Feed's data source. Polls automatically — faster while the
 * repository is still ingesting or a fix job is in flight, slower once
 * everything has settled. (The backend exposes no WebSocket channel, so
 * polling is the transport; the cadence switch keeps it cheap.)
 */
export function useFindings(
  repoId: string,
  status: FindingStatus,
  { active = false }: { active?: boolean } = {},
) {
  return useQuery({
    queryKey: findingsKey(repoId, status),
    queryFn: () => listFindings(repoId, { status, limit: 100 }),
    refetchInterval: active ? FEED_POLL_ACTIVE_MS : FEED_POLL_IDLE_MS,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 404) return false;
      return failureCount < 3;
    },
  });
}

/**
 * Dismiss a finding, or queue its remediation PR.
 *
 * Optimistic: dismissals disappear from the open feed immediately and roll
 * back if the request fails. `generate_fix` cannot be faked — the backend
 * flips the finding to `actioned` only once the worker has actually opened
 * the pull request — so the mutation reports the real queued state and the
 * poll reveals the outcome.
 */
export function useFindingAction(repoId: string, status: FindingStatus) {
  const queryClient = useQueryClient();
  const key = findingsKey(repoId, status);

  return useMutation({
    mutationFn: ({
      findingId,
      action,
    }: {
      findingId: string;
      action: FindingAction;
    }) => actionFinding(findingId, action),

    onMutate: async ({ findingId, action }) => {
      if (action !== "dismiss") return { previous: undefined };
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<FindingPage>(key);
      if (previous) {
        queryClient.setQueryData<FindingPage>(key, {
          ...previous,
          total: Math.max(0, previous.total - 1),
          items: previous.items.filter((item) => item.id !== findingId),
        });
      }
      return { previous };
    },

    onError: (_error, _variables, context) => {
      if (context?.previous) queryClient.setQueryData(key, context.previous);
    },

    onSettled: () => queryClient.invalidateQueries({ queryKey: key }),
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
