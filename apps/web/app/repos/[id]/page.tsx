"use client";

import { useState } from "react";
import type { FindingOut } from "@/lib/api";
import { emptyFindings, manyFindings, singleFinding } from "@/lib/fixtures/findings";
import { FeedList } from "@/components/feed/feed-list";
import { cn } from "@/lib/utils";

/**
 * Truth Feed — currently fixture-backed (T1.5). The fixture switcher below
 * exists to exercise every feed state (loading / empty / one / many) and is
 * removed in T2.5 when the real findings API takes over.
 */

const FIXTURE_SETS = {
  many: () => manyFindings(50),
  one: () => singleFinding(),
  empty: () => emptyFindings,
  loading: () => null,
} as const;

type FixtureKey = keyof typeof FIXTURE_SETS;

const LABELS: Record<FixtureKey, string> = {
  many: "Many (50)",
  one: "Single",
  empty: "Empty",
  loading: "Loading",
};

export default function TruthFeedPage() {
  const [fixture, setFixture] = useState<FixtureKey>("many");
  const findings: FindingOut[] | null = FIXTURE_SETS[fixture]();

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <div
          role="tablist"
          aria-label="Fixture set"
          className="border-border/60 flex rounded-md border p-0.5"
        >
          {(Object.keys(FIXTURE_SETS) as FixtureKey[]).map((key) => (
            <button
              key={key}
              role="tab"
              aria-selected={fixture === key}
              onClick={() => setFixture(key)}
              className={cn(
                "rounded px-2.5 py-1 text-xs transition-colors",
                fixture === key
                  ? "bg-secondary text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {LABELS[key]}
            </button>
          ))}
        </div>
        <span className="text-muted-foreground text-xs">
          Fixture data — live findings arrive with the drift engine.
        </span>
      </div>
      <FeedList findings={findings} />
    </div>
  );
}
