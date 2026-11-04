// @vitest-environment happy-dom
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { safeGet, safeSet, usePersistentState } from "./storage";
import { type Mounted, mount } from "./testDom";

let view: Mounted | null = null;
afterEach(async () => {
  await view?.unmount();
  view = null;
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("safeGet and safeSet", () => {
  it("read back what was written, and remove on null", () => {
    safeSet("k", "v");
    expect(safeGet("k")).toBe("v");
    safeSet("k", null);
    expect(safeGet("k")).toBeNull();
  });

  it("carry on when the browser refuses storage, rather than taking the page down", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    expect(safeGet("k")).toBeNull();
    expect(() => safeSet("k", "v")).not.toThrow();
  });
});

describe("usePersistentState", () => {
  function Counter() {
    const [count, setCount] = usePersistentState(
      "count",
      (stored) => Number(stored ?? "0"),
      String,
    );
    return (
      <button type="button" onClick={() => setCount((n) => n + 1)}>
        {count}
      </button>
    );
  }

  it("starts from what was stored and writes every change back", async () => {
    window.localStorage.setItem("count", "4");
    view = await mount(<Counter />);
    const button = view.container.querySelector("button");
    expect(button?.textContent).toBe("4");
    await act(async () => button?.click());
    expect(button?.textContent).toBe("5");
    expect(window.localStorage.getItem("count")).toBe("5");
  });
});
