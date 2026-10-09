import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { AlertCircle, Clock } from "lucide-react";
import { bookings } from "@/api/client";
import { humanError } from "@/utils/errors";
import { formatDate, formatPrice } from "@/utils/format";
import { Button } from "@/components/ui/Button";
import { CancelBooking } from "@/components/CancelBooking";
import { useCountdown, formatCountdown } from "@/hooks/useCountdown";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";

export function CheckoutPage() {
  useDocumentTitle("Оплата брони");
  const { bookingId } = useParams();
  const navigate = useNavigate();

  const { data: booking, isPending, isError } = useQuery({
    queryKey: ["booking", bookingId],
    queryFn: ({ signal }) => bookings.get(bookingId as string, signal),
    enabled: Boolean(bookingId),
    // The hold ticks down; re-fetch keeps the status honest if it expires.
    refetchInterval: 30_000,
  });

  const pay = useMutation({
    mutationFn: () => bookings.pay(bookingId as string),
    onSuccess: (data) => {
      if (data.status === "confirmed") navigate(`/booking/${data.id}/success`);
    },
  });

  const refund = useMutation({
    mutationFn: () => bookings.refund(bookingId as string),
    onSuccess: () => navigate("/", { replace: true }),
  });

  const msLeft = useCountdown(booking?.hold_expires_at);
  const isHold = booking?.status === "hold";
  const isConfirmed = booking?.status === "confirmed";
  // The reaper moves an expired hold to `cancelled`, so a lapsed booking lands
  // here too — without this branch the page would show payment with a dead
  // button and no way back.
  const isCancelled = booking?.status === "cancelled";
  const lapsed = isCancelled || (isHold && msLeft === 0);

  // While the hold is live the guest keeps the price; after expiry it lapses.
  const expiredOrPaid = lapsed || isConfirmed;

  if (isPending) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-24 text-text-secondary sm:px-6">
        <p role="status">Загружаем бронь…</p>
      </div>
    );
  }

  if (isError || !booking) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-24 sm:px-6">
        <h1 className="font-serif text-4xl font-normal leading-display tracking-tight">
          Бронь не найдена
        </h1>
        <p className="mt-4 text-text-secondary">
          Возможно, она истекла или была отменена.
        </p>
        <Link to="/search" className="mt-8 inline-block">
          <Button variant="secondary">Найти жильё заново</Button>
        </Link>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl px-4 py-24 sm:px-6">
      <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
        Оплата
      </p>
      <h1 className="mt-4 font-serif text-4xl font-normal leading-display tracking-tight">
        Оплата брони
      </h1>
      <p className="mt-2 font-mono text-sm text-text-tertiary">{booking.code}</p>

      {isConfirmed ? <ConfirmedState /> : null}
      {lapsed ? <LapsedState code={booking.code} /> : null}

      <section className="mt-10 rounded-xl border border-border-default bg-surface-card p-8">
        <h2 className="font-serif text-2xl font-normal">Ваша бронь</h2>
        <dl className="mt-6 space-y-3 text-sm">
          <Row label="Заезд">{formatDate(booking.checkin_date)}</Row>
          <Row label="Выезд">{formatDate(booking.checkout_date)}</Row>
          <Row label="Сумма">
            <strong>{formatPrice(booking.total_amount)}</strong>
          </Row>
        </dl>

        {booking.lines.length > 0 ? (
          <div className="mt-6 border-t border-border-default pt-5">
            <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
              Посуточно
            </p>
            <ul className="mt-4 space-y-2 text-sm">
              {booking.lines.map((line) => (
                <li
                  key={line.date}
                  className="flex justify-between gap-4 text-text-secondary"
                >
                  <span>{formatDate(line.date)}</span>
                  <span>{formatPrice(line.price)}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {isHold && !expiredOrPaid ? (
          <div className="mt-6 flex items-center gap-3 border border-border-default bg-feedback-info-bg px-4 py-3 text-sm text-feedback-info-text">
            <Clock className="size-4 shrink-0" aria-hidden="true" />
            <span>
              Цена зафиксирована ещё{" "}
              <strong aria-live="polite">{formatCountdown(msLeft)}</strong>.
              После этого бронь аннулируется.
            </span>
          </div>
        ) : null}

        <div className="mt-8">
          <Button
            onClick={() => pay.mutate()}
            disabled={!isHold || lapsed || pay.isPending}
            loading={pay.isPending}
            className="w-full"
            size="lg"
          >
            Оплатить {formatPrice(booking.total_amount)}
          </Button>
          {pay.isError ? (
            <p
              role="alert"
              className="mt-4 flex items-center gap-2 text-sm text-feedback-error-text"
            >
              <AlertCircle className="size-4 shrink-0" aria-hidden="true" />
              {humanError(pay.error, "Не удалось оплатить, деньги не списаны. Попробуйте ещё раз.")}
            </p>
          ) : null}
          <p className="mt-4 text-center text-xs leading-relaxed text-text-tertiary">
            100% предоплата. Отмена бесплатна за сутки до заезда.
          </p>
          <p className="mt-2 text-center text-xs text-text-tertiary">
            Тестовая оплата, деньги не списываются.
          </p>
        </div>
      </section>

        {isConfirmed ? (
          <div className="mt-8 border-t border-border-default pt-6">
            <CancelBooking
              code={booking.code}
              onConfirm={() => refund.mutate()}
              pending={refund.isPending}
              error={refund.isError ? refund.error : null}
            />
          </div>
        ) : null}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 border-b border-border-default pb-3">
      <dt className="text-text-secondary">{label}</dt>
      <dd className="text-text-primary">{children}</dd>
    </div>
  );
}

function ConfirmedState() {
  return (
    <div className="mt-8 border border-border-default bg-feedback-success-bg p-5 text-feedback-success-text">
      <p className="font-medium">Бронь подтверждена</p>
      <p className="mt-2 text-sm">
        Мгновенное подтверждение. Данные отправлены на почту.
      </p>
    </div>
  );
}

function LapsedState({ code }: { code: string }) {
  return (
    <div className="mt-8 border border-border-default bg-feedback-error-bg p-5 text-feedback-error-text">
      <p className="font-medium">Время оплаты истекло</p>
      <p className="mt-2 text-sm">
        Бронь {code} аннулирована. Даты могли быть заняты, выберите их заново.
      </p>
      <Link to="/search" className="mt-4 inline-block">
        <Button variant="secondary" size="sm">
          Выбрать даты заново
        </Button>
      </Link>
    </div>
  );
}
