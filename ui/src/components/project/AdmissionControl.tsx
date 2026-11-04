/**
 * The project's primary action, on its dashboard and on its Project settings overview: Activate,
 * Pause admission, or — while a requirement stands in the way — the way to that requirement. The
 * branches are `activateButtonState`'s; this draws them and runs the write.
 */

import { useNavigate } from "react-router-dom";
import { useSetProjectActive } from "../../api/queries";
import type { ProjectDetail } from "../../api/types";
import { activateButtonState } from "../configuration/activateButton";
import { blockingRows, sectionForKey } from "../configuration/readiness";
import { Button, ConfirmPopup, ErrorMessage, useActionFeedback } from "../ui";
import { configurationHref, projectTicketsHref } from "./place";

/** Where the dashboard's button and the configuration banner send the reader: the page of the
 *  first requirement that stops an activation, or the process config when the server's blocker
 *  names no row. */
export function firstBlockerHref(project: ProjectDetail): string {
  const first = blockingRows(project)[0];
  const section = first ? sectionForKey(first.key) : null;
  return configurationHref(project.id, section?.slug ?? "process");
}

export function AdmissionControl({ project }: { project: ProjectDetail }) {
  const navigate = useNavigate();
  const setActive = useSetProjectActive(project.id);
  useActionFeedback(setActive, {
    errorTitle: "Couldn't change admission",
    successTitle: (result) =>
      result.active
        ? "Project activated. Matching issues are admitted shortly."
        : "Admission paused",
  });
  const admission = activateButtonState({
    active: project.active,
    ready: project.blockers.length === 0,
  });
  // Activating a project opens its tickets: that is where what it does next will show.
  const toggle = () =>
    setActive.mutate(!project.active, {
      onSuccess: () => {
        if (!project.active) navigate(projectTicketsHref(project.id));
      },
    });

  return (
    <>
      <span data-assist-anchor="project.admission" className="inline-flex">
        {admission.action === "activate" && admission.consequence ? (
          <ConfirmPopup
            label={admission.label}
            title="Activate this project?"
            confirmLabel="Activate project"
            consequence={admission.consequence}
            appearance="primary"
            triggerAppearance="primary"
            resetKey={String(project.active)}
            isPending={setActive.isPending}
            onConfirm={toggle}
          />
        ) : admission.action === "pause" ? (
          <Button isLoading={setActive.isPending} onClick={toggle}>
            {admission.label}
          </Button>
        ) : (
          <Button onClick={() => navigate(firstBlockerHref(project))}>{admission.label}</Button>
        )}
      </span>
      <ErrorMessage error={setActive.error} />
    </>
  );
}
