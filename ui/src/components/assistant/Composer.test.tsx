// @vitest-environment happy-dom
/** The composer: Enter sends, Shift+Enter writes on, Stop takes Send's place while a turn lasts,
 *  and a line above the field only where the thread's typing dots would hide what is wrong. */

import { act } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { ApiError } from "../../api/client";
import type { ChatTurn } from "../../api/types";
import { click, type Mounted, mount, press, settle, type } from "../../testDom";
import { Composer } from "./Composer";
import type { Draft } from "./draft";
import { chatTurn } from "./testFakes";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.useRealTimers();
});

const onSend = vi.fn(async (_draft: Draft) => true);
const said = (text: string): Draft => ({ text, pointed: [], files: [] });
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
    expect(onSend).toHaveBeenCalledExactlyOnceWith(said("which tickets are blocked?"));
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
    expect(onSend).toHaveBeenCalledExactlyOnceWith(said("first\n\nsecond"));
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
    expect(onSend).toHaveBeenCalledExactlyOnceWith(said("hello"));
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

  test("a draft held outside it is the one it draws, and each change goes back there", async () => {
    const onDraft = vi.fn();
    view = await mount(
      <Composer
        turn={null}
        isBlocked={false}
        error={null}
        draft={said("half a thought")}
        onDraft={onDraft}
        onSend={onSend}
        onStop={onStop}
      />,
    );
    const field = view.container.querySelector("textarea");
    expect(field?.value).toBe("half a thought");
    await type(field, "half a thought, finished");
    expect(onDraft).toHaveBeenLastCalledWith(said("half a thought, finished"));
  });

  async function pick(input: HTMLInputElement | null, files: File[]) {
    Object.defineProperty(input, "files", { configurable: true, value: files });
    await act(async () => {
      input?.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await settle();
  }
  const fileChips = (root: ParentNode) =>
    [...root.querySelectorAll("[data-file-chip]")].map((chip) => chip.textContent);

  test("the paperclip's files wait as chips, go with the message, and each can be taken out", async () => {
    const { root, field, button } = await render();
    expect(button("Attach a file")).not.toBeNull();
    const input = root.querySelector<HTMLInputElement>('input[type="file"]');
    await pick(input, [
      new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" }),
      new File(["timed out"], "worker.log", { type: "text/plain" }),
    ]);
    expect(fileChips(root)).toEqual(["shot.png", "worker.log"]);
    await click(button("Remove worker.log"));
    expect(fileChips(root)).toEqual(["shot.png"]);
    await press("Enter", field ?? document);
    await settle();
    expect(onSend).toHaveBeenCalledExactlyOnceWith({
      text: "",
      pointed: [],
      files: [{ name: "shot.png", size: 3, isImage: true, data: btoa("\x01\x02\x03") }],
    });
    expect(fileChips(root)).toEqual([]);
  });

  test("a pasted picture is attached, and pasted words are left to the field", async () => {
    const { root, field } = await render();
    const paste = (files: File[]) => {
      const event = new Event("paste", { bubbles: true, cancelable: true });
      Object.defineProperty(event, "clipboardData", { value: { files, getData: () => "" } });
      return event;
    };
    const withFile = paste([new File([new Uint8Array([9])], "image.png", { type: "image/png" })]);
    await act(async () => {
      field?.dispatchEvent(withFile);
    });
    await settle();
    expect(withFile.defaultPrevented).toBe(true);
    expect(fileChips(root)).toEqual(["image.png"]);
    const words = paste([]);
    await act(async () => {
      field?.dispatchEvent(words);
    });
    expect(words.defaultPrevented).toBe(false);
  });

  test("a file the assistant cannot take says why, and the sentence goes with the next good one", async () => {
    view = await mount(
      <Composer
        turn={null}
        isBlocked={false}
        error={null}
        acceptsImages={false}
        onSend={onSend}
        onStop={onStop}
      />,
    );
    const root = view.container;
    const input = root.querySelector<HTMLInputElement>('input[type="file"]');
    await pick(input, [new File([new Uint8Array([1])], "shot.png", { type: "image/png" })]);
    expect(fileChips(root)).toEqual([]);
    expect(root.querySelector('[role="alert"]')?.textContent).toContain("takes no images");
    await pick(input, [new File(["ok"], "worker.log", { type: "text/plain" })]);
    expect(fileChips(root)).toEqual(["worker.log"]);
    expect(root.querySelector('[role="alert"]')).toBeNull();
  });

  test("in a frame beside the page it is one row with a short prompt and no line under it", async () => {
    view = await mount(
      <Composer
        turn={null}
        isBlocked={false}
        error={null}
        compact
        onSend={onSend}
        onStop={onStop}
      />,
    );
    const field = view.container.querySelector("textarea");
    expect(field?.getAttribute("rows")).toBe("1");
    expect(field?.getAttribute("placeholder")).toBe("Reply");
    expect(view.container.textContent).not.toContain("can make mistakes");
    await type(field, "one\ntwo\nthree");
    expect(field?.getAttribute("rows")).toBe("3");
  });

  test("what joins the draft while a send is on its way is still there when the send lands", async () => {
    let land: (taken: boolean) => void = () => {};
    onSend.mockReset().mockImplementation(() => new Promise<boolean>((taken) => (land = taken)));
    onStop.mockReset();
    let held: Draft = said("first");
    const hold = (next: Draft | ((was: Draft) => Draft)) => {
      held = typeof next === "function" ? next(held) : next;
    };
    const drawn = () => (
      <Composer
        turn={null}
        isBlocked={false}
        error={null}
        draft={held}
        onDraft={hold}
        onSend={onSend}
        onStop={onStop}
      />
    );
    view = await mount(drawn());
    await press("Enter", view.container.querySelector("textarea") ?? document);
    // Pointed at from the page while the request is out.
    const pointed = { label: "Ticket 212", text: "Blocked", page: "/tickets/212" };
    held = { ...held, pointed: [pointed] };
    await view.rerender(drawn());
    await act(async () => {
      land(true);
    });
    await settle();
    expect(held).toEqual({ text: "", pointed: [pointed], files: [] });
  });

  test("words written while a file is being read are kept beside it", async () => {
    let held: Draft = said("");
    const hold = (next: Draft | ((was: Draft) => Draft)) => {
      held = typeof next === "function" ? next(held) : next;
    };
    const drawn = () => (
      <Composer
        turn={null}
        isBlocked={false}
        error={null}
        draft={held}
        onDraft={hold}
        onSend={onSend}
        onStop={onStop}
      />
    );
    view = await mount(drawn());
    let read: (bytes: ArrayBuffer) => void = () => {};
    const slow = new File([new Uint8Array([1])], "shot.png", { type: "image/png" });
    slow.arrayBuffer = () => new Promise<ArrayBuffer>((bytes) => (read = bytes));
    const input = view.container.querySelector<HTMLInputElement>('input[type="file"]');
    Object.defineProperty(input, "files", { configurable: true, value: [slow] });
    await act(async () => {
      input?.dispatchEvent(new Event("change", { bubbles: true }));
    });
    held = { ...held, text: "written meanwhile" };
    await view.rerender(drawn());
    await act(async () => {
      read(new Uint8Array([1]).buffer);
    });
    await settle();
    expect(held.text).toBe("written meanwhile");
    expect(held.files.map((file) => file.name)).toEqual(["shot.png"]);
  });

  test("a refused send keeps the files too", async () => {
    const { root, field } = await render();
    onSend.mockResolvedValue(false);
    await pick(root.querySelector<HTMLInputElement>('input[type="file"]'), [
      new File(["timed out"], "worker.log", { type: "text/plain" }),
    ]);
    await press("Enter", field ?? document);
    await settle();
    expect(fileChips(root)).toEqual(["worker.log"]);
  });

  test("a paste that carries words is words, even when a picture of them comes with it", async () => {
    const { root, field } = await render();
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", {
      value: {
        files: [new File([new Uint8Array([9])], "cells.png", { type: "image/png" })],
        getData: (kind: string) => (kind === "text/plain" ? "a\tb" : ""),
      },
    });
    await act(async () => {
      field?.dispatchEvent(event);
    });
    await settle();
    expect(event.defaultPrevented).toBe(false);
    expect(fileChips(root)).toEqual([]);
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
