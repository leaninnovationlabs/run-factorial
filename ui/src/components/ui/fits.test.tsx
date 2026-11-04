// @vitest-environment happy-dom
/**
 * `useFitsInRow` against a measured copy: the answer follows the copy's width against the row's,
 * and a row with no width yet (hidden, or not laid out) changes nothing.
 */

import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type Mounted, mount } from "../../testDom";
import { useFitsInRow } from "./fits";

let view: Mounted | null = null;
let observed: { fire: () => void; targets: Element[] }[] = [];

class RecordingResizeObserver {
  private readonly own: { fire: () => void; targets: Element[] };
  constructor(callback: ResizeObserverCallback) {
    this.own = { fire: () => callback([], this as unknown as ResizeObserver), targets: [] };
    observed.push(this.own);
  }
  observe(target: Element) {
    this.own.targets.push(target);
  }
  unobserve() {}
  disconnect() {
    this.own.targets = [];
  }
}

beforeEach(() => {
  observed = [];
  vi.stubGlobal("ResizeObserver", RecordingResizeObserver);
});

afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.unstubAllGlobals();
});

function Probe() {
  const { row, measure, fits } = useFitsInRow("a|b");
  return (
    <div ref={row} data-probe="row">
      <div ref={measure} data-probe="measure" />
      <output>{fits ? "fits" : "does not fit"}</output>
    </div>
  );
}

async function resize(row: number, copy: number) {
  const box = document.querySelector('[data-probe="row"]') as HTMLElement;
  const measured = document.querySelector('[data-probe="measure"]') as HTMLElement;
  Object.defineProperty(box, "clientWidth", { configurable: true, value: row });
  Object.defineProperty(measured, "scrollWidth", { configurable: true, value: copy });
  await act(async () => {
    for (const observer of observed) if (observer.targets.length > 0) observer.fire();
  });
}

const answer = () => document.querySelector("output")?.textContent;

/** A row drawn only once something else has answered, with the labels already known. */
function Late({ shown }: { shown: boolean }) {
  const { row, measure, fits } = useFitsInRow("a|b");
  return (
    <>
      {shown && (
        <div ref={row} data-probe="row">
          <div ref={measure} data-probe="measure" />
        </div>
      )}
      <output>{fits ? "fits" : "does not fit"}</output>
    </>
  );
}

describe("useFitsInRow", () => {
  it("fits until the measured row is wider than its box, and again once it is not", async () => {
    view = await mount(<Probe />);
    expect(answer()).toBe("fits");

    await resize(300, 200);
    expect(answer()).toBe("fits");
    await resize(300, 400);
    expect(answer()).toBe("does not fit");
    await resize(500, 400);
    expect(answer()).toBe("fits");
  });

  it("keeps its answer while the box is 0 px wide", async () => {
    view = await mount(<Probe />);
    await resize(300, 400);
    expect(answer()).toBe("does not fit");

    await resize(0, 400);
    expect(answer()).toBe("does not fit");
  });

  it("measures a row that appears after its labels were known", async () => {
    view = await mount(<Late shown={false} />);
    await view.rerender(<Late shown />);

    await resize(300, 400);

    expect(answer()).toBe("does not fit");
  });

  it("measures again when the copy itself grows, as when a font loads", async () => {
    view = await mount(<Probe />);
    await resize(300, 200);
    expect(answer()).toBe("fits");

    const measured = document.querySelector('[data-probe="measure"]') as HTMLElement;
    Object.defineProperty(measured, "scrollWidth", { configurable: true, value: 400 });
    await act(async () => {
      for (const observer of observed) if (observer.targets.includes(measured)) observer.fire();
    });

    expect(answer()).toBe("does not fit");
  });
});
