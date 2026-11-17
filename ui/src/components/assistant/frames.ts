/**
 * The frames a conversation is drawn in, and what a reader chose about them.
 *
 * Beside the modal Drawer and the assistant's own page, the conversation has two frames that
 * leave the page usable: `dock`, a column at the page's right edge that the page narrows for, and
 * `mini`, a small window that floats over its bottom right corner. `bar` is any frame minimized:
 * a small bar in that corner, which opens the frame it came from. The reader's choices — the
 * window's size, the column's width, the text's size — are a convenience kept in the browser, so
 * every value read back is checked on its own and brought into range: a hand edit or an older
 * format costs one choice, never the frame.
 */

import { createContext, useContext } from "react";
import { usePersistentState } from "../../storage";

export type Frame = "closed" | "drawer" | "dock" | "mini" | "bar";
export type TextStep = "small" | "default" | "large";
export type Size = { width: number; height: number };

export const TEXT_STEPS: readonly TextStep[] = ["small", "default", "large"];

/** The type scale's three body sizes: 12, 14 and 16 px. */
export const TEXT_STEP_CLASS: Record<TextStep, string> = {
  small: "text-body-sm",
  default: "text-body",
  large: "text-body-lg",
};

export const MINI_PRESETS = {
  small: { width: 320, height: 384 },
  medium: { width: 384, height: 480 },
  large: { width: 448, height: 576 },
} as const satisfies Record<string, Size>;

export type MiniPreset = keyof typeof MINI_PRESETS;

export const MINI_MIN: Size = { width: 280, height: 240 };
/** The window's gap from the edges of the browser window, on every side. */
export const MINI_MARGIN = 16;

export const DOCK_MIN = 320;
export const DOCK_MAX = 640;

const between = (value: number, low: number, high: number) =>
  Math.round(Math.max(low, Math.min(high, value)));

/** `size`, no smaller than the smallest and no larger than `within` less its margins. */
export function clampMini(size: Size, within: Size): Size {
  return {
    width: between(size.width, MINI_MIN.width, within.width - 2 * MINI_MARGIN),
    height: between(size.height, MINI_MIN.height, within.height - 2 * MINI_MARGIN),
  };
}

export function clampDock(width: number): number {
  return between(width, DOCK_MIN, DOCK_MAX);
}

export function presetOf(size: Size): MiniPreset | null {
  for (const name of Object.keys(MINI_PRESETS) as MiniPreset[]) {
    const preset = MINI_PRESETS[name];
    if (preset.width === size.width && preset.height === size.height) return name;
  }
  return null;
}

export type FramePrefs = { mini: Size; dock: number; text: TextStep };

export const DEFAULT_PREFS: FramePrefs = {
  mini: MINI_PRESETS.medium,
  dock: 400,
  text: "small",
};

export const FRAMES_KEY = "sf-ui:assistant-frames";

const finite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

export function parsePrefs(stored: string | null): FramePrefs {
  let read: unknown = null;
  try {
    read = stored === null ? null : JSON.parse(stored);
  } catch {
    read = null;
  }
  if (typeof read !== "object" || read === null) return DEFAULT_PREFS;
  const { mini, dock, text } = read as Record<string, unknown>;
  const size = mini as Partial<Size> | null | undefined;
  return {
    // Its upper limit is the window's, which is known only where the window is drawn.
    mini:
      size && finite(size.width) && finite(size.height)
        ? {
            width: Math.round(Math.max(MINI_MIN.width, size.width)),
            height: Math.round(Math.max(MINI_MIN.height, size.height)),
          }
        : DEFAULT_PREFS.mini,
    dock: finite(dock) ? clampDock(dock) : DEFAULT_PREFS.dock,
    text: TEXT_STEPS.includes(text as TextStep) ? (text as TextStep) : DEFAULT_PREFS.text,
  };
}

const serializePrefs = (prefs: FramePrefs) => JSON.stringify(prefs);

export function useFramePrefs(): [FramePrefs, (change: Partial<FramePrefs>) => void] {
  const [prefs, setPrefs] = usePersistentState(FRAMES_KEY, parsePrefs, serializePrefs);
  return [prefs, (change) => setPrefs((held) => ({ ...held, ...change }))];
}

/** The size of the conversation's text in the frame around it; the Drawer and the page keep the
 *  dashboard's own. */
export const TextStepContext = createContext<TextStep>("default");

export function useBodyText(): string {
  return TEXT_STEP_CLASS[useContext(TextStepContext)];
}
