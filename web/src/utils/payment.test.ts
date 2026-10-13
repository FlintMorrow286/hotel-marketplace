import { describe, expect, it } from "vitest";

import { keepAsking, returnView } from "./payment";

describe("keepAsking", () => {
  it("asks again while the bank has not answered, and after a failed request", () => {
    expect(keepAsking("pending", false)).toBe(true);
    expect(keepAsking(undefined, false)).toBe(true);
  });

  it("stops at any verdict", () => {
    for (const verdict of ["confirmed", "failed", "returned", "stuck", "none"] as const) {
      expect(keepAsking(verdict, false)).toBe(false);
    }
  });

  it("stops once the page has waited long enough", () => {
    expect(keepAsking("pending", true)).toBe(false);
    expect(keepAsking(undefined, true)).toBe(false);
  });
});

describe("returnView", () => {
  it("shows the success the moment either side says the booking is confirmed", () => {
    expect(returnView("confirmed", undefined)).toBe("confirmed");
    expect(returnView("confirmed", "pending")).toBe("confirmed");
    expect(returnView("hold", "confirmed")).toBe("confirmed");
  });

  it("keeps waiting while the provider has not finished, or has not been asked", () => {
    expect(returnView("hold", "pending")).toBe("waiting");
    expect(returnView("hold", undefined)).toBe("waiting");
    expect(returnView(undefined, undefined)).toBe("waiting");
  });

  it("lets the guest try again after a failed attempt, while the hold lives", () => {
    expect(returnView("hold", "failed")).toBe("failed");
  });

  it("says the time ran out when the hold is gone and nothing was paid", () => {
    expect(returnView("cancelled", "pending")).toBe("lapsed");
    expect(returnView("cancelled", "failed")).toBe("lapsed");
    expect(returnView("cancelled", undefined)).toBe("lapsed");
  });

  it("explains money that came too late, whatever the booking shows", () => {
    expect(returnView("cancelled", "returned")).toBe("returned");
    expect(returnView("hold", "returned")).toBe("returned");
    expect(returnView("cancelled", "stuck")).toBe("stuck");
  });

  it("notices a guest who never started paying", () => {
    expect(returnView("hold", "none")).toBe("none");
  });
});
