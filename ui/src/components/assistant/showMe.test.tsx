// @vitest-environment happy-dom
/** Show me on the page it led to: the address, and letting go of the control. */

import { act } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { click, type Mounted, mount, settle } from "../../testDom";
import { ShowMeSpotlight, showMeHref } from "./showMe";

let view: Mounted | null = null;

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(async () => {
  await view?.unmount();
  view = null;
});

describe("showMeHref", () => {
  test("is the card's page", () => {
    expect(showMeHref({ path: "/tickets/212", anchor: null, note: null })).toBe("/tickets/212");
    expect(showMeHref({ path: "/projects/3/configuration", anchor: null, note: null })).toBe(
      "/projects/3/configuration",
    );
  });
});

describe("ShowMeSpotlight", () => {
  test("lets go of the control when it unmounts, and points at no control it does not know", async () => {
    const anchor = document.createElement("div");
    anchor.dataset.assistAnchor = "ticket.graph";
    document.body.append(anchor);
    const onDismiss = vi.fn();
    view = await mount(
      <ShowMeSpotlight
        card={{ path: "/tickets/1", anchor: "ticket.graph", note: "The graph" }}
        onBack={() => {}}
        onDismiss={onDismiss}
      />,
    );
    expect(anchor.hasAttribute("data-assist-shown")).toBe(true);
    await click(document.querySelector('[aria-label="Stop showing"]'));
    expect(onDismiss).toHaveBeenCalledTimes(1);
    await view.unmount();
    view = null;
    expect(anchor.hasAttribute("data-assist-shown")).toBe(false);

    const stray = document.createElement("div");
    stray.dataset.assistAnchor = "admin.secrets";
    document.body.append(stray);
    view = await mount(
      <ShowMeSpotlight
        card={{ path: "/tickets/1", anchor: "admin.secrets", note: "Here" }}
        onBack={() => {}}
        onDismiss={() => {}}
      />,
    );
    expect(stray.hasAttribute("data-assist-shown")).toBe(false);
    expect(document.querySelector('[role="note"]')?.textContent).toBe("Here");
  });

  test("looks for the control at most once a frame, however often the page changes", async () => {
    const lookups = vi.spyOn(document, "querySelectorAll");
    view = await mount(
      <ShowMeSpotlight
        card={{ path: "/overview", anchor: "overview.board", note: null }}
        onBack={() => {}}
        onDismiss={() => {}}
      />,
    );
    const looked = () =>
      lookups.mock.calls.filter(([selector]) => String(selector).includes("overview.board")).length;
    const before = looked();
    await act(async () => {
      // A board that polls: twenty changes, each in a task of its own, inside one frame.
      for (let change = 0; change < 20; change += 1) {
        document.body.append(document.createElement("span"));
        await Promise.resolve();
      }
    });
    await settle(50);
    expect(looked() - before).toBeLessThanOrEqual(2);
    expect(looked() - before).toBeGreaterThanOrEqual(1);
  });
});
