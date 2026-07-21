"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ArrowRight, GitBranch, Loader2, Play } from "lucide-react";
import { ApiError, connectRepo } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageContainer } from "@/components/layout/page-container";

const FULL_NAME_RE = /^[\w.-]+\/[\w.-]+$/;

/** The only conversion surface: a real repository connection, no demo data. */
export function LandingHero() {
  const router = useRouter();
  const [repo, setRepo] = useState("");
  const [token, setToken] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);

  const connect = useMutation({
    mutationFn: () =>
      connectRepo({ full_name: repo.trim(), token: token.trim() || null }),
    onSuccess: (created) => router.push(`/repos/${created.id}`),
  });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!FULL_NAME_RE.test(repo.trim())) {
      setValidationError("Enter a repository as owner/repository");
      return;
    }
    setValidationError(null);
    connect.mutate();
  }

  const apiError =
    connect.error instanceof ApiError
      ? connect.error.status === 422
        ? "The repository name must look like owner/repository."
        : `The Axon API rejected the request (HTTP ${connect.error.status}).`
      : connect.error
        ? "Can't reach the Axon API — is the backend running?"
        : null;
  const errorText = validationError ?? apiError;

  return (
    <section className="landing-page-bg relative isolate overflow-hidden border-b border-border/20">
      <div className="landing-grid" aria-hidden />
      <div className="landing-glow landing-glow-one" aria-hidden />
      <div className="landing-glow landing-glow-two" aria-hidden />
      <PageContainer className="relative z-10 flex min-h-[calc(100svh-3.5rem)] flex-col items-center justify-center py-20 sm:py-28 lg:py-32">
        <div className="animate-landing-rise flex max-w-4xl flex-col items-center text-center">
          <p className="border-emerald-500/30 bg-emerald-500/10 text-emerald-400 inline-flex items-center gap-2 rounded-full border px-4 py-1.5 font-mono text-[11px] tracking-wide uppercase shadow-[0_0_20px_rgba(16,185,129,0.2)]">
            <span className="bg-emerald-400 inline-block size-2 rounded-full shadow-[0_0_10px_rgba(52,211,153,1)] animate-pulse" />
            Truth maintenance for engineering teams
          </p>
          <h1 className="mt-8 max-w-3xl text-5xl leading-[1.1] font-bold tracking-[-0.04em] text-balance sm:text-6xl lg:text-7xl text-white">
            Reality changed.
            <br />
            <span className="text-gradient">Your documentation didn&apos;t.</span>
          </h1>
          <p className="text-slate-400 mt-7 max-w-2xl text-lg leading-8 text-balance sm:text-xl">
            Axon is an AI-powered Truth Maintenance System that continuously verifies organizational knowledge (Notion, Slack, Docs) against reality (Code, Databases, APIs).
          </p>
        </div>

        <div className="animate-landing-rise animate-landing-rise-delay-1 mt-12 w-full max-w-4xl">
          <h2 className="mb-4 text-center text-sm font-semibold tracking-wide text-muted-foreground uppercase">Connect Knowledge Sources</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {/* GitHub (Active) */}
            <div className="col-span-1 md:col-span-2 lg:col-span-3">
              <form
                onSubmit={submit}
                className="glass-panel rounded-2xl p-4 backdrop-blur-md border border-emerald-500/20 shadow-[0_0_15px_rgba(16,185,129,0.1)] relative overflow-hidden"
              >
                <div className="absolute top-0 right-0 rounded-bl-lg bg-emerald-500/20 px-2 py-1 text-[10px] font-mono text-emerald-400 uppercase font-semibold">Active</div>
                <h3 className="flex items-center gap-2 font-medium mb-3">
                  <GitBranch className="text-emerald-400 size-4" /> GitHub
                </h3>
                <label htmlFor="repository" className="sr-only">
                  GitHub repository
                </label>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <div className="flex min-w-0 flex-1 items-center gap-2 px-3 border rounded-md border-border/40 bg-background/50">
                    <Input
                      id="repository"
                      value={repo}
                      onChange={(event) => setRepo(event.target.value)}
                      placeholder="owner/repository"
                      aria-invalid={Boolean(errorText)}
                      className="border-0 bg-transparent font-mono shadow-none focus-visible:ring-0 dark:bg-transparent px-0"
                      autoComplete="off"
                      autoFocus
                      disabled={connect.isPending}
                    />
                  </div>
                  <Button type="submit" disabled={connect.isPending}>
                    {connect.isPending ? (
                      <><Loader2 className="animate-spin mr-2 size-4" aria-hidden /> Connecting…</>
                    ) : (
                      <>Connect <ArrowRight className="ml-2 size-4" aria-hidden /></>
                    )}
                  </Button>
                </div>
                <details className="group mt-3">
                  <summary className="text-muted-foreground cursor-pointer list-none text-xs hover:text-foreground focus-visible:outline-none [&::-webkit-details-marker]:hidden">
                    <span className="group-open:hidden">Private repository? Add a token</span>
                    <span className="hidden group-open:inline">GitHub token (optional)</span>
                  </summary>
                  <Input
                    value={token}
                    onChange={(event) => setToken(event.target.value)}
                    type="password"
                    autoComplete="off"
                    placeholder="Fine-grained GitHub token"
                    aria-label="GitHub personal access token (optional)"
                    className="mt-2 font-mono text-xs"
                    disabled={connect.isPending}
                  />
                </details>
                {errorText ? <p role="alert" className="mt-2 text-sm text-red-400">{errorText}</p> : null}
              </form>
            </div>

            {/* Coming Soon Cards */}
            {["Notion", "Slack", "Jira", "Confluence", "Google Docs"].map((source) => (
              <div key={source} className="glass-panel rounded-2xl p-4 backdrop-blur-md border border-border/20 opacity-60 grayscale cursor-not-allowed flex items-center justify-between">
                <span className="font-medium">{source}</span>
                <span className="text-[10px] font-mono text-muted-foreground uppercase bg-muted/50 px-2 py-1 rounded">Coming Soon</span>
              </div>
            ))}
          </div>

          <div className="mt-8 flex flex-col items-center justify-center gap-3 text-sm sm:flex-row">
            <Button size="sm" variant="ghost" render={<a href="#product" />}>
              <Play className="size-3.5 mr-2" aria-hidden /> Watch demo
            </Button>
            <span className="text-muted-foreground/40 hidden sm:inline" aria-hidden>•</span>
            <p className="text-muted-foreground text-xs">Credentials never leave your server.</p>
          </div>
        </div>
      </PageContainer>
    </section>
  );
}
