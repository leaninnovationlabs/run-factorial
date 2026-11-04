/**
 * Where a popup or tooltip goes, given the control it belongs to. Pure, so it is tested without a
 * browser: the component measures, this decides.
 *
 * A popup opens on its preferred side and flips to the opposite one when it would not fit there,
 * then slides along its edge to stay inside the viewport's margin. ADS positions its own with
 * Popper; one flip and one clamp is the whole of what these menus and tooltips need (decision 50).
 * `left` and `right` are for the compact side navigation, whose names sit beside its icons, and
 * `right-start` for a submenu, whose top lines up with the item that opens it.
 */

export type Side = "top" | "bottom" | "left" | "right";
export type Align = "start" | "center" | "end";
export type Placement =
  | Side
  | `${"top" | "bottom"}-${Exclude<Align, "center">}`
  | `${"left" | "right"}-start`;

export type Box = { top: number; left: number; width: number; height: number };

const MARGIN = 8;

const OPPOSITE: Record<Side, Side> = { top: "bottom", bottom: "top", left: "right", right: "left" };

const clamp = (value: number, size: number, extent: number) =>
  Math.min(Math.max(value, MARGIN), Math.max(MARGIN, extent - MARGIN - size));

export function place(
  anchor: Box,
  floating: { width: number; height: number },
  viewport: { width: number; height: number },
  placement: Placement = "bottom",
  gap = 6,
): { top: number; left: number; side: Side } {
  const [preferred, align = "center"] = placement.split("-") as [Side, Align | undefined];
  const at: Record<Side, number> = {
    top: anchor.top - gap - floating.height,
    bottom: anchor.top + anchor.height + gap,
    left: anchor.left - gap - floating.width,
    right: anchor.left + anchor.width + gap,
  };
  const fits: Record<Side, boolean> = {
    top: at.top >= MARGIN,
    bottom: at.bottom + floating.height <= viewport.height - MARGIN,
    left: at.left >= MARGIN,
    right: at.right + floating.width <= viewport.width - MARGIN,
  };
  const side = fits[preferred] || !fits[OPPOSITE[preferred]] ? preferred : OPPOSITE[preferred];

  if (side === "left" || side === "right") {
    const top =
      align === "start" ? anchor.top : anchor.top + anchor.height / 2 - floating.height / 2;
    return { top: clamp(top, floating.height, viewport.height), left: at[side], side };
  }
  const left =
    align === "start"
      ? anchor.left
      : align === "end"
        ? anchor.left + anchor.width - floating.width
        : anchor.left + anchor.width / 2 - floating.width / 2;
  return { top: at[side], left: clamp(left, floating.width, viewport.width), side };
}
