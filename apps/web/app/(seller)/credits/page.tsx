"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import {
  ApiError, embeddedShop, getCredits, getPacks, getPayment, isEmbedded, startCheckout,
  type Credits, type Pack, type PacksInfo, type Payment, type PayProvider,
} from "@/lib/api";
import { useSession } from "@/lib/session";
import { parseUtc, REASON_NAMES } from "@/lib/format";

export default function CreditsPage() {
  // useSearchParams needs a Suspense boundary in Next 15.
  return <Suspense fallback={null}><CreditsInner /></Suspense>;
}

function money(amount: number, currency: string): string {
  if (currency === "PKR") return `Rs ${Math.round(amount).toLocaleString("en-US")}`;
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(amount);
  } catch {
    return `${amount} ${currency}`;
  }
}

type Provider = PayProvider;

function CreditsInner() {
  const params = useSearchParams();
  const paymentId = params.get("payment");
  const cancelled = params.get("cancelled") === "1";
  const returnFailed = params.get("failed") === "1";

  const { refreshCredits, user } = useSession();
  // Inside the Shopify admin, merchants pay through Shopify (their Shopify bill) — Shopify's rule.
  const inShopify = isEmbedded();
  const inPakistan = !inShopify && (user.country || "PK") === "PK";   // Safepay (JazzCash, EasyPaisa) is for Pakistan
  const [data, setData] = useState<Credits | null>(null);
  const [shop, setShop] = useState<PacksInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // `${pack}:${provider}` while redirecting
  const [payment, setPayment] = useState<Payment | null>(null);
  const [gaveUp, setGaveUp] = useState(false);

  const loadCredits = useCallback(() => {
    getCredits()
      .then((c) => { setData(c); refreshCredits(); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load credits."));
  }, [refreshCredits]);

  useEffect(() => {
    loadCredits();
    getPacks().then(setShop).catch(() => setShop({ packs: [], providers: { safepay: false, stripe: false }, stripe_currency: "USD" }));
  }, [loadCredits]);

  // Back from a checkout page: wait for the payment to be confirmed (webhook, signed redirect,
  // or billing asking Stripe), then show the new balance.
  useEffect(() => {
    if (!paymentId || cancelled) return;
    let stop = false;
    let tries = 0;
    async function poll() {
      try {
        const p = await getPayment(paymentId as string);
        if (stop) return;
        setPayment(p);
        if (p.status === "paid") { loadCredits(); return; }
        if (p.status === "failed") return;
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) { setGaveUp(true); return; }
      }
      if (++tries >= 20) { setGaveUp(true); return; }
      window.setTimeout(() => { if (!stop) poll(); }, 2000);
    }
    poll();
    return () => { stop = true; };
  }, [paymentId, cancelled, loadCredits]);

  async function buy(pack: Pack, provider: Provider) {
    setError(null);
    setBusy(`${pack.id}:${provider}`);
    try {
      const { checkout_url } = await startCheckout(pack.id, provider, provider === "shopify" ? embeddedShop() : null);
      // Shopify's approval page has to open in the whole admin window, not inside our frame.
      if (provider === "shopify") window.open(checkout_url, "_top");
      else window.location.href = checkout_url;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't open the payment page.");
      setBusy(null);
    }
  }

  const providers = shop?.providers;
  const noProviders = shop !== null && (inShopify ? !providers?.shopify : !providers?.safepay && !providers?.stripe);
  const stripeCur = inShopify ? "USD" : shop?.stripe_currency ?? "USD";

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Credits</h1>
          <p>Each listing uses 1 credit. If the agent can’t write one, the credit comes back.</p>
        </div>
      </div>

      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {paymentId && cancelled && <div className="alert alert-info" role="status">Payment cancelled — nothing was charged.</div>}
      {!paymentId && returnFailed && (
        <div className="alert alert-error" role="alert">We couldn’t confirm that payment. If money was taken, contact support.</div>
      )}
      {paymentId && !cancelled && (
        payment?.status === "paid" ? (
          <div className="alert alert-ok" role="status"><strong>+{payment.credits} credits added.</strong> Thanks — you’re all set.</div>
        ) : payment?.status === "failed" ? (
          <div className="alert alert-error" role="alert">The payment didn’t go through{payment.error ? `: ${payment.error}` : "."}</div>
        ) : returnFailed || gaveUp ? (
          <div className="alert alert-error" role="alert">
            We couldn’t confirm this payment yet. If you paid, the credits will appear once the payment
            provider tells us — refresh in a few minutes. Reference: <code>{paymentId.slice(0, 8)}</code>
          </div>
        ) : (
          <div className="alert alert-info" role="status" style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <span className="spinner" /> Confirming your payment…
          </div>
        )
      )}

      <div className="stats" style={{ gridTemplateColumns: "minmax(0, 320px)" }}>
        <div className="stat"><span>Balance</span><span className="n">{data ? data.balance : "–"}</span></div>
      </div>

      <section className="card">
        <div className="card-head"><h2>Buy credits</h2></div>
        {noProviders && (
          <p className="small muted">
            {inShopify ? "Buying credits isn’t available yet — please check back soon."
              : <>Payments aren’t set up on this server yet. Add Safepay and/or Stripe keys to the backend’s <code>.env</code>.</>}
          </p>
        )}
        {shop && shop.packs.length === 0 && !noProviders && <p className="small muted">No packs on sale right now.</p>}
        <div className="packs">
          {shop === null && [0, 1, 2].map((i) => <div key={i} className="pack skeleton" style={{ height: 190 }} />)}
          {shop?.packs.map((pack, i) => {
            const pkr = pack.prices.PKR;
            const other = pack.prices[stripeCur];
            const perCredit = pkr ? pkr / pack.credits : null;
            return (
              <div key={pack.id} className="pack">
                <span className="pack-name">{pack.name}{i === 1 && shop.packs.length > 2 && <span className="pack-tag">Most popular</span>}</span>
                <span className="pack-credits">{pack.credits.toLocaleString("en-US")} <span>credits</span></span>
                <span className="pack-price">
                  {pkr !== undefined && inPakistan ? money(pkr, "PKR") : other !== undefined ? money(other, stripeCur) : pkr !== undefined ? money(pkr, "PKR") : "—"}
                  {inPakistan && pkr !== undefined && other !== undefined && stripeCur !== "PKR" && (
                    <span className="small muted"> · {money(other, stripeCur)}</span>
                  )}
                </span>
                {perCredit !== null && inPakistan && <span className="small muted">Rs {perCredit.toFixed(perCredit < 10 ? 1 : 0)} per listing</span>}
                <div className="pack-actions">
                  {inShopify && providers?.shopify && other !== undefined && (
                    <button type="button" className="btn btn-primary btn-block" disabled={busy !== null}
                      onClick={() => buy(pack, "shopify")}>
                      {busy === `${pack.id}:shopify` ? <span className="spinner" /> : null}
                      Buy with Shopify
                    </button>
                  )}
                  {!inShopify && providers?.safepay && inPakistan && pkr !== undefined && (
                    <button type="button" className="btn btn-primary btn-block" disabled={busy !== null}
                      onClick={() => buy(pack, "safepay")}>
                      {busy === `${pack.id}:safepay` ? <span className="spinner" /> : null}
                      JazzCash · EasyPaisa · Card
                    </button>
                  )}
                  {!inShopify && providers?.stripe && other !== undefined && (
                    <button type="button" className={`btn btn-block ${providers.safepay && inPakistan ? "btn-ghost" : "btn-primary"}`}
                      disabled={busy !== null} onClick={() => buy(pack, "stripe")}>
                      {busy === `${pack.id}:stripe` ? <span className="spinner" /> : null}
                      International card ({stripeCur})
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
        {inShopify && providers?.shopify && (
          <p className="small muted">Approve the purchase in Shopify — it’s added to your Shopify bill, in US dollars.</p>
        )}
        {!inShopify && (providers?.safepay || providers?.stripe) && (
          <p className="small muted">You’ll pay on {providers.safepay && providers.stripe ? "Safepay’s or Stripe’s" : providers.safepay ? "Safepay’s" : "Stripe’s"} secure page — we never see your card details.</p>
        )}
      </section>

      <section className="table">
        <div className="store-row head keep" style={{ gridTemplateColumns: "2fr 1fr 1fr" }}><span>What</span><span>Credits</span><span>When</span></div>
        {data?.history.length === 0 && <div className="empty"><p className="muted">No activity yet.</p></div>}
        {data?.history.map((h, i) => (
          <div key={i} className="store-row keep" style={{ gridTemplateColumns: "2fr 1fr 1fr" }}>
            <span>{REASON_NAMES[h.reason] ?? h.reason.replace(/_/g, " ")}</span>
            <span style={{ fontWeight: 600, color: h.delta > 0 ? "var(--ok)" : "var(--ink)" }}>{h.delta > 0 ? `+${h.delta}` : h.delta}</span>
            <span className="cell-sm">{parseUtc(h.at).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</span>
          </div>
        ))}
      </section>
    </>
  );
}
