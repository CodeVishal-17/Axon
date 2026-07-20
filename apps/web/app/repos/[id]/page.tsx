import { Feed } from "@/components/feed/feed";

/**
 * Truth Feed — the product's front page. Thin by design: the Feed
 * container owns data fetching, everything under it is presentational.
 */
export default async function TruthFeedPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <Feed repoId={id} />;
}
