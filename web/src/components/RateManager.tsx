import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { partner } from "@/api/client";
import type { RatePlan, UnitTypeOut } from "@/api/types";
import { humanError } from "@/utils/errors";
import { plural } from "@/utils/plural";
import { daysBetween } from "@/utils/date";
import { formatPrice } from "@/utils/format";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

/**
 * Rates and prices per unit type.
 *
 * The price covers a date range in one call: the partner sets a season, not 90
 * individual days. Days without a price_day row fall back to the unit type's
 * base price, so a unit type is bookable before any rate plan exists.
 */
export function RateManager({ unitTypes }: { unitTypes: UnitTypeOut[] }) {
  const [selected, setSelected] = useState<string | null>(null);
  const active = unitTypes.find((u) => u.id === selected) ?? unitTypes[0];

  if (unitTypes.length === 0) return null;

  return (
    <section className="mt-10">
      <h2 className="font-serif text-2xl font-normal">Цены</h2>
      <p className="mt-2 max-w-2xl text-sm text-text-secondary">
        Цена действует на диапазон дат целиком. Дни без явной цены берут
        базовую стоимость номера.
      </p>

      <div className="mt-4">
        <label className="block">
          <span className="mb-2 block text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
            Номер
          </span>
          <select
            value={active?.id}
            onChange={(e) => setSelected(e.target.value)}
            className="h-size-control-field w-full rounded-lg border border-border-strong bg-surface-card px-4 text-base text-text-primary focus:border-border-focus focus:outline-none focus-visible:shadow-focus-ring"
          >
            {unitTypes.map((unit) => (
              <option key={unit.id} value={unit.id}>
                {unit.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      {active ? <PriceEditor unitType={active} /> : null}
    </section>
  );
}

function PriceEditor({ unitType }: { unitType: UnitTypeOut }) {
  const queryClient = useQueryClient();
  const { data: plans } = useQuery({
    queryKey: ["rate-plans", unitType.id],
    queryFn: ({ signal }) => partner.ratePlans.list(unitType.id, signal),
  });

  // Create a default plan on first use rather than making the partner name one
  // before they can price a single night.
  const ensurePlan = useMutation({
    mutationFn: () =>
      partner.ratePlans.create({
        unit_type_id: unitType.id,
        name: "Основной тариф",
        cancellation_policy: "flexible",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["rate-plans", unitType.id],
      });
    },
  });

  if (!plans) return null;

  const plan = plans[0];

  return (
    <div className="mt-4 rounded-xl border border-border-default bg-surface-card p-5">
      {plan ? (
        <>
          <SetPriceForm unitType={unitType} plan={plan} />
          <CommissionField unitTypeId={unitType.id} plan={plan} />
        </>
      ) : (
        <div className="flex flex-wrap items-center gap-4">
          <p className="text-sm text-text-secondary">
            У номера ещё нет тарифа. Дни продаются по базовой цене{" "}
            <strong>{unitType.base_price}</strong> ₽.
          </p>
          <Button
            variant="secondary"
            size="sm"
            loading={ensurePlan.isPending}
            onClick={() => ensurePlan.mutate()}
          >
            Создать тариф
          </Button>
          {ensurePlan.isError ? (
            <p role="alert" className="text-sm text-feedback-error-text">
              {humanError(
                ensurePlan.error,
                "Не удалось создать тариф. Попробуйте ещё раз.",
              )}
            </p>
          ) : null}
        </div>
      )}
    </div>
  );
}

function SetPriceForm({
  unitType,
  plan,
}: {
  unitType: UnitTypeOut;
  plan: RatePlan;
}) {
  const queryClient = useQueryClient();
  const today = new Date().toISOString().slice(0, 10);
  const [from, setFrom] = useState(today);
  const [to, setTo] = useState(addDays(today, 30));
  const [price, setPrice] = useState<number>(unitType.base_price ?? 0);
  const [minStay, setMinStay] = useState(1);
  const setPrices = useMutation({
    mutationFn: () =>
      partner.prices.set({
        rate_plan_id: plan.id,
        date_from: from,
        date_to: to,
        price,
        min_stay: minStay,
      }),
    onSuccess: () => {
      // The chessboard's price column and the guest's availability read both
      // come from price_day.
      void queryClient.invalidateQueries({
        queryKey: ["calendar", unitType.property_id],
      });
      setPrice(0);
    },
  });

  const nights = to > from ? daysBetween(from, to) : 0;
  const valid = nights >= 1 && price >= 0;

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        setPrices.mutate();
      }}
    >
      <p className="text-sm font-medium">{plan.name}</p>
      <div className="grid grid-cols-2 gap-3">
        <Input
          label="Заезд"
          type="date"
          value={from}
          min={today}
          onChange={(e) => setFrom(e.target.value)}
          required
        />
        <Input
          label="Выезд"
          type="date"
          value={to}
          min={addDays(from, 1)}
          onChange={(e) => setTo(e.target.value)}
          required
        />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Input
          label="Цена за ночь, ₽"
          type="number"
          min={0}
          step={100}
          value={price}
          onChange={(e) => setPrice(Number(e.target.value) || 0)}
          required
        />
        <Input
          label="Минимум ночей"
          type="number"
          min={1}
          max={30}
          value={minStay}
          onChange={(e) => setMinStay(Number(e.target.value) || 1)}
          required
        />
      </div>
      {valid ? (
        <p className="text-sm text-text-secondary">
          {nights}{" "}
          {plural(nights, "ночь", "ночи", "ночей")} · итого{" "}
          {formatPrice(nights * price)}
        </p>
      ) : (
        <p className="text-sm text-feedback-error-text">
          Выезд должен быть позже заезда.
        </p>
      )}
      {setPrices.isError ? (
        <p role="alert" className="text-sm text-feedback-error-text">
          {humanError(
            setPrices.error,
            "Не удалось установить цену. Попробуйте ещё раз.",
          )}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" loading={setPrices.isPending} disabled={!valid}>
          Установить цену
        </Button>
        {setPrices.isSuccess ? (
          <p role="status" className="text-sm text-feedback-success-text">
            Цена применена к {nights}{" "}
            {plural(nights, "дню", "дням", "дням")}.
          </p>
        ) : null}
      </div>
    </form>
  );
}

/**
 * The platform's commission share for the nights this plan prices.
 *
 * `null` (the default) means the platform-wide rate applies; the partner sets
 * an explicit share to discount a key object. The booking snapshots the rate
 * at hold time, so a change only affects future stays.
 */
function CommissionField({
  unitTypeId,
  plan,
}: {
  unitTypeId: string;
  plan: RatePlan;
}) {
  const queryClient = useQueryClient();
  const [rate, setRate] = useState<string>(
    plan.commission_rate == null ? "" : String(plan.commission_rate * 100),
  );

  const patch = useMutation({
    mutationFn: () => {
      const n = Number(rate);
      return partner.ratePlans.patch(plan.id, {
        commission_rate: rate === "" || Number.isNaN(n) ? null : n / 100,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["rate-plans", unitTypeId] });
    },
  });

  return (
    <form
      className="mt-5 border-t border-border-default pt-5"
      onSubmit={(e) => {
        e.preventDefault();
        patch.mutate();
      }}
    >
      <div className="flex flex-wrap items-end gap-3">
        <Input
          label="Комиссия платформы, %"
          type="number"
          min={0}
          max={100}
          step={1}
          value={rate}
          placeholder="по умолчанию"
          onChange={(e) => setRate(e.target.value)}
          className="w-44"
        />
        <Button type="submit" variant="secondary" size="sm" loading={patch.isPending}>
          Применить
        </Button>
        <p className="text-sm text-text-tertiary">
          {plan.commission_rate == null
            ? "Сейчас действует ставка платформы по умолчанию."
            : `Действует ${plan.commission_rate * 100}% с каждой брони.`}
        </p>
      </div>
      {patch.isError ? (
        <p role="alert" className="mt-3 text-sm text-feedback-error-text">
          {humanError(patch.error, "Не удалось сохранить комиссию. Попробуйте ещё раз.")}
        </p>
      ) : null}
      {patch.isSuccess ? (
        <p role="status" className="mt-3 text-sm text-feedback-success-text">
          Ставка применена к новым броням.
        </p>
      ) : null}
    </form>
  );
}

function addDays(iso: string, delta: number): string {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + delta);
  return d.toISOString().slice(0, 10);
}

