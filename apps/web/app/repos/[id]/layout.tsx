import type { Metadata } from "next";
import { RepoTabs } from "@/components/layout/repo-tabs";
import { PageContainer } from "@/components/layout/page-container";
import { RepoHeader } from "@/components/repo/repo-header";

/**
 * Shared shell for all repo views. `id` is the repository UUID from
 * POST /api/repos; all live data (name, status, counts) is fetched
 * client-side by RepoHeader, which polls until ingestion reaches a
 * terminal state. `params` is a Promise in Next.js 15+/16.
 */

export const metadata: Metadata = { title: "Repository" };

export default async function RepoLayout({
  params,
  children,
}: {
  params: Promise<{ id: string }>;
  children: React.ReactNode;
}) {
  const { id } = await params;

  return (
    <PageContainer className="flex flex-col gap-4">
      <RepoHeader repoId={id} />
      <RepoTabs repoId={id} />
      <section className="min-h-[50vh]">{children}</section>
    </PageContainer>
  );
}
