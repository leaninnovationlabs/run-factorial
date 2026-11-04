// @vitest-environment happy-dom
/** The provider's Show me hand-off, without the Drawer: where it goes, and when it lets go. */

import { Link } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { byText, click, type Mounted, settle } from "../../testDom";
import { Button } from "../ui";
import { DashboardLink, useAssistant } from "./AssistantProvider";
import { mountInAssistant } from "./testFakes";

let view: Mounted | null = null;

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(async () => {
  await view?.unmount();
  view = null;
});

function ShowConfigurationCheck() {
  const { showMe } = useAssistant();
  return (
    <>
      <Button
        onClick={() =>
          showMe({ path: "/projects/3/configuration", anchor: "configuration.check", note: null })
        }
      >
        Point
      </Button>
      <Link to="/runtimes">Runtimes</Link>
    </>
  );
}

describe("AssistantProvider", () => {
  test("Show me opens the card's page, and a page the person goes to themselves ends it", async () => {
    const mounted = await mountInAssistant(<ShowConfigurationCheck />, {
      path: "/projects/:projectId/configuration",
      page: <div data-assist-anchor="configuration.check">Check</div>,
    });
    view = mounted.view;
    await click(byText(view.container, "button", "Point"));
    // The page arrives after the pointing starts; the control is looked for once a frame.
    await settle(20);
    expect(mounted.where()).toBe("/projects/3/configuration");
    expect(document.querySelector("[data-assist-shown]")?.textContent).toBe("Check");
    expect(byText(document.body, "button", "Back to chat")).not.toBeNull();

    await click(byText(view.container, "a", "Runtimes"));
    expect(mounted.where()).toBe("/runtimes");
    expect(byText(document.body, "button", "Back to chat")).toBeNull();
  });

  test("a dashboard link in the Drawer is followed by the router, never a reload", async () => {
    const mounted = await mountInAssistant(<DashboardLink to="/tickets/212">#212</DashboardLink>);
    view = mounted.view;
    const link = byText(view.container, "a", "#212");
    expect(link?.className).toContain("underline");
    await click(link);
    expect(mounted.where()).toBe("/tickets/212");
  });
});
