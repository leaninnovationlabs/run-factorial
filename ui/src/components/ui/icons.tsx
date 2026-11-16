/**
 * The dashboard's one icon set: a 16×16 grid, 1.5 strokes, square caps, drawn in `currentColor`
 * so every glyph takes the ink of the text beside it in either theme.
 *
 * Drawn here rather than taken from `@atlaskit/icon`, which would be a dependency of its own
 * (decision 50). It replaces the rail's, the theme toggle's and the explorer's separate sets and
 * the Unicode arrows and crosses that followed whatever font was active.
 *
 * An icon beside a word is decoration and hidden from assistive technology; an icon that stands
 * alone passes `label`, and is announced as an image with that name.
 */

import type { ReactNode } from "react";
import { cx } from "./cx";

const RING = <circle cx="8" cy="8" r="6.25" />;
const DOT = (y: number) => <path d={`M8 ${y}v.01`} strokeWidth="2" />;
const FILE = (
  <>
    <path d="M3.5 1.75h5.25l3.75 3.75v8.75H3.5Z" />
    <path d="M8.75 1.75v3.75h3.75" />
  </>
);

const GLYPHS = {
  "status-information": (
    <>
      {RING}
      <path d="M8 7.25v4" />
      {DOT(4.9)}
    </>
  ),
  "status-success": (
    <>
      {RING}
      <path d="m5.25 8.25 2 2 3.5-4" />
    </>
  ),
  "status-warning": (
    <>
      <path d="M8 1.9 14.3 13.25H1.7Z" />
      <path d="M8 6.25v3.5" />
      {DOT(11.4)}
    </>
  ),
  "status-error": (
    <>
      <path d="M5.3 1.75h5.4l3.55 3.55v5.4l-3.55 3.55H5.3L1.75 10.7V5.3Z" />
      <path d="M8 4.75v4" />
      {DOT(11.1)}
    </>
  ),
  "exclamation-circle": (
    <>
      {RING}
      <path d="M8 4.75v4" />
      {DOT(11.1)}
    </>
  ),
  "status-discovery": (
    <>
      {RING}
      <path d="M6.25 6.25a1.75 1.75 0 1 1 2.6 1.55c-.55.3-.85.7-.85 1.2v.25" />
      {DOT(11.1)}
    </>
  ),
  check: <path d="m3.25 8.5 3 3 6.5-7" />,
  close: <path d="m4 4 8 8M12 4l-8 8" />,
  plus: <path d="M8 3v10M3 8h10" />,
  "chevron-down": <path d="m4 6 4 4 4-4" />,
  "chevron-right": <path d="m6 4 4 4-4 4" />,
  "chevron-left": <path d="m10 4-4 4 4 4" />,
  "arrow-down": <path d="M8 3v10m-4-4 4 4 4-4" />,
  "arrow-up": <path d="M8 13V3M4 7l4-4 4 4" />,
  "arrow-left": <path d="M13 8H3m4-4L3 8l4 4" />,
  "sort-up": <path d="M8 12.5V3.5M4.75 6.75 8 3.5l3.25 3.25" />,
  "sort-down": <path d="M8 3.5v9m-3.25-3.25L8 12.5l3.25-3.25" />,
  "link-external": (
    <>
      <path d="M12.5 9.25v4.25h-10v-10h4.25" />
      <path d="M9.5 2.5h4v4m0-4-6 6" />
    </>
  ),
  more: (
    <>
      {DOT(3.5)}
      {DOT(8)}
      {DOT(12.5)}
    </>
  ),
  maximize: <path d="M9.5 2.5h4v4m0-4L9 7M6.5 13.5h-4v-4m0 4L7 9" />,
  eye: (
    <>
      <path d="M1.5 8S4 3.25 8 3.25 14.5 8 14.5 8 12 12.75 8 12.75 1.5 8 1.5 8Z" />
      <path d="M10 8a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z" />
    </>
  ),
  "eye-off": (
    <>
      <path d="M1.5 8S4 3.25 8 3.25 14.5 8 14.5 8 12 12.75 8 12.75 1.5 8 1.5 8Z" />
      <path d="M10 8a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z" />
      <path d="m2.5 2.5 11 11" />
    </>
  ),
  search: (
    <>
      <circle cx="7" cy="7" r="4.5" />
      <path d="m10.5 10.5 3 3" />
    </>
  ),
  copy: (
    <>
      <path d="M5.5 5.5h8v8h-8Z" />
      <path d="M10.5 5.5v-3h-8v8h3" />
    </>
  ),
  refresh: (
    <>
      <path d="M13.25 8a5.25 5.25 0 1 1-1.54-3.71" />
      <path d="M13.25 2.25v2.5h-2.5" />
    </>
  ),
  undo: <path d="M5.5 3.5 2.5 6.5l3 3m-3-3h7a3.5 3.5 0 0 1 0 7H7" />,
  edit: <path d="m10.75 2.75 2.5 2.5-7.5 7.5h-2.5v-2.5Z" />,
  trash: <path d="M2.75 4.5h10.5M6.25 4.5V2.75h3.5V4.5m-5 0 .5 9h5.5l.5-9" />,
  menu: <path d="M2.5 4h11M2.5 8h11M2.5 12h11" />,
  settings: (
    <>
      <path d="M2.5 4.5h6.25m3 0h1.75M2.5 11.5h1.75m3 0h6.25" />
      <path d="M8.75 3h3v3h-3ZM4.25 10h3v3h-3Z" />
    </>
  ),
  // Three tracks with their knobs at different places: configuration as settings you adjust,
  // distinct from `settings`' two-track mark.
  sliders: (
    <>
      <path d="M2 3.5h6.25m3.5 0H14M2 8h1.25m3.5 0H14M2 12.5h7.25m3.5 0H14" />
      <circle cx="10" cy="3.5" r="1.5" />
      <circle cx="5" cy="8" r="1.5" />
      <circle cx="11" cy="12.5" r="1.5" />
    </>
  ),
  help: (
    <>
      {RING}
      <path d="M6.25 6.25a1.75 1.75 0 1 1 2.6 1.55c-.55.3-.85.7-.85 1.2v.25" />
      {DOT(11.1)}
    </>
  ),
  user: (
    <>
      <circle cx="8" cy="5.5" r="2.75" />
      <path d="M2.75 14a5.25 5.25 0 0 1 10.5 0" />
    </>
  ),
  users: (
    <>
      <circle cx="6" cy="5.5" r="2.5" />
      <path d="M1.5 14a4.5 4.5 0 0 1 9 0" />
      <path d="M10 3.1a2.5 2.5 0 0 1 0 4.8M11.75 9.75A4.5 4.5 0 0 1 14.5 14" />
    </>
  ),
  key: (
    <>
      <circle cx="5.25" cy="10.75" r="2.75" />
      <path d="m7.25 8.75 6-6m-1.75 1.75 1.5 1.5M9.75 6.25l1.5 1.5" />
    </>
  ),
  overview: <path d="M2.25 3h11.5v10H2.25ZM6 3v10m4-10v10" />,
  projects: <path d="M1.75 12.75v-9.5h4.25l1.5 1.75h6.75v7.75Z" />,
  sun: (
    <>
      <circle cx="8" cy="8" r="2.75" />
      <path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1.05 1.05M11.55 11.55l1.05 1.05M3.4 12.6l1.05-1.05M11.55 4.45l1.05-1.05" />
    </>
  ),
  moon: <path d="M13.25 9.75A5.5 5.5 0 1 1 6.25 2.75a4.5 4.5 0 0 0 7 7Z" strokeLinejoin="round" />,
  monitor: <path d="M1.75 2.75h12.5v8.5H1.75ZM5.5 14.25h5M8 11.25v3" />,
  organization: (
    <>
      <path d="M3.25 14V2.25h6.5V14M9.75 6.25h3V14M1.75 14h12.5" />
      <path d="M5.5 5h2M5.5 8h2M5.5 11h2" />
    </>
  ),
  dashboard: (
    <path d="M2.25 2.25h4.5v6.5h-4.5Zm7 0h4.5v3.5h-4.5Zm0 6h4.5v5.5h-4.5Zm-7 3h4.5v2.5h-4.5Z" />
  ),
  tickets: (
    <>
      <path d="M2.25 4.25h11.5v7.5H2.25Z" />
      <path d="M5.5 4.25v7.5M8 7h3.25M8 9h2" />
    </>
  ),
  runtimes: (
    <>
      <path d="M4.5 4.5h7v7h-7Z" />
      <path d="M6.5 2v2.5m3-2.5v2.5m-3 7V14m3-2.5V14M2 6.5h2.5M2 9.5h2.5m7-3H14m-2.5 3H14" />
    </>
  ),
  // An agent: a head with an antenna, square-capped like the rest.
  agent: (
    <>
      <path d="M3 5.75h10v7.5H3Z" />
      <path d="M8 5.75V3.5" />
      {DOT(2.4)}
      <path d="M6 9.25v.01M10 9.25v.01" strokeWidth="2" />
      <path d="M1.25 8.5v2m13.5-2v2" />
    </>
  ),
  // A git branch: a line of commits, and one that split from it.
  branch: (
    <>
      <circle cx="4.5" cy="3.25" r="1.5" />
      <circle cx="4.5" cy="12.75" r="1.5" />
      <circle cx="11.5" cy="5" r="1.5" />
      <path d="M4.5 4.75v6.5" />
      <path d="M11.5 6.5v.75c0 2.25-2 3-4.5 3.25-1.5.15-2.5.75-2.5 1" />
    </>
  ),
  // A process lane: a workflow, one step branching to the next ones.
  lane: (
    <>
      <path d="M5.75 1.75h4.5v3.5h-4.5Z" />
      <path d="M1.75 10.75h4.5v3.5h-4.5ZM9.75 10.75h4.5v3.5h-4.5Z" />
      <path d="M8 5.25V8M4 10.75V8h8v2.75" />
    </>
  ),
  // An MCP server: a plug, the thing an agent connects a tool through.
  plug: (
    <>
      <path d="M6 1.75v3m4-3v3" />
      <path d="M3.75 4.75h8.5v2.5a4.25 4.25 0 0 1-8.5 0Z" />
      <path d="M8 11.5v2.75" />
    </>
  ),
  folder: <path d="M1.75 12.75v-9.5h4.25l1.5 1.75h6.75v7.75Z" />,
  "folder-open": (
    <>
      <path d="M1.75 12.75v-9.5h4.25l1.5 1.75h5.25v1.75" />
      <path d="M1.75 12.75 3.5 7.25h10.75l-1.75 5.5Z" />
    </>
  ),
  "file-yaml": (
    <>
      {FILE}
      <path d="M5.75 9v2.75M7 9l-1.25 1.5M9.25 9 11 10.5l-1.75 1.5" />
    </>
  ),
  "file-markdown": (
    <>
      {FILE}
      <path d="M5 12V9l1.5 1.75L8 9v3m2-3v3m-.75-.75.75.75.75-.75" />
    </>
  ),
  lock: (
    <>
      <path d="M3.25 7.25h9.5v6.5h-9.5Z" />
      <path d="M5.25 7.25v-2a2.75 2.75 0 0 1 5.5 0v2" />
    </>
  ),
  send: <path d="M14 2 7.25 8.75M14 2 9.5 14 7.25 8.75 2 6.5Z" strokeLinejoin="round" />,
  "stop-circle": (
    <>
      {RING}
      <path d="M6 6h4v4H6Z" fill="currentColor" strokeWidth="1" />
    </>
  ),
  sparkles: (
    <path
      d="M6.5 3.5 7.75 7.25 11.5 8.5 7.75 9.75 6.5 13.5 5.25 9.75 1.5 8.5 5.25 7.25ZM12.25 1.75l.55 1.45 1.45.55-1.45.55-.55 1.45-.55-1.45-1.45-.55 1.45-.55Z"
      strokeLinejoin="round"
    />
  ),
  history: (
    <>
      <path d="M2.75 8a5.25 5.25 0 1 0 1.55-3.7" />
      <path d="M2.75 2.5v2.75H5.5M8 5.25V8l2 1.5" />
    </>
  ),
  chart: <path d="M2 13.75h12M4.5 11.25V8.5M8 11.25V3.75m3.5 7.5V6.25" />,
} satisfies Record<string, ReactNode>;

export type Glyph = keyof typeof GLYPHS;

export const GLYPH_NAMES = Object.keys(GLYPHS) as Glyph[];

const SIZES = { small: "size-3", medium: "size-4" } as const;

export function Icon({
  glyph,
  label,
  size = "medium",
  className,
}: {
  glyph: Glyph;
  /** Name it only when nothing beside it says the same; an icon next to a word is hidden. */
  label?: string;
  size?: keyof typeof SIZES;
  className?: string;
}) {
  return (
    // biome-ignore lint/a11y/noSvgWithoutTitle: named by aria-label, or hidden, below
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="square"
      data-glyph={glyph}
      className={cx("shrink-0", SIZES[size], className)}
      {...(label ? { role: "img", "aria-label": label } : { "aria-hidden": true })}
    >
      {GLYPHS[glyph]}
    </svg>
  );
}
