import { cx } from "./cx";

/** Each dot starts a little after the one before it, which is what makes three dots a wave. */
const DELAYS = ["0ms", "150ms", "300ms"];

/**
 * Three dots rising in turn: an agent at work. The assistant shows them between its words, and a
 * ticket's activity at the live end of a running agent step.
 *
 * They loop for as long as the work goes on, like the process graph's running step (`motion.css`):
 * "still working" stays true the whole time, and a sign that holds still reads as stalled. Under
 * reduced motion they dim and brighten in place instead of rising.
 *
 * Drawn only: the words beside them say what is working. They take the colour of the text around
 * them, subtle unless a caller says otherwise.
 */
export function WorkingDots({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      data-working-dots
      className={cx("inline-flex items-center gap-1 text-icon-subtle", className)}
    >
      {DELAYS.map((delay) => (
        <span
          key={delay}
          // A delay as a class would lose to the `animation` shorthand on Tailwind's class order.
          style={{ animationDelay: delay }}
          className="size-1.5 shrink-0 animate-working-wave rounded-full bg-current motion-reduce:[animation-name:working-fade]"
        />
      ))}
    </span>
  );
}
