"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { PageContainer } from "@/components/layout/page-container";

/**
 * Landing / connect page.
 *
 * The form currently performs client-side navigation to the repo shell so
 * the whole route structure is exercisable. T1.6 replaces the submit handler
 * with the real POST /api/repos connect flow (PAT input + ingest progress).
 */
export default function HomePage() {
  const router = useRouter();
  const [repo, setRepo] = useState("");

  function openRepo(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = repo.trim();
    if (!/^[\w.-]+\/[\w.-]+$/.test(trimmed)) return;
    router.push(`/repos/${encodeURIComponent(trimmed)}`);
  }

  return (
    <PageContainer className="flex flex-col items-center gap-10 py-20 sm:py-28">
      <div className="flex max-w-2xl flex-col items-center gap-4 text-center">
        <p className="text-muted-foreground font-mono text-xs tracking-widest uppercase">
          Belief → Verify → Detect Drift → Act
        </p>
        <h1 className="text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
          Your documentation is lying to you.
          <br />
          <span className="text-emerald-400">Axon notices.</span>
        </h1>
        <p className="text-muted-foreground max-w-xl text-base text-balance">
          Docs, issues, and ADRs contain beliefs. Code is reality. Axon
          continuously verifies one against the other and flags knowledge the
          moment it becomes false.
        </p>
      </div>

      <Card className="w-full max-w-xl">
        <CardContent className="pt-6">
          <form onSubmit={openRepo} className="flex flex-col gap-3 sm:flex-row">
            <Input
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="owner/repository"
              aria-label="GitHub repository (owner/repository)"
              className="font-mono"
              autoFocus
            />
            <Button type="submit" className="shrink-0">
              Connect repository
            </Button>
          </form>
          <p className="text-muted-foreground mt-3 text-xs">
            GitHub-first. Notion, Slack, and Jira arrive as adapters.
          </p>
        </CardContent>
      </Card>
    </PageContainer>
  );
}
