// @vitest-environment happy-dom
/**
 * What the emulator hands on as typing. xterm itself is Chrome's to check (decision 51); this is
 * the filter in front of it, and what tells a person's input from the terminal's own answers.
 * It also builds real Terminals, to check emoji cell widths, sliced writes and the in-order clear,
 * and mounts the view, to check when its handle says a history is drawn.
 */

import { Terminal } from "@xterm/xterm";
import { act, createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, settle } from "../../testDom";
import XtermView, {
  clearInOrder,
  dropReports,
  Gestures,
  swallowedModes,
  whenSettled,
  widenEmoji,
  writeInSlices,
  type XtermHandle,
} from "./XtermView";

afterEach(() => {
  vi.useRealTimers();
});

describe("dropReports", () => {
  it("drops focus and mouse reports so a click is not typing", () => {
    expect(dropReports("\x1b[I")).toBe("");
    expect(dropReports("\x1b[O")).toBe("");
    expect(dropReports("\x1b[<0;10;5M\x1b[<0;10;5m")).toBe("");
    expect(dropReports("\x1b[M !!")).toBe("");
    expect(dropReports("ls\r")).toBe("ls\r");
  });

  it("keeps the keys around a report, and every other escape sequence", () => {
    expect(dropReports("a\x1b[Ib")).toBe("ab");
    expect(dropReports("\x1b[A\x1b")).toBe("\x1b[A\x1b");
  });
});

describe("swallowedModes", () => {
  it("swallows tmux's alternate screen and mouse modes, so the scrollback and the wheel work", () => {
    expect(swallowedModes([1049])).toBe(true);
    expect(swallowedModes([1047])).toBe(true);
    expect(swallowedModes([1000])).toBe(true);
    expect(swallowedModes([1006])).toBe(true);
    expect(swallowedModes([1000, 1006])).toBe(true);
  });

  it("leaves xterm any other mode, and a sequence that mixes one in", () => {
    expect(swallowedModes([25])).toBe(false);
    expect(swallowedModes([1049, 25])).toBe(false);
    expect(swallowedModes([])).toBe(false);
  });
});

describe("whenSettled", () => {
  it("fits once when a pane stops widening, not at every frame of it", () => {
    vi.useFakeTimers();
    const fit = vi.fn();
    const refit = whenSettled(fit);
    for (let frame = 0; frame < 15; frame++) {
      refit();
      vi.advanceTimersByTime(16);
    }
    expect(fit).not.toHaveBeenCalled();
    vi.advanceTimersByTime(200);
    expect(fit).toHaveBeenCalledTimes(1);
  });

  it("never fits after it is cancelled", () => {
    vi.useFakeTimers();
    const fit = vi.fn();
    const refit = whenSettled(fit);
    refit();
    refit.cancel();
    vi.advanceTimersByTime(1000);
    expect(fit).not.toHaveBeenCalled();
  });
});

describe("Gestures", () => {
  it("counts what follows a key, a paste or a composition as a person's, and nothing after", () => {
    vi.useFakeTimers();
    const host = document.createElement("div");
    const field = document.createElement("textarea");
    host.append(field);
    const gestures = new Gestures();
    const unlisten = gestures.listen(host);
    expect(gestures.now).toBe(false);

    gestures.mark();
    expect(gestures.now).toBe(true);
    vi.advanceTimersByTime(1000);
    expect(gestures.now).toBe(false);

    for (const type of ["paste", "compositionend"]) {
      field.dispatchEvent(new Event(type, { bubbles: true }));
      expect(gestures.now).toBe(true);
      vi.advanceTimersByTime(1000);
      expect(gestures.now).toBe(false);
    }

    unlisten();
    field.dispatchEvent(new Event("paste", { bubbles: true }));
    expect(gestures.now).toBe(false);
  });

  it("counts a key as a person's, but never a modifier pressed alone", () => {
    vi.useFakeTimers();
    const gestures = new Gestures();
    for (const key of ["Shift", "Control", "Alt", "Meta", "AltGraph", "CapsLock"]) {
      gestures.key(key);
      expect(gestures.now).toBe(false);
    }
    gestures.key("a");
    expect(gestures.now).toBe(true);
  });
});

