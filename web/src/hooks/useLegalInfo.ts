import { useQuery } from "@tanstack/react-query";

import { legal } from "@/api/client";

/**
 * The current documents version. The staleTime is short on purpose: a deploy can raise
 * the version while a guest has the form open, and the hold would then be refused.
 */
export function useLegalInfo() {
  return useQuery({
    queryKey: ["legal"],
    queryFn: ({ signal }) => legal.get(signal),
    staleTime: 60_000,
  });
}
