import { describe, expect, it } from "vitest";
import { formatDate, formatPrice, formatPriceFrom, formatTime } from "./format";

describe("formatPrice", () => {
  it("uses Russian grouping and rounds prices to whole units", () => {
    expect(formatPrice(7200)).toBe("7\u00a0200 ₽");
    expect(formatPrice(1234.49)).toBe("1\u00a0234 ₽");
    expect(formatPrice(1234.5)).toBe("1\u00a0235 ₽");
  });

  it("handles zero, negative, NaN, and very large values", () => {
    expect(formatPrice(0)).toBe("0 ₽");
    expect(formatPrice(-1234.6)).toBe("-1\u00a0235 ₽");
    expect(formatPrice(NaN)).toBe("не\u00a0число ₽");
    expect(formatPrice(Number.MAX_SAFE_INTEGER)).toBe(
      "9\u00a0007\u00a0199\u00a0254\u00a0740\u00a0991 ₽",
    );
  });

  it("uses supported symbols and preserves unknown currency codes", () => {
    expect(formatPrice(10, "RUB")).toBe("10 ₽");
    expect(formatPrice(10, "USD")).toBe("10 $");
    expect(formatPrice(10, "EUR")).toBe("10 €");
    expect(formatPrice(10, "GBP")).toBe("10 GBP");
  });

  it("formats a catalog starting price as a nightly rate", () => {
    expect(formatPriceFrom(7200, "EUR")).toBe("от 7\u00a0200 € за ночь");
  });
});

describe("formatDate", () => {
  it("formats ISO calendar dates without converting their timezone", () => {
    expect(formatDate("2026-10-29")).toBe("29.10.2026");
    expect(formatDate("2026-10-29T23:30:00-10:00")).toBe("29.10.2026");
  });
});

describe("formatTime", () => {
  it("shows hours and minutes and handles an empty value", () => {
    expect(formatTime("14:05:59")).toBe("14:05");
    expect(formatTime("00:00:00")).toBe("00:00");
    expect(formatTime("")).toBe("");
  });
});
