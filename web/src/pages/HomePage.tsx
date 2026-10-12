import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { catalog } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { formatPriceFrom } from "@/utils/format";
import { propertyTypeLabel } from "@/api/types";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";

export function HomePage() {
  useDocumentTitle();
  const { data, isPending, isError } = useQuery({
    queryKey: ["properties"],
    queryFn: ({ signal }) =>
      catalog.list(
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        6,
        0,
        signal,
      ),
  });
  const properties = data?.items ?? [];
  return (
    <div>
      {/* hero — paper, not a SaaS gradient. An eyebrow, one serif headline, a
       * single primary button. Photography is gone (photos come through the
       * partner API now), so the type carries the whole section. */}
      <section className="border-b border-border-default">
        {/* ds-allow-hardcode:start - hero measure: fixed editorial measure,
         * not part of the spacing scale. */}
        <div className="mx-auto max-w-[1120px] px-4 py-24 sm:px-6 sm:py-32 lg:px-8">
          {/* ds-allow-hardcode:end */}
          <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
            Коктебель · Крым
          </p>
          <h1 className="mt-6 max-w-3xl font-serif text-display font-normal leading-display tracking-tight text-text-primary">
            Жильё у моря, в&nbsp;котором хочется остаться
          </h1>
          <p className="mt-6 max-w-xl text-lg leading-normal text-text-secondary">
            Отели, квартиры и апартаменты. Мгновенное подтверждение, 100%
            предоплата, бесплатная отмена за сутки до заезда.
          </p>
          <Link
            to="/search"
            className="mt-10 inline-flex h-size-control-lg items-center justify-center rounded-button bg-action-primary px-6 text-base font-medium text-text-on-action transition-colors duration-micro hover:bg-action-primary-hover active:bg-action-primary-active focus-visible:shadow-focus-ring"
          >
            Найти жильё
          </Link>
        </div>
      </section>

      {/* catalog — data from the API, never hardcoded */}
      <section className="mx-auto max-w-[1120px] px-4 py-24 sm:px-6 lg:px-8">
        <div className="flex items-baseline justify-between">
          <h2 className="font-serif text-3xl font-normal tracking-tight">
            Объекты
          </h2>
          <p className="text-sm text-text-tertiary">
            {properties?.length ?? 0} в каталоге
          </p>
        </div>

        {isPending ? (
          <p role="status" className="mt-12 text-text-secondary">
            Загружаем объекты…
          </p>
        ) : isError ? (
          <div className="mt-12 flex flex-wrap items-center gap-4">
            <p role="alert" className="text-feedback-error-text">
              Не удалось загрузить объекты. Обновите страницу.
            </p>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => window.location.reload()}
            >
              Обновить страницу
            </Button>
          </div>
        ) : properties.length === 0 ? (
          <p className="mt-12 text-text-secondary">
            Пока нет опубликованных объектов. Каталог появится, как партнёры
            добавят жильё.
          </p>
        ) : (
          <div className="mt-12 grid gap-8 sm:grid-cols-2">
            {properties.map((property) => (
              <article
                key={property.id}
                className="flex flex-col overflow-hidden rounded-xl border border-border-default bg-surface-card transition-colors duration-micro hover:bg-interactive-hover"
              >
                {property.photos?.[0] ? (
                  <img
                    src={property.photos[0].thumb}
                    alt={property.name}
                    className="h-52 w-full object-cover"
                    loading="lazy"
                  />
                ) : (
                  <div
                    aria-hidden="true"
                    className="flex h-52 w-full items-center justify-center bg-surface-sunken text-sm text-text-tertiary"
                  >
                    Фото скоро появятся
                  </div>
                )}
                <div className="flex flex-1 flex-col p-6">
                  <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
                    {propertyTypeLabel(property.property_type)}
                  </p>
                  <h3 className="mt-2 font-serif text-xl font-normal leading-tight text-text-primary">
                    {property.name}
                  </h3>
                  <p className="mt-2 text-text-secondary">
                    {property.city || "Крым"}
                  </p>
                  <p className="mt-2 text-text-secondary">
                    {property.min_price
                      ? formatPriceFrom(property.min_price, property.currency)
                      : "Цена не указана"}
                  </p>
                  <Link
                    to={`/property/${property.id}`}
                    className="mt-6 inline-flex min-h-10 w-fit items-center text-sm font-medium text-text-primary underline-offset-[3px] transition-colors duration-micro hover:underline"
                  >
                    Подробнее
                  </Link>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
