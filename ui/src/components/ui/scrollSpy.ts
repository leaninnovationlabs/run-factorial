/**
 * Which section of a long region the reader is at, reported as they scroll — what lights the
 * `JumpRail` beside it.
 *
 * The section under the reading line is the last one whose top has passed a line `offset` px below
 * the top of what scrolls; before any has, it is the first, so a rail is never left pointing at
 * nothing. What scrolls is the region itself if it scrolls, else its nearest scrolling ancestor —
 * the shell's `<main>` — else the window.
 */

import { type RefObject, useEffect, useRef } from "react";

/** The section under the line, from each section's top in the same coordinates as the line. */
export function sectionAt(
  tops: ReadonlyArray<readonly [key: string, top: number]>,
  line: number,
): string | null {
  let current: string | null = null;
  for (const [key, top] of tops) if (top <= line) current = key;
  return current ?? tops[0]?.[0] ?? null;
}

/** The element that scrolls `element`: itself, or its nearest scrolling ancestor, or null for
 *  the window. */
function scrollerOf(element: HTMLElement): HTMLElement | null {
  for (let node: HTMLElement | null = element; node; node = node.parentElement) {
    const { overflowY } = getComputedStyle(node);
    if (overflowY === "auto" || overflowY === "scroll") return node;
  }
  return null;
}

export function useScrollSpy({
  root,
  keys,
  section,
  offset,
  onChange,
  paused,
}: {
  /** The region, or an element inside what scrolls it. */
  root: RefObject<HTMLElement | null>;
  /** The sections, in the order they sit on the page. */
  keys: readonly string[];
  section: (key: string) => HTMLElement | null | undefined;
  offset: number;
  /** Called when the section under the line changes, and once when the keys do. */
  onChange: (key: string) => void;
  /** True while the caller is scrolling the region itself, so its own jump is not reported. */
  paused?: RefObject<boolean>;
}): void {
  const reported = useRef<string | null>(null);
  // The latest of both, read at scroll time, so a new callback does not re-attach the listener.
  const latest = useRef({ section, onChange });
  latest.current = { section, onChange };
  const signature = keys.join("\n");

  // biome-ignore lint/correctness/useExhaustiveDependencies: `signature` stands for `keys`
  useEffect(() => {
    const region = root.current;
    if (!region) return;
    const scroller = scrollerOf(region);
    const report = () => {
      if (paused?.current) return;
      const line = (scroller?.getBoundingClientRect().top ?? 0) + offset;
      const tops = keys.flatMap((key) => {
        const element = latest.current.section(key);
        return element ? [[key, element.getBoundingClientRect().top] as const] : [];
      });
      const next = sectionAt(tops, line);
      // Scroll fires continuously; only a change of section is worth saying.
      if (next !== null && next !== reported.current) {
        reported.current = next;
        latest.current.onChange(next);
      }
    };
    let frame = 0;
    const onScroll = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        report();
      });
    };
    report();
    const target: HTMLElement | Window = scroller ?? window;
    target.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      cancelAnimationFrame(frame);
      target.removeEventListener("scroll", onScroll);
    };
  }, [root, signature, offset, paused]);
}
