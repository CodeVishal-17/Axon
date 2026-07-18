import { cn } from "@/lib/utils";

/**
 * Standard content width + gutters. Every page body renders inside one of
 * these so horizontal rhythm stays identical across routes.
 */
export function PageContainer({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("mx-auto w-full max-w-6xl px-4 py-6 sm:px-6", className)}>
      {children}
    </div>
  );
}
