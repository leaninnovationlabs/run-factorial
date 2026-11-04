/**
 * The browser's storage, for conveniences only: a remembered theme, a collapsed navigation, a
 * draft. It can be refused — a private window, blocked site data — and can be absent under the
 * server renderer, so every read and write here swallows the failure and the app carries on with
 * the choice for the life of the tab.
 */

import { useCallback, useState } from "react";

export function safeGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** `null` removes the entry. */
export function safeSet(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // ignored — the value still holds until reload
  }
}

/**
 * State that outlives a reload when the browser lets it. Read once, at mount; written on every
 * change. `parse` narrows whatever was stored — a hand edit, an older format — to a value.
 */
export function usePersistentState<T>(
  key: string,
  parse: (stored: string | null) => T,
  serialize: (value: T) => string,
): [T, (next: T | ((previous: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => parse(safeGet(key)));
  const set = useCallback(
    (next: T | ((previous: T) => T)) =>
      setValue((previous) => {
        const resolved = typeof next === "function" ? (next as (previous: T) => T)(previous) : next;
        safeSet(key, serialize(resolved));
        return resolved;
      }),
    [key, serialize],
  );
  return [value, set];
}
