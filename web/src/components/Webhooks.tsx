import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Webhook } from "lucide-react";

import { partner } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Modal } from "@/components/ui/Modal";
import { Badge } from "@/components/ui/Badge";
import { humanError } from "@/utils/errors";

const EVENT_LABELS: Record<string, string> = {
  "booking.confirmed": "Бронь подтверждена",
  "booking.cancelled": "Бронь отменена",
  "rate.prices_changed": "Цены изменились",
  "inventory.availability_changed": "Доступность изменилась",
  "property.status_changed": "Статус объекта изменился",
};

const ALL_EVENTS = Object.keys(EVENT_LABELS);

/**
 * Webhook subscriptions: we push events out to the partner's channel system.
 * Every event is signed with HMAC and retried with backoff; a channel that is
 * down does not lose events, it gets them on the next attempt.
 */
export function Webhooks() {
  const { data: webhooks, isPending } = useQuery({
    queryKey: ["webhooks"],
    queryFn: ({ signal }) => partner.webhooks.list(signal),
  });

  return (
    <section className="mt-10">
      <h2 className="font-serif text-2xl font-normal">Вебхуки</h2>
      <p className="mt-2 max-w-2xl text-sm text-text-secondary">
        Мы отправляем события наружу: брони, изменения цен и доступности.
        Каждый запрос подписан HMAC-ключом, который вы настраиваете на своей
        стороне, и повторяется с задержкой, если ваш сервер не отвечает.
      </p>
      <p className="mt-2 max-w-2xl text-sm text-text-secondary">
        Тот же API-ключ, что толкает брони, читает тарифы и доступность:
        <span className="font-mono"> GET /v1/channel/rates</span> и
        <span className="font-mono"> GET /v1/channel/availability</span> с
        заголовком <span className="font-mono">X-API-Key</span>. Получив
        событие об изменении цены, канал может перечитать тарифы по диапазону
        из события.
      </p>

      {isPending ? (
        <p role="status" className="mt-4 text-sm text-text-secondary">
          Загружаем подписки…
        </p>
      ) : webhooks && webhooks.length > 0 ? (
        <ul className="mt-4 space-y-3">
          {webhooks.map((hook) => (
            <WebhookRow key={hook.id} webhook={hook} />
          ))}
        </ul>
      ) : (
        <p className="mt-4 rounded-xl border border-border-default bg-surface-card p-4 text-sm text-text-secondary">
          Подписок нет. Создайте одну, чтобы получать события о бронях.
        </p>
      )}

      <div className="mt-4">
        <CreateWebhookButton />
      </div>
    </section>
  );
}

function WebhookRow({ webhook }: { webhook: WebhookRowProps }) {
  const queryClient = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const remove = useMutation({
    mutationFn: () => partner.webhooks.remove(webhook.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["webhooks"] });
      setConfirmOpen(false);
    },
  });

  const types = webhook.event_types.includes("*")
    ? ["Все события"]
    : webhook.event_types.map((t) => EVENT_LABELS[t] ?? t);

  const lastStatus = webhook.last_status
    ? webhook.last_status === "success"
      ? "success"
      : "error"
    : "neutral";

  return (
    <li className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 rounded-xl border border-border-default bg-surface-card p-4">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <Webhook className="size-4 shrink-0 text-text-tertiary" aria-hidden="true" />
          <p className="truncate font-mono text-sm">{webhook.url}</p>
        </div>
        <div className="mt-2 flex flex-wrap gap-1">
          {types.map((label) => (
            <Badge key={label} variant="neutral">
              {label}
            </Badge>
          ))}
        </div>
        <p className="mt-2 text-xs text-text-tertiary">
          {webhook.last_delivery_at
            ? `Последняя отправка: ${new Date(webhook.last_delivery_at).toLocaleString("ru-RU")}`
            : "Отправок ещё не было"}
          {webhook.last_status === "failed" && webhook.last_error
            ? ` · ошибка: ${webhook.last_error}`
            : ""}
        </p>
      </div>
      <div className="flex items-center gap-3">
        {webhook.last_status ? <Badge variant={lastStatus as BadgeVariant}>{webhook.last_status === "success" ? "Работает" : "Ошибка"}</Badge> : null}
        <Button variant="ghost" size="sm" onClick={() => setConfirmOpen(true)}>
          <Trash2 className="size-4" aria-hidden="true" />
          Удалить
        </Button>
      </div>

      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        titleId={`webhook-remove-${webhook.id}`}
        title="Удалить вебхук?"
      >
        <p className="mt-2 text-text-secondary">
          События перестанут отправляться на этот адрес. Уже поставленные в
          очередь события будут потеряны.
        </p>
        <div className="mt-6 flex justify-end gap-2">
          <Button
            type="button"
            variant="secondary"
            onClick={() => setConfirmOpen(false)}
          >
            Отмена
          </Button>
          <Button
            type="button"
            variant="destructive"
            loading={remove.isPending}
            onClick={() => remove.mutate()}
          >
            Удалить
          </Button>
        </div>
      </Modal>
    </li>
  );
}

