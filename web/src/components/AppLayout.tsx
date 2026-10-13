import { Link, Outlet, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { getToken, isAdmin, setToken } from "@/api/client";
import { LEGAL_DOCUMENTS, legalPath } from "@/legal/documents";

export function AppLayout() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  return (
    <div className="flex min-h-screen flex-col bg-surface-page text-text-primary font-sans">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-modal focus:rounded-button focus:bg-action-primary focus:px-4 focus:py-2 focus:text-text-on-action focus:shadow-focus-ring"
      >
        К основному содержимому
      </a>
      <header className="border-b border-border-default bg-surface-page/90">
        <div className="mx-auto flex min-h-16 max-w-7xl flex-wrap items-center justify-between gap-x-4 gap-y-1 px-4 py-2 sm:flex-nowrap sm:py-0 sm:px-6 lg:px-8">
          <Link
            to="/"
            className="inline-flex min-h-10 items-center font-serif text-lg font-normal tracking-tight text-text-primary transition-colors duration-micro hover:text-text-secondary"
          >
            Выше&nbsp;неба
          </Link>
          <nav className="flex min-w-0 flex-wrap items-center gap-1 text-sm sm:flex-nowrap sm:gap-2">
            <NavLink to="/search">Номера</NavLink>
            <NavLink to="/my-booking">Моя бронь</NavLink>
            {getToken() && !isAdmin() ? (
              <>
                <NavLink to="/partner">Кабинет</NavLink>
                <button
                  type="button"
                  onClick={() => {
                    setToken(null);
                    // Anonymous cache must not survive the token.
                    queryClient.clear();
                    navigate("/", { replace: true });
                  }}
                  className="min-h-10 rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:text-text-primary focus-visible:shadow-focus-ring"
                >
                  Выйти
                </button>
              </>
            ) : (
              <NavLink to="/login">Партнёру</NavLink>
            )}
          </nav>
        </div>
      </header>
      <main id="main" tabIndex={-1} className="flex-1 focus:outline-none">
        <Outlet />
      </main>
      <footer className="border-t border-border-default py-8 text-sm text-text-secondary">
        <div className="mx-auto flex max-w-7xl flex-col gap-2 px-4 sm:flex-row sm:items-center sm:justify-between sm:px-6 lg:px-8">
          <p>Отдых в Коктебеле · мгновенное подтверждение · бесплатная отмена за сутки до заезда</p>
          <Link to="/admin" className="inline-flex min-h-10 items-center text-text-link hover:text-text-link-hover">
            Админка
          </Link>
        </div>
        <nav
          aria-label="Документы"
          className="mx-auto mt-2 flex max-w-7xl flex-wrap items-center gap-x-5 px-4 sm:px-6 lg:px-8"
        >
          {LEGAL_DOCUMENTS.map((doc) => (
            <Link
              key={doc.slug}
              to={legalPath(doc.slug)}
              className="inline-flex min-h-10 items-center hover:text-text-primary"
            >
              {doc.title}
            </Link>
          ))}
        </nav>
      </footer>
    </div>
  );
}

/** A plain nav link: ink-soft, ink when active or hovered. No buttons in the nav. */
function NavLink({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <Link
      to={to}
      className="inline-flex min-h-10 items-center rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:text-text-primary focus-visible:shadow-focus-ring"
    >
      {children}
    </Link>
  );
}
