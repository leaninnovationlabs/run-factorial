/**
 * `wrapLabel` — how a node's name fits between its neighbours.
 *
 * The rule the graph depends on is that no line is wider than the gap to the next node's label,
 * because two centred labels grow towards each other and overlap in the middle. What is worth
 * pinning is the boundary behaviour: where a name folds, that the fold keeps the separator it
 * broke on, and that a name too long for two lines is cut rather than allowed a third — a third
 * line would sit where the attempt count goes.
 */

import { describe, expect, it } from "vitest";
import { wrapLabel } from "./geometry";

/** What every line has to satisfy, whatever the input. */
const MAX = 15;

describe("wrapLabel", () => {
  it("leaves a name that already fits alone", () => {
    expect(wrapLabel("plan")).toEqual(["plan"]);
    expect(wrapLabel("code_review")).toEqual(["code_review"]);
  });

  it("folds a long name and keeps the separator on the line it ended", () => {
    // `spec_review_gate` is 16 characters — one over — and the underscore belongs to the half
    // before the fold, or the reader sees two words that were never two words.
    expect(wrapLabel("spec_review_gate")).toEqual(["spec_review_", "gate"]);
  });

  it("breaks on dashes and spaces too", () => {
    expect(wrapLabel("acceptance-diff-guard")).toEqual(["acceptance-", "diff-guard"]);
    expect(wrapLabel("wait for the build")).toEqual(["wait for the", "build"]);
  });

  it("cuts rather than taking a third line", () => {
    const lines = wrapLabel("spec_review_gate_rework_and_resubmit");
    expect(lines).toHaveLength(2);
    expect(lines[1]?.endsWith("…")).toBe(true);
  });

  it("cuts a single token that has no break to take", () => {
    // Nowhere to fold, so wrapping cannot help and the one line is cut to fit.
    expect(wrapLabel("averyverylongstepnamewithnobreaks")).toEqual(["averyverylongs…"]);
  });

  it("never returns a line wider than the gap to the next node", () => {
    for (const label of [
      "plan",
      "spec_review_gate",
      "acceptance-diff-guard",
      "spec_review_gate_rework_and_resubmit",
      "averyverylongstepnamewithnobreaks",
      "a_b_c_d_e_f_g_h_i_j_k_l_m_n_o_p",
    ]) {
      for (const line of wrapLabel(label)) {
        expect(line.length).toBeLessThanOrEqual(MAX);
      }
    }
  });
});
