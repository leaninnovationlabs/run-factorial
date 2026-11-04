/**
 * The overlay stack — ADS's Layering rule: Escape fires only on the layer that is on top.
 *
 * Every overlay portals to `document.body`, beside `#root` rather than inside it. A MODAL layer
 * (a drawer, a dialog) makes `#root` and every layer under it inert, which contains focus without
 * a Tab trap: the browser skips inert subtrees for focus, clicks and the accessibility tree alike.
 * A dialog opened over the drawer is one more modal layer, and the drawer goes inert with the page.
 * A NON-MODAL layer (a menu, a popup, a tooltip) inerts nothing; it only takes its turn at Escape.
 *
 * One `keydown` listener on the document, attached while anything is open. It runs after React's
 * own handlers (React listens on the root and portal containers, which the event reaches first),
 * so a control inside a layer that handles Escape itself says so with `preventDefault` and the
 * layer leaves it.
 */

type Layer = { element: HTMLElement; onEscape?: () => void; modal: boolean };

const stack: Layer[] = [];

/** Open a layer; the returned function closes it, and is safe to call twice. */
export function pushLayer(
  element: HTMLElement,
  onEscape?: () => void,
  { modal = true }: { modal?: boolean } = {},
): () => void {
  const layer: Layer = { element, onEscape, modal };
  stack.push(layer);
  if (stack.length === 1) document.addEventListener("keydown", onKeyDown);
  sync();
  return () => {
    const index = stack.indexOf(layer);
    if (index === -1) return;
    stack.splice(index, 1);
    layer.element.inert = false;
    if (stack.length === 0) document.removeEventListener("keydown", onKeyDown);
    sync();
  };
}

export function openLayerCount(): number {
  return stack.length;
}

function sync() {
  let topModal = -1;
  stack.forEach((layer, index) => {
    if (layer.modal) topModal = index;
  });
  const root = document.getElementById("root");
  if (root) root.inert = topModal !== -1;
  stack.forEach((layer, index) => {
    layer.element.inert = index < topModal;
  });
}

function onKeyDown(event: KeyboardEvent) {
  if (event.key !== "Escape" || event.defaultPrevented) return;
  const top = stack.at(-1);
  if (!top?.onEscape) return;
  event.preventDefault();
  top.onEscape();
}
