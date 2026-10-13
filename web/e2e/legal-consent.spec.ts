import { expect, test } from "@playwright/test";

import {
  fillBookingForm,
  offerCheckbox,
  personalDataCheckbox,
  seedBookableProperty,
} from "./fixtures/api";

test("booking needs both consents, each on its own box", async ({ page, request }) => {
  const fixture = await seedBookableProperty(request);
  await fillBookingForm(page, fixture);

  const book = page.getByRole("button", { name: "Забронировать" });
  await expect(book).toBeDisabled();

  await offerCheckbox(page).check();
  await expect(book).toBeDisabled();

  await personalDataCheckbox(page).check();
  await expect(book).toBeEnabled();

  await offerCheckbox(page).uncheck();
  await expect(book).toBeDisabled();
});

test("the document links open in a new tab and the form keeps what was typed", async ({
  page,
  request,
}) => {
  const fixture = await seedBookableProperty(request);
  await fillBookingForm(page, fixture);

  const popup = page.waitForEvent("popup");
  await page.getByRole("link", { name: /публичной оферты/ }).click();
  await expect(await popup).toHaveURL(/\/legal\/offer$/);

  await expect(page.getByLabel("Имя")).toHaveValue(fixture.guest.name);
  await expect(page.getByLabel("Email")).toHaveValue(fixture.guest.email);
});

test("each document has its own page that shows the current version", async ({
  page,
  request,
}) => {
  const { docs_version } = (await (await request.get("/v1/legal")).json()) as {
    docs_version: string;
  };

  for (const [slug, title] of [
    ["offer", "Публичная оферта"],
    ["personal-data", "Согласие на обработку персональных данных"],
    ["privacy", "Политика конфиденциальности"],
  ] as const) {
    await page.goto(`/legal/${slug}`);
    await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
    await expect(page.getByText(`Версия документов: ${docs_version}`)).toBeVisible();
  }

  await page.goto("/legal/nope");
  await expect(page.getByRole("heading", { name: "Такой страницы нет" })).toBeVisible();
});

test("the footer links to the documents", async ({ page }) => {
  await page.goto("/");
  const footer = page.getByRole("navigation", { name: "Документы" });
  for (const title of [
    "Публичная оферта",
    "Согласие на обработку персональных данных",
    "Политика конфиденциальности",
  ]) {
    await expect(footer.getByRole("link", { name: title })).toBeVisible();
  }
});
