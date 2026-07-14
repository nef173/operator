"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { api, setToken, getToken, ApiError, type AuthUser } from "@/lib/api";

// Real sign-in gate wrapping the whole app. On mount it asks /api/auth/status:
//   authenticated → render the app
//   otherwise     → name/password sign-in
// There is NO self-serve "create your owner" screen: the app ships with a seeded owner
// login (admin/admin, see api/app/users.py) and owners create/rename everyone else from
// inside Settings → Access. The token lives in localStorage (see lib/api) and rides every
// request as a bearer header, because web + api are separate origins. Finance routes are
// ALSO enforced server-side; this gate is the front door, not the only lock.

type AuthState = { user: AuthUser | null; signOut: () => Promise<void> };
const AuthContext = createContext<AuthState>({ user: null, signOut: async () => {} });
export const useAuth = () => useContext(AuthContext);

type Phase = "loading" | "login" | "authed" | "unreachable";

export function AuthGate({ children }: { children: React.ReactNode }) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [user, setUser] = useState<AuthUser | null>(null);

  async function refresh() {
    try {
      const s = await api.authStatus();
      if (s.authenticated && s.user) {
        setUser(s.user);
        setPhase("authed");
      } else {
        // Token missing or expired — drop any stale one and show sign-in.
        if (getToken()) setToken(null);
        setPhase("login");
      }
    } catch {
      // API unreachable — DON'T fail open to the login form. Doing so makes a backend-config
      // problem (wrong NEXT_PUBLIC_API_BASE, API service down) look exactly like a sign-in
      // screen, which is a debugging trap. Show the real cause instead.
      setPhase("unreachable");
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  // Self-healing: "unreachable" is almost always the api mid-restart (every deploy causes a
  // ~30s window). Keep probing every 5s — even in background tabs — so the app comes back on
  // its own instead of parking the operator on an error screen until they click Retry.
  useEffect(() => {
    if (phase !== "unreachable") return;
    const id = setInterval(() => refresh(), 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  async function signOut() {
    try {
      await api.authLogout();
    } catch {
      /* best effort — clear locally regardless */
    }
    setToken(null);
    setUser(null);
    setPhase("login");
  }

  if (phase === "loading") {
    return (
      <div className="grid h-full place-items-center text-sm text-[var(--muted)]">Loading…</div>
    );
  }

  if (phase === "unreachable") {
    return (
      <div className="grid h-full place-items-center bg-[var(--bg)] px-4">
        <div className="w-full max-w-md rounded-2xl border border-[var(--border)] bg-[var(--card)] p-6 text-center shadow-sm">
          <div className="mb-1 flex items-center justify-center gap-2 text-lg font-semibold">
            <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-[var(--accent)] border-t-transparent" />
            Backend not answering — reconnecting…
          </div>
          <p className="mb-4 text-sm text-[var(--muted)]">
            Usually the api is just restarting (a deploy) and this clears by itself in under a
            minute — retrying automatically. If it keeps showing, check the API service is up and{" "}
            <code className="rounded bg-[var(--bg)] px-1">NEXT_PUBLIC_API_BASE</code> points at it.
          </p>
          <div className="mb-4 truncate rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-xs text-[var(--muted)]">
            API base: {process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8077 (default)"}
          </div>
          <button
            onClick={() => {
              setPhase("loading");
              refresh();
            }}
            className="w-full rounded-lg bg-[var(--accent)] px-3.5 py-2.5 text-sm font-semibold text-[var(--accent-fg)]"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (phase === "login") {
    return (
      <AuthForm
        onDone={(u) => {
          setUser(u);
          setPhase("authed");
        }}
      />
    );
  }

  return <AuthContext.Provider value={{ user, signOut }}>{children}</AuthContext.Provider>;
}

function AuthForm({ onDone }: { onDone: (u: AuthUser) => void }) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const res = await api.authLogin(name.trim(), password);
      setToken(res.token);
      onDone(res.user);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Try again.");
      setBusy(false);
    }
  }

  return (
    <div className="grid h-full place-items-center bg-[var(--bg)] px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-2xl border border-[var(--border)] bg-[var(--card)] p-6 shadow-sm"
      >
        <div className="mb-1 text-lg font-semibold">Sign in</div>
        <p className="mb-5 text-sm text-[var(--muted)]">
          Enter your name and password to open the operator console.
        </p>

        <Field label="Name">
          <input
            className={inputCls}
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoComplete="username"
          />
        </Field>

        <Field label="Password">
          <input
            className={inputCls}
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </Field>

        {error ? <div className="mb-3 text-sm text-red-500">{error}</div> : null}

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-lg bg-[var(--accent)] px-3.5 py-2.5 text-sm font-semibold text-[var(--accent-fg)] disabled:opacity-60"
        >
          {busy ? "Please wait…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

const inputCls =
  "w-full rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="mb-3 block">
      <span className="mb-1 block text-xs font-medium text-[var(--muted)]">{label}</span>
      {children}
    </label>
  );
}
