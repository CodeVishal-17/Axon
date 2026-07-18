"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

/**
 * Route-backed navigation tabs for a repo. These are links (each tab is a
 * real URL that survives refresh/sharing), not stateful shadcn <Tabs>, which
 * are for in-page panel switching.
 */
export function RepoTabs({ repoId }: { repoId: string }) {
  const pathname = usePathname();
  const base = `/repos/${repoId}`;

  const tabs = [
    { label: "Truth Feed", href: base, exact: true },
    { label: "Map", href: `${base}/map`, exact: false },
    { label: "Ask Axon", href: `${base}/ask`, exact: false },
  ];

  return (
    <nav
      aria-label="Repository sections"
      className="border-border/60 -mb-px flex gap-1 overflow-x-auto border-b"
    >
      {tabs.map((tab) => {
        const active = tab.exact
          ? pathname === tab.href
          : pathname.startsWith(tab.href);
        return (
          <Link
            key={tab.href}
            href={tab.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "whitespace-nowrap border-b-2 px-4 py-2.5 text-sm font-medium transition-colors",
              active
                ? "border-emerald-400 text-foreground"
                : "text-muted-foreground hover:text-foreground border-transparent",
            )}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
