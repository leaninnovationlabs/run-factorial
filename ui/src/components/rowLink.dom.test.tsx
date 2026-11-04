// @vitest-environment happy-dom
/**
 * A row's click in a DOM: a panel the row opens is portalled to the body, and React still
 * bubbles its clicks to the row, which has to tell them from its own.
 */

import { createPortal } from "react-dom";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { byText, click, type Mounted, mount } from "../testDom";
import { useRowLink } from "./rowLink";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

function Where() {
  return <output data-where>{useLocation().pathname}</output>;
}

function Row() {
  const link = useRowLink("/tickets/11");
  return (
    <table>
      <tbody>
        <tr {...link}>
          <td>#118 Retry the webhook</td>
          <td>{createPortal(<p>The issue's description</p>, document.body)}</td>
        </tr>
      </tbody>
    </table>
  );
}

describe("useRowLink", () => {
  it("follows a click on the row, and not one inside a panel it portalled out", async () => {
    view = await mount(
      <MemoryRouter initialEntries={["/projects/1"]}>
        <Where />
        <Row />
      </MemoryRouter>,
    );
    const where = () => document.querySelector("[data-where]")?.textContent;
    await click(byText(document.body, "p", "The issue's description"));
    expect(where()).toBe("/projects/1");
    await click(byText(document.body, "td", "#118 Retry the webhook"));
    expect(where()).toBe("/tickets/11");
  });
});
