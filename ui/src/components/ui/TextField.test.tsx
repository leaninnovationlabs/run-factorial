// @vitest-environment happy-dom
/** `Select`'s group entries: each is one `<optgroup>`, so two groups that share a name stay two. */

import { afterEach, describe, expect, it } from "vitest";
import { type Mounted, mount } from "../../testDom";
import { Select } from "./TextField";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

describe("Select", () => {
  it("draws a group entry as one optgroup", async () => {
    view = await mount(
      <Select
        label="Files"
        options={[
          { value: "a", label: "A" },
          {
            group: "Reports",
            options: [
              { value: "b", label: "b.html" },
              { value: "c", label: "c.html" },
            ],
          },
          { group: "Reports", options: [{ value: "d", label: "d.md" }] },
        ]}
      />,
    );

    const groups = [...document.querySelectorAll('optgroup[label="Reports"]')];
    expect(
      groups.map((group) => [...group.querySelectorAll("option")].map((o) => o.value)),
    ).toEqual([["b", "c"], ["d"]]);
    expect(document.querySelector('option[value="a"]')?.closest("optgroup")).toBeNull();
  });
});
