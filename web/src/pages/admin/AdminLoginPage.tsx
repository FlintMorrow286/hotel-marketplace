import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { admin, isAdmin, setToken } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { PageShell } from "@/components/PageShell";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { isTooManyAttempts, TOO_MANY_ATTEMPTS } from "@/utils/errors";

/** Staff login. Separate endpoint from the partner cabinet (scope: admin). */
export function AdminLoginPage() {
  useDocumentTitle("Вход для администратора");
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const from =
    (location.state as { from?: string } | null)?.from ?? "/admin";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const login = useMutation({
    mutationFn: () => admin.login(email, password),
    onSuccess: (data) => {
      setToken(data.access_token);
      setError(null);
      // Anonymous cache must not survive the token.
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

  // A partner token must not bounce staff away from their own login form.
  if (isAdmin()) {
    return <Navigate to={from} replace />;
  }

  return (
    <PageShell className="mx-auto max-w-md">
      <p className="text-xs font-medium uppercase tracking-eyebrow text-text-secondary">
        Админка
      </p>
      <h1 className="mt-4 font-serif text-4xl font-normal leading-display tracking-tight">
        Вход для администратора
      </h1>
      <p className="mt-4 text-text-secondary">
        Отчёты по комиссии и модерация объектов.
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
        Демо-доступ: admin@example.com / admin-password
      </p>
    </PageShell>
  );
}
