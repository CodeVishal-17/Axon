import { memo } from "react";
import { cn } from "@/lib/utils";

/**
 * Evidence renderer: path header, line numbers, lightweight syntax
 * highlighting, and a diff mode. Highlighting is a deliberate ~40-line
 * tokenizer rendered as React nodes — no dependency, no
 * dangerouslySetInnerHTML. Long blocks scroll INSIDE their container so the
 * page never scrolls horizontally.
 *
 * Memoized: evidence never changes between polls, so cards re-render
 * without re-tokenizing.
 */

const KEYWORDS = new Set([
  "const", "let", "var", "function", "return", "export", "import", "from",
  "if", "else", "for", "while", "class", "def", "async", "await", "new",
  "try", "except", "catch", "raise", "throw", "in", "of", "None", "null",
  "true", "false", "True", "False", "self", "this", "type", "interface",
]);

const TOKEN_RE =
  /(\/\/[^\n]*|#[^\n]*)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)|(\b\d+(?:\.\d+)?\b)|(\b[A-Za-z_][A-Za-z0-9_]*\b)/g;

function highlightLine(line: string, key: number) {
  const nodes: React.ReactNode[] = [];
  let last = 0;
  for (const match of line.matchAll(TOKEN_RE)) {
    const index = match.index ?? 0;
    if (index > last) nodes.push(line.slice(last, index));
    const [text, comment, string, number, word] = match;
    if (comment) {
      nodes.push(
        <span key={nodes.length} className="text-zinc-500 italic">
          {line.slice(index)}
        </span>,
      );
      last = line.length;
      break; // comments run to end of line
    } else if (string) {
      nodes.push(
        <span key={nodes.length} className="text-emerald-300">{text}</span>,
      );
    } else if (number) {
      nodes.push(
        <span key={nodes.length} className="text-amber-200">{text}</span>,
      );
    } else if (word && KEYWORDS.has(word)) {
      nodes.push(<span key={nodes.length} className="text-sky-300">{text}</span>);
    } else {
      nodes.push(text);
    }
    last = index + text.length;
  }
  if (last < line.length) nodes.push(line.slice(last));
  return <span key={key}>{nodes}</span>;
}

function diffLineClass(line: string): string {
  if (line.startsWith("+")) return "text-emerald-400 bg-emerald-500/10";
  if (line.startsWith("-")) return "text-red-400 bg-red-500/10";
  if (line.startsWith("@@")) return "text-sky-400";
  return "text-zinc-400";
}

export type CodeBlockProps = {
  code: string;
  path?: string | null;
  startLine?: number | null;
  variant?: "code" | "diff";
  /** Truncate to N lines with a "+N more" hint — used for card previews. */
  maxLines?: number;
  className?: string;
};

export const CodeBlock = memo(function CodeBlock({
  code,
  path,
  startLine,
  variant = "code",
  maxLines,
  className,
}: CodeBlockProps) {
  const allLines = code.split("\n");
  const truncated = maxLines != null && allLines.length > maxLines;
  const lines = truncated ? allLines.slice(0, maxLines) : allLines;
  const showNumbers = variant === "code" && startLine != null;

  return (
    <div
      className={cn(
        "border-border/60 overflow-hidden rounded-md border bg-black/40",
        className,
      )}
    >
      {path ? (
        <div className="border-border/60 text-muted-foreground flex items-center justify-between border-b px-3 py-1.5 font-mono text-[11px]">
          <span className="truncate">{path}</span>
          {variant === "diff" ? <span className="shrink-0 pl-2">diff</span> : null}
        </div>
      ) : null}
      <pre
        className={cn(
          "overflow-auto p-3 font-mono text-xs leading-relaxed",
          maxLines == null && "max-h-56",
        )}
      >
        <code>
          {lines.map((line, i) =>
            variant === "diff" ? (
              <div key={i} className={cn("-mx-3 px-3", diffLineClass(line))}>
                {line || " "}
              </div>
            ) : (
              <div key={i} className="flex">
                {showNumbers ? (
                  <span className="w-10 shrink-0 pr-3 text-right text-zinc-600 select-none">
                    {(startLine ?? 1) + i}
                  </span>
                ) : null}
                <span className="whitespace-pre">{highlightLine(line, i)}</span>
              </div>
            ),
          )}
          {truncated ? (
            <div className="text-muted-foreground pt-1 text-[11px]">
              +{allLines.length - lines.length} more lines
            </div>
          ) : null}
        </code>
      </pre>
    </div>
  );
});
