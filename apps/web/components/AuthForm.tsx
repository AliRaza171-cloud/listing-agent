"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { ApiError, getSession, login, register } from "@/lib/api";
import { guessCountry, MARKETS } from "@/lib/markets";
import { LogoMark } from "./Icons";

function nextPath(): string {
  const next = new URLSearchParams(window.location.search).get("next");
  // only same-site paths
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/products";
}

export default function AuthForm({ mode }: { mode: "login" | "signup" }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [country, setCountry] = useState("PK");
  useEffect(() => setCountry(guessCountry()), []);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Carry "where to go after signing in" across the Sign in / Create account links
  // (e.g. a merchant arriving from the Shopify App Store without an account yet).
  const [keepNext, setKeepNext] = useState("");
  const [fromShopify, setFromShopify] = useState(false);
  useEffect(() => {
    const next = new URLSearchParams(window.location.search).get("next");
    if (next) setKeepNext(`?next=${encodeURIComponent(next)}`);
    setFromShopify(Boolean(next?.includes("shopify_install=")));
  }, []);

  useEffect(() => {
    if (getSession()) router.replace(nextPath());
  }, [router]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (mode === "signup" && (password.length < 8 || !/[a-z]/i.test(password) || !/\d/.test(password))) {
      setError("Use at least 8 characters with letters and numbers.");
      return;
    }
    setBusy(true);
    try {
      if (mode === "signup") await register(email.trim(), password, name.trim(), country);
      else await login(email.trim(), password);
      router.replace(nextPath());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Try again.");
      setBusy(false);
    }
  }

  const isSignup = mode === "signup";
  return (
    <div className="auth-wrap">
      <aside className="auth-side">
        <Link href="/" className="brand" style={{ color: "var(--bg)" }}>
          <span className="brand-mark" style={{ background: "var(--bg)", color: "var(--ink)" }}><LogoMark /></span>
          <span className="brand-name">Listing Agent</span>
        </Link>
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <h2>Photo in.<br />Listing out.<br /><span style={{ color: "var(--flame)" }}>Published.</span></h2>
          <p>Your first 10 listings are free. In English (and Urdu), ready for Shopify, WooCommerce, eBay, Daraz or your own store.</p>
        </div>
        <span className="small" style={{ color: "var(--side-muted)" }}>© Listing Agent</span>
      </aside>

      <main className="auth-main">
        <form className="auth-form" onSubmit={submit} noValidate>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <h1>{isSignup ? "Create your account" : "Welcome back"}</h1>
            <p className="muted">
              {isSignup ? "10 free listings, no card needed." : "Sign in to your products and stores."}
            </p>
          </div>

          {fromShopify && (
            <div className="alert alert-info" role="status">
              {isSignup ? "Create your account to finish connecting your Shopify store."
                : "Sign in — or create an account below — to finish connecting your Shopify store."}
            </div>
          )}
          {error && <div className="alert alert-error" role="alert">{error}</div>}

          {isSignup && (
            <label className="field">Your name
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
            </label>
          )}
          {isSignup && (
            <label className="field">Where do you sell?
              <select className="input" value={country} onChange={(e) => setCountry(e.target.value)} name="country">
                {MARKETS.map((m) => <option key={m.code} value={m.code}>{m.name} ({m.currency})</option>)}
              </select>
              <span className="small muted" style={{ fontWeight: 400 }}>Your prices and the AI’s price research use this country’s currency.</span>
            </label>
          )}
          <label className="field">Email
            <input className="input" type="email" required value={email} onChange={(e) => setEmail(e.target.value)}
              autoComplete="email" inputMode="email" />
          </label>
          <label className="field">Password
            <input className="input" type="password" required value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={isSignup ? "new-password" : "current-password"} />
            {isSignup && <span className="small muted" style={{ fontWeight: 400 }}>At least 8 characters, letters and numbers.</span>}
          </label>

          <button className="btn btn-primary btn-lg btn-block" disabled={busy || !email || !password}>
            {busy && <span className="spinner" />}
            {isSignup ? "Create account" : "Sign in"}
          </button>
          {isSignup && (
            <p className="small muted" style={{ textAlign: "center" }}>
              By creating an account you agree to the <Link href="/terms">Terms</Link> and{" "}
              <Link href="/privacy">Privacy policy</Link>.
            </p>
          )}

          <p className="muted" style={{ textAlign: "center" }}>
            {isSignup ? <>Already have an account? <Link href={`/login${keepNext}`}>Sign in</Link></>
              : <>New here? <Link href={`/signup${keepNext}`}>Create an account</Link></>}
          </p>
        </form>
      </main>
    </div>
  );
}
