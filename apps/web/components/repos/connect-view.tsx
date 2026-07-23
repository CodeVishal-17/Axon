"use client";

import { LogIn } from "lucide-react";
import { githubLoginUrl } from "@/lib/api";
import { useMe } from "@/lib/queries";
import { PageContainer } from "@/components/layout/page-container";
import { EmptyState } from "@/components/layout/empty-state";
import { Button } from "@/components/ui/button";
import { RepoPicker } from "@/components/repos/repo-picker";

/** The connect screen: pick repositories to bring under Axon. Gated on auth. */
export function ConnectView() {
  const { data: user, isPending } = useMe();

  if (isPending) {
    return (
      <PageContainer>
        <div className="h-40 animate-pulse rounded-lg border border-border/40 bg-card/40" />
      </PageContainer>
    );
  }

  if (!user) {
    return (
      <PageContainer>
        <EmptyState
          icon={<LogIn className="size-6" aria-hidden />}
          title="Sign in to connect repositories"
          description="Axon needs to know who you are before it can list and connect your repositories."
        >
          <Button className="mt-2" render={<a href={githubLoginUrl()} />}>
            <LogIn className="size-4" aria-hidden />
            Sign in with GitHub
          </Button>
        </EmptyState>
      </PageContainer>
    );
  }

  return (
    <PageContainer className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          Connect repositories
        </h1>
        <p className="text-muted-foreground text-sm">
          Select the repositories Axon should verify. Only repositories where
          you&apos;ve installed the Axon app appear here.
        </p>
      </header>
      <RepoPicker />
    </PageContainer>
  );
}
