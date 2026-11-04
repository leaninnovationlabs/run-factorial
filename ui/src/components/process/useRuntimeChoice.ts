import { useProcessVersionDetail, useRuntimes } from "../../api/queries";
import { runtimeInText } from "./runtimeLine";

/**
 * The runtime the text names on its own `runtime:` line, or the pinned config's while the text
 * names none — the regenerate once carried no runtime and a repo lost its line (decision 36).
 * The editor has no control of its own for it: the text is the one place it is set.
 */
export function useRuntimeChoice(
  projectId: number,
  versionId: number | undefined,
  text: string,
  enabled = true,
) {
  const detail = useProcessVersionDetail(projectId, versionId);
  const runtimes = useRuntimes({ enabled });

  const pinnedRuntime = detail.data?.content.runtime;
  const runtime = runtimeInText(text) ?? (typeof pinnedRuntime === "string" ? pinnedRuntime : null);
  const registered = runtimes.data ?? [];

  return { runtime, registered };
}
