/**
 * What the project's primary button says and does, pulled out of `AdmissionControl.tsx` so its
 * branches can be proven against literal inputs — the same reason `../process/commitGate.ts` exists.
 *
 * The button has two jobs and picks between them: toggling admission, or — when the project
 * cannot be admitted into yet — taking the reader to the requirement standing in the way. It is
 * never disabled for being unready, because "nothing happens when I press it" is a worse answer
 * than "here is what to fix".
 *
 * Activating asks first, in a popup (decision 50): it lets the worker admit issues and spawn
 * agents, which spend money. Pausing does not ask and is not drawn as danger: it can be undone
 * with one press.
 * A request in flight is the button's loading state, not a change of label.
 */

export interface ActivateButtonInputs {
  /** The project is admitting issues right now. */
  active: boolean;
  /** `project.blockers.length === 0` — the server's own verdict on whether activation is legal. */
  ready: boolean;
}

export interface ActivateButtonState {
  label: string;
  action: "activate" | "pause" | "navigate";
  appearance: "primary" | "default";
  /** What the confirmation says the press will spend, or null when the press does not ask. */
  consequence: string | null;
}

export function activateButtonState({ active, ready }: ActivateButtonInputs): ActivateButtonState {
  // An active project's button is how you stop it, whatever the ledger says. A blocker that
  // appeared after activation is not a reason to take the pause control away.
  if (active) {
    return { label: "Pause admission", action: "pause", appearance: "default", consequence: null };
  }
  if (ready) {
    return {
      label: "Activate project",
      action: "activate",
      appearance: "primary",
      consequence:
        "Factorial starts admitting matching issues and running agents on them. Agent runs cost money.",
    };
  }
  return {
    label: "View pending configuration step",
    action: "navigate",
    appearance: "default",
    consequence: null,
  };
}
