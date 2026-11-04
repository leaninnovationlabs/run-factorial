/**
 * The text's top-level `runtime:` line (decision 36) is the one place the editor keeps a lane's
 * runtime: the text is what the commit carries, so anything that remembered a runtime the text
 * did not say would be the regenerate bug in another form.
 */

// Top-level only: an indented `runtime:` belongs to a step's workspace, never to the process.
const VALUE = /^runtime:[ \t]*("([^"]*)"|'([^']*)'|[^\s#]*)[^\n]*$/m;
const WHOLE_LINE = /^runtime:[^\n]*(\n|$)/m;
// A top-level example left commented out, as the Simple template keeps one.
const EXAMPLE = /^#[ \t]*runtime:[^\n]*\n?/m;

/** What the text names: a slug, `null` for text that names none, and `undefined` for a blank
 *  editor, where nothing has been said yet and the pinned config's answer still stands. */
export function runtimeInText(text: string): string | null | undefined {
  if (!text.trim()) return undefined;
  const match = VALUE.exec(text);
  if (!match) return null;
  return match[2] ?? match[3] ?? match[1] ?? null;
}

/** The text with its `runtime:` line set to `slug`, or removed for `null`. A new line goes
 *  under a commented-out `# runtime:` example; failing that, under `version:`, where the fleet
 *  sweep put it; failing that, at the end. */
export function withRuntimeLine(text: string, slug: string | null): string {
  const line = slug === null ? null : `runtime: ${slug}`;
  if (WHOLE_LINE.test(text)) {
    return text.replace(WHOLE_LINE, (_, nl: string) => (line === null ? "" : `${line}${nl}`));
  }
  if (line === null) return text;
  const anchor = EXAMPLE.exec(text) ?? /^version:[^\n]*\n?/m.exec(text);
  if (anchor) {
    const at = anchor.index + anchor[0].length;
    const nl = anchor[0].endsWith("\n") ? "" : "\n";
    return `${text.slice(0, at)}${nl}${line}\n${text.slice(at)}`;
  }
  return `${text}${text.endsWith("\n") || text === "" ? "" : "\n"}${line}\n`;
}
