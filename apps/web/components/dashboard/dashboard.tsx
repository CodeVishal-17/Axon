"use client";

import Link from "next/link";
import {
  ArrowRight,
  Ban,
  GitPullRequest,
  LogIn,
  ScanSearch,
  Wrench,
} from "lucide-react";
import { githubLoginUrl, type DashboardActivity } from "@/lib/api";
import { useDashboard, useMe } from "@/lib/queries";
import { PageContainer } from "@/components/layout/page-container";
import { EmptyState } from "@/components/layout/empty-state";
import { ErrorState } from "@/components/common/error-state";
import { RelativeTime } from "@/components/common/relative-time";
import { Button } from "@/components/ui/button";

/**
 * Per-user dashboard: what Axon has verified and fixed across the signed-in
 * user's repositories. All figures are aggregated server-side from the
 * findings/fixes history — this view is purely presentational.
 */
export function Dashboard() {
  const { data: user, isPending: mePending } = useMe();
  const signedIn = Boolean(user);
  const query = useDashboard(signedIn);

  if (mePending) {
    return (
      <PageContainer>
        <div className="h-40 animate-pulse rounded-lg border border-border/40 bg-card/40" />
      </PageContainer>
    );
  }

  if (!signedIn) {
    return (
      <PageContainer>
        <EmptyState
          icon={<LogIn className="size-6" aria-hidden />}
          title="Sign in to see your dashboard"
          description="Connect with GitHub to view every finding Axon has detected and every fix it has proposed across your repositories."
        >
          <Button className="mt-2" render={<a href={githubLoginUrl()} />}>
            <LogIn className="size-4" aria-hidden />
            Sign in with GitHub
          </Button>
        </EmptyState>
      </PageContainer>
    );
  }

  if (query.isPending) {
    return (
      <PageContainer>
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              className="h-24 animate-pulse rounded-lg border border-border/40 bg-card/40"
            />
          ))}
        </div>
      </PageContainer>
    );
  }

  if (query.error) {
    return (
      <PageContainer>
        <ErrorState
          title="Couldn't load your dashboard"
          description="The Axon API didn't respond. Try again in a moment."
          onRetry={() => query.refetch()}
        />
      </PageContainer>
    );
  }

  const { totals, repos, recent_activity } = query.data;
  const hasRepos = repos.length > 0;

  return (
    <PageContainer className="flex flex-col gap-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          Welcome back{user?.name ? `, ${user.name.split(" ")[0]}` : ""}.
        </h1>
        <p className="text-muted-foreground text-sm">
          Everything Axon has verified and fixed across your repositories.
        </p>
      </header>

      {!hasRepos ? (
        <EmptyState
          icon={<ScanSearch className="size-6" aria-hidden />}
          title="No repositories connected yet"
          description="Connect a GitHub repository and Axon will start verifying your docs against the code. Its findings and fixes will show up here."
        >
          <Button className="mt-2" render={<Link href="/connect" />}>
            Connect a repository
            <ArrowRight className="size-4" aria-hidden />
          </Button>
        </EmptyState>
      ) : (
        <>
          <section className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <StatTile
              icon={<ScanSearch className="size-4" aria-hidden />}
              label="Findings detected"
              value={totals.findings_total}
              hint={`${totals.findings_open} open`}
              tone="sky"
            />
            <StatTile
              icon={<Wrench className="size-4" aria-hidden />}
              label="Fixes proposed"
              value={totals.fixes_proposed}
              hint="awaiting action"
              tone="violet"
            />
            <StatTile
              icon={<GitPullRequest className="size-4" aria-hidden />}
              label="PRs opened"
              value={totals.prs_opened}
              hint="fixes shipped"
              tone="emerald"
            />
            <StatTile
              icon={<Ban className="size-4" aria-hidden />}
              label="Fixes blocked"
              value={totals.fixes_blocked}
              hint="failed grounding"
              tone="amber"
            />
          </section>

          <section className="flex flex-col gap-3">
            <h2 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
              Repositories
            </h2>
            <div className="divide-y divide-border/40 overflow-hidden rounded-lg border border-border/40 bg-card/40">
              {repos.map((repo) => (
                <Link
                  key={repo.id}
                  href={`/repos/${repo.id}`}
                  className="flex items-center justify-between gap-4 px-4 py-3 transition-colors hover:bg-muted/40"
                >
                  <div className="min-w-0">
                    <p className="truncate font-mono text-sm">{repo.full_name}</p>
                    <p className="text-muted-foreground text-xs">
                      {repo.findings_open} open · {repo.findings_actioned} actioned ·{" "}
                      {repo.findings_dismissed} dismissed
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    {repo.prs_opened > 0 ? (
                      <span className="text-emerald-400 inline-flex items-center gap-1 text-xs">
                        <GitPullRequest className="size-3.5" aria-hidden />
                        {repo.prs_opened} PR{repo.prs_opened === 1 ? "" : "s"}
                      </span>
                    ) : null}
                    <ArrowRight
                      className="text-muted-foreground size-4"
                      aria-hidden
                    />
                  </div>
                </Link>
              ))}
            </div>
          </section>

          <section className="flex flex-col gap-3">
            <h2 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
              Recent activity
            </h2>
            {recent_activity.length === 0 ? (
              <p className="text-muted-foreground text-sm">
                No fixes yet. When Axon proposes or opens a fix, it appears here.
              </p>
            ) : (
              <ul className="flex flex-col gap-2">
                {recent_activity.map((item, i) => (
                  <ActivityRow key={`${item.finding_id}-${i}`} item={item} />
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </PageContainer>
  );
}

const TONES: Record<string, string> = {
  sky: "text-sky-400",
  violet: "text-violet-400",
  emerald: "text-emerald-400",
  amber: "text-amber-400",
};

function StatTile({
  icon,
  label,
  value,
  hint,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  hint: string;
  tone: keyof typeof TONES | string;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border/40 bg-card/40 p-4">
      <div className={`flex items-center gap-2 ${TONES[tone] ?? ""}`}>
        {icon}
        <span className="text-muted-foreground text-xs font-medium">{label}</span>
      </div>
      <p className="text-3xl font-semibold tabular-nums">{value}</p>
      <p className="text-muted-foreground text-xs">{hint}</p>
    </div>
  );
}

const ACTIVITY_META: Record<
  string,
  { icon: typeof GitPullRequest; className: string; verb: string }
> = {
  pr_opened: {
    icon: GitPullRequest,
    className: "text-emerald-400",
    verb: "Opened a fix PR",
  },
  blocked: { icon: Ban, className: "text-amber-400", verb: "Fix blocked" },
  proposed: { icon: Wrench, className: "text-violet-400", verb: "Fix proposed" },
};

function ActivityRow({ item }: { item: DashboardActivity }) {
  const meta = ACTIVITY_META[item.kind] ?? ACTIVITY_META.proposed;
  const Icon = meta.icon;
  return (
    <li className="flex items-start gap-3 rounded-lg border border-border/40 bg-card/40 px-4 py-3">
      <Icon className={`mt-0.5 size-4 shrink-0 ${meta.className}`} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="text-sm">
          <span className={meta.className}>{meta.verb}</span>
          <span className="text-muted-foreground"> · </span>
          <span className="text-foreground">{item.title}</span>
        </p>
        <p className="text-muted-foreground truncate text-xs">
          <span className="font-mono">{item.repo_full_name}</span>
          {item.reason ? ` — ${item.reason}` : ""}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-3">
        {item.pr_url ? (
          <a
            href={item.pr_url}
            target="_blank"
            rel="noreferrer"
            className="text-emerald-400 hover:text-emerald-300 text-xs"
          >
            View PR
          </a>
        ) : null}
        <RelativeTime iso={item.at} />
      </div>
    </li>
  );
}
