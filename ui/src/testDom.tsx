/**
 * Mounting for the tests that run under happy-dom (decision 51): a real root inside `#root`, the
 * element the overlay stack makes inert, so focus and dismissal behave as they do in the app.
 */

import { act, type ReactNode } from "react";
import { createRoot } from "react-dom/client";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export type Mounted = {
  container: HTMLElement;
  rerender: (node: ReactNode) => Promise<void>;
  unmount: () => Promise<void>;
};

export async function mount(node: ReactNode): Promise<Mounted> {
  const container = document.createElement("div");
  container.id = "root";
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(node));
  return {
    container,
    rerender: (next) => act(async () => root.render(next)),
    unmount: async () => {
      await act(async () => root.unmount());
      document.body.replaceChildren();
    },
  };
}

export async function click(element: Element | null | undefined): Promise<void> {
  if (!(element instanceof HTMLElement)) throw new Error("nothing to click");
  await act(async () => element.click());
}

export async function press(
  key: string,
  target: EventTarget = document,
  modifiers: Pick<KeyboardEventInit, "ctrlKey" | "metaKey" | "shiftKey" | "altKey"> = {},
): Promise<void> {
  await act(async () => {
    target.dispatchEvent(
      new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...modifiers }),
    );
  });
}

/** The pointer arrives on an element. React derives enter and leave from over and out. */
export async function hover(element: Element | null | undefined): Promise<void> {
  if (!element) throw new Error("nothing to hover");
  await act(async () => {
    element.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, relatedTarget: null }));
  });
}

export async function unhover(element: Element | null | undefined): Promise<void> {
  if (!element) throw new Error("nothing to leave");
  await act(async () => {
    element.dispatchEvent(
      new MouseEvent("mouseout", { bubbles: true, relatedTarget: document.body }),
    );
  });
}

export async function pointerDown(element: Element): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  });
}

/** Type into a controlled field: through the prototype's setter, which is what React watches. */
export async function type(element: Element | null | undefined, value: string): Promise<void> {
  if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement)) {
    throw new Error("nothing to type into");
  }
  const prototype =
    element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, "value")?.set?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

export async function choose(element: Element | null | undefined, value: string): Promise<void> {
  if (!(element instanceof HTMLSelectElement)) throw new Error("nothing to choose from");
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set?.call(
      element,
      value,
    );
    element.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

/** One keystroke in the middle of the text, with the caret after it as a browser leaves it.
 *  Synchronous on purpose: a value that only arrives on a later tick is still the old one here. */
export function typeAt(element: HTMLTextAreaElement, at: number, char: string): void {
  const next = `${element.value.slice(0, at)}${char}${element.value.slice(at)}`;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(
      element,
      next,
    );
    element.setSelectionRange(at + char.length, at + char.length);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

/** Let effects, timers and settled promises run. */
export async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}

export function byText(scope: ParentNode, selector: string, text: string): HTMLElement | null {
  for (const element of scope.querySelectorAll<HTMLElement>(selector)) {
    if (element.textContent?.trim() === text) return element;
  }
  return null;
}
