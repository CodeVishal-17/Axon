"use client";

import { useCallback, useMemo, useState } from "react";
import {
  ApiError,
  type FindingAction,
  type FindingOut,
  type FindingSeverity,
  type FindingStatus,
} from "@/lib/api";
import { useFindingAction, useFindings } from "@/lib/queries";
import { ErrorState } from "@/components/common/error-state";
import { EmptyState } from "@/components/layout/empty-state";
import { FeedList } from "@/components/feed/feed-list";
import { FeedSkeleton } from "@/components/feed/feed-skeleton";
import { FeedToolbar } from "@/components/feed/feed-toolbar";
import { FindingDetail } from "@/components/finding/finding-detail";
import type { ActionState } from "@/components/finding/finding-actions";

const SEVERITY_RANK: Record<FindingSeverity, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};

const IDLE: ActionState = { kind: "idle" };

const EMPTY_COPY: Record<FindingStatus, { title: string; description: string }> = {
  open: {
    title: "No open findings",
    description:
      "Every verified claim currently matches the code. New findings appear here the moment reality drifts from the documentation.",
  },
  actioned: {
    title: "No fix pull requests yet",
    description:
      "Findings you send to GitHub will appear here once Axon has opened their pull requests.",
  },
  dismissed: {
    title: "Nothing dismissed",
    description: "Findings you dismiss are kept here for the record.",
  },
};

/** Error copy differs by context: a 404 on the feed means the repository
 *  isn't connected; a 404 on an action means the finding vanished. */
function friendlyError(error: unknown, context: "feed" | "action"): string {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return "Axon hasn't drafted a remediation for this finding yet.";
    }
    if (error.status === 404) {
      return context === "feed"
        ? "This repository isn't connected to Axon."
        : "This finding no longer exists.";
    }
    return `The API rejected the request (HTTP ${error.status}).`;
  }
  return context === "feed"
    ? "Couldn't reach the Axon API — check that the backend is running."
    : "Couldn't reach the Axon API.";
}

/**
 * Truth Feed container: owns data fetching, polling cadence, mutation
 * state, and selection. Everything below it is presentational and
 * memoized, so a poll that returns unchanged findings costs no rerenders
 * beyond this component.
 */
export function Feed({ repoId }: { repoId: string }) {
  const [status, setStatus] = useState<FindingStatus>("open");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [actionStates, setActionStates] = useState<Record<string, ActionState>>(
    {},
  );

  const hasWorkInFlight = useMemo(
    () =>
      Object.values(actionStates).some(
        (state) => state.kind === "pending" || state.kind === "queued",
      ),
    [actionStates],
  );

  const query = useFindings(repoId, status, { active: hasWorkInFlight });
  const mutation = useFindingAction(repoId, status);

  // Headline first: worst severity, then most recent.
  const findings = useMemo(() => {
    const items = query.data?.items ?? [];
    return [...items].sort(
      (a, b) =>
        SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] ||
        Date.parse(b.created_at) - Date.parse(a.created_at),
    );
  }, [query.data]);

  const selected: FindingOut | null = useMemo(
    () => findings.find((finding) => finding.id === selectedId) ?? null,
    [findings, selectedId],
  );

  const handleAction = useCallback(
    (findingId: string, action: FindingAction) => {
      setActionStates((current) => ({
        ...current,
        [findingId]: { kind: "pending", action },
      }));
      mutation.mutate(
        { findingId, action },
        {
          onSuccess: (response) => {
            setActionStates((current) => ({
              ...current,
              [findingId]:
                action === "dismiss"
                  ? IDLE
                  : response.status === "already_open"
                    ? { kind: "opened", prUrl: response.pr_url }
                    : { kind: "queued" },
            }));
            if (action === "dismiss") setSelectedId(null);
          },
          onError: (error) => {
            setActionStates((current) => ({
              ...current,
              [findingId]: {
                kind: "error",
                message: friendlyError(error, "action"),
              },
            }));
          },
        },
      );
    },
    [mutation],
  );

  const handleOpen = useCallback((findingId: string) => {
    setSelectedId(findingId);
  }, []);

  const handleDetailOpenChange = useCallback((open: boolean) => {
    if (!open) setSelectedId(null);
  }, []);

  const body = () => {
    if (query.isPending) return <FeedSkeleton />;
    if (query.error) {
      return (
        <ErrorState
          title="Couldn't load the Truth Feed"
          description={friendlyError(query.error, "feed")}
          onRetry={() => query.refetch()}
        />
      );
    }
    if (findings.length === 0) {
      return <EmptyState {...EMPTY_COPY[status]} />;
    }
    return (
      <FeedList
        findings={findings}
        actionStates={actionStates}
        onOpen={handleOpen}
        onAction={handleAction}
      />
    );
  };

  return (
    <div className="flex flex-col gap-4">
      <FeedToolbar
        status={status}
        onStatusChange={setStatus}
        total={query.data?.total ?? null}
        isFetching={query.isFetching}
        isLive={!query.error}
      />
      {body()}
      <FindingDetail
        finding={selected}
        state={selected ? (actionStates[selected.id] ?? IDLE) : IDLE}
        onAction={(action) => {
          if (selected) handleAction(selected.id, action);
        }}
        onOpenChange={handleDetailOpenChange}
      />
    </div>
  );
}
