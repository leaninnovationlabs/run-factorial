import { useRef, useState } from "react";

/**
 * An editor draft held in React state and mirrored to storage, so a reload keeps it.
 *
 * State, never the query cache: a textarea is controlled by this value, and TanStack Query tells
 * its observers about `setQueryData` on a timer. React puts a controlled textarea back to its
 * current value when the change event ends, so a value that arrives a tick later moves the caret
 * to the end of the text on every keystroke.
 *
 * `update` computes from the latest written value rather than the render's, for a callback that
 * runs later, such as a commit's `onSuccess`, and must not undo edits made in between.
 */
export function useStoredDraft<T>(read: () => T, save: (next: T) => void) {
  const [data, setData] = useState(read);
  const latest = useRef(data);
  function write(next: T) {
    latest.current = next;
    setData(next);
    save(next);
  }
  function update(change: (current: T) => T) {
    write(change(latest.current));
  }
  return { data, write, update };
}
