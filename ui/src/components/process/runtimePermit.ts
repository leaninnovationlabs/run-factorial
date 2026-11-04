import type { PermitsChange, ProjectDetail, Runtime } from "../../api/types";
import { listSetting, RUNTIME_ALLOWLIST } from "../configuration/PermitsBody";
import { unpermitted } from "./commitGate";

/** The E2B runtimes among `slugs` that `project` does not permit yet. The editor's Permit and
 *  commit and the gallery's import both grant these before they write. */
export function permitsToAdd(
  project: ProjectDetail,
  slugs: string[],
  runtimes: Runtime[],
): string[] {
  return unpermitted(slugs, runtimes, listSetting(project.settings, RUNTIME_ALLOWLIST));
}

export function permitting(slugs: string[]): PermitsChange {
  return { [RUNTIME_ALLOWLIST]: { add: slugs } };
}
