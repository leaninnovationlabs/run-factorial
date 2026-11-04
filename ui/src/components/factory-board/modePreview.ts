/**
 * Hover intent for the mode bar.
 *
 * Rearranging on hover is only pleasant if a cursor crossing the bar on its way elsewhere never
 * triggers it. The CSS transform transition is already interruption-safe — retarget it mid-flight
 * and it eases from wherever it is — so a sweep would not *pop*; it would strobe, five competing
 * layouts fighting while whole bands re-rank underneath. The fix therefore belongs in when the
 * mode changes, not in the animation.
 *
 * A single symmetric delay is the wrong shape: long enough to reject a sweep is also long enough
 * to make deliberate button-to-button comparison feel sticky. So:
 *
 *   - the enter delay only ARMS the bar. A cursor passing through never rests anywhere near long
 *     enough, so nothing fires;
 *   - once armed, switching buttons is instant — intent has been demonstrated already;
 *   - the exit grace is scoped to the whole bar, so the gaps between buttons do not flicker;
 *   - a click bypasses all of it, and leaving falls back to the locked mode rather than to idle.
 *
 * Kept free of React so it can be tested with fake timers in this project's `node` test
 * environment — there is no DOM here to render a hook into. `ModeBar` binds it with
 * `useSyncExternalStore`, which is why `getState` returns a cached snapshot.
 */

import type { Mode } from "./model";

/** Long enough that a crossing cursor never arms the bar, short enough not to feel laggy. */
export const ENTER_DELAY_MS = 140;

/** Grace after the cursor leaves, so a wobble at the edge does not drop the view. */
export const EXIT_DELAY_MS = 90;

export interface ModePreviewState {
  /** The mode a click pinned, or `idle`. Survives the cursor leaving. */
  locked: Mode;
  /** What the cursor or keyboard is previewing right now, if anything. */
  preview: Mode | null;
  /** What the board should draw: `preview ?? locked`. */
  effective: Mode;
}

export interface ModePreview {
  getState(): ModePreviewState;
  subscribe(listener: () => void): () => void;
  /** Pointer entered a button. */
  hover(mode: Mode): void;
  /** Pointer left a button but may still be within the bar. Cancels an arm that has not fired. */
  unhover(): void;
  enterBar(): void;
  leaveBar(): void;
  focus(mode: Mode): void;
  blur(): void;
  /** Click or Enter: apply and pin immediately, or unpin if this mode is already locked. */
  select(mode: Mode): void;
  /** Pin exactly this mode, without the toggle `select` does: the address bar saying what is
   *  pinned, not a press on a button. */
  lock(mode: Mode): void;
  /**
   * Drop the pinned mode and any preview, from OUTSIDE the bar.
   *
   * Not `select(locked)`: that unpins by toggling, but it also arms, because a click on a mode
   * button is a demonstration of intent to compare modes. A click on a clear control elsewhere
   * on the rail demonstrates the opposite, so this leaves the bar disarmed.
   */
  clear(): void;
  dispose(): void;
}

export function createModePreview({ locked: initial = "idle" as Mode } = {}): ModePreview {
  let locked: Mode = initial;
  // Pointer and keyboard are tracked apart so they cannot fight: a blur must restore whatever the
  // pointer was showing rather than blanking the board.
  let pointerMode: Mode | null = null;
  let focusMode: Mode | null = null;
  let armed = false;
  let enterTimer: ReturnType<typeof setTimeout> | null = null;
  let exitTimer: ReturnType<typeof setTimeout> | null = null;

  const listeners = new Set<() => void>();
  let snapshot: ModePreviewState = { locked, preview: null, effective: locked };

  function clearEnter() {
    if (enterTimer !== null) clearTimeout(enterTimer);
    enterTimer = null;
  }

  function clearExit() {
    if (exitTimer !== null) clearTimeout(exitTimer);
    exitTimer = null;
  }

  /** Rebuild the snapshot, and wake React only if something a renderer can see moved. */
  function publish() {
    const preview = focusMode ?? pointerMode;
    const effective = preview ?? locked;
    if (
      snapshot.locked === locked &&
      snapshot.preview === preview &&
      snapshot.effective === effective
    ) {
      return;
    }
    snapshot = { locked, preview, effective };
    for (const listener of listeners) listener();
  }

  return {
    getState: () => snapshot,

    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },

    hover(mode) {
      clearExit();
      if (armed) {
        pointerMode = mode;
        publish();
        return;
      }
      clearEnter();
      enterTimer = setTimeout(() => {
        enterTimer = null;
        armed = true;
        pointerMode = mode;
        publish();
      }, ENTER_DELAY_MS);
    },

    unhover() {
      // Only the pending arm is cancelled. An armed preview survives, because leaving a button
      // for the padding beside it is not leaving the bar.
      clearEnter();
    },

    enterBar() {
      clearExit();
    },

    leaveBar() {
      clearEnter();
      clearExit();
      exitTimer = setTimeout(() => {
        exitTimer = null;
        armed = false;
        pointerMode = null;
        publish();
      }, EXIT_DELAY_MS);
    },

    focus(mode) {
      // Keyboard focus is deliberate by construction — there is no sweep to guard against — so
      // it previews at once, and arms the bar for whatever the pointer does next.
      clearEnter();
      clearExit();
      armed = true;
      focusMode = mode;
      publish();
    },

    blur() {
      focusMode = null;
      publish();
    },

    select(mode) {
      clearEnter();
      clearExit();
      armed = true;
      pointerMode = null;
      focusMode = null;
      locked = locked === mode ? "idle" : mode;
      publish();
    },

    lock(mode) {
      locked = mode;
      publish();
    },

    clear() {
      clearEnter();
      clearExit();
      armed = false;
      pointerMode = null;
      focusMode = null;
      locked = "idle";
      publish();
    },

    dispose() {
      clearEnter();
      clearExit();
      listeners.clear();
    },
  };
}
