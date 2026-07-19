"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { ApiError, connectRepo } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { PageContainer } from "@/components/layout/page-container";

const FULL_NAME_RE = /^[\w.-]+\/[\w.-]+$/;

/**
 * Landing / connect page. Submits to POST /api/repos and navigates to the
 * repo's live status page. The PAT is sent once over the API and stored
 * server-side; it is never echoed back.
 */
export default function HomePage() {
  const router = useRouter();
  const [repo, setRepo] = useState("");
  const [token, setToken] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);

  const connect = useMutation({
    mutationFn: () =>
      connectRepo({
        full_name: repo.trim(),
        token: token.trim() || null,
      }),
    onSuccess: (created) => router.push(`/repos/${created.id}`),
  });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = repo.trim();
    if (!FULL_NAME_RE.test(trimmed)) {
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
          <form onSubmit={submit} className="flex flex-col gap-3">
            <div className="flex flex-col gap-3 sm:flex-row">
              <Input
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
                placeholder="owner/repository"
                aria-label="GitHub repository (owner/repository)"
                aria-invalid={Boolean(errorText)}
                className="font-mono"
                autoFocus
                disabled={connect.isPending}
              />
              <Button
                type="submit"
                className="shrink-0"
                disabled={connect.isPending}
              >
                {connect.isPending ? (
                  <>
                    <Loader2 className="size-4 animate-spin" aria-hidden />
                    Connecting…
                  </>
                ) : (
                  "Connect repository"
                )}
              </Button>
            </div>
            <Input
              value={token}
              onChange={(e) => setToken(e.target.value)}
              type="password"
              autoComplete="off"
              placeholder="GitHub token (optional — for private repos & rate limits)"
              aria-label="GitHub personal access token (optional)"
              className="font-mono text-xs"
              disabled={connect.isPending}
            />
            {errorText ? (
              <p role="alert" className="text-sm text-red-400">
                {errorText}
              </p>
            ) : null}
          </form>
          <p className="text-muted-foreground mt-3 text-xs">
            GitHub-first. Notion, Slack, and Jira arrive as adapters. Tokens
            are stored server-side and never shown again.
          </p>
        </CardContent>
      </Card>
    </PageContainer>
  );
}
