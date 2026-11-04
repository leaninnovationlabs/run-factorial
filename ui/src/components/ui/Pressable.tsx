/**
 * A button whose face is not a label, after ADS's `Pressable` primitive: the board's metric tiles
 * carry a name and two figures, which `Button`'s fixed height cannot hold. The states come from
 * here — rest, hover, press, selected — so a hand-built face still behaves like every other
 * control; `className` lays the face out and sets nothing else. A subtle face has no edge or corner,
 * and no fill until it is hovered or selected: a row in a list that runs edge to edge, as a
 * menu's items do.
 */

import type { ButtonHTMLAttributes, Ref } from "react";
import { cx } from "./cx";

const LOOK = {
  default: {
    rest: "rounded-md border border-default bg-surface text-default hover:bg-surface-hovered active:bg-surface-pressed",
    selected:
      "rounded-md border border-selected bg-selected text-selected hover:bg-selected-hovered active:bg-selected-pressed",
  },
  subtle: {
    rest: "text-default hover:bg-neutral-subtle-hovered active:bg-neutral-subtle-pressed",
    selected: "bg-selected text-selected hover:bg-selected-hovered active:bg-selected-pressed",
  },
};

type PressableProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  appearance?: keyof typeof LOOK;
  /** A pressed toggle, read out as `aria-pressed` — or, with `aria-current`, the place itself. */
  isSelected?: boolean;
  ref?: Ref<HTMLButtonElement>;
};

export function Pressable({
  appearance = "default",
  isSelected,
  type = "button",
  className,
  ref,
  ...rest
}: PressableProps) {
  return (
    <button
      {...rest}
      ref={ref}
      type={type}
      aria-pressed={"aria-current" in rest ? undefined : isSelected}
      className={cx(
        "cursor-pointer text-left transition-colors",
        LOOK[appearance][isSelected ? "selected" : "rest"],
        className,
      )}
    />
  );
}
