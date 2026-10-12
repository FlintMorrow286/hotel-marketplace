import { useState } from "react";
import { AlertCircle, CheckCircle2, Info, Star, X } from "lucide-react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Modal } from "@/components/ui/Modal";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";

/**
 * States harness - every variant x state of the base components in one page.
 * Used for visual + gate review (see ux-ui-agent-skills design-component skill:
 * "a component is only correct when every variant x state renders right").
 * Not linked in the app nav; route-only (/components).
 */
export function ComponentsPage() {
  useDocumentTitle("Компоненты");
  const [modalOpen, setModalOpen] = useState(false);
  const [text, setText] = useState("");

  return (
    <div className="mx-auto max-w-[1120px] px-4 py-16 sm:px-6 lg:px-8">
      <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
        Дизайн-система
      </p>
      <h1 className="mt-4 font-serif text-4xl font-normal leading-display tracking-tight">
        Компоненты — состояния
      </h1>

      <Section title="Button">
        <Row label="default">
          <Button>Забронировать</Button>
        </Row>
        <Row label="secondary">
          <Button variant="secondary">Подробнее</Button>
        </Row>
        <Row label="destructive">
          <Button variant="destructive">Отменить бронь</Button>
        </Row>
        <Row label="ghost">
          <Button variant="ghost">Закрыть</Button>
        </Row>
        <Row label="sizes">
          <Button size="sm">sm</Button>
          <Button size="md">md</Button>
          <Button size="lg">lg</Button>
        </Row>
        <Row label="disabled">
          <Button disabled>Недоступно</Button>
        </Row>
        <Row label="loading">
          <Button loading>Оплачиваем…</Button>
        </Row>
        <Row label="selected (aria-pressed)">
          <Button selected>Выбрано</Button>
        </Row>
      </Section>

      <Section title="Input">
        <Row label="default">
          <Input
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Ваше имя"
          />
        </Row>
        <Row label="error (aria-invalid)">
          <Input type="email" placeholder="email@example.com" hasError />
        </Row>
        <Row label="disabled">
          <Input type="text" placeholder="Поле недоступно" disabled />
        </Row>
      </Section>

      <Section title="Badge">
        <Row label="variants">
          <Badge variant="neutral">Стандарт</Badge>
          <Badge variant="primary">Мгновенная бронь</Badge>
          <Badge variant="success">Подтверждено</Badge>
          <Badge variant="warning">Заезд завтра</Badge>
          <Badge variant="error">Отменено</Badge>
        </Row>
        <Row label="with icon (not color-only)">
          <Badge variant="success">
            <CheckCircle2 className="size-3" aria-hidden="true" /> Оплачено
          </Badge>
          <Badge variant="warning">
            <AlertCircle className="size-3" aria-hidden="true" /> Ожидание
            оплаты истекает
          </Badge>
          <Badge variant="error">
            <X className="size-3" aria-hidden="true" /> Отказано
          </Badge>
        </Row>
      </Section>

      <Section title="Modal">
        <Row label="open via trigger">
          <Button onClick={() => setModalOpen(true)}>Открыть диалог</Button>
        </Row>
        <p className="mt-2 text-sm text-text-secondary">
          Фокус внутри диалога зациклен, Escape закрывает, фокус возвращается на
          кнопку. Клик по затемнению закрывает.
        </p>
      </Section>

      <Section title="Icons (lucide, currentColor)">
        <Row label="inline SVG">
          <Star className="size-5 text-text-primary" aria-hidden="true" />
          <Info className="size-5 text-text-secondary" aria-hidden="true" />
          <CheckCircle2 className="size-5 text-feedback-success-text" aria-hidden="true" />
          <AlertCircle className="size-5 text-feedback-error-text" aria-hidden="true" />
        </Row>
      </Section>

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        titleId="demo-modal-title"
        title="Отмена брони"
      >
        <p className="mt-2 text-text-secondary">
          Отмена бесплатна за сутки до заезда. После этого срока списывается
          штраф.
        </p>
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setModalOpen(false)}>
            Отмена
          </Button>
          <Button
            variant="destructive"
            onClick={() => setModalOpen(false)}
            autoFocus
          >
            Отменить бронь
          </Button>
        </div>
      </Modal>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-10">
      <h2 className="font-serif text-2xl font-normal">{title}</h2>
      <div className="mt-4 space-y-4 rounded-xl border border-border-default bg-surface-card p-5">
        {children}
      </div>
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
      <span className="w-44 shrink-0 font-mono text-xs text-text-secondary">
        {label}
      </span>
      <div className="flex flex-wrap items-center gap-3">{children}</div>
    </div>
  );
}
