// @vitest-environment happy-dom
/**
 * Copying: a copy that happened says so for a moment, and a copy the browser refused says THAT —
 * a dashboard served over plain HTTP off localhost, where the clipboard can say no.
 */

import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { click, type Mounted, mount } from "../../testDom";
import { CodeBlock, CopyButton, ServerText } from "./Code";
import { clearFlags, FlagGroup } from "./Flag";

let view: Mounted | null = null;
let writeText: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});

afterEach(async () => {
  vi.useRealTimers();
  await view?.unmount();
  view = null;
  act(() => clearFlags());
});

const button = () => document.querySelector("button") as HTMLButtonElement;

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("CopyButton", () => {
  it("writes the value to the clipboard and says Copied, then goes back after 1.5 s", async () => {
    view = await mount(<CopyButton value="gh auth login" />);
    expect(button().textContent).toBe("Copy");
    await click(button());
    await flush();
    expect(writeText).toHaveBeenCalledWith("gh auth login");
    expect(button().textContent).toBe("Copied");
    expect(button().querySelector('[aria-live="polite"]')?.textContent).toBe("Copied");
    await act(async () => {
      vi.advanceTimersByTime(1_400);
    });
    expect(button().textContent).toBe("Copied");
    await act(async () => {
      vi.advanceTimersByTime(200);
    });
    expect(button().textContent).toBe("Copy");
  });

  it("raises an error flag when the browser refuses, and never claims it copied", async () => {
    writeText.mockRejectedValue(new DOMException("Document is not focused.", "NotAllowedError"));
    view = await mount(
      <>
        <CopyButton value="gh auth login" />
        <FlagGroup />
      </>,
    );
    await click(button());
    await flush();
    expect(button().textContent).toBe("Copy");
    const flag = document.querySelector('[data-appearance="error"]');
    expect(flag?.textContent).toContain("Couldn't copy that");
  });
});

describe("CodeBlock", () => {
  it("is a named, keyboard-reachable block that copies its whole text", async () => {
    view = await mount(<CodeBlock label="Install command" text={"uv sync\nmake check"} />);
    expect(document.querySelector("figcaption")?.textContent).toBe("Install command");
    expect(document.querySelector("pre")?.tabIndex).toBe(0);
    await click(button());
    await flush();
    expect(writeText).toHaveBeenCalledWith("uv sync\nmake check");
  });

  it("names its copy button after what it copies, so two on a page are not both 'Copy'", async () => {
    view = await mount(
      <>
        <CodeBlock label="Install command" text="uv sync" />
        <CodeBlock label="Check command" text="make check" />
      </>,
    );
    const names = [...document.querySelectorAll("button")].map((b) => b.getAttribute("aria-label"));
    // The visible word starts each name, as label-in-name asks.
    expect(names).toEqual(["Copy Install command", "Copy Check command"]);
  });

  it("numbers its lines without the numbers being read or copied", async () => {
    view = await mount(<CodeBlock label="File" text={"a\nb"} showLineNumbers isCopyable={false} />);
    const numbers = [...document.querySelectorAll('pre [aria-hidden="true"]')];
    expect(numbers.map((number) => number.textContent)).toEqual(["1", "2"]);
    expect(document.querySelector("button")).toBeNull();
  });
});

describe("ServerText", () => {
  it("renders a backtick span as inline code and leaves no backtick behind", async () => {
    view = await mount(
      <p>
        <ServerText text="the `factorial` head was pushed" />
      </p>,
    );
    expect(document.querySelector("code")?.textContent).toBe("factorial");
    expect(document.querySelector("p")?.textContent).toBe("the factorial head was pushed");
  });

  it("keeps an unmatched or empty backtick literal", async () => {
    view = await mount(
      <p>
        <ServerText text="a ` b" />
        <ServerText text="c `` d" />
      </p>,
    );
    expect(document.querySelector("code")).toBeNull();
    expect(document.querySelector("p")?.textContent).toBe("a ` bc `` d");
  });
});
