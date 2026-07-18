import { EmptyState } from "@/components/layout/empty-state";

/**
 * Truth Feed — the default repo view. Real finding cards arrive with the
 * findings API integration (T1.5/T2.5); until then, an honest empty state.
 */
export default function TruthFeedPage() {
  return (
    <EmptyState
      title="No findings yet"
      description="Once this repository is ingested, Axon will verify its documentation and issues against the code and surface contradictions here."
    />
  );
}
