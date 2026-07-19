import { cn } from "@/lib/utils";

/**
 * Evidence code block: path header, optional line numbers, lightweight
 * syntax highlighting, and a diff mode. Highlighting is a deliberate
 * ~40-line tokenizer (comments/strings/keywords/numbers) rendered as React
 * nodes — no dependency, no dangerouslySetInnerHTML, good enough for
 * evidence excerpts. Long blocks scroll INSIDE the card (max-h + overflow)
 * so the page never scrolls horizontally.
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
        <span key={nodes.length} className="italic text-zinc-500">
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
      nodes.push(
        <span key={nodes.length} className="text-sky-300">{text}</span>,
      );
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

export function CodeBlock({
  code,
  path,
  startLine,
  variant = "code",
}: {
  code: string;
  path?: string | null;
  startLine?: number | null;
  variant?: "code" | "diff";
}) {
  const lines = code.split("\n");
  const showNumbers = variant === "code" && startLine != null;

  return (
    <div className="border-border/60 overflow-hidden rounded-md border bg-black/40">
      {path ? (
        <div className="border-border/60 text-muted-foreground flex items-center justify-between border-b px-3 py-1.5 font-mono text-[11px]">
          <span className="truncate">{path}</span>
          {variant === "diff" ? <span className="shrink-0 pl-2">diff</span> : null}
        </div>
      ) : null}
      <pre className="max-h-56 overflow-auto p-3 font-mono text-xs leading-relaxed">
        <code>
          {lines.map((line, i) =>
            variant === "diff" ? (
              <div key={i} className={cn("-mx-3 px-3", diffLineClass(line))}>
                {line || " "}
              </div>
            ) : (
              <div key={i} className="flex">
                {showNumbers ? (
                  <span className="w-10 shrink-0 select-none pr-3 text-right text-zinc-600">
                    {(startLine ?? 1) + i}
                  </span>
                ) : null}
                <span className="whitespace-pre">{highlightLine(line, i) }</span>
              </div>
            ),
          )}
        </code>
      </pre>
    </div>
  );
}
