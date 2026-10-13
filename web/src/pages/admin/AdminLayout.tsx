import { Link, Outlet, Navigate, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { isAdmin, setToken } from "@/api/client";
/**
 * Staff area. One token store serves both partner and admin sessions; the
 * scope is decided by which endpoint issued it (/v1/admin/login here), so the
 * two cabinets never mix.
 */
export function AdminLayout() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // The login page lives at /admin/login (outside this layout) to avoid a
  // redirect loop; it sends staff back here on success.
  // A partner token is a real, valid session — just not this one. Send staff
  // to their own login; the partner keeps their cabinet.
  if (!isAdmin()) {
    return <Navigate to="/admin/login" state={{ from: "/admin" }} replace />;
  }

  return (
    <div>
      <div className="border-b border-border-default bg-surface-sunken">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-1 px-4 py-3 text-sm sm:px-6 lg:px-8">
          <nav className="flex min-w-0 flex-wrap items-center gap-1">
            <Link
              to="/admin"
              className="inline-flex min-h-10 items-center rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:bg-interactive-hover hover:text-text-primary focus-visible:shadow-focus-ring"
            >
              Отчёты
            </Link>
            <Link
              to="/admin/moderation"
              className="inline-flex min-h-10 items-center rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:bg-interactive-hover hover:text-text-primary focus-visible:shadow-focus-ring"
            >
              Модерация
            </Link>
            <Link
              to="/admin/outbox"
              className="inline-flex min-h-10 items-center rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:bg-interactive-hover hover:text-text-primary focus-visible:shadow-focus-ring"
            >
              События
            </Link>
            <Link
              to="/admin/emails"
              className="inline-flex min-h-10 items-center rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:bg-interactive-hover hover:text-text-primary focus-visible:shadow-focus-ring"
            >
              Письма
            </Link>
            <Link
              to="/admin/payouts"
              className="inline-flex min-h-10 items-center rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:bg-interactive-hover hover:text-text-primary focus-visible:shadow-focus-ring"
            >
              Выплаты
            </Link>
            <Link
              to="/admin/reconciliation"
              className="inline-flex min-h-10 items-center rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:bg-interactive-hover hover:text-text-primary focus-visible:shadow-focus-ring"
            >
              Сверка
            </Link>
          </nav>
          <button
            type="button"
            onClick={() => {
              setToken(null);
              // Staff cache must not survive the token.
              queryClient.clear();
              navigate("/", { replace: true });
            }}
            className="min-h-10 rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:bg-interactive-hover hover:text-text-primary focus-visible:shadow-focus-ring"
          >
            Выйти
          </button>
        </div>
      </div>
      <Outlet />
    </div>
  );
}
