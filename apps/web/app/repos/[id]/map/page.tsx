import { EmptyState } from "@/components/layout/empty-state";

/** Map — knowledge graph of the repo (rendered in T4.5). */
export default function MapPage() {
  return (
    <EmptyState
      title="Map not available yet"
      description="The knowledge map shows code, docs, issues, and people as a graph — with bus-factor heat and drift findings overlaid."
    />
  );
}
