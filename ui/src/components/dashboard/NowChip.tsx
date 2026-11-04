import { Lozenge } from "../ui";

/** Marks a figure that counts what stands at `as_of` rather than what happened in the window. */
export function NowChip() {
  return <Lozenge appearance="information">Now</Lozenge>;
}
