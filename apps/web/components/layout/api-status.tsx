"use client";

import { useEffect, useState } from "react";
import { getHealth } from "@/lib/api";
import { cn } from "@/lib/utils";

type Status = "checking" | "ok" | "degraded" | "down";

/**
 * Tiny backend-connectivity dot in the header. Exists for two reasons:
 * it's the demo-day "is everything wired?" glance, and it makes the
 * generated HealthResponse type load-bearing — a backend field rename
 * breaks this component's build until `make types` is re-run.
 */
export function ApiStatus() {
  const [status, setStatus] = useState<Status>("checking");

  useEffect(() => {
    let cancelled = false;
    getHealth()
      .then((health) => {
        if (cancelled) return;
        // `health.database` is typed by the generated schema.
        setStatus(health.database === "ok" ? "ok" : "degraded");
      })
      .catch(() => {
        if (!cancelled) setStatus("down");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const label: Record<Status, string> = {
    checking: "Checking API…",
    ok: "API connected",
    degraded: "API up, database unavailable",
    down: "API unreachable",
  };

  return (
    <span className="flex items-center gap-1.5" title={label[status]}>
      <span
        aria-hidden
        className={cn(
          "inline-block size-1.5 rounded-full",
          status === "ok" && "bg-emerald-400",
          status === "degraded" && "bg-amber-400",
          status === "down" && "bg-red-400",
          status === "checking" && "bg-muted-foreground/50 animate-pulse",
        )}
      />
      <span className="sr-only">{label[status]}</span>
      <span className="text-muted-foreground hidden text-xs lg:inline">API</span>
    </span>
  );
}
