/**
 * The clock, zone and locale a reference page is rendered under, so its static file is the same
 * on every machine (docs/design-system.md, Reference pages). `format.ts` builds its formatters with the default locale
 * when it loads, so a reference test loads this first, before any import reaches `format.ts`:
 *
 *   await vi.hoisted(() => import("./referenceEnvironment"));
 */

import { vi } from "vitest";

/** "Now" for every reference page: the catalogue's relative times are counted from it. */
export const REFERENCE_NOW = new Date("2026-09-14T12:00:00Z");
const LOCALE = "en-US";

process.env.TZ = "UTC";
vi.useFakeTimers({ now: REFERENCE_NOW, toFake: ["Date"] });

type Formatter = typeof Intl.DateTimeFormat | typeof Intl.RelativeTimeFormat;

/** `undefined` or `[]` means "the machine's locale"; a reference page means `en-US`. */
const fixed = <T>(locales: T): T | typeof LOCALE =>
  locales === undefined || (Array.isArray(locales) && locales.length === 0) ? LOCALE : locales;

/** A formatter constructed with the locale `fixed` gives it. */
function pinned<T extends Formatter>(formatter: T): T {
  return new Proxy(formatter, {
    construct: (target, [locales, options]) =>
      new (target as new (l: unknown, o: unknown) => object)(fixed(locales), options),
    apply: (target, _, [locales, options]) =>
      (target as unknown as (l: unknown, o: unknown) => object)(fixed(locales), options),
  });
}

// Read-only in the DOM typings, though writable at run time.
for (const name of ["DateTimeFormat", "RelativeTimeFormat"] as const) {
  Object.defineProperty(Intl, name, { value: pinned(Intl[name]), configurable: true });
}

const toLocaleTimeString = Date.prototype.toLocaleTimeString;
Date.prototype.toLocaleTimeString = function (
  this: Date,
  locales?: Intl.LocalesArgument,
  options?: Intl.DateTimeFormatOptions,
) {
  return toLocaleTimeString.call(this, fixed(locales), options);
} as typeof Date.prototype.toLocaleTimeString;
