"use client";

import { useEffect, useState, type FormEvent } from "react";
import { ApiError, connectStore, disconnectStore, listStores, type Store } from "@/lib/api";
import { PLATFORM_NAMES } from "@/lib/format";

type Platform = Store["platform"];

const FORMS: Record<Platform, {
  intro: string;
  urlLabel: string;
  urlPlaceholder: string;
  fields: { key: string; label: string; placeholder: string; secret?: boolean; optional?: boolean }[];
}> = {
  shopify: {
    intro: "In Shopify admin, create a custom app with the scopes write_products, write_inventory, read_locations and write_publications, install it, and copy its Admin API access token.",
    urlLabel: "Store address",
    urlPlaceholder: "https://yourstore.myshopify.com",
    fields: [{ key: "access_token", label: "Admin API access token", placeholder: "shpat_…", secret: true }],
  },
  woocommerce: {
    intro: "In WordPress: WooCommerce → Settings → Advanced → REST API → Add key with Read/Write access. For photos, also add an application password (Users → Profile → Application Passwords).",
    urlLabel: "Store URL",
    urlPlaceholder: "https://yourstore.com",
    fields: [
      { key: "consumer_key", label: "Consumer key", placeholder: "ck_…" },
      { key: "consumer_secret", label: "Consumer secret", placeholder: "cs_…", secret: true },
      { key: "wp_username", label: "WordPress username (for photos)", placeholder: "Optional", optional: true },
      { key: "wp_app_password", label: "Application password (for photos)", placeholder: "xxxx xxxx xxxx xxxx xxxx xxxx", secret: true, optional: true },
    ],
  },
  custom: {
    intro: "For Smart Click or any site with the Listing API. Use the store’s backend (API) address, not its website address. Running Listing Agent on your PC? Your local Smart Click is http://localhost:8000",
    urlLabel: "Store API URL",
    urlPlaceholder: "https://your-store-backend.onrender.com",
    fields: [{ key: "api_key", label: "API key", placeholder: "LISTING_API_KEY from the store’s .env", secret: true }],
  },
};

export default function StoresPage() {
  const [stores, setStores] = useState<Store[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      setStores(await listStores());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't load your stores.");
    }
  }
  useEffect(() => { load(); }, []);

  async function disconnect(s: Store) {
    if (!window.confirm(`Disconnect ${s.name}? Its saved keys are deleted. Products already in the store stay there.`)) return;
    try {
      await disconnectStore(s.id);
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't disconnect.");
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Stores</h1>
          <p>Connect once, then publish any product to these stores in one click.</p>
        </div>
      </div>

      {error && <div className="alert alert-error" role="alert">{error}</div>}

      <section className="table">
        <div className="store-row head"><span>Store</span><span>Platform</span><span>Status</span><span /></div>
        {stores === null && <div className="store-row"><span className="skeleton" style={{ height: 18, width: "50%" }} /></div>}
        {stores?.length === 0 && <div className="empty"><p className="muted">No stores connected yet — add one below.</p></div>}
        {stores?.map((s) => (
          <div key={s.id} className="store-row">
            <span className="prod-text">
              <b>{s.name}</b>
              <span style={s.status !== "active" ? { color: "var(--bad)" } : undefined}>{s.status !== "active" && s.last_error ? s.last_error : s.store_url}</span>
            </span>
            <span className="cell-strong">{PLATFORM_NAMES[s.platform]}</span>
            <span><span className={`badge ${s.status === "active" ? "badge-published" : "badge-attention"}`}>
              {s.status === "active" ? "Connected" : "Needs attention"}</span></span>
            <span><button type="button" className="btn btn-danger btn-sm" onClick={() => disconnect(s)}>Disconnect</button></span>
          </div>
        ))}
      </section>

      <h2 className="display" style={{ fontWeight: 800, fontSize: 24, marginTop: 8 }}>Connect a store</h2>
      <div className="connect-grid">
        {(Object.keys(FORMS) as Platform[]).map((p) => <ConnectForm key={p} platform={p} onConnected={load} />)}
      </div>
      <div className="note-dark">
        Keys are encrypted as soon as you save them and are never shown again — not even to you.
      </div>
    </>
  );
}

function ConnectForm({ platform, onConnected }: { platform: Platform; onConnected: () => void }) {
  const cfg = FORMS[platform];
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [creds, setCreds] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "ok"; text: string } | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setMsg(null);
    let storeUrl = url.trim();
    if (storeUrl && !/^https?:\/\//i.test(storeUrl)) storeUrl = `https://${storeUrl}`;
    try {
      new URL(storeUrl);
    } catch {
      setMsg({ kind: "error", text: "That doesn't look like a web address." });
      return;
    }
    setBusy(true);
    try {
      await connectStore({
        platform,
        name: name.trim() || new URL(storeUrl).hostname,
        store_url: storeUrl,
        credentials: Object.fromEntries(cfg.fields
          .map((f) => [f.key, (creds[f.key] ?? "").trim()] as const)
          .filter(([, v]) => v !== "")),
      });
      setName(""); setUrl(""); setCreds({});
      setMsg({ kind: "ok", text: "Connected." });
      onConnected();
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Couldn't connect." });
    } finally {
      setBusy(false);
    }
  }

  const complete = url.trim() && cfg.fields.every((f) => f.optional || (creds[f.key] ?? "").trim());

  return (
    <form className="card" onSubmit={submit}>
      <div className="display">{PLATFORM_NAMES[platform]}</div>
      <p className="small muted">{cfg.intro}</p>
      <label className="field">Name (shown in the app)
        <input className="input" value={name} placeholder="e.g. Home Kitchen Co." onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field">{cfg.urlLabel}
        <input className="input" value={url} placeholder={cfg.urlPlaceholder} onChange={(e) => setUrl(e.target.value)} inputMode="url" />
      </label>
      {cfg.fields.map((f) => (
        <label key={f.key} className="field">{f.label}
          <input className="input" type={f.secret ? "password" : "text"} autoComplete="off" placeholder={f.placeholder}
            value={creds[f.key] ?? ""} onChange={(e) => setCreds({ ...creds, [f.key]: e.target.value })} />
        </label>
      ))}
      {msg && <div className={`alert alert-${msg.kind}`} role={msg.kind === "error" ? "alert" : "status"}>{msg.text}</div>}
      <button className="btn btn-dark" disabled={busy || !complete}>{busy && <span className="spinner" />}Test &amp; connect</button>
    </form>
  );
}
