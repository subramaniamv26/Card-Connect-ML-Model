"use client";

import { FormEvent, Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    const res = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: f.get("username"), password: f.get("password") }),
    });
    setBusy(false);
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      setError(d.detail ?? "Sign-in failed");
      return;
    }
    const next = params.get("next");
    router.replace(next && next.startsWith("/") && !next.startsWith("//") ? next : "/");
  }

  return (
    <form onSubmit={submit} className="stack" aria-describedby={error ? "login-error" : undefined}>
      <label className="field"><span className="field-label">Username</span>
        <input name="username" autoComplete="username" required autoFocus /></label>
      <label className="field"><span className="field-label">Password</span>
        <input name="password" type="password" autoComplete="current-password" required /></label>
      {error && <div id="login-error" className="notice notice-bad" role="alert">{error}</div>}
      <button className="btn btn-primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
    </form>
  );
}

export default function LoginPage() {
  return (
    <div className="login">
      <section className="card">
        <h1>Card Connect Offers</h1>
        <p className="sub">Sign in to the recommendation dashboard.</p>
        <Suspense fallback={null}><LoginForm /></Suspense>
      </section>
    </div>
  );
}
