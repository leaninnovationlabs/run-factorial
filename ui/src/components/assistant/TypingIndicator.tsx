/**
 * The assistant at work between words: three dots in its column while its turn is live and no
 * text is arriving — queued, between tool calls, or after a paragraph it has finished.
 *
 * Drawn only: a status region that arrives already filled is seldom read out, so the words are
 * the stream's own region, there from the start (`ItemStream`).
 */

import { WorkingDots } from "../ui";

export function TypingIndicator() {
  return (
    <div className="flex h-6 items-center">
      <WorkingDots />
    </div>
  );
}
