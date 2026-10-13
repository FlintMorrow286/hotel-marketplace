import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { admin } from "@/api/client";
import type { PayoutBalance, PayoutOut } from "@/api/types";
import { PayoutLines } from "@/components/PayoutLines";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Modal } from "@/components/ui/Modal";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { usePublicConfig } from "@/hooks/usePublicConfig";
import { humanError } from "@/utils/errors";
import { formatDate } from "@/utils/format";
import { describeRule, formatMoney, PAYOUT_STATUS_LABEL, payoutBadge } from "@/utils/payout";

const FILTERS: { value: string; label: string }[] = [
  { value: "", label: "Все" },
  { value: "draft", label: "Черновики" },
  { value: "approved", label: "Утверждены" },
  { value: "paid", label: "Выплачены" },
  { value: "cancelled", label: "Отменены" },
];

type Confirm = { payout: PayoutOut; what: "discard" | "cancel" } | null;

/**
 * Выплаты партнёрам: остатки по партнёрам, формирование черновиков, утверждение, отметка о
 * переводе. Деньги переводят вне системы: оператор вводит номер платёжного поручения банка.
 */
export function AdminPayoutsPage() {
  useDocumentTitle("Выплаты - админка");
  const queryClient = useQueryClient();
  const config = usePublicConfig();
  const [filter, setFilter] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const [payingId, setPayingId] = useState<string | null>(null);
  const [reference, setReference] = useState("");
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [showAll, setShowAll] = useState(false);
  // A message about one payout ("all" for the balances table), shown where the operator acted.
  const [notice, setNotice] = useState<{ id: string; text: string } | null>(null);

  const balances = useQuery({
    queryKey: ["admin-payout-balances"],
    queryFn: ({ signal }) => admin.payoutBalances(signal),
    refetchInterval: 30_000,
  });
  const payouts = useQuery({
    queryKey: ["admin-payouts", filter],
    queryFn: ({ signal }) => admin.payouts(filter || null, signal),
  });

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ["admin-payout-balances"] });
    void queryClient.invalidateQueries({ queryKey: ["admin-payouts"] });
    void queryClient.invalidateQueries({ queryKey: ["admin-payout"] });
  }

  const generate = useMutation({
    mutationFn: (partnerId?: string) => admin.generatePayouts(partnerId),
    onSuccess: () => {
      setNotice(null);
      refresh();
    },
    onError: (error) =>
      setNotice({ id: "all", text: humanError(error, "Не удалось сформировать выплаты.") }),
  });
  const approve = useMutation({
    mutationFn: (p: PayoutOut) => admin.approvePayout(p.id),
    onSuccess: () => {
      setNotice(null);
      refresh();
    },
    onError: (error, p) =>
      setNotice({ id: p.id, text: humanError(error, "Не удалось утвердить выплату.") }),
  });
  const pay = useMutation({
    mutationFn: (v: { payout: PayoutOut; reference: string }) =>
      admin.payPayout(v.payout.id, v.reference),
    onSuccess: () => {
      setNotice(null);
      setPayingId(null);
      setReference("");
      refresh();
    },
    onError: (error, v) =>
      setNotice({ id: v.payout.id, text: humanError(error, "Не удалось отметить выплату.") }),
  });
  const cancel = useMutation({
    // What the screen showed is what it asks to cancel: a draft another operator has approved in
    // the meantime must not be cancelled by this dialog.
    mutationFn: (v: { payout: PayoutOut; what: "discard" | "cancel" }) =>
      admin.cancelPayout(v.payout.id, v.what === "discard" ? "draft" : "approved"),
    onSuccess: () => {
      setNotice(null);
      setConfirm(null);
      refresh();
    },
    onError: (error, v) => {
      setConfirm(null);
      setNotice({ id: v.payout.id, text: humanError(error, "Не удалось отменить выплату.") });
      refresh();
    },
  });

  async function download(p: PayoutOut) {
    try {
      await admin.downloadPayout(p.id, p.number);
    } catch (error) {
      setNotice({ id: p.id, text: humanError(error, "Не удалось скачать файл.") });
    }
  }

  const everyone = balances.data?.balances ?? [];
  // A partner whose money is all still waiting for its date has nothing the operator can do.
  const rows = showAll ? everyone : everyone.filter(isActionable);
  const hidden = everyone.length - everyone.filter(isActionable).length;
  const list = payouts.data ?? [];

  return (
    <div className="mx-auto max-w-[1120px] px-4 py-16 sm:px-6 lg:px-8">
      <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">Админка</p>
      <h1 className="mt-4 font-serif text-4xl font-normal leading-display tracking-tight">
        Выплаты партнёрам
      </h1>
      <p className="mt-4 text-text-secondary">
        Платформа принимает деньги гостей и переводит партнёрам стоимость броней за вычетом
        комиссии. Сформируйте черновик, проверьте, утвердите, переведите деньги в банке и
        отметьте выплату с номером платёжного поручения.
      </p>

      {config.data?.payment_test_mode ? (
        <p
          role="status"
          className="mt-6 rounded-xl border border-border-default bg-feedback-info-bg p-4 text-sm text-feedback-info-text"
        >
          Платежи в этой среде тестовые: суммы учебные, переводить нечего.
        </p>
      ) : null}

      {balances.data ? (
        <p className="mt-6 text-sm text-text-secondary">
          Правило выплат (временное, задаётся в <code>app/config/legal.py</code>):{" "}
          {describeRule(balances.data.rule)}
        </p>
      ) : null}

      <section className="mt-10" aria-labelledby="balances-title">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="balances-title" className="font-serif text-2xl font-normal">
            Остатки по партнёрам
          </h2>
          <Button
            variant="secondary"
            size="sm"
            loading={generate.isPending && generate.variables === undefined}
            onClick={() => generate.mutate(undefined)}
          >
            Сформировать для всех
          </Button>
        </div>
        {notice?.id === "all" ? (
          <p role="alert" className="mt-3 text-sm text-feedback-error-text">
            {notice.text}
          </p>
        ) : null}

        {balances.isPending ? (
          <p role="status" className="mt-4 text-text-secondary">
            Загружаем остатки…
          </p>
        ) : balances.isError ? (
          <div className="mt-4 flex flex-wrap items-center gap-4">
            <p role="alert" className="text-feedback-error-text">
              {humanError(balances.error, "Не удалось загрузить остатки. Обновите страницу.")}
            </p>
            <Button variant="secondary" size="sm" onClick={() => void balances.refetch()}>
              Попробовать снова
            </Button>
          </div>
        ) : rows.length === 0 ? (
          <p className="mt-4 rounded-xl border border-border-default bg-surface-card p-6 text-text-secondary">
            {everyone.length === 0
              ? "Начислений пока нет: выплачивать нечего."
              : "Сейчас выплачивать нечего: деньги всех партнёров ждут своего срока."}
          </p>
        ) : (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[44rem] text-left text-sm">
              <caption className="sr-only">Что платформа должна каждому партнёру</caption>
              <thead className="text-xs uppercase tracking-eyebrow text-text-secondary">
                <tr>
                  <th scope="col" className="py-2 pr-4 font-medium">Партнёр</th>
                  <th scope="col" className="py-2 pr-4 text-right font-medium">К выплате</th>
                  <th scope="col" className="py-2 pr-4 text-right font-medium">Ждёт срока</th>
                  <th scope="col" className="py-2 pr-4 text-right font-medium">В обработке</th>
                  <th scope="col" className="py-2 pr-4 text-right font-medium">Выплачено</th>
                  <th scope="col" className="py-2 font-medium">
                    <span className="sr-only">Действие</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((b) => (
                  <BalanceRow
                    key={`${b.partner_id}-${b.currency}`}
                    balance={b}
                    busy={generate.isPending && generate.variables === b.partner_id}
                    onBuild={() => generate.mutate(b.partner_id)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
        {hidden > 0 || showAll ? (
          <Button
            variant="tertiary"
            size="sm"
            className="mt-3"
            aria-pressed={showAll}
            onClick={() => setShowAll((v) => !v)}
          >
            {showAll
              ? "Скрыть партнёров, у которых деньги ещё ждут срока"
              : `Показать и тех, у кого деньги ещё ждут срока (${hidden})`}
          </Button>
        ) : null}
      </section>

      <section className="mt-12" aria-labelledby="payouts-title">
        <h2 id="payouts-title" className="font-serif text-2xl font-normal">
          Выплаты
        </h2>
        <div className="mt-4 flex flex-wrap gap-2">
          {FILTERS.map((f) => (
            <Button
              key={f.value}
              variant={filter === f.value ? "primary" : "secondary"}
              size="sm"
              selected={filter === f.value}
              onClick={() => setFilter(f.value)}
            >
              {f.label}
            </Button>
          ))}
        </div>

        {payouts.isPending ? (
          <p role="status" className="mt-6 text-text-secondary">
            Загружаем выплаты…
          </p>
        ) : payouts.isError ? (
          <p role="alert" className="mt-6 text-feedback-error-text">
            Не удалось загрузить выплаты. Обновите страницу.
          </p>
        ) : list.length === 0 ? (
          <p className="mt-6 rounded-xl border border-border-default bg-surface-card p-6 text-text-secondary">
            Выплат нет. Они появляются, когда вы формируете черновик.
          </p>
        ) : (
          <ul className="mt-6 space-y-3">
            {list.map((p) => (
              <li key={p.id} className="rounded-xl border border-border-default bg-surface-card p-4">
                <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">Выплата № {p.number}</span>
                      <Badge variant={payoutBadge(p.status)}>{PAYOUT_STATUS_LABEL[p.status]}</Badge>
                    </div>
                    <p className="mt-1 text-sm text-text-secondary">
                      {p.partner_name} · {p.partner_email}
                    </p>
                    <p className="mt-1 text-xs text-text-tertiary">
                      создана {formatDate(p.created_at)} · {p.line_count} поз.
                      {p.paid_at ? ` · выплачена ${formatDate(p.paid_at)}` : ""}
                      {p.reference ? ` · платёжное поручение ${p.reference}` : ""}
                    </p>
                  </div>
                  <p className="font-serif text-2xl">{formatMoney(p.net_amount, p.currency)}</p>
                </div>

                <div className="mt-3 flex flex-wrap gap-2">
                  <Button
                    variant="secondary"
                    size="sm"
                    selected={openId === p.id}
                    aria-expanded={openId === p.id}
                    onClick={() => setOpenId(openId === p.id ? null : p.id)}
                  >
                    {openId === p.id ? "Скрыть состав" : "Состав"}
                  </Button>
                  {p.status === "draft" ? (
                    <>
                      <Button
                        size="sm"
                        loading={approve.isPending && approve.variables?.id === p.id}
                        onClick={() => approve.mutate(p)}
                      >
                        Утвердить
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        loading={generate.isPending && generate.variables === p.partner_id}
                        onClick={() => generate.mutate(p.partner_id)}
                      >
                        Пересчитать
                      </Button>
                      <Button
                        variant="tertiary"
                        size="sm"
                        onClick={() => setConfirm({ payout: p, what: "discard" })}
                      >
                        Удалить черновик
                      </Button>
                    </>
                  ) : null}
                  {p.status === "approved" ? (
                    <>
                      <Button
                        size="sm"
                        selected={payingId === p.id}
                        onClick={() => {
                          setPayingId(payingId === p.id ? null : p.id);
                          setReference("");
                        }}
                      >
                        Отметить выплаченной
                      </Button>
                      <Button
                        variant="tertiary"
                        size="sm"
                        onClick={() => setConfirm({ payout: p, what: "cancel" })}
                      >
                        Отменить
                      </Button>
                    </>
                  ) : null}
                  <Button variant="tertiary" size="sm" onClick={() => void download(p)}>
                    Скачать CSV
                  </Button>
                </div>

                {payingId === p.id ? (
                  <form
                    className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-end"
                    onSubmit={(event) => {
                      event.preventDefault();
                      if (reference.trim()) pay.mutate({ payout: p, reference: reference.trim() });
                    }}
                  >
                    <div className="flex-1">
                      <Input
                        label="Номер платёжного поручения"
                        hint="Как перевод найти в выписке банка."
                        value={reference}
                        maxLength={200}
                        onChange={(event) => setReference(event.target.value)}
                      />
                    </div>
                    <Button
                      type="submit"
                      disabled={!reference.trim()}
                      loading={pay.isPending && pay.variables?.payout.id === p.id}
                    >
                      Подтвердить перевод
                    </Button>
                  </form>
                ) : null}

                {notice?.id === p.id ? (
                  <p role="alert" className="mt-3 text-sm text-feedback-error-text">
                    {notice.text}
                  </p>
                ) : null}

                {openId === p.id ? <PayoutBody payout={p} /> : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <Modal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        titleId="payout-confirm-title"
        title={confirm?.what === "discard" ? "Удалить черновик?" : "Отменить выплату?"}
      >
        <p className="mt-4 text-sm leading-relaxed text-text-secondary">
          {confirm?.what === "discard"
            ? `Черновик № ${confirm.payout.number} будет удалён. Брони останутся к выплате, черновик можно сформировать заново.`
            : confirm
              ? `Выплата № ${confirm.payout.number} будет отменена, её брони снова станут доступны к выплате. Если деньги уже переведены, отменять нельзя.`
              : null}
        </p>
        <div className="mt-6 flex flex-wrap justify-end gap-3">
          <Button variant="secondary" onClick={() => setConfirm(null)}>
            Нет, оставить
          </Button>
          <Button
            variant="destructive"
            loading={cancel.isPending}
            onClick={() => confirm && cancel.mutate(confirm)}
          >
            {confirm?.what === "discard" ? "Да, удалить" : "Да, отменить"}
          </Button>
        </div>
      </Modal>
    </div>
  );
}

/** Something to build, approve or pay: money payable now (or owed back), an open draft, or money in processing. */
function isActionable(b: PayoutBalance): boolean {
  return b.available !== 0 || b.draft != null || b.approved > 0;
}

function BalanceRow({
  balance: b,
  busy,
  onBuild,
}: {
  balance: PayoutBalance;
  busy: boolean;
  onBuild: () => void;
}) {
  const money = (v: number) => formatMoney(v, b.currency);
  const owes = b.available < 0;
  return (
    <tr className="border-t border-border-default align-top">
      <td className="py-3 pr-4">
        <span className="font-medium">{b.partner_name ?? "—"}</span>
        <span className="block text-xs text-text-tertiary">
          {b.partner_email} · {b.currency}
        </span>
        {b.draft ? (
          <Badge variant="neutral" className="mt-1">
            Черновик № {b.draft.number}: {formatMoney(b.draft.net_amount, b.currency)}
          </Badge>
        ) : null}
      </td>
      <td
        className={`py-3 pr-4 text-right font-medium whitespace-nowrap ${owes ? "text-feedback-error-text" : ""}`}
        title={owes ? "Партнёр должен платформе: вычтется из следующих выплат" : undefined}
      >
        {money(b.available)}
        <span className="block text-xs font-normal text-text-tertiary">
          {b.available_bookings} бр.
        </span>
      </td>
      <td className="py-3 pr-4 text-right whitespace-nowrap">
        {money(b.upcoming)}
        <span className="block text-xs text-text-tertiary">{b.upcoming_bookings} бр.</span>
      </td>
      <td className="py-3 pr-4 text-right whitespace-nowrap">{money(b.approved)}</td>
      <td className="py-3 pr-4 text-right whitespace-nowrap">{money(b.paid)}</td>
      <td className="py-3 text-right">
        <Button
          variant="secondary"
          size="sm"
          loading={busy}
          disabled={b.available <= 0 && !b.draft}
          onClick={onBuild}
        >
          {b.draft ? "Пересчитать" : "Сформировать"}
        </Button>
      </td>
    </tr>
  );
}

function PayoutBody({ payout }: { payout: PayoutOut }) {
  const detail = useQuery({
    queryKey: ["admin-payout", payout.id],
    queryFn: ({ signal }) => admin.payout(payout.id, signal),
  });
  return (
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
        <>
          <PayoutLines lines={detail.data.lines} currency={payout.currency} />
          {detail.data.approved_by || detail.data.paid_by ? (
            <p className="mt-2 text-xs text-text-tertiary">
              {detail.data.approved_by ? `Утвердил: ${detail.data.approved_by}. ` : ""}
              {detail.data.paid_by ? `Отметил выплату: ${detail.data.paid_by}.` : ""}
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}
