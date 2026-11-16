/**
 * The file panel's width beside the editor: the person's own choice, kept across visits, and
 * never more than two fifths of the editor, so the file being edited always has the larger share.
 */

import { safeGet, safeSet } from "../../../storage";

export const DEFAULT_WIDTH = 288;
export const MIN_WIDTH = 192;
/** The most of the editor the file panel may take. */
export const MAX_SHARE = 0.4;

const KEY = "sf.explorerWidth";

/** `width` held between the minimum and two fifths of `container`. An editor too narrow for the
 *  minimum gives the panel its two fifths; one not measured yet (0) leaves the width alone. */
export function clampWidth(width: number, container: number): number {
  if (container <= 0) return width;
  const most = Math.floor(container * MAX_SHARE);
  return Math.round(Math.min(Math.max(width, Math.min(MIN_WIDTH, most)), most));
}

/** Total: a stored value outlives the app that wrote it. */
export function parseWidth(raw: string | null): number {
  const width = raw === null ? Number.NaN : Number(raw);
  return Number.isFinite(width) && width > 0 ? width : DEFAULT_WIDTH;
}

export function readPaneWidth(): number {
  return parseWidth(safeGet(KEY));
}

export function writePaneWidth(width: number): void {
  safeSet(KEY, String(Math.round(width)));
}
