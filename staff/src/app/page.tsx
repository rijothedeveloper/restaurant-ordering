"use client";

import { useEffect, useState, type FormEvent } from "react";
import {
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut,
  type Unsubscribe,
} from "firebase/auth";

import {
  getStaffAuth,
  getStaffIdentity,
  type StaffIdentity,
} from "@/lib/auth";

import Kitchen from "@/components/kitchen";

type View =
  | { kind: "loading" }
  | { kind: "signed_out" }
  | { kind: "staff"; identity: StaffIdentity }
  | { kind: "error"; message: string; signedIn: boolean };

export default function StaffPage() {
  const [view, setView] = useState<View>({ kind: "loading" });
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    let generation = 0;
    let unsubscribe: Unsubscribe | undefined;

    async function start() {
      try {
        const auth = getStaffAuth();
        await auth.authStateReady();

        if (!active) return;

        unsubscribe = onAuthStateChanged(auth, (user) => {
          if (!active) return;

          const current = ++generation;

          if (!user) {
            setView({ kind: "signed_out" });
            return;
          }

          setView({ kind: "loading" });

          void getStaffIdentity(user).then(
            (identity) => {
              if (active && current === generation) {
                setView({ kind: "staff", identity });
              }
            },
            (error: unknown) => {
              if (active && current === generation) {
                setView({
                  kind: "error",
                  message:
                    error instanceof Error
                      ? error.message
                      : "Staff access could not be checked.",
                  signedIn: true,
                });
              }
            },
          );
        });
      } catch {
        if (active) {
          setView({
            kind: "error",
            message:
              "Staff sign-in is unavailable. Check the app configuration.",
            signedIn: false,
          });
        }
      }
    }

    void start();

    return () => {
      active = false;
      generation += 1;
      unsubscribe?.();
    };
  }, [attempt]);

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setFormError("");

    try {
      await signInWithEmailAndPassword(
        getStaffAuth(),
        email.trim(),
        password,
      );
      setPassword("");
    } catch {
      setFormError(
        "Sign-in failed. Check your email and password, then try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    setBusy(true);
    setFormError("");

    try {
      await signOut(getStaffAuth());
    } catch {
      setFormError("Sign-out failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  if (view.kind === "staff") {
  return (
    <Kitchen
      key={view.identity.user_id}
      onSignOut={logout}
      signingOut={busy}
      signOutError={formError}
    />
  );
}

  return (
    <main>
      <section className="card" aria-labelledby="page-title">
        <p className="eyebrow">RESTAURANT STAFF</p>
        <h1 id="page-title">Kitchen sign-in</h1>

        {view.kind === "loading" && (
          <p role="status">Checking staff access…</p>
        )}

        {view.kind === "signed_out" && (
          <form onSubmit={login}>
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              disabled={busy}
            />

            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={busy}
            />

            <button type="submit" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
        )}

        {view.kind === "error" && (
          <>
            <p role="alert">{view.message}</p>

            <div className="actions">
              <button
                className="secondary"
                disabled={busy}
                onClick={() => {
                  setView({ kind: "loading" });
                  setAttempt((value) => value + 1);
                }}
              >
                Try again
              </button>

              {view.signedIn && (
                <button onClick={logout} disabled={busy}>
                  Sign out
                </button>
              )}
            </div>
          </>
        )}

        {formError && <p role="alert">{formError}</p>}
      </section>
    </main>
  );
}