type WebhookRowProps = {
  id: string;
  url: string;
  event_types: string[];
  last_delivery_at: string | null;
  last_status: "success" | "failed" | null;
  last_error: string | null;
};

type BadgeVariant = "success" | "error" | "neutral";

function CreateWebhookButton() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [selected, setSelected] = useState<string[]>(["*"]);

  const create = useMutation({
    mutationFn: () =>
      partner.webhooks.create(url.trim(), secret.trim(), selected),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["webhooks"] });
      setUrl("");
      setSecret("");
      setSelected(["*"]);
      setOpen(false);
    },
  });

  const toggleEvent = (type: string) => {
    setSelected((prev) => {
      if (type === "*") return ["*"];
      const withoutAll = prev.filter((t) => t !== "*");
      return withoutAll.includes(type)
        ? withoutAll.filter((t) => t !== type)
        : [...withoutAll, type];
    });
  };

  return (
    <>
      <Button variant="secondary" onClick={() => setOpen(true)}>
        <Plus className="size-4" aria-hidden="true" />
        Подключить вебхук
      </Button>
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        titleId="webhook-create-title"
        title="Новый вебхук"
      >
        <form
          className="mt-6 space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (url.trim() && secret.trim()) create.mutate();
          }}
        >
          <Input
            label="URL"
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://your-channel.com/webhook"
            required
          />
          <div className="flex flex-col gap-2">
            <Input
              label="Секретный ключ"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder="Такой же, как на вашей стороне"
              required
              minLength={8}
              hint="Им подписывается каждое сообщение (HMAC-SHA256), чтобы вы могли проверить отправителя."
            />
          </div>
          <fieldset className="block text-sm">
            <legend className="mb-2 text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
              События
            </legend>
            <div className="space-y-2">
              {["*", ...ALL_EVENTS].map((type) => (
                <label key={type} className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={selected.includes(type)}
                    onChange={() => toggleEvent(type)}
                    className="size-4 rounded border-border-strong"
                  />
                  <span>{type === "*" ? "Все события" : EVENT_LABELS[type]}</span>
                  {type !== "*" ? (
                    <span className="font-mono text-xs text-text-tertiary">
                      {type}
                    </span>
                  ) : null}
                </label>
              ))}
            </div>
          </fieldset>
          {create.isError ? (
            <p role="alert" className="text-sm text-feedback-error-text">
              {humanError(
                create.error,
                "Не удалось создать вебхук. Проверьте URL и попробуйте ещё раз.",
              )}
            </p>
          ) : null}
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={() => setOpen(false)}>
              Отмена
            </Button>
            <Button
              type="submit"
              loading={create.isPending}
              disabled={!url.trim() || secret.trim().length < 8}
            >
              Создать
            </Button>
          </div>
        </form>
      </Modal>
    </>
  );
}
