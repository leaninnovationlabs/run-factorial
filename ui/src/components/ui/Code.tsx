/**
 * Code, CodeBlock, CopyButton and useCopy, after ADS.
 *
 * `Code` is an identifier in running text — a step name, a branch, a slug. `CodeBlock` is text to
 * read or copy whole: a command, a file. Copying goes through `useCopy`, the one place that knows
 * the clipboard can refuse (an insecure origin, an unfocused tab) and says so rather than claiming
 * it worked.
 */

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { Button } from "./Button";
import { cx } from "./cx";
import { showFlag } from "./Flag";

export function Code({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <code
      className={cx(
        "rounded-xs bg-neutral px-1 py-px font-mono text-code break-words text-default",
        className,
      )}
    >
      {children}
    </code>
  );
}

/** Server prose carries markdown-style backticks (MCP clients read the same strings as markdown);
 *  each pair becomes inline code. An unmatched or empty pair stays literal. */
export function ServerText({ text }: { text: string }) {
  // A span can repeat ("`factorial` … `factorial`"), so its key counts occurrences.
  const seen = new Map<string, number>();
  return (
    <>
      {text.split(/(`[^`]+`)/).map((part, i) => {
        if (i % 2 === 0) return part;
        const n = (seen.get(part) ?? 0) + 1;
        seen.set(part, n);
        return <Code key={`${part}#${n}`}>{part.slice(1, -1)}</Code>;
      })}
    </>
  );
}

/** Guarded clipboard write: `writeText` rejects on an insecure origin, such as a dashboard on
 *  plain HTTP off localhost, and when the tab is unfocused. */
export async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    return false;
  }
}

/** Copy, then report: `copied` is true for 1.5 s after a copy that happened, and a copy the browser
 *  refused raises an error flag instead of pretending. */
export function useCopy(): { copied: boolean; copy: (value: string) => Promise<void> } {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = useCallback(async (value: string) => {
    if (await copyText(value)) {
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1500);
    } else {
      showFlag({
        appearance: "error",
        title: "Couldn't copy that",
        description: "The browser refused the clipboard. Select the text and copy it by hand.",
      });
    }
  }, []);
  return { copied, copy };
}

export function CopyButton({
  value,
  label = "Copy",
  copiedLabel = "Copied",
  name,
  spacing = "compact",
}: {
  value: string;
  label?: string;
  copiedLabel?: string;
  /** What is copied, when a page holds more than one: read after the visible word, so each
   *  button has its own name and the visible word still starts it. */
  name?: string;
  spacing?: "default" | "compact";
}) {
  const { copied, copy } = useCopy();
  return (
    <Button
      spacing={spacing}
      iconBefore={copied ? "check" : "copy"}
      aria-label={name && !copied ? `${label} ${name}` : undefined}
      onClick={() => copy(value)}
    >
      <span aria-live="polite">{copied ? copiedLabel : label}</span>
    </Button>
  );
}

/** Text to read or copy whole. `label` names it for a screen reader and heads the copy button. */
export function CodeBlock({
  text,
  label,
  isCopyable = true,
  showLineNumbers = false,
  wrap = false,
  className,
}: {
  text: string;
  label: string;
  isCopyable?: boolean;
  showLineNumbers?: boolean;
  wrap?: boolean;
  className?: string;
}) {
  const lines = showLineNumbers ? text.split("\n") : null;
  return (
    <figure className={cx("relative min-w-0 rounded-md bg-neutral", className)}>
      <figcaption className="sr-only">{label}</figcaption>
      {isCopyable && (
        <div className="absolute top-1 right-1">
          <CopyButton value={text} name={label} />
        </div>
      )}
      <pre
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling region has to be reachable by keyboard
        tabIndex={0}
        className={cx(
          "focus-inset overflow-auto rounded-md px-3 py-2 font-mono text-body-sm text-default",
          wrap ? "whitespace-pre-wrap break-words" : "whitespace-pre",
          isCopyable && "pr-24",
        )}
      >
        {lines
          ? lines.map((line, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: a line IS its position
              <span key={index} className="block">
                <span
                  aria-hidden="true"
                  className="mr-3 inline-block w-8 text-right text-subtlest select-none"
                >
                  {index + 1}
                </span>
                {line}
              </span>
            ))
          : text}
      </pre>
    </figure>
  );
}
