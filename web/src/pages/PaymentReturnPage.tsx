import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";

import { bookings } from "@/api/client";
import type { BookingOut } from "@/api/types";
import { Button } from "@/components/ui/Button";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { keepAsking, returnView, type ReturnView } from "@/utils/payment";

const POLL_MS = 3_000;
// After this long the page stops asking and says so: the bank may be slow, and the
// booking confirms itself whenever its notification arrives.
const GIVE_UP_MS = 120_000;

/**
 * Where the payment page sends the guest back. The provider's notification may have
 * confirmed the booking already, or may be lost, so this page asks the provider itself
 * (`syncPayment`) and also watches the booking; whichever says "confirmed" first wins.
 * The query string the provider adds is not read: only the provider's answer counts.
 */
export function PaymentReturnPage() {
  useDocumentTitle("Проверяем оплату");
  const { bookingId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [attempt, setAttempt] = useState(0);
  const [waitedLong, setWaitedLong] = useState(false);

  useEffect(() => {
    setWaitedLong(false);
    const timer = window.setTimeout(() => setWaitedLong(true), GIVE_UP_MS);
    return () => window.clearTimeout(timer);
  }, [bookingId, attempt]);

  // Without the booking token (another tab) this fails; the booking query below still sees
  // the notification's work.
  const sync = useQuery({
    queryKey: ["payment-sync", bookingId, attempt],
    queryFn: () => bookings.syncPayment(bookingId),
    refetchInterval: (query) => (keepAsking(query.state.data?.state, waitedLong) ? POLL_MS : false),
    retry: false,
    gcTime: 0,
  });
  const booking = useQuery({
    queryKey: ["booking", bookingId],
    queryFn: ({ signal }) => bookings.get(bookingId, signal),
    refetchInterval: waitedLong ? false : POLL_MS,
    enabled: Boolean(bookingId),
  });

  const view = returnView(booking.data?.status, sync.data?.state);

  const confirmed = sync.data?.booking;
  useEffect(() => {
    if (view !== "confirmed") return;
    // The success page reads this query: hand it the confirmed booking, not the hold.
    queryClient.setQueryData(["booking", bookingId], (old: BookingOut | undefined) => ({
      ...old,
      ...(confirmed ?? { status: "confirmed", hold_expires_at: null }),
    }));
    void queryClient.invalidateQueries({ queryKey: ["booking", bookingId] });
    navigate(`/booking/${bookingId}/success`, { replace: true });
  }, [view, confirmed, bookingId, navigate, queryClient]);

  function checkAgain() {
    setAttempt((n) => n + 1);
    void booking.refetch();
  }

  return (
    <div className="mx-auto max-w-2xl px-4 py-24 sm:px-6">
      <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">Оплата</p>
      {booking.data?.code ? (
        <p className="mt-4 font-mono text-sm text-text-tertiary">{booking.data.code}</p>
      ) : null}
      <Outcome
        view={view}
        waitedLong={waitedLong}
        code={booking.data?.code}
        bookingId={bookingId}
        onCheckAgain={checkAgain}
      />
    </div>
  );
}

function Outcome({
  view,
  waitedLong,
  code,
  bookingId,
  onCheckAgain,
}: {
  view: ReturnView;
  waitedLong: boolean;
  code: string | undefined;
  bookingId: string;
  onCheckAgain: () => void;
}) {
  const search = (
    <Link to="/search" className="mt-8 inline-block">
      <Button variant="secondary">Найти жильё заново</Button>
    </Link>
  );

  switch (view) {
    case "confirmed":
      return <Heading text="Оплата прошла">Открываем вашу бронь…</Heading>;
    case "failed":
      return (
        <Heading text="Платёж не прошёл" tone="error">
          Деньги не списаны. Бронь за вами, пока идёт время на оплату.
          <br />
          <Link to={`/checkout/${bookingId}`} className="mt-8 inline-block">
            <Button>Попробовать снова</Button>
          </Link>
        </Heading>
      );
    case "lapsed":
      return (
        <Heading text="Время оплаты истекло" tone="error">
          Бронь аннулирована. Даты могли быть заняты, выберите их заново.
          <br />
          {search}
        </Heading>
      );
    case "returned":
      return (
        <Heading text="Бронь подтвердить не удалось" tone="error">
          Платёж пришёл слишком поздно, деньги возвращены. Оформите бронь заново.
          <br />
          {search}
        </Heading>
      );
    case "stuck":
      return (
        <Heading text="Оплата прошла, но бронь не подтвердилась" tone="error">
          Деньги вернём вручную — напишите в поддержку{code ? `, назвав код ${code}` : ""}.
        </Heading>
      );
    case "none":
      return (
        <Heading text="Оплата не начата">
          Вы открыли эту страницу, не дойдя до оплаты.
          <br />
          <Link to={`/checkout/${bookingId}`} className="mt-8 inline-block">
            <Button>К оплате</Button>
          </Link>
        </Heading>
      );
    case "waiting":
      return waitedLong ? (
        <Heading text="Банк ещё обрабатывает платёж">
          Если деньги списаны, бронь подтвердится сама и письмо придёт на почту. Статус можно
          посмотреть по коду на странице «Моя бронь».
          <br />
          <span className="mt-8 flex flex-col gap-3 sm:flex-row">
            <Button onClick={onCheckAgain}>Проверить ещё раз</Button>
            <Link to="/my-booking">
              <Button variant="tertiary">Найти бронь по коду</Button>
            </Link>
          </span>
        </Heading>
      ) : (
        <Heading text="Проверяем оплату">
          Банк сообщает о платеже, это занимает несколько секунд. Не закрывайте страницу.
        </Heading>
      );
  }
}

function Heading({
  text,
  tone,
  children,
}: {
  text: string;
  tone?: "error";
  children: ReactNode;
}) {
  return (
    <div role="status" aria-live="polite">
      <h1 className="mt-4 font-serif text-4xl font-normal leading-display tracking-tight">{text}</h1>
      <div
        className={
          tone === "error"
            ? "mt-6 border border-border-default bg-feedback-error-bg p-5 text-sm text-feedback-error-text"
            : "mt-6 text-text-secondary"
        }
      >
        {children}
      </div>
    </div>
  );
}
