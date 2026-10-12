import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiException, bookings } from "./client";

class MemoryStorage {
  private data = new Map<string, string>();
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
}

const HOLD = {
  unit_type_id: "unit-1",
  checkin: "2030-01-10",
  checkout: "2030-01-12",
  guest: { name: "Иван Гость", email: "guest@example.com", phone: "+79991234567" },
};

function booking(id: string, token?: string) {
  return {
    id,
    ...(token ? { access_token: token } : {}),
    code: "BK-ABC123",
    status: "hold",
    total_amount: 6000,
    hold_expires_at: null,
    checkin_date: HOLD.checkin,
    checkout_date: HOLD.checkout,
    lines: [],
  };
}

function reply(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("the booking access token", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let session: MemoryStorage;

  beforeEach(() => {
    session = new MemoryStorage();
    fetchMock = vi.fn();
    vi.stubGlobal("sessionStorage", session);
    vi.stubGlobal("localStorage", new MemoryStorage());
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function lastRequest() {
    const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit];
    return { url, init, headers: init.headers as Record<string, string> };
  }

  it("is kept for the tab when a hold is made", async () => {
    fetchMock.mockResolvedValueOnce(reply(booking("b1", "tok-1"), 201));

    await bookings.hold(HOLD, "key-1");

    expect(session.getItem("booking-token:b1")).toBe("tok-1");
  });

  it("is kept when a booking is found by code and email", async () => {
    fetchMock.mockResolvedValueOnce(reply(booking("b2", "tok-2")));

    await bookings.lookup("BK-ABC123", "guest@example.com");

    expect(session.getItem("booking-token:b2")).toBe("tok-2");
  });

  it.each([
    ["pay", "/v1/bookings/b1/pay"],
    ["cancel", "/v1/bookings/b1/cancel"],
    ["refund", "/v1/bookings/b1/refund"],
  ] as const)("is sent in a header, not the URL, by %s", async (action, path) => {
    session.setItem("booking-token:b1", "tok-1");
    fetchMock.mockResolvedValueOnce(reply(booking("b1")));

    await bookings[action]("b1");

    const { url, init, headers } = lastRequest();
    expect(url).toBe(path);
    expect(init.method).toBe("POST");
    expect(headers["X-Booking-Token"]).toBe("tok-1");
    expect(url).not.toContain("tok-1");
  });

  it("makes the action fail without a request when this tab has no token", async () => {
    await expect(bookings.pay("b1")).rejects.toMatchObject({
      name: "ApiException",
      status: 403,
      message: "booking token missing",
    });
    await expect(bookings.cancel("b1")).rejects.toBeInstanceOf(ApiException);
    await expect(bookings.refund("b1")).rejects.toBeInstanceOf(ApiException);

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("belongs to one booking: another booking's token is never sent for this one", async () => {
    session.setItem("booking-token:b1", "tok-1");

    await expect(bookings.pay("b2")).rejects.toMatchObject({ status: 403 });

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("survives reading the booking, which carries no token", async () => {
    session.setItem("booking-token:b1", "tok-1");
    fetchMock.mockResolvedValueOnce(reply(booking("b1")));

    await bookings.get("b1");

    expect(session.getItem("booking-token:b1")).toBe("tok-1");
  });

  it("is replaced when the server issues a new one", async () => {
    session.setItem("booking-token:b1", "old");
    fetchMock.mockResolvedValueOnce(reply(booking("b1", "new")));

    await bookings.lookup("BK-ABC123", "guest@example.com");

    expect(session.getItem("booking-token:b1")).toBe("new");
  });
});
