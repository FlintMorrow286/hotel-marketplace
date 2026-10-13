import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Navigate } from "react-router-dom";

import { getToken, isAdmin, partner } from "@/api/client";
import type { PayoutBalance, PayoutOut } from "@/api/types";
import { PayoutLines } from "@/components/PayoutLines";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { usePublicConfig } from "@/hooks/usePublicConfig";
import { humanError } from "@/utils/errors";
import { formatDate } from "@/utils/format";
import {
  describeRule,
  formatMoney,
  PAYOUT_STATUS_LABEL,
  payoutBadge,
  pendingLabel,
} from "@/utils/payout";

const PENDING_PAGE = 200;

/** Выплаты: что платформа должна партнёру, за какие брони и что уже перечислено. */
export function PartnerPayoutsPage() {
  useDocumentTitle("Выплаты - кабинет партнёра");
  const enabled = Boolean(getToken()) && !isAdmin();
  const config = usePublicConfig();
  // The breakdown is asked for in pages: a long list must still add up to the balance above it.
  const [pendingLimit, setPendingLimit] = useState(PENDING_PAGE);

  const summary = useQuery({
    queryKey: ["partner-payout-summary"],
    queryFn: ({ signal }) => partner.payouts.summary(signal),
    enabled,
  });
  const pending = useQuery({
    queryKey: ["partner-payout-pending", pendingLimit],
    queryFn: ({ signal }) => partner.payouts.pending(pendingLimit, signal),
    enabled,
    placeholderData: keepPreviousData,
  });
  const payouts = useQuery({
    queryKey: ["partner-payouts"],
    queryFn: ({ signal }) => partner.payouts.list(signal),
    enabled,
  });

  // A staff token is not the partner's session: the money belongs to a partner.
  if (!getToken() || isAdmin()) {
    return <Navigate to="/login" state={{ from: "/partner/payouts" }} replace />;
  }

  return (
    <div className="mx-auto max-w-[1120px] px-4 py-16 sm:px-6 lg:px-8">
      <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
        Кабинет партнёра
      </p>
      <h1 className="mt-4 font-serif text-4xl font-normal leading-display tracking-tight">
        Выплаты
      </h1>
      <p className="mt-4 text-text-secondary">
        Гость платит платформе, платформа переводит вам стоимость брони за вычетом комиссии. Здесь
        видно, сколько и за какие брони.
      </p>
      {config.data?.payment_test_mode ? (
        <p
          role="status"
          className="mt-6 rounded-xl border border-border-default bg-feedback-info-bg p-4 text-sm text-feedback-info-text"
        >
          Платежи в этой среде тестовые: суммы ниже учебные, настоящие деньги не перечисляются.
        </p>
      ) : null}

      {summary.isPending ? (
        <p role="status" className="mt-10 text-text-secondary">
          Загружаем выплаты…
        </p>
      ) : summary.isError ? (
        <div className="mt-10 flex flex-wrap items-center gap-4">
          <p role="alert" className="text-feedback-error-text">
            {humanError(summary.error, "Не удалось загрузить выплаты. Обновите страницу.")}
          </p>
          <Button variant="secondary" size="sm" onClick={() => void summary.refetch()}>
            Попробовать снова
          </Button>
        </div>
      ) : (
        <>
          <p className="mt-6 text-sm text-text-secondary">
            Сейчас действует: {describeRule(summary.data.rule)} Условия могут измениться.
          </p>
          {summary.data.balances.length === 0 ? (
            <p className="mt-8 rounded-xl border border-border-default bg-surface-card p-6 text-text-secondary">
              Начислений пока нет. Они появляются, когда гость оплачивает бронь на ваших объектах.
            </p>
          ) : (
            summary.data.balances.map((b) => <BalanceCard key={b.currency} balance={b} />)
          )}
        </>
      )}

      {pending.data && pending.data.total > 0 ? (
        <section className="mt-12" aria-labelledby="pending-title">
          <h2 id="pending-title" className="font-serif text-2xl font-normal">
            Из чего складывается остаток
          </h2>
          <p className="mt-2 text-sm text-text-secondary">
            Брони, по которым выплаты ещё не было, и поправки к уже выплаченному.
          </p>
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Брони, из которых складывается остаток к выплате</caption>
              <thead className="text-xs uppercase tracking-eyebrow text-text-secondary">
                <tr>
                  <th scope="col" className="py-2 pr-4 font-medium">Бронь</th>
                  <th scope="col" className="hidden py-2 pr-4 font-medium sm:table-cell">Даты</th>
                  <th scope="col" className="py-2 pr-4 font-medium">Когда</th>
                  <th scope="col" className="py-2 text-right font-medium">Вам</th>
                </tr>
              </thead>
              <tbody>
                {pending.data.items.map((row) => (
                  <tr key={row.booking_id} className="border-t border-border-default">
                    <td className="py-2 pr-4">
                      <span className="font-mono text-xs">{row.code}</span>
                      <span className="block text-xs text-text-tertiary sm:hidden">
                        {formatDate(row.checkin_date)} — {formatDate(row.checkout_date)}
                      </span>
                    </td>
                    <td className="hidden py-2 pr-4 whitespace-nowrap text-text-secondary sm:table-cell">
                      {formatDate(row.checkin_date)} — {formatDate(row.checkout_date)}
                    </td>
                    <td className="py-2 pr-4 whitespace-nowrap">
                      {pendingLabel(row, formatDate)}
                    </td>
                    <td className="py-2 text-right font-medium whitespace-nowrap">
                      {formatMoney(row.net_amount, row.currency)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {pending.data.items.length < pending.data.total ? (
            <div className="mt-3 flex flex-wrap items-center gap-3 text-sm text-text-secondary">
              <span>
                Показано {pending.data.items.length} из {pending.data.total}
              </span>
              <Button
                variant="secondary"
                size="sm"
                loading={pending.isFetching}
                onClick={() => setPendingLimit((n) => n + PENDING_PAGE)}
              >
                Показать ещё
              </Button>
            </div>
          ) : null}
        </section>
      ) : null}

      <section className="mt-12" aria-labelledby="payouts-title">
        <h2 id="payouts-title" className="font-serif text-2xl font-normal">
          Перечисления
        </h2>
        {payouts.isPending ? (
          <p role="status" className="mt-4 text-text-secondary">
            Загружаем перечисления…
          </p>
        ) : payouts.isError ? (
          <p role="alert" className="mt-4 text-feedback-error-text">
            Не удалось загрузить перечисления. Обновите страницу.
          </p>
        ) : payouts.data.length === 0 ? (
          <p className="mt-4 rounded-xl border border-border-default bg-surface-card p-6 text-text-secondary">
            Перечислений ещё не было.
          </p>
        ) : (
          <ul className="mt-4 space-y-3">
            {payouts.data.map((p) => (
              <PayoutItem key={p.id} payout={p} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function BalanceCard({ balance }: { balance: PayoutBalance }) {
  const owes = balance.available < 0;
  const money = (v: number) => formatMoney(v, balance.currency);
  return (
    <section
      aria-label={`Баланс, ${balance.currency}`}
      className="mt-8 rounded-xl border border-border-default bg-surface-card p-6"
    >
      <dl className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <dt className="text-xs uppercase tracking-eyebrow text-text-secondary">
            {owes ? "К зачёту" : "К выплате"}
          </dt>
          <dd
            className={`mt-1 font-serif text-3xl ${owes ? "text-feedback-error-text" : ""}`}
          >
            {money(balance.available)}
          </dd>
          <dd className="mt-1 text-xs text-text-tertiary">
            {owes
              ? "Брони отменены после выплаты: разница вычтется из следующих выплат."
              : "Доступно сейчас, ждёт формирования выплаты."}
          </dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-eyebrow text-text-secondary">Ждёт срока</dt>
          <dd className="mt-1 font-serif text-3xl">{money(balance.upcoming)}</dd>
          <dd className="mt-1 text-xs text-text-tertiary">Гость ещё не выехал.</dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-eyebrow text-text-secondary">В обработке</dt>
          <dd className="mt-1 font-serif text-3xl">{money(balance.approved)}</dd>
          <dd className="mt-1 text-xs text-text-tertiary">Утверждено, ещё не перечислено.</dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-eyebrow text-text-secondary">Выплачено</dt>
          <dd className="mt-1 font-serif text-3xl">{money(balance.paid)}</dd>
          <dd className="mt-1 text-xs text-text-tertiary">Всего перечислено вам.</dd>
        </div>
      </dl>
    </section>
  );
}

function PayoutItem({ payout }: { payout: PayoutOut }) {
  const [open, setOpen] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: ["partner-payout", payout.id],
    queryFn: ({ signal }) => partner.payouts.get(payout.id, signal),
    enabled: open,
  });
  const when = payout.paid_at ?? payout.approved_at ?? payout.created_at;

  async function download() {
    setDownloadError(null);
    try {
      await partner.payouts.download(payout.id, payout.number);
    } catch (error) {
      setDownloadError(humanError(error, "Не удалось скачать файл. Попробуйте ещё раз."));
    }
  }

  return (
    <li className="rounded-xl border border-border-default bg-surface-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">Выплата № {payout.number}</span>
            <Badge variant={payoutBadge(payout.status)}>{PAYOUT_STATUS_LABEL[payout.status]}</Badge>
          </div>
          <p className="mt-1 text-xs text-text-tertiary">
            {formatDate(when)} · {payout.line_count} поз.
            {payout.reference ? ` · платёжное поручение ${payout.reference}` : ""}
          </p>
        </div>
        <p className="font-serif text-2xl">{formatMoney(payout.net_amount, payout.currency)}</p>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          variant="secondary"
          size="sm"
          selected={open}
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "Скрыть состав" : "Состав"}
        </Button>
        <Button variant="tertiary" size="sm" onClick={() => void download()}>
          Скачать CSV
        </Button>
      </div>
      {downloadError ? (
        <p role="alert" className="mt-2 text-sm text-feedback-error-text">
          {downloadError}
        </p>
      ) : null}
      {open ? (
        <div className="mt-4">
          {detail.isPending ? (
            <p role="status" className="text-sm text-text-secondary">
              Загружаем состав…
            </p>
          ) : detail.isError ? (
            <p role="alert" className="text-sm text-feedback-error-text">
              Не удалось загрузить состав.
            </p>
          ) : (
            <PayoutLines lines={detail.data.lines} currency={payout.currency} />
          )}
        </div>
      ) : null}
    </li>
  );
}
