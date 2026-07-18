import { EmptyState } from "@/components/layout/empty-state";

/** Ask Axon — freshness-aware Q&A (wired in T4.3/T4.4). */
export default function AskPage() {
  return (
    <EmptyState
      title="Ask Axon"
      description="Ask questions about this repository. Every answer will cite its sources — and warn you when a source has drifted from the code."
    />
  );
}
