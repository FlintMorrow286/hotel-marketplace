import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ImagePlus, Trash2 } from "lucide-react";

import { partner } from "@/api/client";
import type { Photo } from "@/api/types";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { humanError } from "@/utils/errors";

/**
 * Property photos: upload from the partner cabinet, rendered by the catalog.
 *
 * The first photo in the list is the listing's cover, and every response is the
 * whole list — the UI never reconstructs order locally.
 */
export function PropertyPhotos({
  propertyId,
  propertyName,
}: {
  propertyId: string;
  propertyName: string;
}) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <Button variant="secondary" onClick={() => setOpen(true)}>
        <ImagePlus className="size-4" aria-hidden="true" />
        Фото
      </Button>
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        titleId={`property-photos-${propertyId}`}
        title={`Фотографии — ${propertyName}`}
      >
        <PhotoManager
          propertyId={propertyId}
          onClose={() => setOpen(false)}
        />
      </Modal>
    </>
  );
}

function PhotoManager({
  propertyId,
  onClose,
}: {
  propertyId: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const { data: photos, isPending } = useQuery({
    queryKey: ["property-photos", propertyId],
    queryFn: ({ signal }) => partner.photos.list(propertyId, signal),
  });

  const upload = useMutation({
    mutationFn: (file: File) => partner.photos.upload(propertyId, file),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["property-photos", propertyId] });
      // The cover may have changed — the catalog must not serve a stale one.
      void queryClient.invalidateQueries({ queryKey: ["properties"] });
      void queryClient.invalidateQueries({ queryKey: ["property"] });
    },
    onError: (exc) =>
      setError(humanError(exc, "Не удалось загрузить фото. Попробуйте ещё раз.")),
  });

  const remove = useMutation({
    mutationFn: (photoId: string) => partner.photos.remove(propertyId, photoId),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["property-photos", propertyId] });
      void queryClient.invalidateQueries({ queryKey: ["properties"] });
      void queryClient.invalidateQueries({ queryKey: ["property"] });
    },
    onError: (exc) =>
      setError(humanError(exc, "Не удалось удалить фото. Попробуйте ещё раз.")),
  });

  const list = photos ?? [];

  return (
    <div className="mt-2">
      <p className="text-sm text-text-secondary">
        Первое фото — обложка в каталоге. Поддерживаются JPEG, PNG и WebP,
        до 10 МБ.
      </p>

      {isPending ? (
        <p role="status" className="mt-4 text-sm text-text-secondary">
          Загружаем фотографии…
        </p>
      ) : list.length === 0 ? (
        <p className="mt-4 rounded-lg border border-border-default bg-surface-sunken p-4 text-sm text-text-secondary">
          Фотографий пока нет. Каталог показывает аккуратный плейсхолдер, пока
          вы не загрузите первую.
        </p>
      ) : (
        <ul className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3">
          {list.map((photo, index) => (
            <PhotoTile
              key={photo.id}
              photo={photo}
              isCover={index === 0}
              onRemove={() => remove.mutate(photo.id)}
              removing={remove.isPending}
            />
          ))}
        </ul>
      )}

      <div className="mt-6 flex flex-wrap items-center gap-3">
        <label className="inline-flex h-size-control-md cursor-pointer items-center gap-2 rounded-button border border-border-strong bg-transparent px-4 text-sm font-medium text-text-primary transition-colors duration-micro hover:bg-interactive-hover focus-within:shadow-focus-ring">
          <ImagePlus className="size-4" aria-hidden="true" />
          <span>Выбрать фото</span>
          <input
            type="file"
            accept="image/jpeg,image/png,image/webp"
            className="sr-only"
            disabled={upload.isPending}
            onChange={(e) => {
              const file = e.target.files?.[0];
              // Reset so picking the same file again re-fires onChange.
              e.target.value = "";
              if (file) upload.mutate(file);
            }}
          />
        </label>
        <span className="text-xs text-text-tertiary">
          {list.length} из 12
        </span>
        {upload.isPending ? (
          <span role="status" className="text-xs text-text-secondary">
            Загружаем…
          </span>
        ) : null}
      </div>

      {error ? (
        <p role="alert" className="mt-4 text-sm text-feedback-error-text">
          {error}
        </p>
      ) : null}

      <div className="mt-6 flex justify-end">
        <Button type="button" variant="secondary" onClick={onClose}>
          Готово
        </Button>
      </div>
    </div>
  );
}

function PhotoTile({
  photo,
  isCover,
  onRemove,
  removing,
}: {
  photo: Photo;
  isCover: boolean;
  onRemove: () => void;
  removing: boolean;
}) {
  return (
    <li className="flex flex-col gap-2">
      <div className="relative overflow-hidden rounded-lg border border-border-default bg-surface-sunken">
        <img
          src={photo.thumb}
          alt={isCover ? "Обложка объекта" : "Фото объекта"}
          className="aspect-4/3 w-full object-cover"
          loading="lazy"
        />
        {isCover ? (
          <span className="absolute bottom-1.5 left-1.5 rounded-button bg-surface-card/95 px-2 py-0.5 text-[11px] font-medium uppercase tracking-eyebrow text-text-secondary">
            Обложка
          </span>
        ) : null}
      </div>
      <Button
        variant="ghost"
        size="sm"
        className="justify-start"
        loading={removing}
        onClick={onRemove}
      >
        <Trash2 className="size-4" aria-hidden="true" />
        Удалить
      </Button>
    </li>
  );
}
