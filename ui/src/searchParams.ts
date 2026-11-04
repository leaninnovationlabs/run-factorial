import { useSearchParams } from "react-router-dom";

type ParamChanges = Record<string, string | null>;

/**
 * The address's query, and a writer that sets each key or, given `null`, deletes it — leaving
 * every other key alone, so views on one page can each own theirs.
 *
 * Replaced, not pushed: a view is adjusted many times a minute, and Back should leave the page
 * rather than step back through every adjustment on the way.
 */
export function useViewParams(): [URLSearchParams, (changes: ParamChanges) => void] {
  const [params, setParams] = useSearchParams();
  const update = (changes: ParamChanges) =>
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        for (const [key, value] of Object.entries(changes)) {
          if (value === null) next.delete(key);
          else next.set(key, value);
        }
        return next;
      },
      { replace: true },
    );
  return [params, update];
}
