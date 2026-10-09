import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Navigate, Link, useLocation, useNavigate } from "react-router-dom";

import { auth, getToken, isAdmin, setToken } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { PageShell } from "@/components/PageShell";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { isTooManyAttempts, TOO_MANY_ATTEMPTS } from "@/utils/errors";

/**
 * Partner login. On success the token lands in localStorage and the query
 * cache is cleared, so anything fetched while anonymous is dropped before the
 * cabinet mounts — no full page reload.
 */
export function LoginPage() {
  useDocumentTitle("Вход для партнёра");
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Where to send the partner after a successful login. The expired-session
  // sign-out passes the current page as a query param; a guarded route passes
  // it through location.state.
  const searchFrom = new URLSearchParams(location.search).get("from");
  const from =
    (location.state as { from?: string } | null)?.from ?? searchFrom ?? "/partner";
  const [error, setError] = useState<string | null>(
    // The expired-session sign-out lands here with ?from=<page>.
    searchFrom !== null ? "Сессия истекла. Войдите снова." : null,
  );
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const login = useMutation({
    mutationFn: () => auth.login(email, password),
    onSuccess: (data) => {
      setToken(data.access_token);
      setError(null);
      // Drop anything fetched while anonymous, then route client-side.
      queryClient.clear();
      navigate(from, { replace: true });
    },
    onError: (exc) =>
      setError(
        isTooManyAttempts(exc)
          ? TOO_MANY_ATTEMPTS
          : "Неверный email или пароль. Попробуйте ещё раз.",
      ),
  });

  // Already signed in - no point showing the form.
  // A staff session is not the partner's; staff still needs this form.
  if (getToken() && !isAdmin()) {
    return <Navigate to={from} replace />;
  }

  return (
    <PageShell className="mx-auto max-w-md">
      <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
        Партнёрам
      </p>
      <h1 className="mt-4 font-serif text-4xl font-normal leading-display tracking-tight">
        Вход для партнёра
      </h1>
      <p className="mt-4 text-text-secondary">
        Управляйте объектами, ценами и бронированиями.
      </p>

      <form
        className="mt-10 space-y-5"
        onSubmit={(e) => {
          e.preventDefault();
          login.mutate();
        }}
      >
        <Input
          label="Email"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
          placeholder="name@example.com"
          required
        />
        <Input
          label="Пароль"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
        />

        {error ? (
          <p role="alert" className="text-sm text-feedback-error-text">
            {error}
          </p>
        ) : null}

        <Button
          type="submit"
          loading={login.isPending}
          className="w-full"
          size="lg"
        >
          Войти
        </Button>
      </form>

      <p className="mt-6 text-center text-xs text-text-secondary">
        Демо-доступ: demo@example.com / demo-password
      </p>
      <p className="mt-3 text-center text-xs text-text-secondary">
        Нет аккаунта?{" "}
        <Link to="/register" className="text-text-link hover:text-text-link-hover">
          Зарегистрироваться
        </Link>
      </p>
    </PageShell>
  );
}
