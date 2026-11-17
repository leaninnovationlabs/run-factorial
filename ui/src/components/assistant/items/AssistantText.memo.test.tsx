// @vitest-environment happy-dom
/** A reply's markdown is parsed once for its text, however often what is around it is drawn. */

import { useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { click, type Mounted, mount } from "../../../testDom";

const parsed = vi.hoisted(() => ({ count: 0 }));
vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => {
    parsed.count += 1;
    return <p>{children}</p>;
  },
}));

import { AssistantText } from "./AssistantText";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
});

function Typing() {
  const [keys, setKeys] = useState(0);
  return (
    <>
      <button type="button" onClick={() => setKeys(keys + 1)}>
        Type a key ({keys})
      </button>
      <AssistantText text="Two tickets are blocked." />
    </>
  );
}

test("a draw of what is around a reply does not parse the reply again", async () => {
  view = await mount(<Typing />);
  expect(parsed.count).toBe(1);
  await click(view.container.querySelector("button"));
  await click(view.container.querySelector("button"));
  expect(view.container.textContent).toContain("Type a key (2)");
  expect(parsed.count).toBe(1);
});
