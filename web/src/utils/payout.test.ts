import { describe, expect, it } from "vitest";

import type { PayoutRule, PayoutStatus } from "@/api/types";
import {
  describeRule,
  formatMoney,
  isOpen,
  PAYOUT_STATUS_LABEL,
  payoutBadge,
  pendingLabel,
} from "./payout";

const NBSP = "\u00a0";
const MINUS = "\u2212";

describe("formatMoney", () => {
  it("shows whole amounts without kopecks and groups thousands", () => {
    expect(formatMoney(0)).toBe(`0${NBSP}₽`);
    expect(formatMoney(8800)).toBe(`8${NBSP}800${NBSP}₽`);
    expect(formatMoney(1234567)).toBe(`1${NBSP}234${NBSP}567${NBSP}₽`);
  });

  it("never rounds: a payout is exact money", () => {
    expect(formatMoney(3250.5)).toBe(`3${NBSP}250,50${NBSP}₽`);
    expect(formatMoney(2933.34)).toBe(`2${NBSP}933,34${NBSP}₽`);
    expect(formatMoney(0.07)).toBe(`0,07${NBSP}₽`);
    expect(formatMoney(599.99)).toBe(`599,99${NBSP}₽`);
  });

  it("writes a negative amount with a true minus, whatever the locale data says", () => {
    expect(formatMoney(-2640)).toBe(`${MINUS}2${NBSP}640${NBSP}₽`);
    expect(formatMoney(-0.5)).toBe(`${MINUS}0,50${NBSP}₽`);
  });

  it("does not show a minus for an amount that rounds to nothing", () => {
    expect(formatMoney(-0.001)).toBe(`0${NBSP}₽`);
  });

  it("uses the currency's symbol and keeps an unknown code", () => {
    expect(formatMoney(200, "USD")).toBe(`200${NBSP}$`);
    expect(formatMoney(176, "EUR")).toBe(`176${NBSP}€`);
    expect(formatMoney(5, "GBP")).toBe(`5${NBSP}GBP`);
  });
});

describe("describeRule", () => {
  const rule = (over: Partial<PayoutRule> = {}): PayoutRule => ({
    eligible_from: "checkout",
    delay_days: 0,
    min_amount: 0.01,
    money_flow: "agent_collect",
    ...over,
  });

  it("says from which day a booking is payable", () => {
    expect(describeRule(rule())).toBe("Бронь становится доступной к выплате со дня выезда гостя.");
    expect(describeRule(rule({ eligible_from: "checkin" }))).toContain("со дня заезда гостя");
    expect(describeRule(rule({ eligible_from: "payment" }))).toContain("со дня оплаты брони гостем");
  });

  it("adds the wait, agreeing the word with the number", () => {
    expect(describeRule(rule({ delay_days: 1 }))).toContain("плюс 1 день.");
    expect(describeRule(rule({ delay_days: 3 }))).toContain("плюс 3 дня.");
    expect(describeRule(rule({ delay_days: 14 }))).toContain("плюс 14 дней.");
  });

  it("mentions a minimum only when there really is one", () => {
    expect(describeRule(rule({ min_amount: 0.01 }))).not.toContain("не формируется");
    expect(describeRule(rule({ min_amount: 5000 }))).toContain(
      `Выплата меньше 5${NBSP}000${NBSP}₽ не формируется`,
    );
  });
});

describe("payout states", () => {
  const all: PayoutStatus[] = ["draft", "approved", "paid", "cancelled"];

  it("names and colours every state", () => {
    for (const status of all) {
      expect(PAYOUT_STATUS_LABEL[status]).toBeTruthy();
      expect(["neutral", "warning", "success", "error"]).toContain(payoutBadge(status));
    }
    expect(payoutBadge("paid")).toBe("success");
  });

  it("calls a payout open while the operator can still act on it", () => {
    expect(all.filter(isOpen)).toEqual(["draft", "approved"]);
  });
});

describe("pendingLabel", () => {
  const date = (iso: string) => `d(${iso})`;

  it("tells what a booking behind the balance is waiting for", () => {
    expect(pendingLabel({ state: "available", payable_from: "2026-10-01" }, date)).toBe("К выплате");
    expect(pendingLabel({ state: "correction", payable_from: "2026-10-01" }, date)).toBe(
      "Корректировка",
    );
    expect(pendingLabel({ state: "upcoming", payable_from: "2026-10-29" }, date)).toBe(
      "С d(2026-10-29)",
    );
    expect(pendingLabel({ state: "upcoming", payable_from: null }, date)).toBe("Ждёт срока");
  });
});
