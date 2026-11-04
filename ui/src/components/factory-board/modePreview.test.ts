import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createModePreview, ENTER_DELAY_MS, EXIT_DELAY_MS } from "./modePreview";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("createModePreview", () => {
  it("ignores a cursor sweeping across the bar on its way somewhere else", () => {
    const bar = createModePreview();
    bar.enterBar();
    // Three buttons crossed, none of them rested on. This is the whole point of the delay.
    for (const mode of ["shade_time", "shade_cost", "shade_tokens"] as const) {
      bar.hover(mode);
      vi.advanceTimersByTime(ENTER_DELAY_MS / 4);
      bar.unhover();
    }
    bar.leaveBar();
    vi.advanceTimersByTime(ENTER_DELAY_MS * 4);
    expect(bar.getState().effective).toBe("idle");
  });

  it("previews a button the cursor rests on", () => {
    const bar = createModePreview();
    bar.enterBar();
    bar.hover("shade_cost");
    expect(bar.getState().effective).toBe("idle");
    vi.advanceTimersByTime(ENTER_DELAY_MS);
    expect(bar.getState().effective).toBe("shade_cost");
  });

  it("switches instantly between buttons once the bar is armed", () => {
    const bar = createModePreview();
    bar.enterBar();
    bar.hover("shade_cost");
    vi.advanceTimersByTime(ENTER_DELAY_MS);

    // Re-charging the delay at every button is what would make comparison feel sluggish; intent
    // has already been demonstrated, so the second button is immediate.
    bar.hover("shade_time");
    expect(bar.getState().effective).toBe("shade_time");
  });

  it("holds the preview across the gap between two buttons", () => {
    const bar = createModePreview();
    bar.enterBar();
    bar.hover("shade_cost");
    vi.advanceTimersByTime(ENTER_DELAY_MS);
    // Off the button but still inside the bar: the grace is bar-scoped, so this must not flicker.
    bar.unhover();
    vi.advanceTimersByTime(EXIT_DELAY_MS * 4);
    expect(bar.getState().effective).toBe("shade_cost");
  });

  it("waits out the grace period before reverting when the cursor leaves the bar", () => {
    const bar = createModePreview();
    bar.enterBar();
    bar.hover("shade_cost");
    vi.advanceTimersByTime(ENTER_DELAY_MS);
    bar.leaveBar();
    vi.advanceTimersByTime(EXIT_DELAY_MS - 1);
    expect(bar.getState().effective).toBe("shade_cost");
    vi.advanceTimersByTime(1);
    expect(bar.getState().effective).toBe("idle");
  });

  it("cancels the revert when the cursor comes back inside the grace period", () => {
    const bar = createModePreview();
    bar.enterBar();
    bar.hover("shade_cost");
    vi.advanceTimersByTime(ENTER_DELAY_MS);
    bar.leaveBar();
    vi.advanceTimersByTime(EXIT_DELAY_MS - 1);
    bar.enterBar();
    vi.advanceTimersByTime(EXIT_DELAY_MS * 4);
    expect(bar.getState().effective).toBe("shade_cost");
  });

  it("applies a click immediately, with no delay to wait out", () => {
    const bar = createModePreview();
    bar.select("shade_cost");
    expect(bar.getState()).toMatchObject({ locked: "shade_cost", effective: "shade_cost" });
  });

  it("clears a pending hover when the click lands first", () => {
    const bar = createModePreview();
    bar.enterBar();
    bar.hover("shade_cost");
    bar.select("shade_retry");
    vi.advanceTimersByTime(ENTER_DELAY_MS * 4);
    expect(bar.getState().effective).toBe("shade_retry");
  });

  it("returns to idle when the locked button is clicked again", () => {
    const bar = createModePreview();
    bar.select("shade_cost");
    bar.select("shade_cost");
    expect(bar.getState().locked).toBe("idle");
  });

  it("falls back to the locked mode, not to idle, when the cursor leaves", () => {
    const bar = createModePreview();
    bar.select("shade_retry");
    bar.enterBar();
    bar.hover("shade_cost");
    vi.advanceTimersByTime(ENTER_DELAY_MS);
    expect(bar.getState().effective).toBe("shade_cost");
    bar.leaveBar();
    vi.advanceTimersByTime(EXIT_DELAY_MS);
    expect(bar.getState().effective).toBe("shade_retry");
  });

  it("previews on keyboard focus with no delay, since there is no sweep to guard against", () => {
    const bar = createModePreview();
    bar.focus("shade_tokens");
    expect(bar.getState().effective).toBe("shade_tokens");
    bar.blur();
    expect(bar.getState().effective).toBe("idle");
  });

  it("lets keyboard focus win over the pointer while both are somewhere in the bar", () => {
    const bar = createModePreview();
    bar.enterBar();
    bar.hover("shade_cost");
    vi.advanceTimersByTime(ENTER_DELAY_MS);
    bar.focus("shade_retry");
    expect(bar.getState().effective).toBe("shade_retry");
    bar.blur();
    expect(bar.getState().effective).toBe("shade_cost");
  });

  it("notifies subscribers only when the effective mode actually changes", () => {
    const bar = createModePreview();
    const seen = vi.fn();
    bar.subscribe(seen);
    bar.enterBar();
    bar.hover("shade_cost");
    vi.advanceTimersByTime(ENTER_DELAY_MS);
    expect(seen).toHaveBeenCalledTimes(1);
    // Same button again: nothing has changed, so React must not be woken.
    bar.hover("shade_cost");
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it("hands back a stable snapshot so useSyncExternalStore does not loop", () => {
    const bar = createModePreview();
    expect(bar.getState()).toBe(bar.getState());
  });

  it("drops its timers on dispose", () => {
    const bar = createModePreview();
    bar.enterBar();
    bar.hover("shade_cost");
    bar.dispose();
    vi.advanceTimersByTime(ENTER_DELAY_MS * 4);
    expect(bar.getState().effective).toBe("idle");
  });
});

describe("clear", () => {
  it("unpins the locked mode", () => {
    const preview = createModePreview();
    preview.select("shade_cost");
    expect(preview.getState().effective).toBe("shade_cost");
    preview.clear();
    expect(preview.getState()).toMatchObject({ locked: "idle", preview: null, effective: "idle" });
    preview.dispose();
  });

  it("leaves the bar disarmed, unlike unpinning by clicking the button again", () => {
    // `select` arms, because clicking a mode button is a demonstration of intent to compare
    // modes. Clearing from a control elsewhere on the rail demonstrates the opposite, so the
    // next hover has to serve out the full enter delay again.
    const preview = createModePreview();
    preview.select("shade_cost");
    preview.clear();
    preview.hover("shade_time");
    expect(preview.getState().effective).toBe("idle");
    preview.dispose();
  });

  it("drops a live preview too, so nothing survives the clear", () => {
    const preview = createModePreview();
    preview.focus("shade_retry");
    expect(preview.getState().effective).toBe("shade_retry");
    preview.clear();
    expect(preview.getState().effective).toBe("idle");
    preview.dispose();
  });
});

describe("a mode pinned from outside the bar", () => {
  it("starts pinned on the mode it is given, so a linked view opens shaded", () => {
    const preview = createModePreview({ locked: "shade_cost" });
    expect(preview.getState()).toEqual({
      locked: "shade_cost",
      preview: null,
      effective: "shade_cost",
    });
    preview.dispose();
  });

  it("pins exactly what `lock` names, where `select` on the same mode would unpin it", () => {
    const preview = createModePreview({ locked: "shade_cost" });
    preview.lock("shade_cost");
    expect(preview.getState().locked).toBe("shade_cost");
    preview.lock("shade_time");
    expect(preview.getState().locked).toBe("shade_time");
    preview.lock("idle");
    expect(preview.getState().effective).toBe("idle");
    preview.dispose();
  });

  it("leaves a preview showing over the new pin, since the pointer is still on its button", () => {
    const preview = createModePreview();
    preview.focus("shade_retry");
    preview.lock("shade_cost");
    expect(preview.getState()).toMatchObject({ locked: "shade_cost", effective: "shade_retry" });
    preview.blur();
    expect(preview.getState().effective).toBe("shade_cost");
    preview.dispose();
  });
});
