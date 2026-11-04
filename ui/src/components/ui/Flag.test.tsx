// @vitest-environment happy-dom
/**
 * Flags: raised from anywhere, drawn by one FlagGroup, spoken through one live region. Good news
 * leaves on its own; news the reader may have to act on stays until they dismiss it.
 */

import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { byText, click, hover, type Mounted, mount } from "../../testDom";
import { announce, clearFlags, FlagGroup, showFlag } from "./Flag";

let view: Mounted | null = null;

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(async () => {
  vi.useRealTimers();
  await view?.unmount();
  view = null;
  act(() => clearFlags());
});

const flags = () => [...document.querySelectorAll("[data-appearance]")];
const region = () => document.querySelector('[aria-live="polite"]');

describe("the announcer", () => {
  it("speaks from beside the page, where an open dialog's inert page cannot silence it", async () => {
    // A modal layer makes `#root` inert; an announcer inside it would leave the accessibility
    // tree with the page, and what a flag says while a drawer is open would go unheard.
    const view = await mount(<FlagGroup />);
    expect(region()?.parentElement).toBe(document.body);
    await view.unmount();
  });
});

async function raise(input: Parameters<typeof showFlag>[0]) {
  await act(async () => {
    showFlag(input);
  });
}

async function wait(ms: number) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
}

describe("FlagGroup", () => {
  it("draws a flag raised from outside any component, under Notifications", async () => {
    view = await mount(<FlagGroup />);
    await raise({ appearance: "success", title: "Stored OPENAI_API_KEY" });
    const group = document.querySelector('section[aria-label="Notifications"]');
    expect(group?.textContent).toContain("Stored OPENAI_API_KEY");
  });

  it("lets a success and an info flag go after 8 s, and keeps an error until dismissed", async () => {
    view = await mount(<FlagGroup />);
    await raise({ appearance: "success", title: "Saved" });
    await raise({ appearance: "info", title: "Pause the project first." });
    await raise({ appearance: "error", title: "Couldn't exit the ticket" });
    await wait(7_900);
    expect(flags()).toHaveLength(3);
    await wait(200);
    expect(flags().map((flag) => flag.getAttribute("data-appearance"))).toEqual(["error"]);
    await wait(60_000);
    expect(flags()).toHaveLength(1);
  });

  it("holds a flag the pointer is on, so it cannot vanish while it is being read", async () => {
    view = await mount(<FlagGroup />);
    await raise({ appearance: "success", title: "Saved" });
    await hover(flags()[0]);
    await wait(9_000);
    expect(flags()).toHaveLength(1);
  });

  it("goes when dismissed", async () => {
    view = await mount(<FlagGroup />);
    await raise({ appearance: "error", title: "Couldn't exit the ticket" });
    await click(document.querySelector('button[aria-label="Dismiss"]'));
    expect(flags()).toHaveLength(0);
  });

  it("runs a flag's action and then clears the flag", async () => {
    view = await mount(<FlagGroup />);
    const undo = vi.fn();
    await raise({
      appearance: "info",
      title: "Edits discarded",
      actions: [{ text: "Undo", onClick: undo }],
    });
    await click(byText(document, "button", "Undo"));
    expect(undo).toHaveBeenCalledOnce();
    expect(flags()).toHaveLength(0);
  });
});

describe("the live region", () => {
  it("is mounted before anything is said, so the first thing said is heard", async () => {
    view = await mount(<FlagGroup />);
    expect(region()).not.toBeNull();
    expect(region()?.textContent).toBe("");
  });

  it("speaks what announce() is given, and every flag's title and text", async () => {
    view = await mount(<FlagGroup />);
    await act(async () => {
      announce("Exit armed. Press Exit ticket to confirm.");
    });
    expect(region()?.textContent).toBe("Exit armed. Press Exit ticket to confirm.");
    await raise({
      appearance: "success",
      title: "Stored OPENAI_API_KEY",
      description: "It cannot be read back.",
    });
    expect(region()?.textContent).toBe("Stored OPENAI_API_KEY. It cannot be read back.");
  });
});
