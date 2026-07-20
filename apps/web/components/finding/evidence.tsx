import type { EvidenceOut } from "@/lib/api";
import { CodeBlock } from "@/components/common/code-block";

/**
 * The proof. Quotes are verbatim spans the verifier matched against real
 * source (the backend rejects any contradiction whose quote it can't find),
 * so this is evidence, not narration.
 */
export function Evidence({
  evidence,
  maxLines,
}: {
  evidence: EvidenceOut;
  maxLines?: number;
}) {
  const quotes = evidence.quotes ?? [];
  if (quotes.length === 0 && !evidence.diff) return null;

  return (
    <div className="flex flex-col gap-2">
      {quotes.map((quote, i) => (
        <CodeBlock
          key={i}
          code={quote.text}
          path={quote.path}
          startLine={quote.start_line}
          maxLines={maxLines}
        />
      ))}
      {evidence.diff ? (
        <CodeBlock
          code={evidence.diff}
          path="change"
          variant="diff"
          maxLines={maxLines}
        />
      ) : null}
    </div>
  );
}
