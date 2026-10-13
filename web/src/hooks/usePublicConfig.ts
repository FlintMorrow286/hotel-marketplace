import { useQuery } from "@tanstack/react-query";

import { site } from "@/api/client";

/**
 * What the pages may promise. Until the answer comes nothing is claimed: `data` is undefined, and
 * the pages treat that as "no test payment line, no letter".
 */
export function usePublicConfig() {
  return useQuery({
    queryKey: ["public-config"],
    queryFn: ({ signal }) => site.config(signal),
    staleTime: 5 * 60_000,
  });
}
