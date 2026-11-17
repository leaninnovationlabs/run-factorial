/**
 * What a right-click pointed at, as the assistant will be told of it (`draft.ts`'s `Pointed`).
 *
 * In this order: the lines of the config editor under the selection or the caret, with their
 * file; text the person selected, when the click was on it; otherwise the nearest thing with a
 * meaning of its own — a named control, a row, a list item, a link, a button, a panel, a heading —
 * with the dashboard link it holds. The words are read off the page, so they are capped here and
 * told to the model as data written by others (`assistant/prompt.py`); the API caps them again.
 */

import type { Pointed } from "./draft";
import { isDashboardPath } from "./paths";

export const LABEL_MAX = 80;
export const TEXT_MAX = 2000;

const THING =
  '[data-assist-anchor], tr, [role="row"], li, a[href], button, [role="button"], section, article, h1, h2, h3, h4';

/** Where the browser's own menu stays: the conversation, which is not something to ask itself
 *  about, with the menus and cards it opens, and a field that takes text, where the menu is how
 *  a person pastes. */
const CHAT =
  '[data-assistant-mini], [data-assistant-dock], [data-assistant-chat], [data-assistant-bar], .xterm, [role="menu"], [role="tooltip"], [data-hover-card]';

const squash = (text: string) => text.replace(/\s+/g, " ").trim();
const cut = (text: string, max: number) =>
  text.length > max ? `${text.slice(0, max - 1)}…` : text;

function readable(element: Element): string {
  if (element instanceof HTMLTableRowElement) {
    // `innerText`, which breaks a line between a cell's stacked parts: `textContent` runs a
    // ticket's number and its title together.
    return [...element.cells]
      .map((cell) => squash(cell.innerText ?? cell.textContent ?? ""))
      .filter(Boolean)
      .join(" · ");
  }
  const text = (element as HTMLElement).innerText ?? element.textContent ?? "";
  return text.split("\n").map(squash).filter(Boolean).join("\n");
}

/** The dashboard page a link goes to, as the router addresses it: without `base`, which is what
 *  the browser shows before it (the app's prefix and the organization). A link that is not under
 *  the base is another site's, or another organization's, and no page of this dashboard. */
function linkOf(target: Element, thing: Element, base: string): string | null {
  const link = target.closest("a[href]") ?? thing.querySelector("a[href]");
  const href = link?.getAttribute("href") ?? "";
  if (base && !href.startsWith(`${base}/`)) return null;
  const path = href.slice(base.length);
  return isDashboardPath(path) ? path.split(/[?#]/, 1)[0] || null : null;
}

function selected(target: Element): string {
  const selection = window.getSelection();
  const text = selection ? String(selection) : "";
  if (!selection || !text.trim() || selection.rangeCount === 0) return "";
  const range = selection.getRangeAt(0);
  const holds =
    typeof range.intersectsNode === "function"
      ? range.intersectsNode(target)
      : target.contains(selection.anchorNode);
  return holds ? text.trim() : "";
}

function inEditor(editor: HTMLTextAreaElement, page: string): Pointed {
  const { value, selectionStart: start } = editor;
  let end = editor.selectionEnd;
  // A selection that ends at the start of a line holds none of that line.
  if (end > start && value[end - 1] === "\n") end -= 1;
  const first = value.slice(0, start).split("\n").length;
  const last = value.slice(0, end).split("\n").length;
  const file = editor.getAttribute("data-code-file");
  const name = file?.split("/").pop() || "The editor";
  return {
    label: cut(
      `${name}, ${first === last ? `line ${first}` : `lines ${first} to ${last}`}`,
      LABEL_MAX,
    ),
    text: cut(
      value
        .split("\n")
        .slice(first - 1, last)
        .join("\n"),
      TEXT_MAX,
    ),
    page,
    href: null,
    anchor: null,
    file,
    lines: [first, last],
  };
}

/** `pointed` within what one message takes of it, wherever it was made. */
export function capped(pointed: Pointed): Pointed {
  return {
    ...pointed,
    label: cut(pointed.label, LABEL_MAX),
    text: cut(pointed.text, TEXT_MAX),
  };
}

/** The element a right-click on `target` is about: what `describeTarget` reads, and what is
 *  ringed while the menu is open. */
export function pointedElement(target: Element): Element {
  return target.closest("textarea[data-code-editor]") ?? target.closest(THING) ?? target;
}

/**
 * `honourSelection` is false for a selection the person did not make: some systems select the
 * word under the pointer on the way to a context menu, and that word is not what they meant.
 */
export function describeTarget(
  target: Element,
  page: string,
  honourSelection = true,
  base = "",
): Pointed {
  const editor = target.closest<HTMLTextAreaElement>("textarea[data-code-editor]");
  if (editor) return inEditor(editor, page);

  const thing = target.closest(THING) ?? target;
  const anchor = target.closest("[data-assist-anchor]")?.getAttribute("data-assist-anchor") ?? null;
  const place = { page, href: linkOf(target, thing, base), anchor, file: null, lines: null };

  const picked = honourSelection ? selected(target) : "";
  if (picked) {
    return {
      ...place,
      label: `"${cut(squash(picked), LABEL_MAX - 2)}"`,
      text: cut(picked, TEXT_MAX),
    };
  }
  const said = readable(thing);
  const name = said.split("\n", 1)[0] || thing.getAttribute("aria-label") || "This page";
  return { ...place, label: cut(name, LABEL_MAX), text: cut(said, TEXT_MAX) };
}

export function keepsNativeMenu(event: MouseEvent): boolean {
  // Shift with the right button asks for the browser's menu. Shift+F10 is the keyboard's way to
  // any context menu, and comes with no button.
  if (event.shiftKey && event.button === 2) return true;
  const target = event.target;
  if (!(target instanceof Element)) return true;
  if (target.closest(CHAT)) return true;
  // The assistant's Drawer: the dialog that holds the conversation.
  if (target.closest('[role="dialog"]')?.querySelector("[data-item-stream]")) return true;
  const field = target.closest("input, textarea, select, [contenteditable]");
  return field !== null && !field.matches("textarea[data-code-editor]");
}
