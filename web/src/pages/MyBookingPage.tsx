import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";

import { bookings } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { isTooManyAttempts, TOO_MANY_ATTEMPTS } from "@/utils/errors";

const STATUS_LABEL: Record<string, string> = {
  hold: "Ожидает оплаты",
  paid: "Оплачивается",
  confirmed: "Подтверждена",
  cancelled: "Отменена",
  failed: "Платёж не прошёл",
  refunded: "Возвращена",
};

/**
 * The guest's way back to a booking: the traveller has the BK-XXXXXX code from
 * the success page or the email, never the UUID, and proves it with the email
 * the booking was made with. A found booking links to its checkout, which owns
 * the pay/cancel actions.
 */
export function MyBookingPage() {
  useDocumentTitle("Моя бронь");
  const navigate = useNavigate();
  const [code, setCode] = useState("");
  const [email, setEmail] = useState("");

  const lookup = useMutation({
    mutationFn: () => bookings.lookup(code.trim().toUpperCase(), email.trim()),
  });
  const booking = lookup.data;
  const canSubmit = Boolean(code.trim() && email.trim());

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    lookup.mutate();
  }

  return (
    <div className="mx-auto max-w-2xl px-4 py-24 sm:px-6">
      <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
        Бронирование
      </p>
      <h1 className="mt-4 font-serif text-4xl font-normal leading-display tracking-tight">
        Найти мою бронь
      </h1>
      <p className="mt-4 text-text-secondary">
        Введите код брони и email, указанный при бронировании. Код начинается на
        «BK» и есть в письме подтверждения.
      </p>

      <form
        className="mt-8 flex flex-col gap-4 sm:flex-row sm:items-end"
        onSubmit={onSubmit}
      >
        <div className="flex-1">
          <Input
            label="Код брони"
            placeholder="BK-7K2Q9F"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            hasError={lookup.isError}
          />
        </div>
        <div className="flex-1">
          <Input
            label="Email"
            type="email"
            placeholder="name@example.com"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            autoComplete="email"
            hasError={lookup.isError}
          />
        </div>
        <Button type="submit" loading={lookup.isPending} disabled={!canSubmit}>
          Найти
        </Button>
      </form>
      {lookup.isError ? (
        <p role="alert" className="mt-3 text-sm text-feedback-error-text">
          {isTooManyAttempts(lookup.error)
            ? TOO_MANY_ATTEMPTS
            : "Бронь не найдена. Проверьте код и email, указанный при бронировании."}
        </p>
      ) : null}

      {booking ? (
        <section className="mt-10 rounded-xl border border-border-default bg-surface-card p-6">
          <div className="flex items-baseline justify-between gap-4">
            <h2 className="font-serif text-2xl font-normal">
              {STATUS_LABEL[booking.status] ?? booking.status}
            </h2>
            <span className="font-mono text-sm text-text-tertiary">
              {booking.code}
            </span>
          </div>
          <dl className="mt-5 space-y-3 text-sm">
            <div className="flex justify-between gap-4">
              <dt className="text-text-secondary">Заезд</dt>
              <dd>{booking.checkin_date}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-text-secondary">Выезд</dt>
              <dd>{booking.checkout_date}</dd>
            </div>
            <div className="flex justify-between gap-4 border-t border-border-default pt-3">
              <dt className="text-text-secondary">Сумма</dt>
              <dd>
                <strong>{booking.total_amount.toFixed(0)} ₽</strong>
              </dd>
            </div>
          </dl>
          <div className="mt-6">
            {booking.status === "hold" || booking.status === "paid" ? (
              <Button
                variant="secondary"
                onClick={() => navigate(`/checkout/${booking.id}`)}
              >
                Перейти к оплате
              </Button>
            ) : (
              <Button
                variant="secondary"
                onClick={() => navigate(`/booking/${booking.id}/success`)}
              >
                Открыть бронь
              </Button>
            )}
          </div>
        </section>
      ) : null}

      <p className="mt-10 text-sm text-text-tertiary">
        Не помните код?{" "}
        <Link to="/search" className="underline underline-offset-[3px]">
          Подберите жильё заново
        </Link>
        .
      </p>
    </div>
  );
}
