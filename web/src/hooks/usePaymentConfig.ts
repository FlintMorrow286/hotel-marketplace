import { useQuery } from "@tanstack/react-query";

import { payments } from "@/api/client";

/** Whether the payment is a test one. Unknown until the answer comes: nothing is claimed before. */
export function usePaymentConfig() {
  return useQuery({
    queryKey: ["payment-config"],
    queryFn: ({ signal }) => payments.config(signal),
    staleTime: 5 * 60_000,
  });
}
