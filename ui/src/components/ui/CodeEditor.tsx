/**
 * A plain-text code editor: a textarea beside a gutter of line numbers, filling the box it is
 * given. No highlighting and no library — the numbers are the one thing a textarea cannot draw.
 *
 * Lines never wrap, so line N of the text is always row N of the gutter; the gutter follows the
 * textarea's vertical scroll, and its spare bottom padding covers the horizontal scrollbar.
 */

import { useRef } from "react";
import { cx } from "./cx";

export function CodeEditor({
  label,
  value,
  onChange,
  readOnly = false,
  placeholder,
  file,
  className,
}: {
  /** The textarea's accessible name. */
  label: string;
  /** The file the text is, when it is one: a script that reads a line can then say where it is. */
  file?: string;
  value: string;
  onChange?: (value: string) => void;
  readOnly?: boolean;
  placeholder?: string;
  /** Layout only. */
  className?: string;
}) {
  const gutter = useRef<HTMLDivElement>(null);
  const lines = value.split("\n").length;
  return (
    <div
      className={cx(
        "flex min-h-0 bg-surface font-mono text-body-sm leading-relaxed text-default",
        className,
      )}
    >
      <div
        ref={gutter}
        aria-hidden="true"
        className="shrink-0 overflow-hidden border-r border-default bg-surface-sunken py-2 pr-3 pb-8 pl-4 text-right text-subtlest select-none"
      >
        {Array.from({ length: lines }, (_, index) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: a line number is its own identity
          <div key={index}>{index + 1}</div>
        ))}
      </div>
      <textarea
        data-code-editor
        data-code-file={file}
        aria-label={label}
        value={value}
        onChange={(event) => onChange?.(event.target.value)}
        onScroll={(event) => {
          if (gutter.current) gutter.current.scrollTop = event.currentTarget.scrollTop;
        }}
        readOnly={readOnly}
        placeholder={placeholder}
        spellCheck={false}
        wrap="off"
        className="focus-inset min-w-0 flex-1 resize-none overflow-auto bg-transparent px-3 py-2 whitespace-pre outline-none placeholder:text-subtlest"
      />
    </div>
  );
}
