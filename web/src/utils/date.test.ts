import { describe, expect, it } from "vitest";
import { addDays, daysBetween } from "./date";

describe("daysBetween", () => {
  it("returns zero for empty, same-day, and reversed ranges", () => {
    expect(daysBetween("", "2026-01-02")).toBe(0);
    expect(daysBetween("2026-01-02", "")).toBe(0);
    expect(daysBetween("2026-01-02", "2026-01-02")).toBe(0);
    expect(daysBetween("2026-01-03", "2026-01-02")).toBe(0);
  });

  it("counts nights in the half-open stay interval", () => {
    expect(daysBetween("2026-10-28", "2026-10-29")).toBe(1);
    expect(daysBetween("2026-10-28", "2026-11-02")).toBe(5);
  });

  it("counts calendar days across leap days and year boundaries", () => {
    expect(daysBetween("2024-02-28", "2024-03-01")).toBe(2);
    expect(daysBetween("2025-12-30", "2026-01-02")).toBe(3);
    expect(daysBetween("2026-03-07", "2026-03-10")).toBe(3);
  });

  it("handles a full 400-year Gregorian cycle", () => {
    expect(daysBetween("2000-01-01", "2400-01-01")).toBe(146097);
  });
});

describe("addDays", () => {
  it.each([
    { date: "2024-02-28", days: 1, expected: "2024-02-29" },
    { date: "2024-02-29", days: 1, expected: "2024-03-01" },
    { date: "2024-03-01", days: -2, expected: "2024-02-28" },
    { date: "2026-01-01", days: -1, expected: "2025-12-31" },
    { date: "2026-06-15", days: 0, expected: "2026-06-15" },
  ])("adds $days days to $date", ({ date, days, expected }) => {
    expect(addDays(date, days)).toBe(expected);
  });

  it("handles large calendar offsets without losing a date", () => {
    expect(addDays("2000-01-01", 146097)).toBe("2400-01-01");
    expect(addDays("2400-01-01", -146097)).toBe("2000-01-01");
  });
});
