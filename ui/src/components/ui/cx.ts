/** Join class names, dropping the falsy ones. Layout classes only at call sites (decision 50):
 *  a design primitive's look comes from its props. */
export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}
