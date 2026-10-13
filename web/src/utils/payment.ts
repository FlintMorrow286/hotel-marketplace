import type { BookingStatus, PaymentState } from "@/api/types";

/**
 * Whether the return page should ask the provider again. It does while the answer is
 * "pending" or has not come (a failed request, a busy booking), and stops at any verdict.
 */
export function keepAsking(state: PaymentState | undefined, waitedLong: boolean): boolean {
  return !waitedLong && (state === undefined || state === "pending");
}

/** What the page the guest returns to from the payment page has to say. */
export type ReturnView =
  | "confirmed"
  | "returned"
  | "stuck"
  | "lapsed"
  | "failed"
  | "none"
  | "waiting";

/**
 * Combines the booking's own status with the provider's answer. The booking wins
 * when it is confirmed (a notification may have got there first); a cancelled
 * booking with nothing paid means the hold ran out while the guest was away.
 */
export function returnView(
  booking: BookingStatus | undefined,
  payment: PaymentState | undefined,
): ReturnView {
  if (booking === "confirmed" || payment === "confirmed") return "confirmed";
  if (payment === "returned") return "returned";
  if (payment === "stuck") return "stuck";
  if (booking === "cancelled") return "lapsed";
  if (payment === "failed") return "failed";
  if (payment === "none") return "none";
  return "waiting";
}
