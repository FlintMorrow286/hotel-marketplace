import { HomePage } from "@/pages/HomePage";
import { SearchPage } from "@/pages/SearchPage";
import { PropertyPage } from "@/pages/PropertyPage";
import { CheckoutPage } from "@/pages/CheckoutPage";
import { BookingSuccessPage } from "@/pages/BookingSuccessPage";
import { PaymentReturnPage } from "@/pages/PaymentReturnPage";
import { MyBookingPage } from "@/pages/MyBookingPage";
import { LegalPage } from "@/pages/LegalPage";
import { LoginPage } from "@/pages/auth/LoginPage";
import { RegisterPage } from "@/pages/auth/RegisterPage";
import { ComponentsPage } from "@/pages/ComponentsPage";
import { PartnerLayout } from "@/pages/partner/PartnerLayout";
import { PartnerDashboardPage } from "@/pages/partner/PartnerDashboardPage";
import { PartnerCalendarPage } from "@/pages/partner/PartnerCalendarPage";
import { PartnerBookingsPage } from "@/pages/partner/PartnerBookingsPage";

import { AdminLayout } from "@/pages/admin/AdminLayout";
import { AdminLoginPage } from "@/pages/admin/AdminLoginPage";
import { AdminReportsPage } from "@/pages/admin/AdminReportsPage";
import { AdminModerationPage } from "@/pages/admin/AdminModerationPage";
import { AdminEmailPage } from "@/pages/admin/AdminEmailPage";
import { AdminOutboxPage } from "@/pages/admin/AdminOutboxPage";
import { AdminReconciliationPage } from "@/pages/admin/AdminReconciliationPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import type { RouteObject } from "react-router-dom";

import { AppLayout } from "@/components/AppLayout";


export const routes: RouteObject[] = [
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "search", element: <SearchPage /> },
      { path: "property/:id", element: <PropertyPage /> },
      { path: "checkout/:bookingId", element: <CheckoutPage /> },
      // Where the payment page sends the guest back: asks the provider and moves on.
      { path: "checkout/:bookingId/return", element: <PaymentReturnPage /> },
      { path: "booking/:bookingId/success", element: <BookingSuccessPage /> },
      { path: "login", element: <LoginPage /> },
      // Reachable from the login page; without this the link 404s.
      { path: "register", element: <RegisterPage /> },
      // The guest returns to a booking by code: linked from the header and
      // from the checkout success screen.
      { path: "my-booking", element: <MyBookingPage /> },
      // Offer, consent to personal data processing, privacy policy: linked from
      // the booking form (new tab) and from the footer.
      { path: "legal/:slug", element: <LegalPage /> },
      // States harness for the base components - gate/visual review only.
      { path: "components", element: <ComponentsPage /> },
      {
        path: "partner",
        element: <PartnerLayout />,
        children: [
          { index: true, element: <PartnerDashboardPage /> },
          { path: "calendar", element: <PartnerCalendarPage /> },
          { path: "bookings", element: <PartnerBookingsPage /> },
        ],
      },
      { path: "admin/login", element: <AdminLoginPage /> },
      {
        path: "admin",
        element: <AdminLayout />,
        children: [
          { index: true, element: <AdminReportsPage /> },
          { path: "moderation", element: <AdminModerationPage /> },
          { path: "outbox", element: <AdminOutboxPage /> },
          { path: "emails", element: <AdminEmailPage /> },
          { path: "reconciliation", element: <AdminReconciliationPage /> },
        ],
      },
      // The SPA fallback serves index.html on any URL, so an unknown path still
      // runs the app - it needs a real screen, not a blank one.
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
