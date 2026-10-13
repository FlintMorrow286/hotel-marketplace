import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { admin } from "@/api/client";
import type { AdminEmail, EmailStatus } from "@/api/types";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";

const FILTERS: { value: string; label: string }[] = [
  { value: "", label: "Все" },
  { value: "queued", label: "В очереди" },
  { value: "sent", label: "Отправлены" },
  { value: "failed", label: "Ошибки" },
  { value: "skipped", label: "Пропущены" },
];

const STATUS_LABEL: Record<EmailStatus, string> = {
  queued: "В очереди",
  sent: "Отправлено",
  failed: "Ошибка",
  skipped: "Пропущено",
};

const STATUS_VARIANT: Record<EmailStatus, "neutral" | "success" | "error"> = {
  queued: "neutral",
  sent: "success",
  failed: "error",
  skipped: "neutral",
};

const KIND_LABEL: Record<AdminEmail["kind"], string> = {
  guest_booking_confirmed: "Гостю: бронь подтверждена",
  partner_booking_confirmed: "Партнёру: новая бронь",
  guest_booking_cancelled: "Гостю: бронь отменена",
  partner_booking_cancelled: "Партнёру: бронь отменена",
};

/**
 * The mail queue: every letter a booking caused, and where it stands. With the log backend nothing
 * is delivered and the text of each letter is kept here to read instead; with SMTP this is where a
 * letter that could not be sent shows up, and where it is put back in the queue.
 */
export function AdminEmailPage() {
  useDocumentTitle("Письма - админка");
  const [filter, setFilter] = useState("");
  const queryClient = useQueryClient();

  const { data, isPending, isError } = useQuery({
    queryKey: ["admin-emails", filter],
    queryFn: ({ signal }) => admin.emails(filter || null, signal),
    refetchInterval: 15_000,
  });

  const retry = useMutation({
    mutationFn: (emailId: string) => admin.retryEmail(emailId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin-emails"] }),
  });

  const items = data?.items ?? [];

  return (
    <div className="mx-auto max-w-[1120px] px-4 py-16 sm:px-6 lg:px-8">
      <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">Админка</p>
      <h1 className="mt-4 font-serif text-4xl font-normal leading-display tracking-tight">Письма</h1>
      <p className="mt-4 text-text-secondary">
        Письма гостям и партнёрам о подтверждении и отмене брони. Каждое ставится в очередь вместе с
        самой бронью и уходит отдельным джобом.
      </p>

      {data?.backend === "log" ? (
        <p
          role="status"
          className="mt-6 rounded-xl border border-border-default bg-feedback-info-bg p-4 text-sm text-feedback-info-text"
        >
          Почта не настроена (MAIL_BACKEND=log): письма никому не уходят, их текст сохраняется ниже.
          Чтобы отправлять, задайте MAIL_BACKEND=smtp и SMTP_HOST.
        </p>
      ) : null}

      {data ? (
        <p className="mt-6 text-sm text-text-secondary">
          В очереди {data.counts.queued} · отправлено {data.counts.sent} · ошибок{" "}
          {data.counts.failed} · пропущено {data.counts.skipped}
        </p>
      ) : null}

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

      {isPending ? (
        <p role="status" className="mt-10 text-text-secondary">
          Загружаем письма…
        </p>
      ) : isError ? (
        <div className="mt-10 flex flex-wrap items-center gap-4">
          <p role="alert" className="text-feedback-error-text">
            Не удалось загрузить письма. Обновите страницу.
          </p>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => queryClient.invalidateQueries({ queryKey: ["admin-emails"] })}
          >
            Попробовать снова
          </Button>
        </div>
      ) : items.length === 0 ? (
        <p className="mt-10 rounded-xl border border-border-default bg-surface-card p-6 text-text-secondary">
          Писем нет. Они появляются, когда бронь подтверждена или отменена.
        </p>
      ) : (
        <ul className="mt-6 space-y-3">
          {items.map((mail) => (
            <li
              key={mail.id}
              className="rounded-xl border border-border-default bg-surface-card p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">{KIND_LABEL[mail.kind]}</span>
                    <Badge variant={STATUS_VARIANT[mail.status]}>{STATUS_LABEL[mail.status]}</Badge>
                    {mail.booking_code ? (
                      <span className="font-mono text-xs text-text-secondary">
                        {mail.booking_code}
                      </span>
                    ) : null}
                    <span className="text-xs text-text-tertiary">попыток {mail.attempts}</span>
                  </div>
                  <p className="mt-1 text-xs text-text-tertiary">
                    {new Date(mail.created_at).toLocaleString("ru-RU")}
                    {mail.sent_at
                      ? ` · отправлено ${new Date(mail.sent_at).toLocaleString("ru-RU")}`
                      : ""}
                    {mail.delivery === "log" ? " · никому не доставлено (log)" : ""}
                  </p>
                  {mail.to_email ? (
                    <p className="mt-1 text-sm text-text-secondary">{mail.to_email}</p>
                  ) : null}
                  {mail.subject ? <p className="mt-1 text-sm">{mail.subject}</p> : null}
                  {mail.last_error ? (
                    <p
                      role="alert"
                      className="mt-2 rounded-lg bg-surface-sunken p-2 font-mono text-xs text-feedback-error-text"
                    >
                      {mail.last_error}
                    </p>
                  ) : null}
                  {mail.body ? (
                    <details className="mt-2">
                      <summary className="cursor-pointer text-sm text-text-link">
                        Показать письмо
                      </summary>
                      <pre className="mt-2 whitespace-pre-wrap rounded-lg bg-surface-sunken p-3 font-sans text-sm">
                        {mail.body}
                      </pre>
                    </details>
                  ) : null}
                </div>
                {mail.status === "failed" ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={retry.isPending && retry.variables === mail.id}
                    onClick={() => retry.mutate(mail.id)}
                  >
                    Повторить
                  </Button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}

      {retry.isError ? (
        <p role="alert" className="mt-4 text-sm text-feedback-error-text">
          Не удалось поставить письмо в очередь. Попробуйте ещё раз.
        </p>
      ) : null}
    </div>
  );
}
