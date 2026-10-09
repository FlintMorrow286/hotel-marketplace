/**
 * Turn a mutation error into a sentence the user can act on.
 *
 * The API speaks English (`hold expired; please book again`); the UI is
 * Russian, and a raw detail leaking in is worse than a generic retry. Known
 * causes get a specific sentence, everything else falls back — the detail is
 * still in the browser console via the ApiException.
 */

import { ApiException } from "@/api/client";

const MESSAGES: ReadonlyArray<[RegExp, string]> = [
  // hold
  [/dates not available/i, "На эти даты мест нет. Выберите другие."],
  [/idempotency conflict/i, "Это бронирование уже оформляется. Обновите страницу."],
  [/hold expired/i, "Время бронирования истекло — оформите его заново."],
  [/not holdable/i, "Эта бронь больше не активна. Оформите её заново."],
  // payment
  [/payment failed/i, "Платёж не прошёл. Деньги не списаны, попробуйте ещё раз."],
  [/changed state during payment/i, "Состояние брони изменилось во время оплаты. Попробуйте ещё раз."],
  // shared
  [/booking not found/i, "Бронирование не найдено. Обновите страницу."],
  // photos
  [/photo too large/i, "Файл слишком большой. Лимит — 10 МБ, сожмите фото и попробуйте снова."],
  [
    /unsupported file type/i,
    "Этот формат не поддерживается. Подходят JPEG, PNG и WebP.",
  ],
  [
    /could not decode the image/i,
    "Файл не читается — кажется, он повреждён. Возьмите другое фото.",
  ],
  [
    /at most \d+ photos/i,
    "Слишком много фото у одного объекта. Удалите лишнее, чтобы добавить новое.",
  ],
];

/** Logins, registration and booking lookups answer 429 once guessed at too often. */
export const TOO_MANY_ATTEMPTS =
  "Слишком много попыток. Подождите минуту и попробуйте снова.";

export function isTooManyAttempts(exc: unknown): boolean {
  return exc instanceof ApiException && exc.status === 429;
}

export function humanError(exc: unknown, fallback: string): string {
  if (!(exc instanceof ApiException)) return fallback;
  const match = MESSAGES.find(([pattern]) => pattern.test(exc.message));
  return match ? match[1] : fallback;
}
