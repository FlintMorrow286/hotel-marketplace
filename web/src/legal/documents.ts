/**
 * The public documents a guest accepts before booking.
 *
 * The text here is a placeholder: the offer, the consent to personal data processing
 * and the privacy policy are being agreed with a lawyer (docs/LEGAL.md). To publish the
 * final text, replace `paragraphs` and raise LEGAL_DOCS_VERSION in app/config/legal.py,
 * so that bookings record which version the guest saw. The version itself is not kept
 * here — the page reads it from GET /v1/legal.
 */

export type LegalSlug = "offer" | "personal-data" | "privacy";

export interface LegalDocument {
  slug: LegalSlug;
  title: string;
  paragraphs: string[];
}

const DRAFT =
  "Документ находится на согласовании и пока не имеет юридической силы. " +
  "Окончательный текст будет опубликован до приёма реальных платежей.";

export const LEGAL_DOCUMENTS: readonly LegalDocument[] = [
  { slug: "offer", title: "Публичная оферта", paragraphs: [DRAFT] },
  {
    slug: "personal-data",
    title: "Согласие на обработку персональных данных",
    paragraphs: [DRAFT],
  },
  { slug: "privacy", title: "Политика конфиденциальности", paragraphs: [DRAFT] },
];

export function legalPath(slug: LegalSlug): string {
  return `/legal/${slug}`;
}

export function findLegalDocument(slug: string | undefined): LegalDocument | undefined {
  return LEGAL_DOCUMENTS.find((doc) => doc.slug === slug);
}
