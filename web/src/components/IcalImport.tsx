import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";

import { partner } from "@/api/client";
import type { UnitTypeOut } from "@/api/types";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Modal } from "@/components/ui/Modal";
import { humanError } from "@/utils/errors";

/**
 * iCal import per unit type. An external calendar's blocked dates become
 * stop sell in our inventory; free dates stay bookable. Paid bookings are
 * never cancelled by an import - the feed closes the date, the booking stays.
 */
export function IcalImport({ propertyId }: { propertyId: string }) {
  const { data: unitTypes } = useQuery({
    queryKey: ["unit-types", propertyId],
    queryFn: () => partner.listUnitTypes(propertyId),
  });

  if (!unitTypes || unitTypes.length === 0) {
    return null;
  }

  return (
    <section className="mt-10">
      <h2 className="font-serif text-2xl font-normal">iCal-импорт</h2>
      <p className="mt-2 max-w-2xl text-sm text-text-secondary">
        Заблокированные даты из внешнего календаря закрывают номер у нас.
        Импорт не отменяет уже оплаченные брони и не снимает ваши ручные
        закрытия дат.
      </p>
      <ul className="mt-4 space-y-4">
        {unitTypes.map((unitType) => (
          <IcalImportRow key={unitType.id} unitType={unitType} />
        ))}
      </ul>
    </section>
  );
}

function IcalImportRow({ unitType }: { unitType: UnitTypeOut }) {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);

  const { data: sub, isPending } = useQuery({
    queryKey: ["ical-import", unitType.id],
    queryFn: ({ signal }) => partner.icalImport.get(unitType.id, signal),
    // 404 = no subscription yet, not an error worth alerting about.
    retry: false,
  });

  // Show the subscribed URL once it loads; a controlled input needs the value
  // in state, defaultValue is ignored once value is set.
  useEffect(() => {
    if (sub?.url && !url) setUrl(sub.url);
  }, [sub?.url, url]);

  const save = useMutation({
    mutationFn: () => partner.icalImport.set(unitType.id, url.trim()),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["ical-import", unitType.id] }),
  });

  const remove = useMutation({
    mutationFn: () => partner.icalImport.remove(unitType.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["ical-import", unitType.id] });
      setConfirmOpen(false);
    },
  });

  const syncNow = useMutation({
    mutationFn: () => partner.icalImport.sync(unitType.id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["ical-import", unitType.id] }),
  });

  const synced = sub?.last_synced_at
    ? new Date(sub.last_synced_at).toLocaleString("ru-RU")
    : null;

  return (
    <li className="rounded-xl border border-border-default bg-surface-card p-4">
      <p className="font-medium">{unitType.name}</p>

      <form
        className="mt-3 flex flex-col gap-2 sm:flex-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (url.trim()) save.mutate();
        }}
      >
        <Input
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://…"
          aria-label={`Ссылка iCal для импорта в ${unitType.name}`}
          required
        />
        <Button type="submit" loading={save.isPending} disabled={!url.trim()}>
          Сохранить
        </Button>
      </form>

      {isPending ? (
        <p role="status" className="mt-2 text-xs text-text-secondary">
          Загружаем подписку…
        </p>
      ) : sub ? (
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-text-secondary">
          {sub.last_status === "ok" && synced ? (
            <span>
              Синхронизировано {synced}, заблокировано дат:{" "}
              {sub.last_blocked}
            </span>
          ) : sub.last_status === "error" ? (
            <span role="alert" className="text-feedback-error-text">
              Ошибка синхронизации. {sub.last_error ?? "Попробуйте ещё раз."}
            </span>
          ) : (
            <span>Ждёт первой синхронизации.</span>
          )}
          <Button
            variant="secondary"
            size="sm"
            loading={syncNow.isPending}
            onClick={() => syncNow.mutate()}
          >
            Синхронизировать сейчас
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setConfirmOpen(true)}
          >
            <Trash2 className="size-4" aria-hidden="true" />
            Отключить импорт
          </Button>
        </div>
      ) : null}

      {save.isError ? (
        <p role="alert" className="mt-2 text-xs text-feedback-error-text">
          {humanError(save.error, "Не удалось сохранить ссылку. Попробуйте ещё раз.")}
        </p>
      ) : null}
      {syncNow.isError ? (
        <p role="alert" className="mt-2 text-xs text-feedback-error-text">
          Не удалось синхронизировать. Попробуйте ещё раз.
        </p>
      ) : null}

      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        titleId={`ical-remove-${unitType.id}`}
        title="Отключить импорт?"
      >
        <p className="mt-2 text-text-secondary">
          Календарь перестанет обновляться. Уже импортированные закрытия дат
          останутся, снимайте их вручную.
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
            Отключить импорт
          </Button>
        </div>
      </Modal>
    </li>
  );
}
