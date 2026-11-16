/**
 * The runtime a run used, named by the registry slug a reader can open at `/runtimes/<slug>`.
 * The step header and the transcript drawer both draw this, so the two cannot name it differently.
 */

import { Link } from "react-router-dom";
import type { RuntimeType } from "../../api/types";
import { RUNTIME_TYPE_LABELS } from "../runtimes/runtimes";
import type { Spawn } from "./activity";

function typeLabel(type: string): string {
  return Object.hasOwn(RUNTIME_TYPE_LABELS, type) ? RUNTIME_TYPE_LABELS[type as RuntimeType] : type;
}

export function RunRuntime({
  spawn,
  model,
  planned,
  withAgent,
}: {
  spawn: Spawn | undefined;
  model: string | null;
  /** Not spawned yet: the runtime the pinned config will start the step on. */
  planned?: boolean;
  withAgent?: boolean;
}) {
  const agent = withAgent ? spawn?.agent : null;
  const slug = spawn?.runtimeSlug;
  // An empty span would still take a gap in the row it sits in.
  if (!agent && !slug && !model) return null;
  return (
    <span className="text-body-sm text-subtlest">
      {agent && `${agent} · `}
      {planned && slug && "will run on "}
      {slug && (
        <Link to={`/runtimes/${slug}`} className="text-subtle hover:underline">
          {slug}
        </Link>
      )}
      {spawn?.runtimeType && ` (${typeLabel(spawn.runtimeType)})`}
      {model && `${slug ? " · " : ""}${model}`}
    </span>
  );
}
