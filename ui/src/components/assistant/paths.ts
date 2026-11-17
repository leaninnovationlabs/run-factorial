/** A path on this dashboard. `//host` and `/\host` start with a slash and are another site to a
 *  browser, so they are not. */
export function isDashboardPath(url: string): boolean {
  return url.startsWith("/") && !/^\/[/\\]/.test(url);
}
