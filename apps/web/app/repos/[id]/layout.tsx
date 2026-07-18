import type { Metadata } from "next";
import { RepoTabs } from "@/components/layout/repo-tabs";
import { PageContainer } from "@/components/layout/page-container";
import { Badge } from "@/components/ui/badge";

/**
 * Shared shell for all repo views: repo identity header + section tabs.
 * `params` is a Promise in Next.js 15+/16 — awaited here (server component).
 *
 * `id` is the URL-encoded repo identifier. Until repo connection is wired
 * to the backend (T1.6), we decode it for display; afterwards this layout
 * will resolve real repo metadata via the API client.
 */

type RepoParams = { params: Promise<{ id: string }> };

export async function generateMetadata({ params }: RepoParams): Promise<Metadata> {
  const { id } = await params;
  return { title: decodeURIComponent(id) };
}

export default async function RepoLayout({
  params,
  children,
}: RepoParams & { children: React.ReactNode }) {
  const { id } = await params;
  const repoName = decodeURIComponent(id);

  return (
    <PageContainer className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3 pt-2">
        <h1 className="font-mono text-xl font-semibold tracking-tight">
          {repoName}
        </h1>
        <Badge variant="secondary" className="text-xs">
          not ingested
        </Badge>
      </div>
      <RepoTabs repoId={id} />
      <section className="min-h-[50vh]">{children}</section>
    </PageContainer>
  );
}
