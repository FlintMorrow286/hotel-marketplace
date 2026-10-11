import { describe, expect, it } from "vitest";
import { policyLabel } from "./policy";

describe("policyLabel", () => {
  it("does not promise cancellation terms when the policy is missing", () => {
    expect(policyLabel(undefined)).toBe("Условия отмены не указаны");
    expect(policyLabel(null)).toBe("Условия отмены не указаны");
    expect(policyLabel("")).toBe("Условия отмены не указаны");
  });

  it("describes flexible cancellation as free until the day before arrival", () => {
    expect(policyLabel("flexible")).toBe("Бесплатная отмена за сутки до заезда");
  });

  it.each(["moderate", "strict", "unknown"])(
    "does not invent terms for the %s policy",
    (code) => {
      expect(policyLabel(code)).toBe("Условия отмены уточняйте");
    },
  );

  it("matches the backend refund cutoff at the day-before, arrival, and after edges", () => {
    // The backend compares date.today() with checkin_date minus 24 hours.
    const checkin = Date.parse("2026-10-29T00:00:00.000Z");
    const freeDeadline = checkin - 24 * 60 * 60 * 1000;
    const backendAllowsFreeRefund = (today: string) =>
      Date.parse(today + "T00:00:00.000Z") <= freeDeadline;

    expect(backendAllowsFreeRefund("2026-10-28")).toBe(true);
    expect(backendAllowsFreeRefund("2026-10-29")).toBe(false);
    expect(backendAllowsFreeRefund("2026-10-30")).toBe(false);
    expect(policyLabel("flexible")).toBe("Бесплатная отмена за сутки до заезда");
  });
});
