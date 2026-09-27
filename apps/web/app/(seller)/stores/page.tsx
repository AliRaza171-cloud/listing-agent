"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState, type FormEvent } from "react";
import {
  ApiError, connectStore, disconnectStore, getConnectOptions, getConnectRequest, listStores, startConnect, type Store,
} from "@/lib/api";
import { PLATFORM_NAMES } from "@/lib/format";

type Platform = Store["platform"];
type Field = { key: string; label: string; placeholder: string; secret?: boolean; optional?: boolean };

// Manual (API key) forms. For Shopify and WooCommerce these sit under "Advanced" — most sellers
// use the one-click Connect button instead.
const FORMS: Record<Platform, { intro: string; urlLabel: string; urlPlaceholder: string; fields: Field[] }> = {
  shopify: {
    intro: "Paste an Admin API access token from a custom app with the scopes write_products, write_inventory, read_locations and write_publications.",
    urlLabel: "Store address",
    urlPlaceholder: "https://yourstore.myshopify.com",
    fields: [{ key: "access_token", label: "Admin API access token", placeholder: "shpat_…", secret: true }],
  },
  woocommerce: {
    intro: "WooCommerce → Settings → Advanced → REST API → Add key with Read/Write access.",
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

type OneClick = "shopify" | "woocommerce" | "custom";
const ONE_CLICK: Record<OneClick, { intro: string; label: string; placeholder: string; suffix?: string }> = {
  woocommerce: {
    intro: "Enter your shop’s address. You’ll log in to your WordPress and click Approve — that’s it.",
    label: "Your shop’s address",
    placeholder: "yourstore.com",
  },
  shopify: {
    intro: "Enter your Shopify store name. You’ll log in to Shopify and click Install — that’s it.",
    label: "Your Shopify store name",
    placeholder: "yourstore",
    suffix: ".myshopify.com",
  },
  custom: {
    intro: "Smart Click and other sites that support Listing Agent. Enter your website’s address — you’ll log in as the store’s admin and click Approve.",
    label: "Your website’s address",
    placeholder: "yourstore.vercel.app",
  },
};

export default function StoresPage() {
  // useSearchParams needs a Suspense boundary in Next 15.
  return <Suspense fallback={null}><Stores /></Suspense>;
}

function Stores() {
  const params = useSearchParams();
  const returning = params.get("connect");
  const expired = params.get("connect_error") === "expired";
  const denied = params.get("denied") === "1";

  const [stores, setStores] = useState<Store[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [oneClick, setOneClick] = useState<{ shopify: boolean; woocommerce: boolean; custom?: boolean } | null>(null);
  const [result, setResult] = useState<{ kind: "ok" | "error" | "wait"; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setStores(await listStores());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't load your stores.");
    }
  }, []);

  useEffect(() => {
    load();
    getConnectOptions().then(setOneClick).catch(() => setOneClick({ shopify: false, woocommerce: false, custom: false }));
  }, [load]);

  // Back from Shopify / WordPress: wait for the connection to be confirmed.
  useEffect(() => {
    if (expired) {
      setResult({ kind: "error", text: "That connection link expired. Please click Connect again." });
      return;
    }
    if (!returning) return;
    if (denied) {
      setResult({ kind: "error", text: "You cancelled the connection. Nothing was connected." });
      return;
    }
    let stop = false;
    let tries = 0;
    setResult({ kind: "wait", text: "Finishing the connection…" });
    async function poll() {
      try {
        const r = await getConnectRequest(returning as string);
        if (stop) return;
        if (r.status === "connected") {
          setResult({ kind: "ok", text: `${r.name} is connected. You can publish to it now.` });
          load();
          return;
        }
        if (r.status === "failed") {
          setResult({ kind: "error", text: r.error || "The connection didn't go through. Please try again." });
          return;
        }
      } catch {
        /* keep trying */
      }
      if (++tries >= 15) {
        setResult({ kind: "error", text: "The store didn’t confirm the connection. If you clicked Approve, check the list above in a minute; otherwise try again." });
        return;
      }
      window.setTimeout(() => { if (!stop) poll(); }, 1500);
    }
    poll();
    return () => { stop = true; };
  }, [returning, expired, denied, load]);

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
      {result && (
        <div className={`alert alert-${result.kind === "wait" ? "info" : result.kind}`} role={result.kind === "error" ? "alert" : "status"}
          style={result.kind === "wait" ? { display: "flex", gap: 10, alignItems: "center" } : undefined}>
          {result.kind === "wait" && <span className="spinner" />}{result.text}
        </div>
      )}

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
        {(["woocommerce", "shopify", "custom"] as const).map((p) => (
          <OneClickCard key={p} platform={p} available={oneClick ? Boolean(oneClick[p]) : null} onConnected={load} />
        ))}
      </div>
      <div className="note-dark">
        Your store’s access keys are encrypted as soon as they reach us and are never shown again — not even to you.
        You can disconnect at any time.
      </div>
    </>
  );
}

