import { cn } from "@/lib/utils";

/**
 * Reusable empty/placeholder state. The three repo tabs render one of these
 * until their real content lands (Feed: T2.5, Map: T4.5, Ask: T4.4) — an
 * intentional, styled empty state reads as product, not as missing work.
 */
export function EmptyState({
  icon,
  title,
  description,
  children,
  className,
}: {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "border-border/60 bg-card/40 flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed px-6 py-16 text-center",
        className,
      )}
    >
      {icon ? <div className="text-muted-foreground">{icon}</div> : null}
      <h2 className="text-lg font-medium">{title}</h2>
      {description ? (
        <p className="text-muted-foreground max-w-md text-sm">{description}</p>
      ) : null}
      {children}
    </div>
  );
}
