// @vitest-environment happy-dom
/** The composer: Enter sends, Shift+Enter writes on, Stop takes Send's place while a turn lasts,
 *  and a line above the field only where the thread's typing dots would hide what is wrong. */

import { act } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { ApiError } from "../../api/client";
import type { ChatTurn } from "../../api/types";
import { click, type Mounted, mount, press, settle, type } from "../../testDom";
import { Composer } from "./Composer";
import { chatTurn } from "./testFakes";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.useRealTimers();
});

const onSend = vi.fn(async (_text: string) => true);
const onStop = vi.fn(async (_turnId: number): Promise<unknown> => undefined);

function composer(turn: ChatTurn | null, { isBlocked = false, error = null as unknown } = {}) {
  return (
    <Composer turn={turn} isBlocked={isBlocked} error={error} onSend={onSend} onStop={onStop} />
  );
}

async function render(turn: ChatTurn | null = null, options?: Parameters<typeof composer>[1]) {
  onSend.mockReset().mockResolvedValue(true);
  onStop.mockReset().mockResolvedValue(undefined);
  view = await mount(composer(turn, options));
  const root = view.container;
  return {
    root,
    field: root.querySelector("textarea"),
    button: (name: string) => root.querySelector<HTMLButtonElement>(`button[aria-label="${name}"]`),
    status: () => root.querySelector('[role="status"]')?.textContent ?? null,
  };
}

