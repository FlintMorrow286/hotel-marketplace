import { randomUUID } from "node:crypto";
import { expect, type APIRequestContext, type APIResponse, type Page } from "@playwright/test";

const BASE_PRICE = 6_500;

export interface StayDates {
  checkin: string;
  checkout: string;
  nights: number;
}

export interface BookingFixture {
  propertyId: string;
  propertyName: string;
  unitTypeId: string;
  roomName: string;
  totalUnits: number;
  guest: { name: string; email: string; phone: string };
  dates: StayDates;
  totalAmount: number;
}

export interface BookingRecord {
  id: string;
  code: string;
  status: string;
  total_amount: number;
  checkin_date: string;
  checkout_date: string;
}

export interface AvailabilityDay {
  date: string;
  free: number;
  closed: boolean;
  price: number | null;
}

async function json<T>(response: APIResponse, expectedStatus = 200): Promise<T> {
  const body = (await response.json()) as T;
  expect(response.status(), JSON.stringify(body)).toBe(expectedStatus);
  return body;
}

function isoDate(date: Date): string {
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`;
}

export function bookingDates(startInDays = 14, nights = 2): StayDates {
  const checkin = new Date();
  checkin.setUTCHours(0, 0, 0, 0);
  checkin.setUTCDate(checkin.getUTCDate() + startInDays);
  const checkout = new Date(checkin);
  checkout.setUTCDate(checkout.getUTCDate() + nights);
  return { checkin: isoDate(checkin), checkout: isoDate(checkout), nights };
}

export async function seedBookableProperty(
  request: APIRequestContext,
): Promise<BookingFixture> {
  const suffix = randomUUID().replaceAll("-", "").slice(0, 10);
  const email = `partner-${suffix}@example.com`;
  const password = `e2e-${suffix}-password`;
  await json<{ access_token: string }>(
    await request.post("/v1/auth/register", {
      data: { email, password, name: `E2E partner ${suffix}` },
    }),
    201,
  );
  const login = await json<{ access_token: string }>(
    await request.post("/v1/auth/login", { data: { email, password } }),
  );
  const headers = { Authorization: `Bearer ${login.access_token}` };
  const propertyName = `E2E stay ${suffix}`;
  const property = await json<{ id: string }>(
    await request.post("/v1/partner/properties", {
      headers,
      data: {
        name: propertyName,
        property_type: "apartment",
        city: "Москва",
        timezone: "UTC",
        currency: "RUB",
      },
    }),
    201,
  );
  await json(
    await request.patch(`/v1/partner/properties/${property.id}`, {
      headers,
      data: { status: "published" },
    }),
  );

  const roomName = `E2E room ${suffix}`;
  const unitType = await json<{ id: string }>(
    await request.post("/v1/partner/unit-types", {
      headers,
      data: {
        property_id: property.id,
        name: roomName,
        capacity: 2,
        total_units: 2,
        base_price: BASE_PRICE,
      },
    }),
    201,
  );
  await json(
    await request.post("/v1/partner/inventory/generate", {
      headers,
      data: { unit_type_id: unitType.id, horizon_days: 90 },
    }),
  );

  const catalog = await json<{ items: Array<{ id: string }> }>(
    await request.get("/v1/properties", { params: { q: propertyName } }),
  );
  expect(catalog.items.some((item) => item.id === property.id)).toBe(true);

  const dates = bookingDates();
  return {
    propertyId: property.id,
    propertyName,
    unitTypeId: unitType.id,
    roomName,
    totalUnits: 2,
    guest: {
      name: `E2E guest ${suffix}`,
      email: `guest-${suffix}@example.com`,
      phone: "+7 999 123-45-67",
    },
    dates,
    totalAmount: BASE_PRICE * dates.nights,
  };
}

export async function createHoldThroughApi(
  request: APIRequestContext,
  fixture: BookingFixture,
): Promise<BookingRecord> {
  const legal = await json<{ docs_version: string }>(await request.get("/v1/legal"));
  return json<BookingRecord>(
    await request.post("/v1/bookings/hold", {
      data: {
        unit_type_id: fixture.unitTypeId,
        checkin: fixture.dates.checkin,
        checkout: fixture.dates.checkout,
        guest: fixture.guest,
        consent: { docs_version: legal.docs_version, offer: true, personal_data: true },
      },
    }),
    201,
  );
}

/** The booking form with dates checked and contacts filled, consents not yet ticked. */
export async function fillBookingForm(page: Page, fixture: BookingFixture): Promise<void> {
  await page.goto(`/property/${fixture.propertyId}`);
  await expect(page.getByRole("heading", { name: fixture.propertyName })).toBeVisible();
  await page.getByLabel("Заезд").fill(fixture.dates.checkin);
  await page.getByLabel("Выезд").fill(fixture.dates.checkout);
  await page.getByRole("button", { name: "Проверить даты" }).click();
  await expect(page.getByText(`${fixture.dates.nights} ночи`, { exact: true })).toBeVisible();
  await expect(page.getByText(amountPattern(fixture.totalAmount))).toBeVisible();

  await page.getByLabel("Имя").fill(fixture.guest.name);
  await page.getByLabel("Email").fill(fixture.guest.email);
  await page.getByLabel("Телефон").fill(fixture.guest.phone);
}

export function offerCheckbox(page: Page) {
  return page.getByRole("checkbox", { name: /публичной оферты/ });
}

export function personalDataCheckbox(page: Page) {
  return page.getByRole("checkbox", { name: /согласие на обработку персональных данных/ });
}

export async function createHoldThroughUi(page: Page, fixture: BookingFixture): Promise<string> {
  await fillBookingForm(page, fixture);
  await offerCheckbox(page).check();
  await personalDataCheckbox(page).check();
  await page.getByRole("button", { name: "Забронировать" }).click();
  await expect(page).toHaveURL(/\/checkout\/[^/]+$/);
  return new URL(page.url()).pathname.split("/").at(-1)!;
}

/**
 * How the API under test takes payment: `stub` confirms at once, `mock_acquiring` sends the guest
 * to a payment page and back. Set PAYMENT_MODE for Playwright to the value the API runs with.
 */
export const PAYMENT_MODE = process.env.PAYMENT_MODE ?? "stub";
export const paysOnProviderPage = PAYMENT_MODE === "mock_acquiring";

const PROVIDER_PAGE = /\/mock-acquirer\/pay\/mock_/;

/** On the provider's page: press one of its buttons (by default the one that pays). */
export async function payOnProviderPage(page: Page, button: RegExp | string = /^Оплатить/) {
  await expect(page).toHaveURL(PROVIDER_PAGE);
  await page.getByRole("button", { name: button }).click();
}

/** Press "Оплатить" on the checkout and, when the provider has a page of its own, pay there. */
export async function payThroughUi(page: Page): Promise<void> {
  await page.getByRole("button", { name: /Оплатить/ }).click();
  if (paysOnProviderPage) await payOnProviderPage(page);
}

export async function getBooking(
  request: APIRequestContext,
  id: string,
): Promise<BookingRecord> {
  return json<BookingRecord>(await request.get(`/v1/bookings/${id}`));
}

export async function getAvailability(
  request: APIRequestContext,
  fixture: BookingFixture,
): Promise<AvailabilityDay[]> {
  const result = await json<{ days: AvailabilityDay[] }>(
    await request.get("/v1/availability", {
      params: {
        unit_type_id: fixture.unitTypeId,
        date_from: fixture.dates.checkin,
        date_to: fixture.dates.checkout,
      },
    }),
  );
  return result.days;
}

export function amountPattern(amount: number): RegExp {
  const grouped = amount.toLocaleString("ru-RU");
  const escaped = grouped
    .replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
    .replace(/[\s\u00a0\u202f]+/g, "\\s*");
  return new RegExp(escaped);
}

export function appUrl(path: string): string {
  return new URL(path, process.env.E2E_BASE_URL ?? "http://127.0.0.1:8002").toString();
}
