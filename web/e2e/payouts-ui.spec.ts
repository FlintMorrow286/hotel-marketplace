import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * The payout screens against a stubbed API: the logic worth proving here is the UI's own (a stale
 * draft, the bank reference, the confirmation of a cancellation, the download), and the real
 * numbers are proved by the backend tests. Staff and partner sessions are tokens the SPA only
 * reads the scope from; every API call below is answered by the stub.
 */

function session(scope: "admin" | "partner"): string {
  const payload = Buffer.from(
    JSON.stringify({ sub: "stub", scope, exp: 4102444800 }),
  ).toString("base64url");
  return `h.${payload}.s`;
}

async function signIn(page: Page, scope: "admin" | "partner") {
  await page.addInitScript((token) => localStorage.setItem("hm_access_token", token), session(scope));
  await page.route("**/v1/config", (route) =>
    route.fulfill({ json: { payment_test_mode: true, email_enabled: false } }),
  );
}

// The operator's list is asked with or without a ?status=... tail; a glob would miss the bare one,
// and a call that reaches the real API with this stub token is answered 401 and ends the session.
const ADMIN_LIST = /\/v1\/admin\/payouts(\?.*)?$/;

const RULE = { eligible_from: "checkout", delay_days: 1, min_amount: 5000, money_flow: "agent_collect" };

const LINES = [
  {
    booking_id: "b1",
    code: "BK-7K2Q9F",
    property_name: "Дом у моря",
    booking_status: "confirmed",
    checkin_date: "2026-10-01",
    checkout_date: "2026-10-03",
    kind: "booking",
    gross_amount: 10000,
    commission_amount: 1200,
    net_amount: 8800,
  },
  {
    booking_id: "b2",
    code: "BK-AAAA11",
    property_name: "Дом у моря",
    booking_status: "refunded",
    checkin_date: "2026-09-20",
    checkout_date: "2026-09-22",
    kind: "adjustment",
    gross_amount: -3000,
    commission_amount: -360,
    net_amount: -2640,
  },
];

function payout(over: Record<string, unknown> = {}) {
  return {
    id: "po-1",
    number: 12,
    partner_id: "partner-1",
    partner_email: "maria@example.com",
    partner_name: "Мария",
    currency: "RUB",
    status: "draft",
    gross_amount: 7000,
    commission_amount: 840,
    net_amount: 6160,
    line_count: 2,
    reference: null,
    created_at: "2026-10-13T10:00:00Z",
    approved_at: null,
    paid_at: null,
    cancelled_at: null,
    ...over,
  };
}

