/** The build the running image came from. It changes only with a deploy. */

import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import { keys } from "./keys";

export function useVersion() {
  return useQuery({ queryKey: keys.version, queryFn: api.getVersion, staleTime: Infinity });
}
