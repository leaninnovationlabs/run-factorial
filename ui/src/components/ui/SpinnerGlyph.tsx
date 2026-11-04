import { cx } from "./cx";

/** The spinning arc on its own, for a button's loading state and the page `Spinner`. Motion stops
 *  under reduced motion; the arc alone still reads as "working". */
export function SpinnerGlyph({ size = "medium" }: { size?: "small" | "medium" | "large" }) {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden="true"
      className={cx(
        "shrink-0 animate-spin motion-reduce:animate-none",
        size === "small" ? "size-3" : size === "large" ? "size-6" : "size-4",
      )}
    >
      <circle
        cx="8"
        cy="8"
        r="6"
        fill="none"
        stroke="currentColor"
        strokeOpacity="0.25"
        strokeWidth="2"
      />
      <path
        d="M14 8a6 6 0 0 0-6-6"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}
