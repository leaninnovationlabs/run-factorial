import { Lozenge } from "../ui";

/** The admission switch's state. Not a ticket status: a project is not "running", it is admitting
 *  issues or not, so it takes success and neutral rather than the ticket glossary. */
export function AdmissionLozenge({ active, isQuiet }: { active: boolean; isQuiet?: boolean }) {
  return (
    <Lozenge appearance={active ? "success" : "neutral"} isQuiet={isQuiet}>
      {active ? "Active" : "Paused"}
    </Lozenge>
  );
}
