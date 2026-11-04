// Top-level only: a step's `- name:` is indented.
const NAME_LINE = /^name:[^\n]*$/m;

/** The text declaring lane `lane` on its top-level `name:` line, which is also the stem of
 *  `processes/process-<name>.yaml` the commit writes. Text with no such line is returned as is. */
export function withLaneName(text: string, lane: string): string {
  return text.replace(NAME_LINE, `name: ${lane}`);
}

/** The lane `text` declares on its top-level `name:` line, or null when it declares none. */
export function laneNameIn(text: string): string | null {
  return text.match(NAME_LINE)?.[0].slice("name:".length).trim() || null;
}

/** `check_process_key`'s slug rule. The reserved names are left to the server, which says why. */
export const LANE_KEY = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;

export function laneNameFor(base: string, taken: readonly string[]): string {
  if (!taken.includes(base)) return base;
  let n = 2;
  while (taken.includes(`${base}-${n}`)) n += 1;
  return `${base}-${n}`;
}
