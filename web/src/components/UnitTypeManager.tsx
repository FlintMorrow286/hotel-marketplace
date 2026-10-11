import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";

import { partner } from "@/api/client";
import type { UnitTypeOut } from "@/api/types";
import { humanError } from "@/utils/errors";
import { plural } from "@/utils/plural";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Modal } from "@/components/ui/Modal";

/**
 * Unit types of one property: the rows of the chessboard and the thing guests
 * actually book. A partner with no unit types has an empty calendar and an
 * unbookable listing, so creating them is the step right after the property.
 */
export function UnitTypeManager({ propertyId }: { propertyId: string }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const { data: unitTypes } = useQuery({
    queryKey: ["unit-types", propertyId],
    queryFn: ({ signal }) => partner.listUnitTypes(propertyId, signal),
  });

  const list = unitTypes ?? [];

  function refresh() {
    void queryClient.invalidateQueries({
      queryKey: ["unit-types", propertyId],
    });
    // The chessboard renders one row per unit type, so it follows along.
    void queryClient.invalidateQueries({ queryKey: ["calendar", propertyId] });
  }

  return (
    <section className="mt-10">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="font-serif text-2xl font-normal">Номера</h2>
          <p className="mt-2 max-w-2xl text-sm text-text-secondary">
            Типы номеров этого объекта — это то, что бронируют гости. Каждый
            становится строкой в шахматке.
          </p>
        </div>
        <Button
          variant="secondary"
          onClick={() => setOpen(true)}
          className="shrink-0"
        >
          <Plus className="size-4" aria-hidden="true" />
          Добавить номер
        </Button>
      </div>

      {list.length === 0 ? (
        <p className="mt-4 rounded-xl border border-border-default bg-surface-card p-6 text-sm text-text-secondary">
          У объекта ещё нет номеров. Добавьте первый — и в шахматке появится
          расписание по датам.
        </p>
      ) : (
        <ul className="mt-4 divide-y divide-border-default border-y border-border-default">
          {list.map((unit) => (
            <li
              key={unit.id}
              className="flex items-center justify-between gap-4 py-4"
            >
              <div>
                <p className="font-medium">{unit.name}</p>
                <p className="mt-1 text-sm text-text-secondary">
                  до {unit.capacity} человек · {unit.total_units}{" "}
                  {plural(unit.total_units, "номер", "номера", "номеров")}
                </p>
              </div>
              <UnitTypePrice unit={unit} onSaved={refresh} />
            </li>
          ))}
        </ul>
      )}
      <CreateUnitTypeModal
        propertyId={propertyId}
        open={open}
        onClose={() => setOpen(false)}
        onCreated={refresh}
      />
    </section>
  );
}

/**
 * The room's price, editable in place. base_price is the fallback every day
 * without a rate row reads, so saving here reprices the season the partner
 * never opened in RateManager. Click turns the number into a field; Enter or
 * blur saves, Esc puts it back.
 */
function UnitTypePrice({
  unit,
  onSaved,
}: {
  unit: UnitTypeOut;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(unit.base_price ?? 0);

  const save = useMutation({
    mutationFn: () => partner.updateUnitType(unit.id, { base_price: draft }),
    onSuccess: () => {
      setEditing(false);
      onSaved();
    },
  });

  function commit() {
    if (draft === unit.base_price) {
      setEditing(false);
      return;
    }
    save.mutate();
  }

  if (editing) {
    return (
      <form
        className="flex shrink-0 items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          commit();
        }}
      >
        <Input
          type="number"
          min={0}
          max={1000000}
          value={draft}
          onChange={(e) => setDraft(Number(e.target.value) || 0)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setDraft(unit.base_price ?? 0);
              setEditing(false);
            }
          }}
          className="w-28"
          aria-label={`Цена за ночь, ${unit.name}`}
        />
        <span className="text-sm text-text-secondary">₽</span>
        {save.isError ? (
          <p role="alert" className="text-sm text-feedback-error-text">
            {humanError(save.error, "Не удалось сохранить цену.")}
          </p>
        ) : null}
      </form>
    );
  }

  return (
    <button
      type="button"
      onClick={() => {
        setDraft(unit.base_price ?? 0);
        setEditing(true);
      }}
      className="inline-flex min-h-10 shrink-0 items-center text-sm font-medium text-text-primary underline-offset-[3px] transition-colors duration-micro hover:underline"
    >
      {unit.base_price ? `${unit.base_price} ₽ за ночь` : "Цена не задана"}
    </button>
  );
}

function CreateUnitTypeModal({
  propertyId,
  open,
  onClose,
  onCreated,
}: {
  propertyId: string;
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [capacity, setCapacity] = useState(2);
  const [totalUnits, setTotalUnits] = useState(1);
  const [basePrice, setBasePrice] = useState(0);

  const create = useMutation({
    mutationFn: () =>
      partner.createUnitType({
        property_id: propertyId,
        name,
        capacity,
        total_units: totalUnits,
        base_price: basePrice,
      }),
    onSuccess: () => {
      setName("");
      setCapacity(2);
      setTotalUnits(1);
      setBasePrice(0);
      onCreated();
      onClose();
    },
  });

  return (
    <Modal
      open={open}
      onClose={onClose}
      titleId="create-unit-type-title"
      title="Новый номер"
    >
      <p className="mt-2 text-sm text-text-secondary">
        Инвентарь создаётся сразу — календарь не будет пустым.
      </p>
      <form
        className="mt-6 space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Input
          label="Название"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Например: Стандарт с видом на море"
          required
          minLength={2}
        />
        <div className="grid grid-cols-2 gap-3">
          <Input
            label="Вместимость"
            type="number"
            min={1}
            max={20}
            value={capacity}
            onChange={(e) => setCapacity(Number(e.target.value) || 1)}
            required
          />
          <Input
            label="Всего номеров"
            type="number"
            min={1}
            max={100}
            value={totalUnits}
            onChange={(e) => setTotalUnits(Number(e.target.value) || 1)}
            required
          />
        </div>
        <Input
          label="Базовая цена за ночь, ₽"
          type="number"
          min={0}
          max={1000000}
          value={basePrice}
          onChange={(e) => setBasePrice(Number(e.target.value) || 0)}
          required
        />
        {create.isError ? (
          <p role="alert" className="text-sm text-feedback-error-text">
            {humanError(
              create.error,
              "Не удалось создать номер. Попробуйте ещё раз.",
            )}
          </p>
        ) : null}
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Отмена
          </Button>
          <Button
            type="submit"
            loading={create.isPending}
            disabled={name.length < 2}
          >
            Создать номер
          </Button>
        </div>
      </form>
    </Modal>
  );
}
