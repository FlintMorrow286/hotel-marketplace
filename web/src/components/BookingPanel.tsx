import { useState, type ReactNode } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";

import { bookings, availability } from "@/api/client";
import { humanError, isLegalDocsChanged } from "@/utils/errors";
import { addDays } from "@/utils/date";
import { formatDate, formatPrice } from "@/utils/format";
import { plural } from "@/utils/plural";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Checkbox";
import { Input } from "@/components/ui/Input";
import { useLegalInfo } from "@/hooks/useLegalInfo";
import { legalPath, type LegalSlug } from "@/legal/documents";
import type { AvailabilityDay } from "@/api/types";
interface BookingPanelProps {
  unitTypeId: string;
  currency: string;
}

/**
 * Booking panel: pick dates, see per-night availability and total, then place
 * a hold. Hold TTL is 15 minutes; the checkout page counts it down and the
 * payment confirms. The guest never sees the commission. Two separate boxes —
 * the offer and the consent to personal data processing — gate the hold, and the
 * hold carries the documents version the guest was shown (docs/LEGAL.md §3).
 */
export function BookingPanel({ unitTypeId, currency }: BookingPanelProps) {
  const navigate = useNavigate();
  const [checkin, setCheckin] = useState("");
  const [checkout, setCheckout] = useState("");
  const [days, setDays] = useState<AvailabilityDay[] | null>(null);
  const [guest, setGuest] = useState({ name: "", email: "", phone: "" });
  const [acceptedOffer, setAcceptedOffer] = useState(false);
  const [acceptedPersonalData, setAcceptedPersonalData] = useState(false);
  const legalInfo = useLegalInfo();

  // The API reads a half-open interval [checkin, checkout): a checkout day is
  // not a stay. Sending checkout - 1 collapses a one-night stay to an empty
  // range and the API rejects it.
  const price = useMutation({
    mutationFn: () => availability.get(unitTypeId, checkin, checkout),
    onSuccess: (data) => setDays(data.days),
  });

  const hold = useMutation({
    mutationFn: () => {
      const docsVersion = legalInfo.data?.docs_version;
      if (!docsVersion || !acceptedOffer || !acceptedPersonalData) {
        throw new Error("both consents are required before a hold");
      }
      return bookings.hold(
        {
          unit_type_id: unitTypeId,
          checkin,
          checkout,
          guest,
          consent: { docs_version: docsVersion, offer: true, personal_data: true },
        },
        `hold-${unitTypeId}-${checkin}-${checkout}-${guest.email}`,
      );
    },
    onSuccess: (data) => navigate(`/checkout/${data.id}`),
    onError: (error) => {
      // The text changed under the guest: they read and accept the new one.
      if (isLegalDocsChanged(error)) {
        setAcceptedOffer(false);
        setAcceptedPersonalData(false);
        void legalInfo.refetch();
      }
    },
  });

  const total = days ? days.reduce((s, d) => s + (d.price ?? 0), 0) : 0;
  const blocked = days?.some((d) => d.free <= 0 || d.closed) ?? false;
  const guestComplete = Boolean(guest.name && guest.email && guest.phone);
  const consentGiven = acceptedOffer && acceptedPersonalData && Boolean(legalInfo.data);
  const canBook =
    Boolean(checkin && checkout > checkin && days?.length) &&
    !blocked &&
    guestComplete &&
    consentGiven;

  function onCheck() {
    if (!checkin || !checkout) return;
    price.mutate();
  }

  return (
    <section className="mt-6 rounded-xl border border-border-default bg-surface-card p-6">
      <h2 className="font-serif text-2xl font-normal">Бронирование</h2>

      <div className="mt-6 grid grid-cols-2 gap-3">
        <Input
          label="Заезд"
          type="date"
          value={checkin}
          onChange={(e) => {
            const nextCheckin = e.target.value;
            setCheckin(nextCheckin);
            if (checkout && checkout <= nextCheckin) {
              setCheckout(addDays(nextCheckin, 1));
            }
            setDays(null);
            price.reset();
          }}
          min={today()}
        />
        <Input
          label="Выезд"
          type="date"
          value={checkout}
          onChange={(e) => {
            setCheckout(e.target.value);
            setDays(null);
            price.reset();
          }}
          min={checkin ? addDays(checkin, 1) : today()}
        />
      </div>

      <div className="mt-5">
        <Button
          variant="secondary"
          onClick={onCheck}
          loading={price.isPending}
          disabled={!checkin || !checkout}
          className="w-full"
        >
          Проверить даты
        </Button>
        {price.isError ? (
          <p
            role="alert"
            className="mt-3 text-sm text-feedback-error-text"
          >
            Не удалось проверить даты. Обновите страницу и попробуйте снова.
          </p>
        ) : null}
      </div>

      {days ? (
        <div className="mt-6 border-t border-border-default pt-5 text-sm">
          {blocked ? (
            <p className="text-feedback-error-text">
              На выбранные даты мест нет. Попробуйте другие.
            </p>
          ) : (
            <>
              <div className="flex justify-between">
                <span className="text-text-secondary">
                  {days.length} {plural(days.length, "ночь", "ночи", "ночей")}
                </span>
                <strong>
                  {formatPrice(total, currency)}
                </strong>
              </div>
              <ul className="mt-2 space-y-1">
                {days.map((d) => (
                  <li
                    key={d.date}
                    className="flex justify-between text-text-secondary"
                  >
                    <span>{formatDate(d.date)}</span>
                    <span>{formatPrice(d.price ?? 0, currency)}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      ) : null}

      <div className="mt-6 space-y-4 border-t border-border-default pt-5">
        <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
          Контакты гостя
        </p>
        <Input
          label="Имя"
          value={guest.name}
          onChange={(e) => setGuest({ ...guest, name: e.target.value })}
          autoComplete="name"
          placeholder="Как к вам обращаться"
          required
        />
        <Input
          label="Email"
          type="email"
          value={guest.email}
          onChange={(e) => setGuest({ ...guest, email: e.target.value })}
          autoComplete="email"
          placeholder="name@example.com"
          required
        />
        <Input
          label="Телефон"
          type="tel"
          value={guest.phone}
          onChange={(e) => setGuest({ ...guest, phone: e.target.value })}
          autoComplete="tel"
          placeholder="+7 999 000-00-00"
          required
        />
      </div>

      <div className="mt-4">
        <Checkbox
          checked={acceptedOffer}
          onChange={(e) => setAcceptedOffer(e.target.checked)}
        >
          Принимаю условия <DocLink slug="offer">публичной оферты</DocLink>
        </Checkbox>
        <Checkbox
          checked={acceptedPersonalData}
          onChange={(e) => setAcceptedPersonalData(e.target.checked)}
        >
          Даю{" "}
          <DocLink slug="personal-data">согласие на обработку персональных данных</DocLink>
        </Checkbox>
        <p className="mt-1 text-xs leading-relaxed text-text-tertiary">
          Как мы обращаемся с данными, написано в{" "}
          <DocLink slug="privacy">политике конфиденциальности</DocLink>.
        </p>
        {legalInfo.isError ? (
          <p role="alert" className="mt-2 text-sm text-feedback-error-text">
            Не удалось загрузить условия. Обновите страницу.
          </p>
        ) : null}
      </div>

      <div className="mt-6">
        <Button
          onClick={() => hold.mutate()}
          disabled={!canBook || hold.isPending}
          loading={hold.isPending}
          size="lg"
          className="w-full"
        >
          Забронировать
        </Button>
        {hold.isError ? (
          <p
            role="alert"
            className="mt-3 text-sm text-feedback-error-text"
          >
            {humanError(hold.error, "Не удалось забронировать. Попробуйте ещё раз.")}
          </p>
        ) : null}
        <p className="mt-4 text-center text-xs leading-relaxed text-text-tertiary">
          Мгновенное подтверждение · 100% предоплата · бесплатная отмена за
          сутки до заезда
        </p>
        {!guestComplete ? (
          <p className="mt-2 text-center text-xs text-text-tertiary">
            Заполните даты и контакты гостя, чтобы забронировать.
          </p>
        ) : !consentGiven ? (
          <p className="mt-2 text-center text-xs text-text-tertiary">
            Отметьте оба пункта выше, чтобы забронировать.
          </p>
        ) : null}
      </div>
    </section>
  );
}

/** A document link opens in a new tab: the form keeps its dates and contacts. */
function DocLink({ slug, children }: { slug: LegalSlug; children: ReactNode }) {
  return (
    <Link
      to={legalPath(slug)}
      target="_blank"
      rel="noopener noreferrer"
      className="text-text-link underline underline-offset-[3px] hover:text-text-link-hover"
    >
      {children}
      <span className="sr-only"> (откроется в новой вкладке)</span>
    </Link>
  );
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

