// @vitest-environment happy-dom
/**
 * The two ways a button says it cannot act, and the two ways it says it is selected. What these
 * guard is #167: a reason that lives only in a disabled button's `title` is read by nobody.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { click, type Mounted, mount } from "../../testDom";
import { Button, IconButton } from "./Button";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

const button = () => document.querySelector("button") as HTMLButtonElement;

describe("Button", () => {
  it("writes a disabled reason beside itself, links it, and ignores the press", async () => {
    const onClick = vi.fn();
    view = await mount(
      <Button disabledReason="Reopen the issue on GitHub first." onClick={onClick}>
        Re-enter
      </Button>,
    );
    // Focusable: `aria-disabled`, never the attribute that takes it out of the tab order.
    expect(button().getAttribute("aria-disabled")).toBe("true");
    expect(button().disabled).toBe(false);
    const reason = document.getElementById(button().getAttribute("aria-describedby") ?? "");
    expect(reason?.textContent).toBe("Reopen the issue on GitHub first.");
    await click(button());
    expect(onClick).not.toHaveBeenCalled();
  });

  it("keeps a caller's own description alongside the reason", async () => {
    view = await mount(
      <>
        <p id="note">Nothing is applied.</p>
        <Button aria-describedby="note" disabledReason="Nothing to reset.">
          Reset
        </Button>
      </>,
    );
    expect(button().getAttribute("aria-describedby")?.split(" ")).toContain("note");
    expect(button().getAttribute("aria-describedby")?.split(" ")).toHaveLength(2);
  });

  it("honours aria-disabled from a caller whose reason is already on the page", async () => {
    const onClick = vi.fn();
    view = await mount(
      <Button aria-disabled onClick={onClick}>
        Reset view
      </Button>,
    );
    expect(button().getAttribute("aria-disabled")).toBe("true");
    expect(button().disabled).toBe(false);
    await click(button());
    expect(onClick).not.toHaveBeenCalled();
  });

  it("says it is busy while loading, and ignores the press", async () => {
    const onClick = vi.fn();
    view = await mount(
      <Button isLoading onClick={onClick}>
        Save
      </Button>,
    );
    expect(button().getAttribute("aria-busy")).toBe("true");
    await click(button());
    expect(onClick).not.toHaveBeenCalled();
  });

  it("acts on a press when nothing stops it", async () => {
    const onClick = vi.fn();
    view = await mount(<Button onClick={onClick}>Save</Button>);
    await click(button());
    expect(onClick).toHaveBeenCalledOnce();
    expect(button().hasAttribute("aria-disabled")).toBe(false);
  });

  it("reads a selected toggle as pressed, and a selected place as current rather than both", async () => {
    view = await mount(<Button isSelected>Cost</Button>);
    expect(button().getAttribute("aria-pressed")).toBe("true");
    await view.rerender(
      <Button isSelected aria-current="page">
        3
      </Button>,
    );
    expect(button().getAttribute("aria-current")).toBe("page");
    expect(button().hasAttribute("aria-pressed")).toBe(false);
  });

  it("reads a selected disclosure as expanded, never also as pressed", async () => {
    // The armed look of an inline confirmation's trigger: what it opened is the state, not a
    // toggle being held down.
    view = await mount(
      <Button isSelected aria-expanded>
        Exit ticket
      </Button>,
    );
    expect(button().getAttribute("aria-expanded")).toBe("true");
    expect(button().hasAttribute("aria-pressed")).toBe(false);
  });
});

describe("IconButton", () => {
  it("looks unavailable and ignores a press under aria-disabled, keeping the focus", async () => {
    const onClick = vi.fn();
    view = await mount(<IconButton aria-disabled icon="send" label="Send" onClick={onClick} />);
    expect(button().disabled).toBe(false);
    await click(button());
    expect(onClick).not.toHaveBeenCalled();
  });

  it("ignores a press while loading and says it is busy", async () => {
    const onClick = vi.fn();
    view = await mount(<IconButton isLoading icon="send" label="Send" onClick={onClick} />);
    expect(button().getAttribute("aria-busy")).toBe("true");
    await click(button());
    expect(onClick).not.toHaveBeenCalled();
  });

  it("is never also pressed when it opens a disclosure", async () => {
    view = await mount(<IconButton isSelected aria-expanded icon="history" label="History" />);
    expect(button().hasAttribute("aria-pressed")).toBe(false);
  });
});
