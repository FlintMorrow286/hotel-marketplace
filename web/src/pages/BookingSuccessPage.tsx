import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";

import { bookings } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { CancelBooking } from "@/components/CancelBooking";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";

export function BookingSuccessPage() {
  useDocumentTitle("Бронь подтверждена");
  const { bookingId } = useParams();
  const navigate = useNavigate();
  const { data: booking } = useQuery({
    queryKey: ["booking", bookingId],
    queryFn: ({ signal }) => bookings.get(bookingId as string, signal),
    enabled: Boolean(bookingId),
  });

  // The success screen is also where the guest lands from `/my-booking` after
  // paying, so a cancellation here is the same path the checkout offers.
  const refund = useMutation({
    mutationFn: () => bookings.refund(bookingId as string),
    onSuccess: () => navigate("/", { replace: true }),
  });

  return (
    <div className="mx-auto max-w-2xl px-4 py-32 text-center">
      <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
        Готово
      </p>
      <h1 className="mt-6 font-serif text-display font-normal leading-display tracking-tight">
        Бронь подтверждена
      </h1>
      <p className="mt-6 text-lg leading-normal text-text-secondary">
        Мгновенное подтверждение. Мы отправили детали на вашу почту.
      </p>
      {booking?.code ? (
        <p className="mt-3 font-mono text-sm text-text-tertiary">{booking.code}</p>
      ) : null}
      <p className="mx-auto mt-8 max-w-md text-sm leading-relaxed text-text-tertiary">
        Бесплатная отмена за сутки до заезда. После этого срока списывается
        штраф.
      </p>
      <div className="mt-10 flex flex-col gap-3 sm:flex-row sm:justify-center">
        <Link to="/search">
          <Button variant="secondary">Найти ещё жильё</Button>
        </Link>
        <Link to="/my-booking">
          <Button variant="tertiary">Найти бронь по коду</Button>
        </Link>
      </div>
      {booking?.status === "confirmed" ? (
        <div className="mt-12 border-t border-border-default pt-8">
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
