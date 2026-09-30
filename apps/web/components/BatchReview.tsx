"use client";

/** A bulk batch, every product on one page: read and fix each listing, set prices, publish one or all. */
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import StatusBadge from "@/components/StatusBadge";
import { SparkIcon } from "@/components/Icons";
import {
  ApiError, mediaUrl, publishProduct, updateListing, updateProduct, type Listing, type Product, type Store,
} from "@/lib/api";
import { displayStatus, PLATFORM_NAMES } from "@/lib/format";
import { currencySymbol } from "@/lib/markets";

type Draft = {
  title?: string; highlights?: string; description?: string; category?: string;
  price?: string; discount?: string; stock?: string;
};
type Lang = "en" | "ur";

/** The listing that gets published: the general one for that language (else the first one). */
function mainListing(p: Product, lang: Lang): Listing | undefined {
  const all = p.listings.filter((l) => l.language === lang);
  return all.find((l) => l.platform === null) ?? all[0];
}

const errText = (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : fallback);
function num(s: string | undefined): number | null {
  const clean = (s ?? "").replace(/[^\d.]/g, "");
  const n = clean === "" ? NaN : Number(clean);
  return Number.isFinite(n) ? n : null;
}

export default function BatchReview({ products, stores, onChanged }: {
  products: Product[]; stores: Store[]; onChanged: (p?: Product) => void;
}) {
  const active = stores.filter((s) => s.status === "active");
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [busy, setBusy] = useState<Record<string, "saving" | "publishing" | undefined>>({});
  const [notes, setNotes] = useState<Record<string, { text: string; bad?: boolean }>>({});
  const [langChoice, setLang] = useState<Lang | null>(null);
  const [picked, setPicked] = useState<string[]>(() => active.map((s) => s.id));
  const [touched, setTouched] = useState(false);
  const activeKey = active.map((s) => s.id).join(",");
  useEffect(() => {   // stores arrive after the page: tick them all until the seller changes the choice
    if (!touched) setPicked(activeKey ? activeKey.split(",") : []);
  }, [activeKey, touched]);
  const [mode, setMode] = useState<"draft" | "live">("draft");
  const [bulk, setBulk] = useState<{ done: number; total: number } | null>(null);
  const [bulkNote, setBulkNote] = useState<{ text: string; bad?: boolean } | null>(null);
  const chosen = picked.filter((id) => active.some((s) => s.id === id));
  const languages = useMemo(() => Array.from(new Set(products.flatMap((p) => p.listings.map((l) => l.language)))), [products]);
  // English unless the batch was written only in Urdu (listings arrive while the page is open)
  const lang: Lang = langChoice ?? (languages.length && !languages.includes("en") ? (languages[0] as Lang) : "en");

  const edit = (id: string, patch: Draft) => setDrafts((d) => ({ ...d, [id]: { ...d[id], ...patch } }));
  const dirty = (id: string) => Object.keys(drafts[id] ?? {}).length > 0;
  const storeName = (id: string) => stores.find((s) => s.id === id)?.name ?? "Store";

  // Ready products that still need publishing to at least one chosen store.
  const needsPublishing = (p: Product) => p.status === "ready" && !!mainListing(p, lang)
    && chosen.some((sid) => !p.publications.some((x) => x.store_connection_id === sid && x.status !== "failed"));
  const priceOf = (p: Product) => (drafts[p.id]?.price !== undefined ? num(drafts[p.id].price) : p.price);
  const toPublish = products.filter((p) => needsPublishing(p) && priceOf(p));
  const missingPrice = products.filter((p) => needsPublishing(p) && !priceOf(p));

  /** Saves this product's edits. -> the saved product, or null when something failed (message shown). */
  async function save(p: Product): Promise<Product | null> {
    const d = drafts[p.id];
    if (!d) return p;
    const listing = mainListing(p, lang);
    let saved = p;
    try {
      const productPatch: Parameters<typeof updateProduct>[1] = {};
      if (d.price !== undefined) {
        const v = num(d.price);
        if (v !== null && !(v > 0)) throw new Error("price");
        productPatch.price = v;
      }
      if (d.discount !== undefined) productPatch.discount_pct = num(d.discount) || null;
      if (d.stock !== undefined) productPatch.stock = num(d.stock);
      if (Object.keys(productPatch).length) saved = await updateProduct(p.id, productPatch);
      if (listing && (d.title !== undefined || d.highlights !== undefined || d.description !== undefined || d.category !== undefined)) {
        const patch: Parameters<typeof updateListing>[2] = {};
        if (d.title !== undefined) patch.title = d.title.trim();
        if (d.description !== undefined) patch.description = d.description.trim();
        if (d.highlights !== undefined) patch.highlights = d.highlights.split("\n").map((x) => x.trim()).filter(Boolean);
        if (d.category !== undefined) patch.category_suggestion = d.category.trim();
        if (patch.title === "" || patch.description === "") throw new Error("empty");
        saved = await updateListing(p.id, listing.id, patch);
      }
      setDrafts((all) => { const { [p.id]: _gone, ...rest } = all; return rest; });
      onChanged(saved);
      return saved;
    } catch (e) {
      const text = e instanceof Error && e.message === "price" ? "Enter a price above 0."
        : e instanceof Error && e.message === "empty" ? "The title and description can't be empty."
        : errText(e, "Couldn't save — try again.");
      setNotes((n) => ({ ...n, [p.id]: { text, bad: true } }));
      return null;
    }
  }

  async function publishOne(p: Product, quiet = false): Promise<boolean> {
    if (!chosen.length) {
      setNotes((n) => ({ ...n, [p.id]: { text: "Tick at least one store at the top.", bad: true } }));
      return false;
    }
    setBusy((b) => ({ ...b, [p.id]: "publishing" }));
    try {
      const saved = await save(p);
      if (!saved) return false;
      if (!saved.price) {
        setNotes((n) => ({ ...n, [p.id]: { text: "Set a price first.", bad: true } }));
        return false;
      }
      await publishProduct(p.id, chosen, mode, lang);
      if (!quiet) setNotes((n) => ({ ...n, [p.id]: { text: `Publishing to ${chosen.map(storeName).join(", ")}…` } }));
      onChanged();
      return true;
    } catch (e) {
      setNotes((n) => ({ ...n, [p.id]: { text: errText(e, "Couldn't publish — try again."), bad: true } }));
      return false;
    } finally {
      setBusy((b) => ({ ...b, [p.id]: undefined }));
    }
  }

  async function publishAll() {
    const list = toPublish;
    setBulkNote(null);
    setBulk({ done: 0, total: list.length });
    let ok = 0;
    for (let i = 0; i < list.length; i++) {
      if (await publishOne(list[i], true)) ok++;
      setBulk({ done: i + 1, total: list.length });
    }
    setBulk(null);
    setBulkNote({
      text: `Sent ${ok} of ${list.length} product${list.length === 1 ? "" : "s"} to ${chosen.map(storeName).join(", ")}.`
        + (ok < list.length ? " See the messages on the products below." : ""),
      bad: ok < list.length,
    });
    onChanged();
  }

  async function saveOne(p: Product) {
    setBusy((b) => ({ ...b, [p.id]: "saving" }));
    const saved = await save(p);
    if (saved) setNotes((n) => ({ ...n, [p.id]: { text: "Saved." } }));
    setBusy((b) => ({ ...b, [p.id]: undefined }));
  }

  return (
    <div className="review">
      <section className="card review-bar" aria-label="Publish this batch">
        <div className="review-bar-row">
          <strong>Publish to</strong>
          {active.length === 0 && <span className="small muted">No store connected — <Link href="/stores">connect one</Link>.</span>}
          {active.map((s) => (
            <label key={s.id} className="check">
              <input type="checkbox" checked={picked.includes(s.id)}
                onChange={(e) => { setTouched(true); setPicked(e.target.checked ? [...picked, s.id] : picked.filter((x) => x !== s.id)); }} />
              {s.name} <span className="small muted">({PLATFORM_NAMES[s.platform]})</span>
            </label>
          ))}
        </div>
        <div className="review-bar-row">
          <span className="small muted">Arrive as</span>
          <label className="check"><input type="radio" name="review-mode" checked={mode === "draft"} onChange={() => setMode("draft")} />Draft</label>
          <label className="check"><input type="radio" name="review-mode" checked={mode === "live"} onChange={() => setMode("live")} />Live</label>
          {languages.length > 1 && (
            <>
              <span className="small muted" style={{ marginLeft: 8 }}>Language</span>
              {languages.map((l) => (
                <label key={l} className="check"><input type="radio" name="review-lang" checked={lang === l}
                  onChange={() => setLang(l as Lang)} />{l === "en" ? "English" : "Urdu"}</label>
              ))}
            </>
          )}
          <button type="button" className="btn btn-primary" style={{ marginLeft: "auto" }}
            disabled={!!bulk || !toPublish.length || !chosen.length} onClick={publishAll}>
            <SparkIcon size={16} />{bulk ? `Publishing ${bulk.done} of ${bulk.total}…` : `Publish ${toPublish.length || ""} ready product${toPublish.length === 1 ? "" : "s"}`}
          </button>
        </div>
        {missingPrice.length > 0 && (
          <span className="small" style={{ color: "var(--bad)" }}>
            {missingPrice.length} product{missingPrice.length === 1 ? " needs" : "s need"} a price before publishing — add it below.
          </span>
        )}
        {bulkNote && <span className="small" role="status" style={{ color: bulkNote.bad ? "var(--bad)" : "var(--ok)" }}>{bulkNote.text}</span>}
      </section>

      {products.map((p, idx) => {
        const d = drafts[p.id] ?? {};
        const listing = mainListing(p, lang);
        const status = displayStatus(p);
        const title = d.title ?? listing?.title ?? "";
        const sym = currencySymbol(p.currency);
        const note = notes[p.id];
        const isUr = lang === "ur";
        return (
          <article key={p.id} className="card review-card" aria-label={`Product ${idx + 1}`}>
            <div className="review-photos">
              {p.images[0] ? <img className="review-cover" src={mediaUrl(p.images[0])} alt="" /> : <span className="review-cover" />}
              {p.images.length > 1 && (
                <div className="review-thumbs">
                  {p.images.slice(1, 5).map((u) => <img key={u} src={mediaUrl(u)} alt="" />)}
                  {p.images.length > 5 && <span className="small muted">+{p.images.length - 5}</span>}
                </div>
              )}
            </div>

            <div className="review-main">
              <div className="review-head">
                <StatusBadge status={status} />
                <Link href={`/products/${p.id}`} className="small">Open full page</Link>
              </div>
              {p.status === "generating" || p.status === "draft" ? (
                <div className="review-wait"><span className="spinner" /> The agent is writing this listing…</div>
              ) : !listing ? (
                <p className="small" style={{ color: "var(--bad)" }}>{p.last_error || "No listing yet."} <Link href={`/products/${p.id}`}>Try again</Link></p>
              ) : (
                <>
                  <label className="field">
                    <span className="field-row">Title <span className="small muted">{title.length} / 70</span></span>
                    <input className={`input${isUr ? " urdu" : ""}`} dir={isUr ? "rtl" : undefined} value={title}
                      aria-label={`Title of product ${idx + 1}`} onChange={(e) => edit(p.id, { title: e.target.value })} />
                  </label>
                  <label className="field">Highlights <span className="small muted">— one per line</span>
                    <textarea className={`textarea${isUr ? " urdu" : ""}`} dir={isUr ? "rtl" : undefined} rows={4}
                      aria-label={`Highlights of product ${idx + 1}`}
                      value={d.highlights ?? listing.highlights.join("\n")} onChange={(e) => edit(p.id, { highlights: e.target.value })} />
                  </label>
                  <label className="field">Description
                    <textarea className={`textarea${isUr ? " urdu" : ""}`} dir={isUr ? "rtl" : undefined} rows={7}
                      aria-label={`Description of product ${idx + 1}`}
                      value={d.description ?? listing.description} onChange={(e) => edit(p.id, { description: e.target.value })} />
                  </label>
                </>
              )}
            </div>

            <div className="review-side">
              {listing && (
                <label className="field">Category
                  <input className="input" value={d.category ?? listing.category_suggestion ?? ""}
                    aria-label={`Category of product ${idx + 1}`} onChange={(e) => edit(p.id, { category: e.target.value })} />
                </label>
              )}
              <div className="review-nums">
                <label className="field">Price ({sym})
                  <input className="input" inputMode="decimal" value={d.price ?? (p.price ?? "")} placeholder="Required"
                    aria-label={`Price of product ${idx + 1}`} onChange={(e) => edit(p.id, { price: e.target.value })} />
                </label>
                <label className="field">Discount %
                  <input className="input" inputMode="numeric" value={d.discount ?? (p.discount_pct ?? "")}
                    aria-label={`Discount of product ${idx + 1}`} onChange={(e) => edit(p.id, { discount: e.target.value })} />
                </label>
                <label className="field">Stock
                  <input className="input" inputMode="numeric" value={d.stock ?? (p.stock ?? "")}
                    aria-label={`Stock of product ${idx + 1}`} onChange={(e) => edit(p.id, { stock: e.target.value })} />
                </label>
              </div>
              {p.publications.length > 0 && (
                <ul className="review-pubs">
                  {p.publications.map((x) => (
                    <li key={x.store_connection_id}>
                      <span>{storeName(x.store_connection_id)}</span>
                      <span className={`chip ${x.status === "failed" ? "chip-bad" : x.status === "published" ? "chip-ok" : ""}`}>
                        {x.status === "published" ? (x.mode === "live" ? "Live" : "Draft ✓") : x.status === "failed" ? "Failed" : "Publishing…"}
                      </span>
                      {x.status === "failed" && x.error && <span className="small" style={{ color: "var(--bad)" }}>{x.error}</span>}
                      {x.status === "published" && x.external_url && <a className="small" href={x.external_url} target="_blank" rel="noreferrer">View</a>}
                    </li>
                  ))}
                </ul>
              )}
              <div className="review-actions">
                <button type="button" className="btn btn-ghost btn-sm" disabled={!dirty(p.id) || !!busy[p.id]}
                  onClick={() => saveOne(p)}>{busy[p.id] === "saving" ? <span className="spinner" /> : null}Save changes</button>
                <button type="button" className="btn btn-primary btn-sm" disabled={p.status !== "ready" || !listing || !!busy[p.id] || !!bulk}
                  onClick={() => publishOne(p)}>{busy[p.id] === "publishing" ? <span className="spinner" /> : null}Publish</button>
              </div>
              {note && <span className="small" role="status" style={{ color: note.bad ? "var(--bad)" : "var(--ok)" }}>{note.text}</span>}
            </div>
          </article>
        );
      })}
    </div>
  );
}
