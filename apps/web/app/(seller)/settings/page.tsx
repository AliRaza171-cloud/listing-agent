"use client";

import { useState, type FormEvent } from "react";
import { ApiError, updateMe } from "@/lib/api";
import { CURRENCIES, marketOf, MARKETS } from "@/lib/markets";
import { useSession } from "@/lib/session";

export default function SettingsPage() {
  const { user } = useSession();
  const [name, setName] = useState(user.full_name ?? "");
  const [country, setCountry] = useState(user.country || "PK");
  const [currency, setCurrency] = useState(user.currency || marketOf(user.country).currency);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  function pickCountry(code: string) {
    setCountry(code);
    setCurrency(marketOf(code).currency);   // a new country starts with its own currency
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      await updateMe({ full_name: name.trim() || null, country, currency });
      setMsg({ kind: "ok", text: "Saved." });
      window.setTimeout(() => window.location.reload(), 600);   // the whole app picks up the new market
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Couldn't save." });
      setBusy(false);
    }
  }

  const changed = (user.country || "PK") !== country || (user.currency || "PKR") !== currency;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p>Your name and where you sell.</p>
        </div>
      </div>
      <section className="card" style={{ maxWidth: 620 }}>
        <form onSubmit={save} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {msg && <div className={`alert ${msg.kind === "ok" ? "alert-ok" : "alert-error"}`} role={msg.kind === "ok" ? "status" : "alert"}>{msg.text}</div>}
          <label className="field">Your name
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
          </label>
          <label className="field">Email
            <input className="input" value={user.email} disabled />
          </label>
          <div className="grid-2">
            <label className="field">Where you sell
              <select className="input" value={country} onChange={(e) => pickCountry(e.target.value)}>
                {MARKETS.map((m) => <option key={m.code} value={m.code}>{m.name}</option>)}
              </select>
            </label>
            <label className="field">Currency
              <select className="input" value={currency} onChange={(e) => setCurrency(e.target.value)}>
                {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
          </div>
          <p className="small muted">
            New products use this country and currency: the AI writes and checks prices for this market, and voice
            commands read prices in this currency. Products you already made keep their own currency.
          </p>
          {changed && (
            <p className="small" style={{ color: "var(--warn, #B45309)" }}>
              Tip: publish to stores that sell in the same currency (e.g. an eBay UK store for GBP).
            </p>
          )}
          <button className="btn btn-primary" style={{ alignSelf: "flex-start" }} disabled={busy}>
            {busy && <span className="spinner" />}Save
          </button>
        </form>
      </section>
    </>
  );
}
