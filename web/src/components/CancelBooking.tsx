import { useState } from "react";
import { AlertCircle } from "lucide-react";

import { humanError } from "@/utils/errors";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";

interface CancelBookingProps {
  code: string;
  onConfirm: () => void;
  pending: boolean;
  error: unknown;
}

/**
 * Guest cancellation. The landing flow promises a free cancellation until
 * 24:00 of the day before check-in; the backend decides whether the penalty
 * applies and returns the booking with `free_cancelled`. The button stays
 * available after the deadline — a paid penalty is still a cancellation the
 * guest may want.
 */
export function CancelBooking({ code, onConfirm, pending, error }: CancelBookingProps) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <div className="flex flex-wrap items-center gap-4">
        <Button
          variant="secondary"
          onClick={() => setOpen(true)}
          className="text-feedback-error-text"
        >
          Отменить бронь
        </Button>
        <p className="text-sm text-text-tertiary">
          Отмена бесплатна за сутки до заезда.
        </p>
      </div>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        titleId="cancel-booking-title"
        title="Отменить бронь?"
      >
        <p className="mt-4 text-sm leading-relaxed text-text-secondary">
          Бронь <span className="font-mono">{code}</span> будет отменена, а
          даты освободятся для других гостей. Бесплатно за сутки до заезда,
          позже списывается штраф.
        </p>

        {error ? (
          <p
            role="alert"
            className="mt-4 flex items-center gap-2 text-sm text-feedback-error-text"
          >
            <AlertCircle className="size-4 shrink-0" aria-hidden="true" />
            {humanError(error, "Не удалось отменить бронь. Попробуйте ещё раз.")}
          </p>
        ) : null}

        <div className="mt-6 flex justify-end gap-3">
          <Button variant="tertiary" onClick={() => setOpen(false)}>
            Оставить бронь
          </Button>
          <Button
            variant="destructive"
            loading={pending}
            onClick={() => onConfirm()}
          >
            Да, отменить
          </Button>
        </div>
      </Modal>
    </>
  );
}
