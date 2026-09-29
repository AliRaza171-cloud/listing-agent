"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  isEmbedded, myMarket,
  type ConnectOptions,
  ApiError, confirmConnect, connectStore, detectStore, disconnectStore, getConnectOptions, getConnectRequest, listStores, startConnect, type Store,
} from "@/lib/api";
import { PLATFORM_NAMES } from "@/lib/format";

type Platform = Store["platform"];
type Field = { key: string; label: string; placeholder: string; secret?: boolean; optional?: boolean };

// Manual (API key) forms. For Shopify and WooCommerce these sit under "Advanced" — most sellers
// use the one-click Connect button instead.
type KeyPlatform = Exclude<Platform, "daraz" | "ebay">;   // Daraz and eBay connect only through their own login
const FORMS: Record<KeyPlatform, {
  intro: string; urlLabel: string; urlPlaceholder: string; fields: Field[];
  ready?: (creds: Record<string, string>) => boolean;
}> = {
  shopify: {
    intro: "In Shopify’s Dev Dashboard (dev.shopify.com), create an app with the scopes write_products, write_inventory, read_locations and write_publications, release it and install it on your store. Then paste its Client ID and Client secret here (from the app’s Settings). Have an older shpat_… token instead? Paste that and leave the other two empty.",
    urlLabel: "Store address",
    urlPlaceholder: "https://yourstore.myshopify.com",
    fields: [
      { key: "client_id", label: "Client ID", placeholder: "From the app’s Settings", optional: true },
      { key: "client_secret", label: "Client secret", placeholder: "From the app’s Settings", secret: true, optional: true },
      { key: "access_token", label: "…or Admin API access token", placeholder: "shpat_…", secret: true, optional: true },
    ],
    // Either a token, or both Client ID and secret.
    ready: (c) => Boolean(c.access_token || (c.client_id && c.client_secret)),
  },
  woocommerce: {
    intro: "In WordPress: WooCommerce → Settings → Advanced → REST API → Add key. Pick your admin user, Permissions: Read/Write, Generate — then copy both keys here. For photos, also add an Application password (Users → Profile → Application Passwords).",
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

type OneClick = "shopify" | "woocommerce" | "custom" | "daraz";
const ONE_CLICK: Record<OneClick, { intro: string; label: string; placeholder: string; suffix?: string; noInput?: boolean }> = {
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
  daraz: {
    intro: "Log in to your Daraz Seller Center and click Authorize — that’s it. Daraz checks new products (quality control) before they show in your shop.",
    label: "", placeholder: "", noInput: true,
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
  // Arrived from the Shopify App Store / Shopify admin (our App URL sends the shop here).
  const shopifyInstall = (params.get("shopify_install") || "").toLowerCase();
  const installStarted = useRef(false);

  const [stores, setStores] = useState<Store[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [oneClick, setOneClick] = useState<ConnectOptions | null>(null);
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
    getConnectOptions().then(setOneClick).catch(() => setOneClick({ shopify: false, woocommerce: false, custom: false, daraz: false, ebay: false }));
  }, [load]);

  // A link with ?shopify_install=<shop> (e.g. from our older Shopify App URL) suggests connecting that
  // shop. We ask first rather than connecting by ourselves: a link can come from anyone.
  const [offerShop, setOfferShop] = useState<string | null>(null);
  useEffect(() => {
    if (!shopifyInstall || installStarted.current || stores === null || oneClick === null) return;
    installStarted.current = true;
    if (!/^[a-z0-9][a-z0-9-]*\.myshopify\.com$/.test(shopifyInstall)) return;
    const already = stores.find((s) => s.platform === "shopify" && s.status === "active"
      && s.store_url.replace(/\/$/, "").toLowerCase() === `https://${shopifyInstall}`);
    if (already) {
      setResult({ kind: "ok", text: `${already.name} is connected. You can publish to it now.` });
      window.history.replaceState(null, "", "/stores");
      return;
    }
    if (!oneClick.shopify) {
      setResult({ kind: "error", text: "Shopify connections aren’t switched on yet. Please try again later." });
      return;
    }
    setOfferShop(shopifyInstall);
  }, [shopifyInstall, stores, oneClick]);

  async function connectOffered() {
    if (!offerShop) return;
    setResult({ kind: "wait", text: `Connecting ${offerShop}…` });
    try {
      const { authorize_url } = await startConnect("shopify", offerShop);
      window.location.href = authorize_url;
    } catch (e) {
      setResult({ kind: "error", text: e instanceof ApiError ? e.message : "Couldn't start the Shopify connection." });
    }
  }

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
    // Shopify: finish with the one-time code Shopify's return brought in the #fragment, then forget it.
    const confirm = new URLSearchParams(window.location.hash.slice(1)).get("confirm");
    if (confirm) window.history.replaceState(null, "", window.location.pathname + window.location.search);
    async function start() {
      if (confirm) {
        try {
          await confirmConnect(returning as string, confirm);
        } catch (e) {
          if (!stop) setResult({ kind: "error", text: e instanceof ApiError ? e.message : "The connection didn't go through. Please try again." });
          return;
        }
      }
      poll();
    }
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
    start();
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
      {offerShop && !result && (
        <div className="alert alert-info" role="status" style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <span>Connect your Shopify store <b>{offerShop}</b> to Listing Agent?</span>
          <button type="button" className="btn btn-primary btn-sm" onClick={connectOffered}>Connect {offerShop.split(".")[0]}</button>
          <button type="button" className="link-btn small" onClick={() => { setOfferShop(null); window.history.replaceState(null, "", "/stores"); }}>Not now</button>
        </div>
      )}
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
      <SmartConnect options={oneClick} />
      <p className="small muted" style={{ margin: "4px 0 0" }}>Or pick your platform:</p>
      <div className="connect-grid">
        {/* Inside the Shopify admin this shop is already connected, so no Shopify card there. */}
        {(["woocommerce", "shopify", "daraz", "custom"] as const).filter((p) => !(p === "shopify" && isEmbedded())).map((p) => (
          <OneClickCard key={p} platform={p} id={`connect-${p}`} available={oneClick ? Boolean(oneClick[p]) : null} onConnected={load}
            appStoreUrl={p === "shopify" ? oneClick?.shopify_app_store_url ?? null : null} />
        ))}
        <EbayCard available={oneClick ? Boolean(oneClick.ebay) : null} marketplaces={oneClick?.ebay_marketplaces ?? []} />
      </div>
      <div className="note-dark">
        Your store’s access keys are encrypted as soon as they reach us and are never shown again — not even to you.
        You can disconnect at any time.
      </div>
    </>
  );
}

/** One box for any store address: works out the platform and starts the right connection. */
function SmartConnect({ options }: { options: ConnectOptions | null }) {
  const [address, setAddress] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "info" | "error"; text: string } | null>(null);

  function goTo(card: string, text: string) {
    setMsg({ kind: "info", text });
    document.getElementById(`connect-${card}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  async function go(e: FormEvent) {
    e.preventDefault();
    setMsg(null);
    setBusy(true);
    try {
      const d = await detectStore(address.trim());
      const start = async (platform: "woocommerce" | "shopify" | "custom", store: string) => {
        setMsg({ kind: "info", text: `${d.name} found — taking you there to approve Listing Agent…` });
        const { authorize_url } = await startConnect(platform, store);
        window.location.href = authorize_url;
      };
      if (d.platform === "woocommerce" && options?.woocommerce) return await start("woocommerce", d.store);
      if (d.platform === "custom") return await start("custom", d.store);
      if (d.platform === "shopify") {
        if (isEmbedded()) { setMsg({ kind: "info", text: "You're already in Shopify — this store is connected." }); setBusy(false); return; }
        if (options?.shopify_app_store_url) {
          setMsg({ kind: "info", text: "Shopify store found. Install Listing Agent from the Shopify App Store — it connects by itself." });
          window.open(options.shopify_app_store_url, "_blank", "noopener"); setBusy(false); return;
        }
        if (options?.shopify && d.store) return await start("shopify", d.store);
        setBusy(false);
        return goTo("shopify", d.note || "This is a Shopify store — use the Shopify card below.");
      }
      setBusy(false);
      if (d.platform === "woocommerce") return goTo("woocommerce", "WooCommerce store found. Use the WooCommerce card below (API keys).");
      if (d.platform === "daraz") return goTo("daraz", d.note);
      if (d.platform === "ebay") return goTo("ebay", d.note);
      setMsg({ kind: "error", text: d.note || "Listing Agent can't connect to this store yet." });
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Couldn't check that address. Try again." });
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <form onSubmit={go} autoComplete="off" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div>
          <div className="display">Your store’s address</div>
          <p className="small muted" style={{ margin: "4px 0 0" }}>
            Type your shop’s website. Listing Agent checks what it runs on (WooCommerce, Shopify, Smart Click…) and connects it.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <input className="input" name="store-address" value={address} placeholder="yourstore.com" inputMode="url"
            onChange={(e) => setAddress(e.target.value)} style={{ flex: "1 1 240px" }} aria-label="Your store’s address" />
          <button className="btn btn-primary" disabled={busy || !address.trim() || options === null}>
            {busy && <span className="spinner" />}Connect
          </button>
        </div>
        {msg && <div className={`alert alert-${msg.kind}`} role={msg.kind === "error" ? "alert" : "status"}>{msg.text}</div>}
      </form>
    </section>
  );
}

function OneClickCard({ platform, available, onConnected, appStoreUrl = null, id }: {
  platform: OneClick; available: boolean | null; onConnected: () => void; appStoreUrl?: string | null; id?: string;
}) {
  const cfg = ONE_CLICK[platform];
  const [store, setStore] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const keyPlatform = platform === "daraz" ? null : platform;
  const showManual = keyPlatform !== null && (advanced || available === false);

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
    <section className="card" id={id}>
      <div className="display">{PLATFORM_NAMES[platform]}</div>
      {appStoreUrl && available !== false ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p className="small muted">Install Listing Agent from the Shopify App Store — it opens right inside your Shopify admin, already connected.</p>
          <a className="btn btn-primary" href={appStoreUrl} target="_blank" rel="noopener noreferrer">Get it on the Shopify App Store</a>
        </div>
      ) : available !== false && (
        <form onSubmit={go} autoComplete="off" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p className="small muted">{cfg.intro}</p>
          {!cfg.noInput && <label className="field">{cfg.label}
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input className="input" name={`${platform}-shop`} value={store} placeholder={cfg.placeholder} inputMode="url" autoComplete="off"
                onChange={(e) => setStore(e.target.value)} style={{ flex: 1 }} />
              {cfg.suffix && <span className="small muted">{cfg.suffix}</span>}
            </span>
          </label>}
          {msg && <div className="alert alert-error" role="alert">{msg}</div>}
          <button className="btn btn-primary" disabled={busy || (!cfg.noInput && !store.trim()) || available === null}>
            {busy && <span className="spinner" />}{platform === "custom" ? "Connect my store" : platform === "daraz" ? "Connect Daraz" : `Connect with ${PLATFORM_NAMES[platform]}`}
          </button>
        </form>
      )}
      {available === false && (keyPlatform ? (
        <p className="small muted">
          One-click connect for {PLATFORM_NAMES[platform]} works once Listing Agent is online (it needs a secure https address). For now, use API keys:
        </p>
      ) : (
        <p className="small muted">
          Daraz connect works once Listing Agent is online and registered with Daraz (open.daraz.com). Daraz has no API-key option.
        </p>
      ))}
      {available !== false && keyPlatform && (
        <button type="button" className="link-btn small" style={{ alignSelf: "flex-start" }} onClick={() => setAdvanced(!advanced)}>
          {advanced ? "Hide advanced" : "Advanced: connect with API keys"}
        </button>
      )}
      {showManual && keyPlatform && <ManualForm platform={keyPlatform} onConnected={onConnected} />}
    </section>
  );
}

function EbayCard({ available, marketplaces }: {
  available: boolean | null; marketplaces: { id: string; name: string; currency: string }[];
}) {
  const mine = myMarket();
  const [marketplace, setMarketplace] = useState("");
  const [city, setCity] = useState("");
  const [postal, setPostal] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // Start on the eBay site that sells in the seller's own currency, if there is one.
  const site = marketplace || marketplaces.find((m) => m.currency === mine.currency)?.id || marketplaces[0]?.id || "";
  const chosen = marketplaces.find((m) => m.id === site);

  async function go(e: FormEvent) {
    e.preventDefault();
    setMsg(null);
    setBusy(true);
    try {
      const { authorize_url } = await startConnect("ebay", "", undefined, { marketplace: site, city: city.trim(), postal_code: postal.trim() });
      window.location.href = authorize_url;
    } catch (err) {
      setMsg(err instanceof ApiError ? err.message : "Couldn't start the connection.");
      setBusy(false);
    }
  }

  return (
    <section className="card" id="connect-ebay">
      <div className="display">eBay</div>
      {available === false ? (
        <p className="small muted">
          eBay connect works once Listing Agent is online and registered with eBay (developer.ebay.com). eBay has no API-key option.
        </p>
      ) : (
        <form onSubmit={go} autoComplete="off" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p className="small muted">Pick your eBay site and where your items ship from, then log in to eBay and click Agree.</p>
          <label className="field">eBay site
            <select className="input" name="ebay-site" value={site} onChange={(e) => setMarketplace(e.target.value)}>
              {marketplaces.map((m) => <option key={m.id} value={m.id}>{m.name} ({m.currency})</option>)}
            </select>
          </label>
          {chosen && chosen.currency !== mine.currency && (
            <p className="small" style={{ color: "#B45309", margin: 0 }}>
              {chosen.name} sells in {chosen.currency}; your products are priced in {mine.currency}. Change your market in Settings to list there.
            </p>
          )}
          <div className="grid-2">
            <label className="field">City<input className="input" name="ebay-city" value={city} autoComplete="off"
              onChange={(e) => setCity(e.target.value)} placeholder="e.g. Manchester" /></label>
            <label className="field">Postal code<input className="input" name="ebay-postal" value={postal} autoComplete="off"
              onChange={(e) => setPostal(e.target.value)} placeholder="e.g. M1 1AA" /></label>
          </div>
          {msg && <div className="alert alert-error" role="alert">{msg}</div>}
          <button className="btn btn-primary" disabled={busy || available === null || !site || !city.trim() || !postal.trim()}>
            {busy && <span className="spinner" />}Connect eBay
          </button>
          <p className="small muted" style={{ margin: 0 }}>Before publishing, set up shipping and returns in eBay Seller Hub → Business policies.</p>
        </form>
      )}
    </section>
  );
}

function ManualForm({ platform, onConnected }: { platform: KeyPlatform; onConnected: () => void }) {
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

  const trimmed = Object.fromEntries(cfg.fields.map((f) => [f.key, (creds[f.key] ?? "").trim()]));
  const complete = url.trim() && cfg.fields.every((f) => f.optional || trimmed[f.key]) && (cfg.ready ? cfg.ready(trimmed) : true);

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
