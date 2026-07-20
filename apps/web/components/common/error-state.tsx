import { TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";

/** Honest failure surface: says what broke and offers the one useful action. */
export function ErrorState({
  title,
  description,
  onRetry,
}: {
  title: string;
  description: string;
  onRetry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="border-border/60 bg-card/40 flex flex-col items-center gap-3 rounded-lg border border-dashed px-6 py-12 text-center"
    >
      <TriangleAlert className="size-5 text-amber-400" aria-hidden />
      <h2 className="text-sm font-medium">{title}</h2>
      <p className="text-muted-foreground max-w-md text-sm">{description}</p>
      {onRetry ? (
        <Button size="sm" variant="outline" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}
