import { describe, expect, it } from "vitest";
import { plural } from "./plural";

const russianPluralCases = [
  { n: 1, expected: "one" },
  { n: 2, expected: "few" },
  { n: 5, expected: "many" },
  { n: 11, expected: "many" },
  { n: 12, expected: "many" },
  { n: 14, expected: "many" },
  { n: 21, expected: "one" },
  { n: 22, expected: "few" },
  { n: 25, expected: "many" },
  { n: 101, expected: "one" },
  { n: 111, expected: "many" },
  { n: 112, expected: "many" },
] as const;

describe("plural", () => {
  it.each(russianPluralCases)("uses the $expected form for $n", ({ n, expected }) => {
    expect(plural(n, "one", "few", "many")).toBe(expected);
  });

  it("uses the many form for zero, negative, and NaN inputs", () => {
    expect(plural(0, "one", "few", "many")).toBe("many");
    expect(plural(-1, "one", "few", "many")).toBe("many");
    expect(plural(Number.NaN, "one", "few", "many")).toBe("many");
  });

  it("preserves the Russian endings for very large counts", () => {
    expect(plural(Number.MAX_SAFE_INTEGER, "one", "few", "many")).toBe("one");
    expect(plural(1_000_000_000_012, "one", "few", "many")).toBe("many");
  });
});