test("a partner reads the balance, the bookings behind it and a payout's lines, and takes the file", async ({
  page,
}) => {
  await signIn(page, "partner");
  await page.route("**/v1/partner/payouts/summary", (route) =>
    route.fulfill({
      json: {
        rule: RULE,
        balances: [
          { partner_id: "p", partner_email: "m@example.com", partner_name: "Мария", currency: "RUB",
            available: -2640.5, available_bookings: 1, upcoming: 4400, upcoming_bookings: 1,
            approved: 6160, paid: 8800 },
        ],
      },
    }),
  );
  await page.route("**/v1/partner/payouts/pending?**", (route) =>
    route.fulfill({
      json: { total: 2, items: [
        { booking_id: "b3", code: "BK-NEXT01", booking_status: "confirmed", currency: "RUB",
          checkin_date: "2026-11-01", checkout_date: "2026-11-03", payable_from: "2026-11-04",
          state: "upcoming", gross_amount: 5000, commission_amount: 600, net_amount: 4400 },
        { booking_id: "b2", code: "BK-AAAA11", booking_status: "refunded", currency: "RUB",
          checkin_date: "2026-09-20", checkout_date: "2026-09-22", payable_from: "2026-09-23",
          state: "correction", gross_amount: -3000, commission_amount: -360, net_amount: -2640.5 },
      ] },
    }),
  );
  await page.route("**/v1/partner/payouts", (route) =>
    route.fulfill({ json: [payout({ status: "approved", approved_at: "2026-10-13T11:00:00Z" })] }),
  );
  await page.route("**/v1/partner/payouts/po-1", (route) =>
    route.fulfill({ json: { ...payout({ status: "approved" }), lines: LINES } }),
  );
  await page.route("**/v1/partner/payouts/po-1/export.csv", (route: Route) =>
    route.fulfill({ status: 200, contentType: "text/csv; charset=utf-8", body: "Выплата;№ 12\n" }),
  );

  await page.goto("/partner/payouts");

  await expect(page.getByRole("heading", { name: "Выплаты", level: 1 })).toBeVisible();
  await expect(page.getByText("Платежи в этой среде тестовые")).toBeVisible();
  await expect(
    page.getByText("Бронь становится доступной к выплате со дня выезда гостя, плюс 1 день."),
  ).toBeVisible();
  await expect(page.getByText("Выплата меньше 5\u00a0000\u00a0₽ не формируется")).toBeVisible();

  const balance = page.getByRole("region", { name: "Баланс, RUB" });
  await expect(balance.getByText("К зачёту")).toBeVisible();
  await expect(balance.getByText("\u22122\u00a0640,50\u00a0₽")).toBeVisible();
  await expect(balance.getByText("8\u00a0800\u00a0₽")).toBeVisible();

  const pending = page.getByRole("table", { name: /из которых складывается остаток/ });
  await expect(pending.getByText("С 04.11.2026")).toBeVisible();
  await expect(pending.getByText("Корректировка")).toBeVisible();

  await expect(page.getByText("Выплата № 12")).toBeVisible();
  await expect(page.getByText("Утверждена", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Состав" }).click();
  const lines = page.getByRole("table", { name: "Состав выплаты по броням" });
  await expect(lines.getByText("BK-7K2Q9F")).toBeVisible();
  await expect(lines.getByText("Корректировка: бронь изменилась после выплаты")).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Скачать CSV" }).click();
  expect((await download).suggestedFilename()).toBe("payout-12.csv");
});

test("a partner with nothing earned is told so, not shown zeros", async ({ page }) => {
  await signIn(page, "partner");
  await page.route("**/v1/partner/payouts/summary", (route) =>
    route.fulfill({ json: { rule: RULE, balances: [] } }),
  );
  await page.route("**/v1/partner/payouts/pending?**", (route) =>
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.route("**/v1/partner/payouts", (route) => route.fulfill({ json: [] }));

  await page.goto("/partner/payouts");

  await expect(page.getByText("Начислений пока нет")).toBeVisible();
  await expect(page.getByText("Перечислений ещё не было")).toBeVisible();
});

test("staff cannot open the partner's statement and a partner cannot open the operator's", async ({
  browser,
}) => {
  const asStaff = await browser.newPage();
  await signIn(asStaff, "admin");
  await asStaff.goto("/partner/payouts");
  await expect(asStaff).toHaveURL(/\/login/);

  const asPartner = await browser.newPage();
  await signIn(asPartner, "partner");
  await asPartner.goto("/admin/payouts");
  await expect(asPartner).toHaveURL(/\/admin\/login/);
});

test("the operator builds a draft for a partner, and cannot build one where nothing is owed", async ({
  page,
}) => {
  await signIn(page, "admin");
  const built: unknown[] = [];
  const balance = (over: Record<string, unknown>) => ({
    partner_id: "partner-1", partner_email: "maria@example.com", partner_name: "Мария",
    currency: "RUB", available: 8800, available_bookings: 1, upcoming: 0, upcoming_bookings: 0,
    approved: 0, paid: 0, draft: null, ...over,
  });
  await page.route("**/v1/admin/payouts/balances", (route) =>
    route.fulfill({
      json: {
        rule: RULE,
        balances: [
          balance({}),
          balance({ partner_id: "partner-2", partner_email: "quiet@example.com", partner_name: "Тихий",
            available: 0, available_bookings: 0 }),
        ],
      },
    }),
  );
  await page.route(ADMIN_LIST, (route) => route.fulfill({ json: [] }));
  await page.route("**/v1/admin/payouts/generate", (route) => {
    built.push(route.request().postDataJSON());
    return route.fulfill({ json: { payouts: [payout()] } });
  });

  await page.goto("/admin/payouts");

  await expect(page.getByRole("heading", { name: "Выплаты партнёрам" })).toBeVisible();
  await expect(page.getByText("Правило выплат (временное")).toBeVisible();
  const rows = page.getByRole("row");
  await expect(rows.filter({ hasText: "Тихий" })).toHaveCount(0);
  await page.getByRole("button", { name: /Показать и тех, у кого деньги ещё ждут срока \(1\)/ }).click();
  await expect(rows.filter({ hasText: "Тихий" }).getByRole("button", { name: "Сформировать" })).toBeDisabled();
  await rows.filter({ hasText: "Мария" }).getByRole("button", { name: "Сформировать" }).click();

  await expect.poll(() => built).toEqual([{ partner_id: "partner-1" }]);
});

test("approving a stale draft says why, and rebuilding is one click away", async ({ page }) => {
  await signIn(page, "admin");
  let approvals = 0;
  const rebuilt: unknown[] = [];
  await page.route("**/v1/admin/payouts/balances", (route) =>
    route.fulfill({ json: { rule: RULE, balances: [] } }),
  );
  await page.route(ADMIN_LIST, (route) => route.fulfill({ json: [payout()] }));
  await page.route("**/v1/admin/payouts/po-1/approve", (route) => {
    approvals += 1;
    return route.fulfill({
      status: 409,
      json: { detail: "the draft is out of date: bookings changed since it was made; rebuild it" },
    });
  });
  await page.route("**/v1/admin/payouts/generate", (route) => {
    rebuilt.push(route.request().postDataJSON());
    return route.fulfill({ json: { payouts: [payout()] } });
  });

  await page.goto("/admin/payouts");
  await page.getByRole("button", { name: "Утвердить" }).click();

  await expect(page.getByRole("alert").filter({ hasText: "Черновик устарел" })).toBeVisible();
  expect(approvals).toBe(1);
  await page.getByRole("button", { name: "Пересчитать" }).click();
  await expect.poll(() => rebuilt).toEqual([{ partner_id: "partner-1" }]);
});

test("marking a payout paid needs the bank's reference, trimmed, before it goes", async ({ page }) => {
  await signIn(page, "admin");
  const paid: unknown[] = [];
  await page.route("**/v1/admin/payouts/balances", (route) =>
    route.fulfill({ json: { rule: RULE, balances: [] } }),
  );
  await page.route(ADMIN_LIST, (route) =>
    route.fulfill({ json: [payout({ status: "approved", approved_at: "2026-10-13T11:00:00Z" })] }),
  );
  await page.route("**/v1/admin/payouts/po-1/paid", (route) => {
    paid.push(route.request().postDataJSON());
    return route.fulfill({ json: payout({ status: "paid", reference: "ПП 4711" }) });
  });

  await page.goto("/admin/payouts");
  await page.getByRole("button", { name: "Отметить выплаченной" }).click();
  const confirm = page.getByRole("button", { name: "Подтвердить перевод" });
  await expect(confirm).toBeDisabled();

  await page.getByLabel("Номер платёжного поручения").fill("   ");
  await expect(confirm).toBeDisabled();
  await page.getByLabel("Номер платёжного поручения").fill("  ПП 4711  ");
  await confirm.click();

  await expect.poll(() => paid).toEqual([{ reference: "ПП 4711" }]);
});

test("cancelling an approved payout asks first and only then does it", async ({ page }) => {
  await signIn(page, "admin");
  const cancelled: unknown[] = [];
  await page.route("**/v1/admin/payouts/balances", (route) =>
    route.fulfill({ json: { rule: RULE, balances: [] } }),
  );
  await page.route(ADMIN_LIST, (route) =>
    route.fulfill({ json: [payout({ status: "approved", approved_at: "2026-10-13T11:00:00Z" })] }),
  );
  await page.route("**/v1/admin/payouts/po-1/cancel", (route) => {
    cancelled.push(route.request().postDataJSON());
    return route.fulfill({ json: { id: "po-1", outcome: "cancelled" } });
  });

  await page.goto("/admin/payouts");
  await page.getByRole("button", { name: "Отменить", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Отменить выплату?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Нет, оставить" }).click();
  await expect(dialog).toBeHidden();
  expect(cancelled).toEqual([]);

  await page.getByRole("button", { name: "Отменить", exact: true }).click();
  await page.getByRole("dialog", { name: "Отменить выплату?" }).getByRole("button", { name: "Да, отменить" }).click();

  await expect.poll(() => cancelled).toEqual([{ expect: "approved" }]);
});

test("a misconfigured money flow is explained, not shown as an empty table", async ({ page }) => {
  await signIn(page, "admin");
  await page.route("**/v1/admin/payouts/balances", (route) =>
    route.fulfill({
      status: 503,
      json: { detail: "MONEY_FLOW='direct_to_partner': payouts exist only for agent_collect, where the platform holds the money." },
    }),
  );
  await page.route(ADMIN_LIST, (route) => route.fulfill({ json: [] }));

  await page.goto("/admin/payouts");

  await expect(
    page.getByRole("alert").filter({ hasText: "Выплаты работают только когда деньги гостей принимает платформа" }),
  ).toBeVisible();
});

test("a long breakdown is read in pages and says how much of it is shown", async ({ page }) => {
  await signIn(page, "partner");
  const asked: string[] = [];
  const row = (n: number) => ({
    booking_id: `b${n}`, code: `BK-PAGE0${n}`, booking_status: "confirmed", currency: "RUB",
    checkin_date: "2026-11-01", checkout_date: "2026-11-03", payable_from: "2026-11-04",
    state: "upcoming", gross_amount: 1000, commission_amount: 120, net_amount: 880,
  });
  await page.route("**/v1/partner/payouts/summary", (route) =>
    route.fulfill({ json: { rule: RULE, balances: [] } }),
  );
  await page.route("**/v1/partner/payouts", (route) => route.fulfill({ json: [] }));
  await page.route("**/v1/partner/payouts/pending?**", (route) => {
    const limit = Number(new URL(route.request().url()).searchParams.get("limit"));
    asked.push(String(limit));
    const items = [1, 2, 3].map(row).slice(0, limit === 200 ? 2 : 3);
    return route.fulfill({ json: { items, total: 3 } });
  });

  await page.goto("/partner/payouts");
  await expect(page.getByText("BK-PAGE01")).toBeVisible();
  await expect(page.getByText("BK-PAGE03")).toHaveCount(0);
  await expect(page.getByText("Показано 2 из 3")).toBeVisible();

  await page.getByRole("button", { name: "Показать ещё" }).click();

  await expect(page.getByText("BK-PAGE03")).toBeVisible();
  await expect(page.getByRole("button", { name: "Показать ещё" })).toHaveCount(0);
  expect(asked).toEqual(["200", "400"]);
});

test("a payout another operator already approved is not deleted by an old dialog", async ({ page }) => {
  await signIn(page, "admin");
  await page.route("**/v1/admin/payouts/balances", (route) =>
    route.fulfill({ json: { rule: RULE, balances: [] } }),
  );
  await page.route(ADMIN_LIST, (route) => route.fulfill({ json: [payout()] }));
  await page.route("**/v1/admin/payouts/po-1/cancel", (route) =>
    route.fulfill({
      status: 409,
      json: { detail: "payout is approved, not draft: nothing was changed" },
    }),
  );

  await page.goto("/admin/payouts");
  await page.getByRole("button", { name: "Удалить черновик" }).click();
  await page
    .getByRole("dialog", { name: "Удалить черновик?" })
    .getByRole("button", { name: "Да, удалить" })
    .click();

  await expect(
    page.getByRole("alert").filter({ hasText: "Выплату уже изменил другой оператор" }),
  ).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
