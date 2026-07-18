import Link from "next/link";
import { ApiStatus } from "@/components/layout/api-status";

/**
 * Global top bar, present on every page.
 * Server component — no interactivity beyond links.
 */
export function SiteHeader() {
  return (
    <header className="border-border/60 bg-background/80 sticky top-0 z-40 border-b backdrop-blur">
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center justify-between px-4 sm:px-6">
        <Link href="/" className="flex items-center gap-2">
          {/* Wordmark: a pulse dot + name. Cheap, distinctive, no asset needed. */}
          <span
            aria-hidden
            className="bg-emerald-400 inline-block size-2 rounded-full shadow-[0_0_8px_2px_rgba(52,211,153,0.5)]"
          />
          <span className="text-base font-semibold tracking-tight">Axon</span>
          <span className="text-muted-foreground hidden text-xs sm:inline">
            truth maintenance for engineering orgs
          </span>
        </Link>
        <nav className="text-muted-foreground flex items-center gap-4 text-sm">
          <ApiStatus />
          <a
            href="https://github.com/CodeVishal-17/Axon"
            target="_blank"
            rel="noreferrer"
            className="hover:text-foreground transition-colors"
          >
            GitHub
          </a>
        </nav>
      </div>
    </header>
  );
}