describe("widenEmoji", () => {
  async function cursorAfter(text: string): Promise<number> {
    const term = new Terminal({ allowProposedApi: true, cols: 20, rows: 2 });
    widenEmoji(term);
    await new Promise<void>((done) => term.write(text, done));
    return term.buffer.active.cursorX;
  }

  it("counts an emoji as two cells, as tmux and the CLIs do", async () => {
    expect(await cursorAfter("a🤖b")).toBe(4);
    expect(await cursorAfter("a✅b")).toBe(4);
  });

  it("leaves a VS16 emoji one cell wide, matching the E2B template's tmux", async () => {
    expect(await cursorAfter("a⚠️b")).toBe(3);
  });
});

describe("writeInSlices", () => {
  function recorder() {
    const writes: number[] = [];
    const pending: (() => void)[] = [];
    const write = (bytes: Uint8Array, done: () => void) => {
      writes.push(bytes.length);
      pending.push(done);
    };
    const parse = () => pending.shift()?.();
    return { writes, write, parse };
  }

  it("writes 64 KiB at a time, each once xterm has parsed the one before", () => {
    const { writes, write, parse } = recorder();
    const done = vi.fn();
    writeInSlices(write, new Uint8Array(150_000), done, () => true);
    expect(writes).toEqual([65_536]);
    parse();
    expect(writes).toEqual([65_536, 65_536]);
    parse();
    expect(writes).toEqual([65_536, 65_536, 18_928]);
    expect(done).not.toHaveBeenCalled();
    parse();
    expect(done).toHaveBeenCalledOnce();
  });

  it("writes nothing more, and never says it is done, once the terminal has gone", () => {
    const { writes, write, parse } = recorder();
    const done = vi.fn();
    let alive = true;
    writeInSlices(write, new Uint8Array(150_000), done, () => alive);
    parse();
    alive = false;
    parse();
    parse();
    expect(writes).toEqual([65_536, 65_536]);
    expect(done).not.toHaveBeenCalled();
  });
});

describe("XtermHandle.write", () => {
  async function shown() {
    Object.defineProperty(document, "fonts", {
      value: { load: () => Promise.resolve([]) },
      configurable: true,
    });
    const ref = createRef<XtermHandle>();
    const view = await mount(
      <XtermView
        typing="off"
        onData={() => {}}
        onResize={() => {}}
        onReady={() => {}}
        onLeave={() => {}}
        ref={ref}
      />,
    );
    return { view, handle: ref.current as XtermHandle };
  }
  const history = () => new Uint8Array(150_000).fill(0x61);

  it("says the history is done when the terminal goes mid-write, or was already gone", async () => {
    const { view, handle } = await shown();
    const done = vi.fn();
    await act(async () => handle.write(history(), done));
    expect(done).not.toHaveBeenCalled();
    await view.unmount();
    await settle(100);
    expect(done).toHaveBeenCalledOnce();
    const late = vi.fn();
    handle.write(history(), late);
    expect(late).toHaveBeenCalledOnce();
  });

  it("never says the history is done once a new connection reset the screen", async () => {
    const { view, handle } = await shown();
    const done = vi.fn();
    await act(async () => handle.write(history(), done));
    handle.reset();
    await settle(100);
    expect(done).not.toHaveBeenCalled();
    await view.unmount();
  });
});

describe("clearInOrder", () => {
  const term = () => new Terminal({ allowProposedApi: true, cols: 20, rows: 2 });
  const write = (into: Terminal, data: string | Uint8Array) =>
    new Promise<void>((done) => into.write(data, done));

  it.each([
    ["plain text", "old"],
    ["a cut CSI", "old\x1b[3"],
    ["a cut OSC", "old\x1b]0;tit"],
    ["a cut DCS", "old\x1bP$qm"],
    ["a cut UTF-8 character", new Uint8Array([0x6f, 0x6c, 0x64, 0xe2, 0x9c])],
  ])("clears what was written before it, even when that ends in %s", async (_, stale) => {
    const into = term();
    const answers: string[] = [];
    into.onData((data) => answers.push(data));
    into.write(stale);
    clearInOrder(into);
    await write(into, "new");
    expect(into.buffer.active.getLine(0)?.translateToString(true)).toBe("new");
    expect(into.buffer.active.cursorX).toBe(3);
    // A sequence cut short is dropped, never finished: a finished query would be answered.
    expect(answers).toEqual([]);
  });
});
