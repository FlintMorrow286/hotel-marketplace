import { expect, test } from "@playwright/test";

import {
  createHoldThroughUi,
  getBooking,
  payOnProviderPage,
  paysOnProviderPage,
  seedBookableProperty,
} from "./fixtures/api";

test.skip(!paysOnProviderPage, "needs the API on PAYMENT_MODE=mock_acquiring");

test("a declined payment brings the guest back to try again", async ({ page, request }) => {
  const fixture = await seedBookableProperty(request);
  const bookingId = await createHoldThroughUi(page, fixture);

  await page.getByRole("button", { name: /Оплатить/ }).click();
  await payOnProviderPage(page, "Банк отклонил платёж");

  await expect(page).toHaveURL(new RegExp(`/checkout/${bookingId}/return`));
  await expect(page.getByRole("heading", { name: "Платёж не прошёл" })).toBeVisible();
  expect((await getBooking(request, bookingId)).status).toBe("hold");

  await page.getByRole("link", { name: "Попробовать снова" }).click();
  await expect(page).toHaveURL(new RegExp(`/checkout/${bookingId}$`));
  await page.getByRole("button", { name: /Оплатить/ }).click();
  await payOnProviderPage(page);

  await expect(page).toHaveURL(new RegExp(`/booking/${bookingId}/success$`));
  await expect.poll(async () => (await getBooking(request, bookingId)).status).toBe("confirmed");
});

test("a payment the shop is never told about is found when the guest returns", async ({
  page,
  request,
}) => {
  const fixture = await seedBookableProperty(request);
  const bookingId = await createHoldThroughUi(page, fixture);

  await page.getByRole("button", { name: /Оплатить/ }).click();
  await payOnProviderPage(page, /не получит уведомление/);

  await expect(page).toHaveURL(new RegExp(`/booking/${bookingId}/success$`));
  await expect(page.getByRole("heading", { name: "Бронь подтверждена" })).toBeVisible();
  await expect.poll(async () => (await getBooking(request, bookingId)).status).toBe("confirmed");
});

test("paying again while the first page is still open leads to the same page", async ({
  page,
  request,
}) => {
  const fixture = await seedBookableProperty(request);
  const bookingId = await createHoldThroughUi(page, fixture);

  await page.getByRole("button", { name: /Оплатить/ }).click();
  await expect(page).toHaveURL(/\/mock-acquirer\/pay\/mock_/);
  const first = page.url();

  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/checkout/${bookingId}$`));
  await page.getByRole("button", { name: /Оплатить/ }).click();
  await expect(page).toHaveURL(/\/mock-acquirer\/pay\/mock_/);

  expect(page.url()).toBe(first);
});

test("the provider's page says what the payment is for and that nobody is charged", async ({
  page,
  request,
}) => {
  const fixture = await seedBookableProperty(request);
  await createHoldThroughUi(page, fixture);
  const booking = await getBooking(request, new URL(page.url()).pathname.split("/").at(-1)!);

  await page.getByRole("button", { name: /Оплатить/ }).click();

  await expect(page.getByText("деньги не списываются")).toBeVisible();
  await expect(page.getByText(`Бронь ${booking.code}`)).toBeVisible();
});
