import { expect, test } from "@playwright/test";

import {
  amountPattern,
  appUrl,
  getAvailability,
  getBooking,
  seedBookableProperty,
  createHoldThroughUi,
  payThroughUi,
} from "./fixtures/api";

test("guest can book, pay, look up, and cancel a stay", async ({ browser, page, request }, testInfo) => {
  const fixture = await seedBookableProperty(request);
  const bookingId = await createHoldThroughUi(page, fixture);

  let booking = await getBooking(request, bookingId);
  expect(booking.status).toBe("hold");
  await expect(page.getByRole("heading", { name: "Оплата брони" })).toBeVisible();
  await expect(page.getByText(booking.code, { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: new RegExp(`Оплатить\\s*${amountPattern(fixture.totalAmount).source}`),
    }),
  ).toBeVisible();

  await payThroughUi(page);
  await expect(page).toHaveURL(new RegExp(`/booking/${bookingId}/success$`));
  await expect(page.getByRole("heading", { name: "Бронь подтверждена" })).toBeVisible();
  await expect(page.getByText(booking.code, { exact: true })).toBeVisible();

  booking = await getBooking(request, bookingId);
  expect(booking.status).toBe("confirmed");
  let days = await getAvailability(request, fixture);
  expect(days.map((day) => day.date)).toEqual([fixture.dates.checkin, ...nextDate(fixture)]);
  expect(days.map((day) => day.free)).toEqual([1, 1]);

  const mobile = testInfo.project.name === "mobile";
  const lookupContext = await browser.newContext({
    viewport: mobile ? { width: 412, height: 915 } : { width: 1280, height: 720 },
    deviceScaleFactor: mobile ? 2.625 : 1,
    isMobile: mobile,
    hasTouch: mobile,
  });
  try {
    const guestPage = await lookupContext.newPage();
    await guestPage.goto(appUrl(`/booking/${bookingId}/success`));
    await expect(guestPage.getByRole("heading", { name: "Бронь подтверждена" })).toBeVisible();
    await expect(guestPage.getByText(booking.code, { exact: true })).toBeVisible();
    await guestPage.getByRole("link", { name: "Найти бронь по коду" }).click();
    await expect(guestPage.getByRole("heading", { name: "Найти мою бронь" })).toBeVisible();
    await guestPage.getByLabel("Код брони").fill(booking.code);
    await guestPage.getByLabel("Email").fill(fixture.guest.email);
    await guestPage.getByRole("button", { name: "Найти" }).click();
    await expect(guestPage.getByRole("heading", { name: "Подтверждена" })).toBeVisible();
    await guestPage.getByRole("button", { name: "Открыть бронь" }).click();
    await expect(guestPage).toHaveURL(new RegExp(`/booking/${bookingId}/success$`));

    await guestPage.getByRole("button", { name: "Отменить бронь" }).click();
    const dialog = guestPage.getByRole("dialog", { name: "Отменить бронь?" });
    await expect(dialog).toContainText("даты освободятся для других гостей");
    await dialog.getByRole("button", { name: "Да, отменить" }).click();
    await expect(guestPage).toHaveURL(/\/$/);

    await expect.poll(async () => (await getBooking(request, bookingId)).status).toBe("refunded");
    days = await getAvailability(request, fixture);
    expect(days.map((day) => day.free)).toEqual([fixture.totalUnits, fixture.totalUnits]);

    await guestPage.goto(appUrl("/my-booking"));
    await guestPage.getByLabel("Код брони").fill(booking.code);
    await guestPage.getByLabel("Email").fill(fixture.guest.email);
    await guestPage.getByRole("button", { name: "Найти" }).click();
    await expect(guestPage.getByRole("heading", { name: "Возвращена" })).toBeVisible();
  } finally {
    await lookupContext.close();
  }
});

test("guest who just paid can find the booking and cancel it in the same tab", async ({
  page,
  request,
}) => {
  const fixture = await seedBookableProperty(request);
  const bookingId = await createHoldThroughUi(page, fixture);
  const booking = await getBooking(request, bookingId);

  // The booking is slow to refetch: what the success page shows first is whatever the
  // checkout page left in the shared cache. It must be the confirmed booking, not the hold.
  await page.route(`**/v1/bookings/${bookingId}`, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    await route.continue();
  });
  await payThroughUi(page);
  await expect(page).toHaveURL(new RegExp(`/booking/${bookingId}/success$`));
  await expect(page.getByRole("button", { name: "Отменить бронь" })).toBeVisible({
    timeout: 1500,
  });
  await page.unroute(`**/v1/bookings/${bookingId}`);

  // The lookup page must not bring the old "hold" back either.
  await page.getByRole("link", { name: "Найти бронь по коду" }).click();
  await page.getByLabel("Код брони").fill(booking.code);
  await page.getByLabel("Email").fill(fixture.guest.email);
  await page.getByRole("button", { name: "Найти" }).click();
  await expect(page.getByRole("heading", { name: "Подтверждена" })).toBeVisible();
  await page.getByRole("button", { name: "Открыть бронь" }).click();
  await expect(page).toHaveURL(new RegExp(`/booking/${bookingId}/success$`));

  await page.getByRole("button", { name: "Отменить бронь" }).click();
  await page
    .getByRole("dialog", { name: "Отменить бронь?" })
    .getByRole("button", { name: "Да, отменить" })
    .click();
  await expect(page).toHaveURL(/\/$/);
  await expect.poll(async () => (await getBooking(request, bookingId)).status).toBe("refunded");
});

test("guest whose hold ended can book the same dates again", async ({ page, request }) => {
  const fixture = await seedBookableProperty(request);
  const firstId = await createHoldThroughUi(page, fixture);
  expect((await getBooking(request, firstId)).status).toBe("hold");

  // The hold ends (here: the guest's own cancellation, the same state the reaper leaves).
  const token = await page.evaluate((id) => sessionStorage.getItem(`booking-token:${id}`), firstId);
  expect(token).toBeTruthy();
  const cancelled = await request.post(`/v1/bookings/${firstId}/cancel`, {
    headers: { "X-Booking-Token": token! },
  });
  expect(cancelled.ok()).toBe(true);
  expect((await getBooking(request, firstId)).status).toBe("cancelled");

  // Same unit, dates and email: the SPA sends the same Idempotency-Key as before.
  const secondId = await createHoldThroughUi(page, fixture);

  expect(secondId).not.toBe(firstId);
  expect((await getBooking(request, secondId)).status).toBe("hold");
  await expect(page.getByRole("heading", { name: "Оплата брони" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Оплатить/ })).toBeEnabled();
});

function nextDate(fixture: { dates: { checkin: string; nights: number } }): string[] {
  return Array.from({ length: fixture.dates.nights - 1 }, (_, index) => {
    const date = new Date(`${fixture.dates.checkin}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + index + 1);
    return date.toISOString().slice(0, 10);
  });
}
