import { PullRequests } from "@/components/pulls/pull-requests";

/**
 * Pull Requests — Axon reviewing work the team already opened. Thin by
 * design: the container owns data fetching, everything under it is
 * presentational.
 */
export default async function PullRequestsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <PullRequests repoId={id} />;
}
