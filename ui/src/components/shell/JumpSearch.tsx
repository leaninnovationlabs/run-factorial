/**
 * The search in the top bar: type a ticket number, a title word, a project or a runtime, and go.
 * A combobox after the ARIA pattern — the focus stays in the field, the arrows move the active
 * answer, Enter follows it, Escape closes the list and then clears the field. ⌘K or Ctrl+K from
 * anywhere puts the focus here. A chord, not a single key like `/`: a one-character shortcut
 * fires on a speech user's dictated words and needs an off switch (WCAG 2.1.4).
 *
 * `collapsible` (below 1024px) draws it as an icon until it is wanted: pressing the icon or the
 * chord opens the field with the focus in it, and it folds back when the focus leaves an empty
 * field, when it goes somewhere, or on its own Close. A query typed and left stays open, so a
 * reader who taps away to look at the page does not lose it. Whether it is open is the top bar's
 * (`unfolded`), which gives the open field the room the names held.
 */

import { type KeyboardEvent, useEffect, useId, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useCan } from "../../api/organizationQueries";
import { useOverview, useRuntimes } from "../../api/queries";
import { cx, Icon, IconButton, TextField } from "../ui";
import { type Jump, jumpMatches } from "./jump";

const KIND_GLYPH = {
  ticket: "status-information",
  project: "projects",
  runtime: "runtimes",
} as const;

export function JumpSearch({
  collapsible = false,
  unfolded = false,
  onUnfoldedChange = () => {},
}: {
  collapsible?: boolean;
  unfolded?: boolean;
  onUnfoldedChange?: (unfolded: boolean) => void;
}) {
  const listId = useId();
  const field = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  // The chord's listener is added once, so it reads the latest setter through a ref.
  const setUnfolded = useRef(onUnfoldedChange);
  setUnfolded.current = onUnfoldedChange;
  const folded = collapsible && !unfolded;
  /** Set when the field is asked for while folded: it takes the focus once it is drawn. */
  const wantsFocus = useRef(false);
  const [active, setActive] = useState(0);
  // Read only while the reader is searching: the overview is polled for the board, and a search
  // that kept its own copy warm on every page would double that traffic for nothing.
  const overview = useOverview({ enabled: open });
  // Runtimes are settings: a reader who cannot manage them gets no matches, not a refusal.
  const canRuntimes = useCan("runtimes:manage");
  const runtimes = useRuntimes({ enabled: open && canRuntimes });
  const answers = useMemo(
    () => jumpMatches(query, overview.data, runtimes.data),
    [query, overview.data, runtimes.data],
  );

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key.toLowerCase() !== "k" || !(event.metaKey || event.ctrlKey)) return;
      if (event.altKey || event.shiftKey) return;
      event.preventDefault();
      if (field.current) field.current.focus();
      else {
        wantsFocus.current = true;
        setUnfolded.current(true);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (folded || !wantsFocus.current) return;
    wantsFocus.current = false;
    field.current?.focus();
  }, [folded]);

  const unfold = () => {
    wantsFocus.current = true;
    setUnfolded.current(true);
  };
  const fold = () => {
    setQuery("");
    setOpen(false);
    setUnfolded.current(false);
  };

  const go = (answer: Jump | undefined) => {
    if (!answer) return;
    setQuery("");
    setOpen(false);
    setUnfolded.current(false);
    field.current?.blur();
    navigate(answer.to);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive((current) =>
        answers.length ? (current + step + answers.length) % answers.length : 0,
      );
    } else if (event.key === "Enter") {
      event.preventDefault();
      go(answers[active]);
    } else if (event.key === "Escape") {
      // Spent here, so a drawer or panel under the bar is not closed by the same press.
      event.preventDefault();
      if (open && query) setOpen(false);
      else {
        setQuery("");
        setUnfolded.current(false);
        field.current?.blur();
      }
    }
  };

  const shown = open && query.trim() !== "";
  const loading = overview.isPending || (canRuntimes && runtimes.isPending);
  if (folded) {
    return (
      <div className="flex w-full justify-end">
        <IconButton
          icon="search"
          label="Search"
          aria-keyshortcuts="Meta+K Control+K"
          onClick={unfold}
        />
      </div>
    );
  }
  return (
    <div className="relative flex w-full items-center gap-1">
      <TextField
        ref={field}
        type="search"
        role="combobox"
        aria-label="Jump to a ticket, project or runtime"
        aria-keyshortcuts="Meta+K Control+K"
        aria-expanded={shown}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={shown && answers[active] ? `${listId}-${active}` : undefined}
        // Short enough to survive a phone's bar; the name above says what it finds.
        placeholder="Search"
        elemBeforeInput={<Icon glyph="search" size="small" />}
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => {
          setOpen(false);
          if (query === "") setUnfolded.current(false);
        }}
        onKeyDown={onKeyDown}
      />
      {collapsible && <IconButton icon="close" label="Close search" onClick={fold} />}
      {shown && (
        <div
          id={listId}
          role="listbox"
          aria-label="Matches"
          className="absolute inset-x-0 top-full z-layer mt-1 max-h-80 overflow-y-auto rounded-md border border-default bg-surface-overlay py-1 shadow-overlay"
        >
          {answers.length === 0 ? (
            <p className="px-3 py-2 text-body-sm text-subtle">
              {loading ? "Searching…" : `Nothing matches "${query.trim()}".`}
            </p>
          ) : (
            answers.map((answer, index) => (
              <div
                key={answer.to}
                id={`${listId}-${index}`}
                role="option"
                // Named by the field's active descendant; the focus itself stays in the field.
                tabIndex={-1}
                aria-selected={index === active}
                // Chosen on press, before the field's blur closes the list under the pointer.
                onMouseDown={(event) => {
                  event.preventDefault();
                  go(answer);
                }}
                onMouseEnter={() => setActive(index)}
                className={cx(
                  "flex cursor-pointer items-center gap-2 px-3 py-1.5 text-body",
                  index === active ? "bg-selected text-selected" : "text-default",
                )}
              >
                <Icon glyph={KIND_GLYPH[answer.kind]} className="text-icon-subtle" />
                <span className="min-w-0 flex-1 truncate">{answer.label}</span>
                <span className="shrink-0 text-body-sm text-subtle">{answer.detail}</span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
