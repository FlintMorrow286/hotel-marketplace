import { describe, expect, it } from "vitest";
import { ApiException } from "@/api/client";
import {
  humanError,
  isLegalDocsChanged,
  isTooManyAttempts,
  TOO_MANY_ATTEMPTS,
} from "./errors";

const knownErrors = [
  {
    detail: "dates not available: check-in date is in the past",
    message: "Дата заезда уже прошла. Выберите другие даты.",
  },
  {
    detail: "dates not available",
    message: "На эти даты мест нет. Выберите другие.",
  },
  {
    detail: "IDEMPOTENCY CONFLICT",
    message: "Это бронирование уже оформляется. Обновите страницу.",
  },
  {
    detail: "hold expired; please book again",
    message: "Время бронирования истекло — оформите его заново.",
  },
  {
    detail: "not holdable",
    message: "Эта бронь больше не активна. Оформите её заново.",
  },
  {
    detail: "legal documents changed: reload the page and accept the current version",
    message: "Условия обновились. Ознакомьтесь с ними и подтвердите согласие заново.",
  },
  {
    detail: "the draft is out of date: bookings changed since it was made; rebuild it",
    message: "Черновик устарел: брони изменились. Пересчитайте его.",
  },
  {
    detail: "payout is approved, only a draft can be approved",
    message: "Утвердить можно только черновик. Обновите страницу.",
  },
  {
    detail: "payout is draft, only an approved payout can be marked paid",
    message: "Отметить выплаченной можно только утверждённую выплату. Обновите страницу.",
  },
  {
    detail: "payout is paid, only a draft or an approved payout can be cancelled",
    message: "Эту выплату уже нельзя отменить: деньги ушли или она отменена.",
  },
  {
    detail: "the payout is below the minimum of 5000",
    message: "Сумма меньше минимальной выплаты. Дождитесь новых броней или измените минимум.",
  },
  {
    detail: "MONEY_FLOW='direct_to_partner': payouts exist only for agent_collect, where the platform holds the money.",
    message:
      "Выплаты работают только когда деньги гостей принимает платформа (MONEY_FLOW=agent_collect).",
  },
  {
    detail: "payout is approved, not draft: nothing was changed",
    message: "Выплату уже изменил другой оператор. Обновите страницу и проверьте её состояние.",
  },
  {
    detail: "payout is draft, not approved: nothing was changed",
    message: "Выплату уже изменил другой оператор. Обновите страницу и проверьте её состояние.",
  },
  {
    detail: "payout not found",
    message: "Выплата не найдена. Обновите страницу.",
  },
  {
    detail: "payment failed",
    message: "Платёж не прошёл. Деньги не списаны, попробуйте ещё раз.",
  },
  {
    detail: "changed state during payment",
    message: "Состояние брони изменилось во время оплаты. Попробуйте ещё раз.",
  },
  {
    detail: "hold vanished on 2026-10-12; the booking is inconsistent; your payment was returned",
    message: "Бронь подтвердить не удалось, деньги возвращены. Оформите её заново.",
  },
  {
    detail: "booking changed state during payment; your payment was returned",
    message: "Бронь подтвердить не удалось, деньги возвращены. Оформите её заново.",
  },
  {
    detail:
      "booking changed state during payment; your payment could not be returned automatically; support will refund it",
    message: "Бронь подтвердить не удалось. Деньги вернём вручную — напишите в поддержку.",
  },
  {
    detail: "the stay is already over; ask the property for a refund",
    message:
      "Проживание уже закончилось, бронь отменить нельзя. Если нужен возврат, обратитесь к владельцу объекта.",
  },
  {
    detail: "booking token missing",
    message:
      "Эта бронь открыта не в этой вкладке. Найдите её по коду и email на странице «Моя бронь».",
  },
  {
    detail: "booking not found",
    message: "Бронирование не найдено. Обновите страницу.",
  },
  {
    detail: "image too large: at most 40 megapixels",
    message: "Фото слишком большое по размерам. Уменьшите его (до 40 мегапикселей) и попробуйте снова.",
  },
  {
    detail: "photo too large",
    message: "Файл слишком большой. Лимит — 10 МБ, сожмите фото и попробуйте снова.",
  },
  {
    detail: "unsupported file type",
    message: "Этот формат не поддерживается. Подходят JPEG, PNG и WebP.",
  },
  {
    detail: "could not decode the image",
    message: "Файл не читается — кажется, он повреждён. Возьмите другое фото.",
  },
  {
    detail: "at most 12 photos",
    message: "Слишком много фото у одного объекта. Удалите лишнее, чтобы добавить новое.",
  },
  {
    detail: "must point to a public address",
    message:
      "Адрес должен быть доступен из интернета. Адреса localhost и внутренней сети (10.x, 192.168.x и т. п.) не поддерживаются.",
  },
] as const;

describe("humanError", () => {
  it.each(knownErrors)("maps '$detail' to its Russian message", ({ detail, message }) => {
    expect(humanError(new ApiException(400, detail), "Fallback")).toBe(message);
  });

  it("uses the fallback for unknown API details and non-API errors", () => {
    const fallback = "Не удалось выполнить действие.";
    expect(humanError(new ApiException(500, "new backend detail"), fallback)).toBe(fallback);
    expect(humanError(new Error("network failure"), fallback)).toBe(fallback);
    expect(humanError({ message: "hold expired" }, fallback)).toBe(fallback);
  });

  it("preserves an empty fallback", () => {
    expect(humanError(new Error("network failure"), "")).toBe("");
  });
});

describe("isTooManyAttempts", () => {
  it("recognizes only API exceptions with status 429", () => {
    expect(isTooManyAttempts(new ApiException(429, "rate limited"))).toBe(true);
    expect(isTooManyAttempts(new ApiException(401, "rate limited"))).toBe(false);
    expect(isTooManyAttempts(new Error("429"))).toBe(false);
    expect(isTooManyAttempts({ status: 429 })).toBe(false);
  });

  it("exposes the guest-facing retry message", () => {
    expect(TOO_MANY_ATTEMPTS).toBe("Слишком много попыток. Подождите минуту и попробуйте снова.");
  });
});

describe("isLegalDocsChanged", () => {
  const detail = "legal documents changed: reload the page and accept the current version";

  it("recognizes the 409 that asks the guest to accept the documents again", () => {
    expect(isLegalDocsChanged(new ApiException(409, detail))).toBe(true);
  });

  it("ignores other conflicts, other statuses and non-API errors", () => {
    expect(isLegalDocsChanged(new ApiException(409, "dates not available: no free inventory"))).toBe(false);
    expect(isLegalDocsChanged(new ApiException(422, detail))).toBe(false);
    expect(isLegalDocsChanged(new Error(detail))).toBe(false);
  });
});
