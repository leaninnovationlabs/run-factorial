/**
 * The assistant at work between words: three dots in its column while its turn is live and no
 * text is arriving — queued, between tool calls, or after a paragraph it has finished. The dots'
 * motion lives in `assistant.css`, with a pulse in place for reduced motion.
 *
 * Drawn only: a status region that arrives already filled is seldom read out, so the words are
 * the stream's own region, there from the start (`ItemStream`).
 */

export function TypingIndicator() {
  return (
    <div aria-hidden="true" className="flex h-6 items-center gap-1">
      <span className="assist-typing-dot" />
      <span className="assist-typing-dot" />
      <span className="assist-typing-dot" />
    </div>
  );
}
