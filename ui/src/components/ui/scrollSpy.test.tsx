// @vitest-environment happy-dom
import { act, useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type Mounted, mount, settle } from "../../testDom";
import { sectionAt, useScrollSpy } from "./scrollSpy";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
});

describe("sectionAt", () => {
  it("takes the last section whose top has passed the line", () => {
    const tops = [
      ["a", -300],
      ["b", 40],
      ["c", 400],
    ] as const;
    expect(sectionAt(tops, 50)).toBe("b");
    expect(sectionAt(tops, 400)).toBe("c");
  });

  it("gives the first section before any has reached the line, never nothing", () => {
    expect(
      sectionAt(
        [
          ["a", 100],
          ["b", 300],
        ],
        10,
      ),
    ).toBe("a");
    expect(sectionAt([], 10)).toBeNull();
  });
});

/** A region of three sections whose tops the test decides, since happy-dom lays nothing out. */
function Region({
  tops,
  onChange,
}: {
  tops: Record<string, number>;
  onChange: (key: string) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  useScrollSpy({
    root,
    keys: Object.keys(tops),
    section: (key) => document.getElementById(`section-${key}`),
    offset: 12,
    onChange,
  });
  return (
    <div ref={root} data-region style={{ overflowY: "auto" }}>
      {Object.keys(tops).map((key) => (
        <section key={key} id={`section-${key}`} />
      ))}
    </div>
  );
}

function place(tops: Record<string, number>) {
  for (const [key, top] of Object.entries(tops)) {
    const element = document.getElementById(`section-${key}`);
    if (element) element.getBoundingClientRect = () => ({ top }) as DOMRect;
  }
  const region = document.querySelector<HTMLElement>("[data-region]");
  if (region) region.getBoundingClientRect = () => ({ top: 0 }) as DOMRect;
}

describe("useScrollSpy", () => {
  it("reports the section under the reading line as the region scrolls, and only a change", async () => {
    const onChange = vi.fn();
    const tops = { a: 0, b: 200, c: 400 };
    view = await mount(<Region tops={tops} onChange={onChange} />);
    place(tops);
    const region = document.querySelector<HTMLElement>("[data-region]");

    place({ a: -250, b: -50, c: 150 });
    await act(async () => {
      region?.dispatchEvent(new Event("scroll"));
    });
    await settle(30);
    expect(onChange).toHaveBeenLastCalledWith("b");

    const calls = onChange.mock.calls.length;
    await act(async () => {
      region?.dispatchEvent(new Event("scroll"));
    });
    await settle(30);
    // The same section again says nothing new.
    expect(onChange.mock.calls.length).toBe(calls);
  });
});