describe("Composer", () => {
  test("Enter sends the text and empties the field; Shift+Enter and a blank message do not", async () => {
    const { field } = await render();
    await press("Enter", field ?? document);
    expect(onSend).not.toHaveBeenCalled();
    await type(field, "  which tickets are blocked?  ");
    await press("Enter", field ?? document, { shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
    await press("Enter", field ?? document);
    await settle();
    expect(onSend).toHaveBeenCalledExactlyOnceWith("which tickets are blocked?");
    expect(field?.value).toBe("");
  });

  test("Option+Enter and Ctrl+Enter write a new line where the caret is, and do not send", async () => {
    const { field } = await render();
    const input = field as HTMLTextAreaElement;
    await type(input, "first second");
    input.setSelectionRange(5, 6);
    await press("Enter", input, { altKey: true });
    expect(input.value).toBe("first\nsecond");
    await press("Enter", input, { ctrlKey: true });
    expect(input.value).toBe("first\n\nsecond");
    expect(onSend).not.toHaveBeenCalled();
    await press("Enter", input);
    await settle();
    expect(onSend).toHaveBeenCalledExactlyOnceWith("first\n\nsecond");
  });

  test("Enter while an IME is composing does not send", async () => {
    const { field } = await render();
    await type(field, "東京");
    await act(async () => {
      const event = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
      Object.defineProperty(event, "isComposing", { value: true });
      field?.dispatchEvent(event);
    });
    expect(onSend).not.toHaveBeenCalled();
  });

  test("Send is a labelled icon button beside the field, named for a screen reader", async () => {
    const { root, field, button } = await render();
    expect(field?.getAttribute("aria-label")).toBe("Message the assistant");
    expect(field?.placeholder).toBe(
      "Ask about tickets, runs, costs or a process. Shift+Enter for a new line.",
    );
    const send = button("Send");
    expect(send?.textContent).toBe("");
    // The field and the button share one row, with no label or wrapper between them.
    expect(send?.parentElement).toBe(field?.parentElement);
    await type(field, "hello");
    await click(send);
    await settle();
    expect(onSend).toHaveBeenCalledExactlyOnceWith("hello");
    expect(root.textContent).toContain(
      "Factorial Assistant can make mistakes. Check important results.",
    );
  });

  test("while the API has not answered, Send says it is busy and ignores another press", async () => {
    const { field, button } = await render();
    let answer = (_taken: boolean) => {};
    onSend.mockImplementation(() => new Promise<boolean>((resolve) => (answer = resolve)));
    await type(field, "hello");
    await press("Enter", field ?? document);
    expect(button("Send")?.getAttribute("aria-busy")).toBe("true");
    await click(button("Send"));
    expect(onSend).toHaveBeenCalledOnce();
    await act(async () => answer(true));
    expect(button("Send")?.getAttribute("aria-busy")).toBeNull();
    expect(field?.value).toBe("");
  });

  test("a refused send keeps the text and says why", async () => {
    const { field } = await render();
    onSend.mockResolvedValue(false);
    await type(field, "hello");
    await press("Enter", field ?? document);
    await settle();
    expect(field?.value).toBe("hello");
    await view?.rerender(
      composer(null, {
        error: new ApiError(429, "today's assistant budget of 20.00 USD is spent", "daily_budget"),
      }),
    );
    expect(view?.container.querySelector('[role="alert"]')?.textContent).toContain(
      "budget of 20.00 USD is spent",
    );
  });

  test("while a turn lasts Stop stands where Send was, with no status line, and cancels once", async () => {
    const { field, button, status } = await render(chatTurn(4, "running"));
    expect(field?.disabled).toBe(true);
    expect(button("Send")).toBeNull();
    expect(status()).toBeNull();
    const stop = button("Stop");
    expect(stop?.textContent).toBe("");
    await click(stop);
    await click(stop);
    expect(onStop).toHaveBeenCalledExactlyOnceWith(4);
    expect(stop?.getAttribute("aria-disabled")).toBe("true");
    // Pending, it still holds the focus a keyboard press left on it.
    expect(stop?.disabled).toBe(false);

    await view?.rerender(composer(chatTurn(5, "running")));
    expect(button("Stop")?.getAttribute("aria-disabled")).toBeNull();
    await click(button("Stop"));
    expect(onStop).toHaveBeenLastCalledWith(5);
  });

  test("a refused cancel makes Stop pressable again", async () => {
    const { button } = await render(chatTurn(4, "running"));
    onStop.mockRejectedValueOnce(new Error("refused"));
    await click(button("Stop"));
    expect(button("Stop")?.getAttribute("aria-disabled")).toBeNull();
    await click(button("Stop"));
    expect(onStop).toHaveBeenCalledTimes(2);
  });

  test("a queued turn shows no line until it has waited 10 seconds unclaimed", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { root, status } = await render(chatTurn(5, "queued"));
    expect(status()).toBeNull();
    expect(root.textContent).not.toContain("Queued");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(status()).toBe(
      "The assistant hasn't started on this yet. If nothing happens soon, press Stop and try again.",
    );
  });

  test("a turn the stream has not shown yet says another message is being answered", async () => {
    const { field, button, status } = await render(null, { isBlocked: true });
    expect(status()).toBe("The assistant is answering another message in this conversation.");
    expect(field?.disabled).toBe(true);
    expect(button("Send")?.getAttribute("aria-disabled")).toBe("true");
  });

  test("when the turn ends the focus goes back to the field it was sent from", async () => {
    const { field } = await render();
    field?.focus();
    await type(field, "hello");
    await press("Enter", field ?? document);
    await settle();
    // A browser drops the focus from the field it disables; the composer still owes it back.
    field?.blur();
    await view?.rerender(composer(chatTurn(6, "running")));
    expect(document.activeElement).not.toBe(field);
    await view?.rerender(composer(chatTurn(6, "done")));
    expect(document.activeElement).toBe(field);
  });

  test("when the turn ends the focus stays wherever the person moved it", async () => {
    const elsewhere = document.createElement("button");
    const { field } = await render();
    document.body.append(elsewhere);
    field?.focus();
    await view?.rerender(composer(chatTurn(7, "running")));
    elsewhere.focus();
    await view?.rerender(composer(chatTurn(7, "done")));
    expect(document.activeElement).toBe(elsewhere);
  });

  test("when the turn ends the focus stays off the field if the person pressed elsewhere", async () => {
    const { field } = await render();
    field?.focus();
    field?.blur();
    await view?.rerender(composer(chatTurn(9, "running")));
    // Selecting text in the reply moves no focus, so no blur says the person has left.
    await act(async () => {
      document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    });
    await view?.rerender(composer(chatTurn(9, "done")));
    expect(document.activeElement).toBe(document.body);
  });

  test("when the turn ends the focus is not taken from a person who never used the composer", async () => {
    await render(chatTurn(8, "running"));
    await view?.rerender(composer(chatTurn(8, "done")));
    expect(document.activeElement).toBe(document.body);
  });
});