function OneClickCard({ platform, available, onConnected }: {
  platform: OneClick; available: boolean | null; onConnected: () => void;
}) {
  const cfg = ONE_CLICK[platform];
  const [store, setStore] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const showManual = advanced || available === false;

  async function go(e: FormEvent) {
    e.preventDefault();
    setMsg(null);
    setBusy(true);
    try {
      const { authorize_url } = await startConnect(platform, store.trim());
      window.location.href = authorize_url;
    } catch (err) {
      setMsg(err instanceof ApiError ? err.message : "Couldn't start the connection.");
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <div className="display">{PLATFORM_NAMES[platform]}</div>
      {available !== false && (
        <form onSubmit={go} autoComplete="off" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p className="small muted">{cfg.intro}</p>
          <label className="field">{cfg.label}
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input className="input" name={`${platform}-shop`} value={store} placeholder={cfg.placeholder} inputMode="url" autoComplete="off"
                onChange={(e) => setStore(e.target.value)} style={{ flex: 1 }} />
              {cfg.suffix && <span className="small muted">{cfg.suffix}</span>}
            </span>
          </label>
          {msg && <div className="alert alert-error" role="alert">{msg}</div>}
          <button className="btn btn-primary" disabled={busy || !store.trim() || available === null}>
            {busy && <span className="spinner" />}{platform === "custom" ? "Connect my store" : `Connect with ${PLATFORM_NAMES[platform]}`}
          </button>
        </form>
      )}
      {available === false && (
        <p className="small muted">One-click connect for {PLATFORM_NAMES[platform]} isn’t set up on this server yet — use API keys:</p>
      )}
      {available !== false && (
        <button type="button" className="link-btn small" style={{ alignSelf: "flex-start" }} onClick={() => setAdvanced(!advanced)}>
          {advanced ? "Hide advanced" : "Advanced: connect with API keys"}
        </button>
      )}
      {showManual && <ManualForm platform={platform} onConnected={onConnected} />}
    </section>
  );
}

function ManualForm({ platform, onConnected }: { platform: Platform; onConnected: () => void }) {
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
    <form onSubmit={submit} autoComplete="off" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <p className="small muted">{cfg.intro}</p>
      <label className="field">Name (shown in the app)
        <input className="input" name={`${platform}-store-name`} autoComplete="off" value={name} placeholder="e.g. Home Kitchen Co."
          onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field">{cfg.urlLabel}
        <input className="input" name={`${platform}-store-url`} autoComplete="off" value={url} placeholder={cfg.urlPlaceholder}
          onChange={(e) => setUrl(e.target.value)} inputMode="url" />
      </label>
      {cfg.fields.map((f) => (
        <label key={f.key} className="field">{f.label}
          <input className="input" name={`${platform}-${f.key}`} type={f.secret ? "password" : "text"}
            autoComplete={f.secret ? "new-password" : "off"} data-lpignore="true" data-1p-ignore placeholder={f.placeholder}
            value={creds[f.key] ?? ""} onChange={(e) => setCreds({ ...creds, [f.key]: e.target.value })} />
        </label>
      ))}
      {msg && <div className={`alert alert-${msg.kind}`} role={msg.kind === "error" ? "alert" : "status"}>{msg.text}</div>}
      <button className="btn btn-dark" disabled={busy || !complete}>{busy && <span className="spinner" />}Test &amp; connect</button>
    </form>
  );
}
