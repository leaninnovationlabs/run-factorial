import { describe, expect, it } from "vitest";
import { activateButtonState } from "./activateButton";

const CONFIGURED = { active: false, ready: true };

describe("activateButtonState", () => {
  it("offers to activate a configured project, and asks first: agent runs cost money", () => {
    expect(activateButtonState(CONFIGURED)).toEqual({
      label: "Activate project",
      action: "activate",
      appearance: "primary",
      consequence:
        "Factorial starts admitting matching issues and running agents on them. Agent runs cost money.",
    });
  });

  it("offers to pause a running one, as an ordinary action: pausing can be undone", () => {
    const state = activateButtonState({ ...CONFIGURED, active: true });
    expect(state.label).toBe("Pause admission");
    expect(state.action).toBe("pause");
    expect(state.appearance).toBe("default");
    expect(state.consequence).toBeNull();
  });

  it("keeps the pause control on a running project that has since picked up a blocker", () => {
    // A blocker appearing after activation is not a reason to take away the way to stop it.
    const state = activateButtonState({ active: true, ready: false });
    expect(state.label).toBe("Pause admission");
    expect(state.action).toBe("pause");
  });

  it("sends the reader to the work rather than sitting disabled", () => {
    const state = activateButtonState({ ...CONFIGURED, ready: false });
    expect(state.label).toBe("View pending configuration step");
    expect(state.action).toBe("navigate");
    expect(state.consequence).toBeNull();
  });
});
