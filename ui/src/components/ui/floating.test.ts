import { describe, expect, it } from "vitest";
import { place } from "./floating";

const VIEWPORT = { width: 1000, height: 800 };
const MENU = { width: 200, height: 120 };

describe("place", () => {
  it("opens below the control, centred on it", () => {
    const at = place({ top: 100, left: 400, width: 100, height: 32 }, MENU, VIEWPORT);
    expect(at).toEqual({ top: 138, left: 350, side: "bottom" });
  });

  it("flips above when the control sits too low for the popup to fit under it", () => {
    const at = place({ top: 740, left: 400, width: 100, height: 32 }, MENU, VIEWPORT);
    expect(at.side).toBe("top");
    expect(at.top).toBe(740 - 6 - 120);
  });

  it("stays below when neither side fits, rather than leaving the top of the window", () => {
    const at = place(
      { top: 60, left: 400, width: 100, height: 32 },
      { width: 200, height: 780 },
      VIEWPORT,
    );
    expect(at.side).toBe("bottom");
  });

  it("keeps a tooltip above its control when there is room", () => {
    const at = place({ top: 300, left: 400, width: 100, height: 32 }, MENU, VIEWPORT, "top");
    expect(at.side).toBe("top");
  });

  it("aligns to the control's start or end edge when asked", () => {
    const anchor = { top: 100, left: 400, width: 100, height: 32 };
    expect(place(anchor, MENU, VIEWPORT, "bottom-start").left).toBe(400);
    expect(place(anchor, MENU, VIEWPORT, "bottom-end").left).toBe(300);
  });

  it("slides along its edge to stay inside the window", () => {
    expect(place({ top: 100, left: 5, width: 20, height: 32 }, MENU, VIEWPORT).left).toBe(8);
    expect(place({ top: 100, left: 980, width: 20, height: 32 }, MENU, VIEWPORT).left).toBe(792);
  });

  it("sits beside the control, centred on it, for a name that belongs next to an icon", () => {
    const tip = { width: 80, height: 24 };
    const at = place({ top: 100, left: 8, width: 40, height: 40 }, tip, VIEWPORT, "right");
    expect(at).toEqual({ top: 108, left: 54, side: "right" });
  });

  it("flips to the other side when the preferred one has no room", () => {
    const tip = { width: 80, height: 24 };
    expect(place({ top: 100, left: 950, width: 40, height: 40 }, tip, VIEWPORT, "right").side).toBe(
      "left",
    );
    expect(place({ top: 100, left: 20, width: 40, height: 40 }, tip, VIEWPORT, "left").side).toBe(
      "right",
    );
  });
});
