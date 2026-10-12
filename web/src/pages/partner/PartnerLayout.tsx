import { Link, Outlet } from "react-router-dom";

export function PartnerLayout() {
  return (
    <div>
      <div className="border-b border-border-default bg-surface-sunken">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-1 px-4 py-3 text-sm sm:px-6 lg:px-8">
          <Link
            to="/partner"
            className="inline-flex min-h-10 items-center rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:bg-interactive-hover hover:text-text-primary focus-visible:shadow-focus-ring"
          >
            Объекты
          </Link>
          <Link
            to="/partner/calendar"
            className="inline-flex min-h-10 items-center rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:bg-interactive-hover hover:text-text-primary focus-visible:shadow-focus-ring"
          >
            Шахматка
          </Link>
          <Link
            to="/partner/bookings"
            className="inline-flex min-h-10 items-center rounded-button px-3 py-2 text-text-secondary transition-colors duration-micro hover:bg-interactive-hover hover:text-text-primary focus-visible:shadow-focus-ring"
          >
            Бронирования
          </Link>
        </div>
      </div>
      <Outlet />
    </div>
  );
}
