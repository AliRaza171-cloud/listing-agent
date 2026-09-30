"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { PlusIcon, SearchIcon } from "@/components/Icons";
import BatchReview from "@/components/BatchReview";
import StatusBadge from "@/components/StatusBadge";
import { usePolling } from "@/lib/hooks";
import { ApiError, listBatches, listProducts, listStores, mediaUrl, type Batch, type Product, type Store } from "@/lib/api";
import { displayStatus, finalPrice, PLATFORM_NAMES, productTitle, timeAgo, type Display } from "@/lib/format";
import { money } from "@/lib/markets";

const FILTERS: { key: "all" | Display["key"]; label: string }[] = [
  { key: "all", label: "All" },
  { key: "ready", label: "Ready" },
  { key: "generating", label: "In progress" },
  { key: "published", label: "Published" },
  { key: "attention", label: "Needs attention" },
];

export default function ProductsPage() {
  // useSearchParams needs a Suspense boundary in Next 15.
  return <Suspense fallback={null}><Products /></Suspense>;
}

function readNote(batchId: string): string | null {
  try {
    return window.sessionStorage.getItem(`batch-note-${batchId}`);
  } catch {
    return null;
  }
}

function Products() {
  const batchId = useSearchParams().get("batch");
  const [batch, setBatch] = useState<Batch | null>(null);
  const [batchNote, setBatchNote] = useState<string | null>(null);
  const [products, setProducts] = useState<Product[] | null>(null);
  const [stores, setStores] = useState<Store[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["key"]>("all");
  const [query, setQuery] = useState("");
  const [view, setView] = useState<"review" | "table">("review");

  async function load() {
    try {
      setProducts(await listProducts());
      if (batchId) listBatches().then((all) => setBatch(all.find((b) => b.id === batchId) ?? null)).catch(() => {});
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't load products.");
    }
  }

  useEffect(() => {
    load();
    listStores().then(setStores).catch(() => {});
    setBatch(null);
    setBatchNote(batchId ? readNote(batchId) : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchId]);

  const inScope = useMemo(
    () => (products ?? []).filter((p) => !batchId || p.batch_id === batchId), [products, batchId]);

  const busy = inScope.some((p) => displayStatus(p).key === "generating" || p.publications.some((x) => x.status === "publishing"));
  usePolling(load, busy ? 3000 : null);

  const counts = useMemo(() => {
    const c = { ready: 0, generating: 0, published: 0, attention: 0, draft: 0 };
    inScope.forEach((p) => { c[displayStatus(p).key] += 1; });
    return c;
  }, [inScope]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return inScope.filter((p) => {
      if (filter !== "all" && displayStatus(p).key !== filter) return false;
      if (!q) return true;
      return productTitle(p).toLowerCase().includes(q) || (p.seller_notes ?? "").toLowerCase().includes(q);
    });
  }, [inScope, filter, query]);

  const storeName = (id: string) => {
    const s = stores.find((x) => x.id === id);
    return s ? s.name : "Store";
  };

  return (
    <>
      <div className="page-head">
        <h1>Products</h1>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <label className="search">
            <SearchIcon size={16} />
            <input type="search" placeholder="Search products" aria-label="Search products"
              value={query} onChange={(e) => setQuery(e.target.value)} />
          </label>
          <Link href="/products/new" className="btn btn-primary"><PlusIcon size={16} />New listing</Link>
        </div>
      </div>

      {batchId && (
        <section className="card batch-banner" aria-label="Batch">
          <div className="batch-head">
            <strong>{batch ? batch.name : "Batch"}</strong>
            <Link href="/products" className="small">Show all products</Link>
          </div>
          {batch && batch.total > 0 && (
            <>
              <div className="bar" aria-hidden="true">
                <span style={{ width: `${Math.round(((batch.ready + batch.failed) / batch.total) * 100)}%` }} />
              </div>
              <span className="small muted">
                {batch.ready} of {batch.total} written
                {batch.generating ? ` · ${batch.generating} in progress` : ""}
                {batch.failed ? ` · ${batch.failed} failed` : ""}
                {batch.draft ? ` · ${batch.draft} not started` : ""}
              </span>
            </>
          )}
          {batchNote && <div className="alert alert-error" style={{ whiteSpace: "pre-line" }}>{batchNote}</div>}
          <div className="filters" role="group" aria-label="View">
            <button type="button" className="pill" aria-pressed={view === "review"} onClick={() => setView("review")}>Review all</button>
            <button type="button" className="pill" aria-pressed={view === "table"} onClick={() => setView("table")}>Table</button>
          </div>
        </section>
      )}

      {batchId && view === "review" && products && (
        <BatchReview products={inScope} stores={stores}
          onChanged={(p) => (p ? setProducts((all) => (all ?? []).map((x) => (x.id === p.id ? p : x))) : load())} />
      )}
      {!(batchId && view === "review") && (<>

      <div className="stats">
        <div className="stat"><span>Ready to publish</span><span className="n">{products ? counts.ready : "–"}</span></div>
        <div className="stat"><span>In progress</span><span className="n">{products ? counts.generating : "–"}</span></div>
        <div className="stat"><span>Published</span><span className="n">{products ? counts.published : "–"}</span></div>
        <div className="stat"><span>Needs attention</span>
          <span className={`n${counts.attention ? " bad" : ""}`}>{products ? counts.attention : "–"}</span></div>
      </div>

      <div className="filters" role="group" aria-label="Filter products">
        {FILTERS.map((f) => (
          <button key={f.key} type="button" className="pill" aria-pressed={filter === f.key} onClick={() => setFilter(f.key)}>
            {f.label}
          </button>
        ))}
      </div>

      {error && <div className="alert alert-error" role="alert">{error}</div>}

      <div className="table">
        <div className="row head"><span>Product</span><span>Status</span><span>Languages</span><span>Stores</span><span>Updated</span></div>

        {products === null && !error && [0, 1, 2].map((i) => (
          <div className="row" key={i}>
            <span className="prod"><span className="thumb skeleton" /><span className="skeleton" style={{ height: 16, width: "60%" }} /></span>
            <span className="skeleton" style={{ height: 14, width: 90 }} /><span /><span /><span />
          </div>
        ))}

        {products && products.length === 0 && (
          <div className="empty">
            <h2>No products yet</h2>
            <p className="muted">Add a few photos and the agent writes your first listing.</p>
            <Link href="/products/new" className="btn btn-primary"><PlusIcon size={16} />Create your first listing</Link>
          </div>
        )}

        {products && products.length > 0 && visible.length === 0 && (
          <div className="empty"><p className="muted">Nothing matches this filter.</p></div>
        )}

        {visible.map((p) => {
          const d = displayStatus(p);
          const langs = Array.from(new Set(p.listings.map((l) => l.language.toUpperCase())));
          const price = finalPrice(p);
          const failedPub = p.publications.find((x) => x.status === "failed");
          const sub = p.status === "failed" ? (p.last_error || "Try generating again.")
            : failedPub ? `${storeName(failedPub.store_connection_id)}: ${failedPub.error || "publish failed"}`
            : [p.listings[0]?.category_suggestion, price !== null ? money(price, p.currency) : null].filter(Boolean).join(" · ")
              || (p.detected?.questions_for_seller?.length ? `${p.detected.questions_for_seller.length} questions for you` : "");
          return (
            <Link key={p.id} href={`/products/${p.id}`} className={`row${d.key === "attention" ? " warn" : ""}`}>
              <span className="prod">
                {p.images[0] ? <img className="thumb" src={mediaUrl(p.images[0])} alt="" /> : <span className="thumb" />}
                <span className="prod-text">
                  <b>{productTitle(p)}</b>
                  {sub && <span style={d.key === "attention" ? { color: "var(--bad)" } : undefined}>{sub}</span>}
                </span>
              </span>
              <span><StatusBadge status={d} /></span>
              <span className="cell-strong">{langs.length ? langs.join(" · ") : "—"}</span>
              <span className="chips">
                {p.publications.length === 0 ? <span className="cell-sm">Not published yet</span>
                  : p.publications.map((x) => (
                    <span key={x.store_connection_id} className="chip" style={{ fontSize: 12, fontWeight: 600 }}>
                      {storeName(x.store_connection_id)} · {x.status === "published" ? x.mode : x.status}
                    </span>
                  ))}
              </span>
              <span className="cell-sm">{timeAgo(p.updated_at)}</span>
            </Link>
          );
        })}
      </div>
      </>)}
      {stores.length === 0 && products && products.length > 0 && (
        <p className="small muted">
          Connect a store to publish: <Link href="/stores">{Object.values(PLATFORM_NAMES).join(", ")}</Link>.
        </p>
      )}
    </>
  );
}
