import type {
  PayoutEligibleFrom,
  PayoutRule,
  PayoutStatus,
  PendingBooking,
} from "@/api/types";
import { plural } from "@/utils/plural";

const CURRENCY_SYMBOL: Record<string, string> = { RUB: "₽", USD: "$", EUR: "€" };

/**
 * An amount of a payout, kopecks included when there are any: `13 000 ₽`, `3 250,50 ₽`.
 * Unlike `formatPrice` it never rounds: a payout is exact money. A negative one is written with a
 * true minus (`−2 640 ₽`) built here, so the text does not depend on the browser's locale data.
 */
export function formatMoney(value: number, currency = "RUB"): string {
  const cents = Math.round(Math.abs(value) * 100);
  const whole = Math.floor(cents / 100);
  const kopecks = cents % 100;
  const grouped = whole.toLocaleString("ru-RU");
  const fraction = kopecks === 0 ? "" : `,${String(kopecks).padStart(2, "0")}`;
  const symbol = CURRENCY_SYMBOL[currency] ?? currency;
  const sign = value < 0 && cents > 0 ? "\u2212" : "";
  return `${sign}${grouped}${fraction}\u00a0${symbol}`;
}

export const PAYOUT_STATUS_LABEL: Record<PayoutStatus, string> = {
  draft: "Черновик",
  approved: "Утверждена",
  paid: "Выплачена",
  cancelled: "Отменена",
};

export function payoutBadge(status: PayoutStatus) {
  switch (status) {
    case "paid":
      return "success" as const;
    case "approved":
      return "warning" as const;
    case "cancelled":
      return "error" as const;
    case "draft":
      return "neutral" as const;
  }
}

export const LINE_KIND_LABEL = { booking: "Бронь", adjustment: "Корректировка" } as const;

const FROM_LABEL: Record<PayoutEligibleFrom, string> = {
  payment: "со дня оплаты брони гостем",
  checkin: "со дня заезда гостя",
  checkout: "со дня выезда гостя",
};

/** The rule in words, for the page that shows it: it is read from the API, never hardcoded. */
export function describeRule(rule: PayoutRule): string {
  const delay =
    rule.delay_days > 0
      ? `, плюс ${rule.delay_days} ${plural(rule.delay_days, "день", "дня", "дней")}`
      : "";
  const minimum =
    rule.min_amount > 0.01
      ? ` Выплата меньше ${formatMoney(rule.min_amount)} не формируется: сумма копится до следующей.`
      : "";
  return `Бронь становится доступной к выплате ${FROM_LABEL[rule.eligible_from]}${delay}.${minimum}`;
}

/** What a booking behind the balance is waiting for. `payable_from` is an ISO date. */
export function pendingLabel(row: Pick<PendingBooking, "state" | "payable_from">, formatDate: (iso: string) => string): string {
  if (row.state === "available") return "К выплате";
  if (row.state === "correction") return "Корректировка";
  return row.payable_from ? `С ${formatDate(row.payable_from)}` : "Ждёт срока";
}

/** An open payout the operator can still act on. */
export function isOpen(status: PayoutStatus): boolean {
  return status === "draft" || status === "approved";
}
