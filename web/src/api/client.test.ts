import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { admin, ApiException, bookings, fetchFile, legal, partner, site } from "./client";

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
  consent: { docs_version: "2026.09-draft", offer: true, personal_data: true } as const,
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

  it("carries the guest's consent and the documents version in the hold body", async () => {
    fetchMock.mockResolvedValueOnce(reply(booking("b1", "tok-1"), 201));

    await bookings.hold(HOLD, "key-1");

    const { url, init } = lastRequest();
    expect(url).toBe("/v1/bookings/hold");
    expect(JSON.parse(init.body as string).consent).toEqual({
      docs_version: "2026.09-draft",
      offer: true,
      personal_data: true,
    });
  });

  it("passes on the address of the provider's page when the payment is taken there", async () => {
    session.setItem("booking-token:b1", "tok-1");
    const redirect = {
      status: "pending",
      payment_id: "mock_1",
      payment_url: "https://bank.example/pay/mock_1",
    };
    fetchMock.mockResolvedValueOnce(reply(redirect, 202));

    const answer = await bookings.pay("b1");

    expect(answer).toEqual(redirect);
  });

  it("reads where the payment stands from the sync answer", async () => {
    session.setItem("booking-token:b1", "tok-1");
    const answer = { state: "failed", payment_url: null, booking: null };
    fetchMock.mockResolvedValueOnce(reply(answer));

    await expect(bookings.syncPayment("b1")).resolves.toEqual(answer);
  });

  it("asks what the pages may promise, without a token", async () => {
    const answer = { payment_test_mode: true, email_enabled: false };
    fetchMock.mockResolvedValueOnce(reply(answer));

    await expect(site.config()).resolves.toEqual(answer);

    const { url, headers } = lastRequest();
    expect(url).toBe("/v1/config");
    expect(headers?.["X-Booking-Token"]).toBeUndefined();
  });

  it("reads the mail queue by status and puts a failed letter back", async () => {
    fetchMock.mockResolvedValueOnce(reply({ backend: "log", counts: {}, items: [] }));
    await admin.emails("failed");
    expect(lastRequest().url).toBe("/v1/admin/emails?status=failed");

    fetchMock.mockResolvedValueOnce(reply({ backend: "log", counts: {}, items: [] }));
    await admin.emails(null);
    expect(lastRequest().url).toBe("/v1/admin/emails");

    fetchMock.mockResolvedValueOnce(reply({ retried: "e1", kind: "guest_booking_confirmed" }));
    await admin.retryEmail("e1");
    expect(lastRequest().url).toBe("/v1/admin/emails/e1/retry");
    expect(lastRequest().init.method).toBe("POST");
  });

  it("asks for the operator's payouts with their filters and acts on them with POST", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(reply({})));

    await admin.payoutBalances();
    expect(lastRequest().url).toBe("/v1/admin/payouts/balances");
    await admin.payouts("paid");
    expect(lastRequest().url).toBe("/v1/admin/payouts?status=paid");
    await admin.payouts(null);
    expect(lastRequest().url).toBe("/v1/admin/payouts");
    await admin.payout("p1");
    expect(lastRequest().url).toBe("/v1/admin/payouts/p1");

    await admin.generatePayouts("partner-1");
    expect(lastRequest().url).toBe("/v1/admin/payouts/generate");
    expect(JSON.parse(lastRequest().init.body as string)).toEqual({ partner_id: "partner-1" });
    await admin.generatePayouts();
    expect(JSON.parse(lastRequest().init.body as string)).toEqual({});

    await admin.approvePayout("p1");
    expect([lastRequest().url, lastRequest().init.method]).toEqual([
      "/v1/admin/payouts/p1/approve",
      "POST",
    ]);
    await admin.payPayout("p1", "ПП № 4711");
    expect(lastRequest().url).toBe("/v1/admin/payouts/p1/paid");
    expect(JSON.parse(lastRequest().init.body as string)).toEqual({ reference: "ПП № 4711" });
    await admin.cancelPayout("p1", "draft");
    expect(lastRequest().url).toBe("/v1/admin/payouts/p1/cancel");
    expect(JSON.parse(lastRequest().init.body as string)).toEqual({ expect: "draft" });
    await admin.cancelPayout("p1", "approved");
    expect(JSON.parse(lastRequest().init.body as string)).toEqual({ expect: "approved" });
  });

  it("reads the partner's own statement from the partner routes", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(reply([])));

    await partner.payouts.summary();
    expect(lastRequest().url).toBe("/v1/partner/payouts/summary");
    await partner.payouts.pending(200);
    expect(lastRequest().url).toBe("/v1/partner/payouts/pending?limit=200");
    await partner.payouts.pending(400);
    expect(lastRequest().url).toBe("/v1/partner/payouts/pending?limit=400");
    await partner.payouts.list();
    expect(lastRequest().url).toBe("/v1/partner/payouts");
    await partner.payouts.get("p1");
    expect(lastRequest().url).toBe("/v1/partner/payouts/p1");
  });

  describe("a file behind the session", () => {
    it("is fetched with the token, which a plain link could not carry", async () => {
      localStorage.setItem("hm_access_token", "jwt-1");
      fetchMock.mockResolvedValueOnce(
        new Response("a;b", { status: 200, headers: { "Content-Type": "text/csv" } }),
      );

      const blob = await fetchFile("/partner/payouts/p1/export.csv");

      const { url, init } = lastRequest();
      expect(url).toBe("/v1/partner/payouts/p1/export.csv");
      expect((init.headers as Record<string, string>).Authorization).toBe("Bearer jwt-1");
      expect(await blob.text()).toBe("a;b");
    });

    it("fails with the API's own reason when the file is refused", async () => {
      fetchMock.mockResolvedValueOnce(reply({ detail: "payout not found" }, 404));

      await expect(fetchFile("/partner/payouts/x/export.csv")).rejects.toMatchObject({
        name: "ApiException",
        status: 404,
        message: "payout not found",
      });
    });

    it("fails with the status when the answer is not JSON", async () => {
      fetchMock.mockResolvedValueOnce(new Response("<html>bad gateway</html>", { status: 502 }));

      await expect(fetchFile("/admin/payouts/x/export.csv")).rejects.toMatchObject({
        status: 502,
        message: "request failed: 502",
      });
    });
  });

  it("reads the current documents version from /v1/legal", async () => {
    fetchMock.mockResolvedValueOnce(reply({ docs_version: "2026.09-draft" }));

    await expect(legal.get()).resolves.toEqual({ docs_version: "2026.09-draft" });

    expect(lastRequest().url).toBe("/v1/legal");
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
    ["syncPayment", "/v1/bookings/b1/payment/sync"],
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
    await expect(bookings.syncPayment("b1")).rejects.toBeInstanceOf(ApiException);

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
