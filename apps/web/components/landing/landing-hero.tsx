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
    <section className="landing-grid relative isolate overflow-hidden border-b border-border/60">
      <div className="landing-glow landing-glow-one" aria-hidden />
      <div className="landing-glow landing-glow-two" aria-hidden />
      <PageContainer className="relative flex min-h-[calc(100svh-3.5rem)] flex-col items-center justify-center py-20 sm:py-28 lg:py-32">
        <div className="animate-landing-rise flex max-w-4xl flex-col items-center text-center">
          <p className="border-border/70 bg-card/70 text-muted-foreground inline-flex items-center gap-2 rounded-full border px-3 py-1.5 font-mono text-[11px] tracking-wide uppercase shadow-sm">
            <span className="bg-emerald-400 inline-block size-1.5 rounded-full shadow-[0_0_10px_rgba(52,211,153,0.9)]" />
            Truth maintenance for engineering teams
          </p>
          <h1 className="mt-7 max-w-3xl text-5xl leading-[0.98] font-semibold tracking-[-0.055em] text-balance sm:text-6xl lg:text-7xl">
            Reality changed.
            <br />
            <span className="text-emerald-300">Your documentation didn&apos;t.</span>
          </h1>
          <p className="text-muted-foreground mt-7 max-w-2xl text-base leading-7 text-balance sm:text-lg">
            Axon continuously verifies engineering knowledge against reality and
            detects when documentation becomes false.
          </p>
        </div>

        <div className="animate-landing-rise animate-landing-rise-delay-one mt-10 w-full max-w-2xl">
          <form
            onSubmit={submit}
            className="border-border/70 bg-card/80 rounded-2xl border p-2 shadow-2xl shadow-black/15 backdrop-blur-sm"
          >
            <label htmlFor="repository" className="sr-only">
              GitHub repository
            </label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <div className="flex min-w-0 flex-1 items-center gap-2 px-2">
                <GitBranch className="text-muted-foreground size-4 shrink-0" aria-hidden />
                <Input
                  id="repository"
                  value={repo}
                  onChange={(event) => setRepo(event.target.value)}
                  placeholder="owner/repository"
                  aria-invalid={Boolean(errorText)}
                  className="border-0 bg-transparent font-mono shadow-none focus-visible:ring-0 dark:bg-transparent"
                  autoComplete="off"
                  autoFocus
                  disabled={connect.isPending}
                />
              </div>
              <Button type="submit" size="lg" disabled={connect.isPending}>
                {connect.isPending ? (
                  <><Loader2 className="animate-spin" aria-hidden /> Connecting…</>
                ) : (
                  <>Connect repository <ArrowRight aria-hidden /></>
                )}
              </Button>
            </div>
            <details className="group px-2 pt-1.5">
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
            {errorText ? <p role="alert" className="px-2 pt-2 text-sm text-red-400">{errorText}</p> : null}
          </form>
          <div className="mt-4 flex flex-col items-center justify-center gap-3 text-sm sm:flex-row">
            <Button size="sm" variant="ghost" render={<a href="#product" />}>
              <Play className="size-3.5" aria-hidden /> Watch demo
            </Button>
            <span className="text-muted-foreground/40 hidden sm:inline" aria-hidden>•</span>
            <p className="text-muted-foreground text-xs">GitHub token stays server-side. It is never shown again.</p>
          </div>
        </div>
      </PageContainer>
    </section>
  );
}
