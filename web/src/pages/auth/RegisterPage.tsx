import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Navigate, Link, useLocation, useNavigate } from "react-router-dom";

import { ApiException, auth, getToken, isAdmin, setToken } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { PageShell } from "@/components/PageShell";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { isTooManyAttempts, TOO_MANY_ATTEMPTS } from "@/utils/errors";

/**
 * Partner self-registration. On success the token lands in localStorage and
 * the query cache is cleared, then the app routes into the cabinet — no full
 * page reload.
 */
export function RegisterPage() {
  useDocumentTitle("Регистрация партнёра");
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Where to send the partner once the account exists.
  const from = (location.state as { from?: string } | null)?.from ?? "/partner";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const register = useMutation({
    mutationFn: () => auth.register(email, password, name),
    onSuccess: (data) => {
      setToken(data.access_token);
      setError(null);
      // Drop anything fetched while anonymous, then route client-side.
      queryClient.clear();
      navigate(from, { replace: true });
    },
    onError: (exc) => {
      if (exc instanceof ApiException && exc.status === 409) {
        setError("Партнёр с таким email уже существует. Войдите вместо регистрации.");
      } else if (isTooManyAttempts(exc)) {
        setError(TOO_MANY_ATTEMPTS);
      } else {
        setError("Не удалось зарегистрироваться. Попробуйте ещё раз.");
      }
    },
  });

  // Already signed in — no point showing the form.
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
        Регистрация партнёра
      </h1>
      <p className="mt-4 text-text-secondary">
        Заведите аккаунт и начните сдавать жильё.
      </p>

      <form
        className="mt-10 space-y-5"
        onSubmit={(e) => {
          e.preventDefault();
          register.mutate();
        }}
      >
        <Input
          label="Название"
          value={name}
          onChange={(e) => setName(e.target.value)}
          autoComplete="organization"
          placeholder="Гостевой дом «Маяк»"
          required
          minLength={2}
        />
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
          autoComplete="new-password"
          required
          minLength={6}
          hint="Минимум 6 символов."
        />

        {error ? (
          <p role="alert" className="text-sm text-feedback-error-text">
            {error}
          </p>
        ) : null}

        <Button
          type="submit"
          loading={register.isPending}
          className="w-full"
          size="lg"
        >
          Зарегистрироваться
        </Button>
      </form>

      <p className="mt-6 text-center text-xs text-text-secondary">
        Уже есть аккаунт?{" "}
        <Link to="/login" className="text-text-link hover:text-text-link-hover">
          Войти
        </Link>
      </p>
    </PageShell>
  );
}
