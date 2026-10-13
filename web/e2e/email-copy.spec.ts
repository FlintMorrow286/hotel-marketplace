import { expect, test } from "@playwright/test";

import {
  createHoldThroughUi,
  payThroughUi,
  seedBookableProperty,
} from "./fixtures/api";

test("the API says whether letters are really delivered", async ({ request }) => {
  const config = await (await request.get("/v1/config")).json();

  expect(typeof config.email_enabled).toBe("boolean");
  expect(typeof config.payment_test_mode).toBe("boolean");
});

// The pages read /v1/config, so both wordings can be checked whatever the server under test sends.
for (const emailEnabled of [false, true]) {
  test(`pages ${emailEnabled ? "promise" : "do not promise"} a letter when ${
    emailEnabled ? "mail is delivered" : "none is sent"
  }`, async ({ page, request }) => {
    await page.route("**/v1/config", (route) =>
      route.fulfill({ json: { payment_test_mode: true, email_enabled: emailEnabled } }),
    );
    const fixture = await seedBookableProperty(request);
    const bookingId = await createHoldThroughUi(page, fixture);
    await payThroughUi(page);
    await expect(page).toHaveURL(new RegExp(`/booking/${bookingId}/success$`));
    await expect(page.getByRole("heading", { name: "Бронь подтверждена" })).toBeVisible();

    const promise = page.getByText("Детали брони придут на вашу почту");
    const advice = page.getByText("Сохраните код брони: по нему и email бронь можно найти");
    await page.goto("/my-booking");
    const lookupHint = page.getByText("есть в письме подтверждения");
    if (emailEnabled) {
      await expect(lookupHint).toBeVisible();
    } else {
      await expect(lookupHint).toHaveCount(0);
      await expect(page.getByText("показан на странице подтверждения брони")).toBeVisible();
    }

    await page.goBack();
    if (emailEnabled) {
      await expect(promise).toBeVisible();
      await expect(advice).toHaveCount(0);
    } else {
      await expect(advice).toBeVisible();
      await expect(promise).toHaveCount(0);
    }
  });
}
