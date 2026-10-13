import { Link, useParams } from "react-router-dom";

import { PageShell, PageTitle } from "@/components/PageShell";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useLegalInfo } from "@/hooks/useLegalInfo";
import { findLegalDocument, LEGAL_DOCUMENTS, legalPath } from "@/legal/documents";
import { NotFoundPage } from "@/pages/NotFoundPage";

/** One public document: offer, consent to personal data processing, privacy policy. */
export function LegalPage() {
  const { slug } = useParams();
  const doc = findLegalDocument(slug);
  const info = useLegalInfo();
  useDocumentTitle(doc?.title);

  if (!doc) return <NotFoundPage />;

  return (
    <PageShell>
      <PageTitle
        subtitle={info.data ? `Версия документов: ${info.data.docs_version}` : undefined}
      >
        {doc.title}
      </PageTitle>
      <div className="space-y-4 leading-relaxed">
        {doc.paragraphs.map((text) => (
          <p key={text}>{text}</p>
        ))}
      </div>
      <nav
        aria-label="Другие документы"
        className="mt-10 flex flex-wrap gap-x-6 border-t border-border-default pt-4 text-sm"
      >
        {LEGAL_DOCUMENTS.filter((other) => other.slug !== doc.slug).map((other) => (
          <Link
            key={other.slug}
            to={legalPath(other.slug)}
            className="inline-flex min-h-10 items-center text-text-link hover:text-text-link-hover"
          >
            {other.title}
          </Link>
        ))}
      </nav>
    </PageShell>
  );
}
