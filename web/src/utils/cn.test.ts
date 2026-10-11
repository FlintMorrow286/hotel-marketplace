import { describe, expect, it } from "vitest";
import { cn } from "./cn";

describe("cn", () => {
  it("combines strings, arrays, and conditional classes", () => {
    expect(
      cn("rounded-md", ["text-sm", null, false], {
        "bg-white": true,
        hidden: false,
      }),
    ).toBe("rounded-md text-sm bg-white");
  });

  it("keeps the last conflicting Tailwind utilities", () => {
    expect(cn("p-2 px-4 text-sm", "p-4 px-1 text-lg")).toBe("p-4 px-1 text-lg");
  });

  it("merges conflicts within a variant without crossing variant scopes", () => {
    expect(cn("opacity-0 hover:opacity-50", "hover:opacity-100")).toBe(
      "opacity-0 hover:opacity-100",
    );
  });

  it("returns an empty string when no classes are present", () => {
    expect(cn()).toBe("");
    expect(cn(null, false, undefined, [])).toBe("");
  });
});
