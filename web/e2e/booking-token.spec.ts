import { expect, test } from "@playwright/test";

import {
  appUrl,
  createHoldThroughApi,
  createHoldThroughUi,
  getBooking,
  payThroughUi,
  seedBookableProperty,
} from "./fixtures/api";

test("pay requires the booking access token", async ({ request }) => {
  const fixture = await seedBookableProperty(request);
  const hold = await createHoldThroughApi(request, fixture);

  const missingToken = await request.post(`/v1/bookings/${hold.id}/pay`);
  expect(missingToken.status()).toBe(422);

  const wrongToken = await request.post(`/v1/bookings/${hold.id}/pay`, {
    headers: { "X-Booking-Token": "not-the-booking-token" },
  });
  expect(wrongToken.status()).toBe(404);
  expect((await getBooking(request, hold.id)).status).toBe("hold");
});

test("a fresh tab must find a hold by code and email before paying", async ({
  browser,
  page,
  request,
}) => {
  const fixture = await seedBookableProperty(request);
  const bookingId = await createHoldThroughUi(page, fixture);
  const hold = await getBooking(request, bookingId);
  expect(hold.status).toBe("hold");

  const context = await browser.newContext();
  try {
    const freshPage = await context.newPage();
    await freshPage.goto(appUrl(`/checkout/${bookingId}`));
    await expect(freshPage.getByRole("heading", { name: "Оплата брони" })).toBeVisible();
    await freshPage.getByRole("button", { name: /Оплатить/ }).click();
    await expect(freshPage.getByRole("alert")).toContainText(
      "Найдите её по коду и email",
    );
    expect((await getBooking(request, bookingId)).status).toBe("hold");

    await freshPage.goto(appUrl("/my-booking"));
    await freshPage.getByLabel("Код брони").fill(hold.code);
    await freshPage.getByLabel("Email").fill(fixture.guest.email);
    await freshPage.getByRole("button", { name: "Найти" }).click();
    await expect(freshPage.getByRole("heading", { name: "Ожидает оплаты" })).toBeVisible();
    await freshPage.getByRole("button", { name: "Перейти к оплате" }).click();
    await expect(freshPage).toHaveURL(new RegExp(`/checkout/${bookingId}$`));
    await payThroughUi(freshPage);
    await expect(
      freshPage.getByRole("heading", { name: "Бронь подтверждена" }),
    ).toBeVisible();
    await expect.poll(async () => (await getBooking(request, bookingId)).status).toBe("confirmed");
  } finally {
    await context.close();
  }
});
