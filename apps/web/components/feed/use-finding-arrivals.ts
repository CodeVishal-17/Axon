"use client";

import { useEffect, useRef, useState } from "react";
import type { FindingOut } from "@/lib/api";

const ARRIVAL_MS = 700;

/**
 * Tracks additions after the initial feed load. The API owns ordering and
 * truth; this hook only acknowledges a genuine new finding or promotion.
 */
export function useFindingArrivals(
  findings: FindingOut[],
  { enabled, scopeKey }: { enabled: boolean; scopeKey: string },
) {
  const previousIds = useRef<Set<string> | null>(null);
  const previousFeaturedId = useRef<string | null>(null);
  const previousScopeKey = useRef<string | null>(null);
  const timeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [arrivalIds, setArrivalIds] = useState<Set<string>>(() => new Set());
  const [featuredChanged, setFeaturedChanged] = useState(false);

  useEffect(() => {
    // A filter change is a new view, not a wave of arrivals.
    if (previousScopeKey.current !== scopeKey) {
      previousScopeKey.current = scopeKey;
      previousIds.current = null;
      previousFeaturedId.current = null;
      setArrivalIds(new Set());
      setFeaturedChanged(false);
    }
    if (!enabled) return;

    const ids = new Set(findings.map((finding) => finding.id));
    const featuredId = findings[0]?.id ?? null;

    if (previousIds.current === null) {
      previousIds.current = ids;
      previousFeaturedId.current = featuredId;
      return;
    }

    const added = [...ids].filter((id) => !previousIds.current?.has(id));
    const promoted =
      featuredId !== null &&
      previousFeaturedId.current !== null &&
      featuredId !== previousFeaturedId.current;

    previousIds.current = ids;
    previousFeaturedId.current = featuredId;
    if (added.length === 0 && !promoted) return;

    setArrivalIds(new Set(added));
    setFeaturedChanged(promoted);
    if (timeout.current) clearTimeout(timeout.current);
    timeout.current = setTimeout(() => {
      setArrivalIds(new Set());
      setFeaturedChanged(false);
      timeout.current = null;
    }, ARRIVAL_MS);

    return () => {
      if (timeout.current) {
        clearTimeout(timeout.current);
        timeout.current = null;
      }
    };
  }, [enabled, findings, scopeKey]);

  return { arrivalIds, featuredChanged };
}
