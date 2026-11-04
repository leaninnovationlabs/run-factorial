/**
 * Whether a log is being followed.
 *
 * A running agent's transcript grows under the reader every few seconds, and the only reading
 * position that survives that is the foot of it. So the console sits at the foot by default and
 * leaves when the reader does: scrolling up IS the off switch, and it needs no control to find
 * first. Scrolling back down re-arms it, which is what the "jump to latest" button does in one
 * gesture.
 *
 * The slack is what makes that judgement survive a fractional scroll height — a zoomed page, a
 * sub-pixel line height — which otherwise leaves an element that is visibly at its foot a pixel
 * short of it, and the log stops following for no reason the reader can see.
 */

export const FOOT_SLACK_PX = 32;

/** The three numbers a scroll position is: enough to ask the question, and no element needed. */
export interface Viewport {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

export function atFoot(view: Viewport): boolean {
  return view.scrollHeight - view.scrollTop - view.clientHeight <= FOOT_SLACK_PX;
}
