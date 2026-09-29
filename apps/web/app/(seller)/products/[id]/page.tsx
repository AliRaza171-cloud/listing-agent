"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { BackIcon, CloseIcon, MicIcon, SparkIcon, StopIcon, TrashIcon } from "@/components/Icons";
import StatusBadge from "@/components/StatusBadge";
import { usePolling, useRecorder } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import {
  ApiError, deleteProduct, generateListing, getProduct, listStores, mediaUrl, parseCommand, publishProduct,
  transcribe, updateListing, updateProduct, type CommandResult, type Listing, type Product, type Store,
} from "@/lib/api";
import { displayStatus, finalPrice, PLATFORM_NAMES, productTitle, rs } from "@/lib/format";

const TITLE_LIMIT = 70;
const errText = (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : fallback);

type Draft = { title: string; highlights: string[]; description: string; tags: string[]; category: string };
const toDraft = (l: Listing): Draft => ({
  title: l.title, highlights: [...l.highlights], description: l.description, tags: [...l.tags],
  category: l.category_suggestion ?? "",
});
const sameDraft = (a: Draft, b: Draft) => JSON.stringify(a) === JSON.stringify(b);

export default function WorkspacePage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { refreshCredits } = useSession();

  const [product, setProduct] = useState<Product | null>(null);
  const [stores, setStores] = useState<Store[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "error" | "ok"; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setProduct(await getProduct(id));
      setLoadError(null);
    } catch (e) {
      setLoadError(errText(e, "Couldn't load this product."));
    }
  }, [id]);

  useEffect(() => {
    load();
    listStores().then(setStores).catch(() => {});
    const err = new URLSearchParams(window.location.search).get("error");
    if (err) {
      setNotice({ kind: "error", text: err });
      window.history.replaceState(null, "", window.location.pathname);
    }
  }, [load]);

  const inProgress = !!product && (product.status === "generating"
    || product.publications.some((x) => x.status === "publishing"));
  usePolling(load, inProgress ? 2500 : null);

  // refresh the credit count when a generation finishes (a failure refunds the credit)
  const [wasGenerating, setWasGenerating] = useState(false);
  useEffect(() => {
    if (!product) return;
    if (product.status === "generating") setWasGenerating(true);
    else if (wasGenerating) {
      setWasGenerating(false);
      refreshCredits();
    }
  }, [product, wasGenerating, refreshCredits]);

  if (loadError && !product) {
    return (
      <div className="empty">
        <h2>Can’t open this product</h2>
        <p className="muted">{loadError}</p>
        <Link href="/products" className="btn btn-ghost">Back to products</Link>
      </div>
    );
  }
  if (!product) {
    return <div className="generating-panel"><span className="spinner" /></div>;
  }

  async function remove() {
    if (!product || !window.confirm("Delete this product and its listings? This can’t be undone.")) return;
    try {
      await deleteProduct(product.id);
      router.push("/products");
    } catch (e) {
      setNotice({ kind: "error", text: errText(e, "Couldn't delete the product.") });
    }
  }

  const status = displayStatus(product);
  const current = product.listings;
  const category = current.find((l) => l.category_suggestion)?.category_suggestion;
  const version = current.reduce((v, l) => Math.max(v, l.version), 0);

  return (
    <>
      <div className="ws-head">
        <div className="ws-title">
          <Link href="/products" className="back" aria-label="Back to products"><BackIcon /></Link>
          <div style={{ minWidth: 0 }}>
            <h1>{productTitle(product)}</h1>
            <span className="small muted">
              {[category, version ? `version ${version}` : null].filter(Boolean).join(" · ") || "New product"}
            </span>
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <StatusBadge status={status} />
          <button type="button" className="btn btn-danger btn-sm" onClick={remove} disabled={product.status === "generating"}
            aria-label="Delete product"><TrashIcon size={16} /></button>
        </div>
      </div>

      {notice && (
        <div className={`alert alert-${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}
          style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
          <span>{notice.text}</span>
          <button type="button" aria-label="Dismiss" onClick={() => setNotice(null)}
            style={{ border: "none", background: "none", color: "inherit", cursor: "pointer" }}><CloseIcon size={16} /></button>
        </div>
      )}

      <div className="ws">
        <div className="col">
          <Photos product={product} />
          <Detected product={product} onSaved={setProduct} onNotice={setNotice} />
        </div>

        <section className="card" style={{ minHeight: 420 }}>
          <ListingEditor product={product} stores={stores} onSaved={setProduct} onNotice={setNotice}
            onRegenerated={() => { refreshCredits(); load(); }} />
        </section>

        <div className="col">
          <VoiceCard product={product} stores={stores} onSaved={setProduct} onPublished={load} onNotice={setNotice} />
          <DetailsCard product={product} onSaved={setProduct} onNotice={setNotice} />
          <ResearchCard product={product} />
          <PublishCard product={product} stores={stores} onPublished={load} onNotice={setNotice} />
        </div>
      </div>
    </>
  );
}

type Notice = (n: { kind: "error" | "ok"; text: string } | null) => void;

// ---------------------------------------------------------------- photos

function Photos({ product }: { product: Product }) {
  const [active, setActive] = useState(0);
  const src = product.images[active] ?? product.images[0];
  return (
    <section className="card">
      {src ? <img className="cover-img" src={mediaUrl(src)} alt="Product photo" />
        : <div className="cover-img small">No photos</div>}
      {product.images.length > 1 && (
        <div className="thumbs">
          {product.images.map((url, i) => (
            <button key={url} type="button" aria-pressed={i === active} aria-label={`Show photo ${i + 1}`}
              onClick={() => setActive(i)}><img src={mediaUrl(url)} alt="" /></button>
          ))}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- what the agent saw / asked

function Detected({ product, onSaved, onNotice }: { product: Product; onSaved: (p: Product) => void; onNotice: Notice }) {
  const d = product.detected;
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const notes = product.seller_notes ?? "";

  if (!d) return null;
  const seen = [
    d.product_type, d.brand, d.model,
    ...Object.values(d.attributes ?? {}),
    ...(d.features ?? []),
  ].filter((x): x is string => !!x);
  const questions = d.questions_for_seller ?? [];
  const answered = (q: string) => notes.includes(`${q} `);
  const pending = Object.entries(answers).filter(([, a]) => a.trim());

  async function saveAnswers() {
    setSaving(true);
    try {
      const lines = pending.map(([q, a]) => `${q} ${a.trim()}`);
      const next = [notes.trim(), ...lines].filter(Boolean).join("\n");
      onSaved(await updateProduct(product.id, { seller_notes: next }));
      setAnswers({});
      onNotice({ kind: "ok", text: "Saved. Regenerate the listing to use your answers." });
    } catch (e) {
      onNotice({ kind: "error", text: errText(e, "Couldn't save your answers.") });
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      {seen.length > 0 && (
        <section className="card">
          <h2>What the agent saw</h2>
          <div className="chips">{seen.map((s) => <span key={s} className="chip">{s}</span>)}</div>
        </section>
      )}
      {questions.length > 0 && (
        <section className="card">
          <h2>The agent asked</h2>
          {questions.map((q) => {
            const line = notes.split("\n").find((l) => l.startsWith(`${q} `));
            return (
              <div className="qa" key={q}>
                <label htmlFor={`q-${q}`} style={{ fontWeight: 500 }}>{q}</label>
                {answered(q) && line ? <span style={{ color: "var(--ok)", fontWeight: 600 }}>✓ {line.slice(q.length + 1)}</span>
                  : <input id={`q-${q}`} className="input" placeholder="Type your answer"
                    value={answers[q] ?? ""} onChange={(e) => setAnswers({ ...answers, [q]: e.target.value })} />}
              </div>
            );
          })}
          {pending.length > 0 && (
            <button type="button" className="btn btn-dark btn-sm" onClick={saveAnswers} disabled={saving}>
              {saving && <span className="spinner" />}Save answers
            </button>
          )}
        </section>
      )}
    </>
  );
}

// ---------------------------------------------------------------- listing editor

function ListingEditor({ product, stores, onSaved, onNotice, onRegenerated }: {
  product: Product; stores: Store[]; onSaved: (p: Product) => void; onNotice: Notice; onRegenerated: () => void;
}) {
  const languages = useMemo(
    () => (["en", "ur"] as const).filter((l) => product.listings.some((x) => x.language === l)),
    [product.listings],
  );
  const [lang, setLang] = useState<"en" | "ur">("en");
  const [platform, setPlatform] = useState<string>("generic");
  const [regenerating, setRegenerating] = useState(false);

  useEffect(() => {
    if (languages.length && !languages.includes(lang)) setLang(languages[0]);
  }, [languages, lang]);

  const forLang = product.listings.filter((l) => l.language === lang);
  const listing = forLang.find((l) => (l.platform ?? "generic") === platform) ?? forLang[0];

  async function regenerate() {
    const ok = product.listings.length === 0
      || window.confirm("Write a fresh listing? This uses 1 credit and replaces the current text (your price and stock stay).");
    if (!ok) return;
    setRegenerating(true);
    try {
      const langs = languages.length ? [...languages] : ["en", "ur"];
      const platforms = Array.from(new Set(product.listings.map((l) => l.platform).filter((p): p is NonNullable<typeof p> => !!p)));
      await generateListing(product.id, {
        languages: langs, platforms, research: product.listings.length === 0 || (!!product.research && product.research.mode !== "skipped"),
        store_connection_ids: stores.filter((s) => s.status === "active" && (platforms as string[]).includes(s.platform)).map((s) => s.id),
      });
      onNotice(null);
      onRegenerated();
    } catch (e) {
      onNotice({ kind: "error", text: errText(e, "Couldn't start the listing.") });
    } finally {
      setRegenerating(false);
    }
  }

  if (product.status === "generating") {
    return (
      <div className="generating-panel">
        <span className="spinner" />
        <h2 className="display" style={{ fontSize: 22 }}>Writing your listing…</h2>
        <p className="muted small">Looking at the photos, reading your notes{product.listings.length ? "" : " and drafting both languages"}. This page updates by itself.</p>
      </div>
    );
  }

  if (!listing) {
    return (
      <div className="generating-panel">
        <h2 className="display" style={{ fontSize: 22 }}>
          {product.status === "failed" ? "The agent couldn’t write this one" : "No listing yet"}
        </h2>
        <p className="muted small">
          {product.status === "failed" ? (product.last_error || "Try again — your credit was returned.")
            : "Generate an English and Urdu listing from the photos and your notes."}
        </p>
        <button type="button" className="btn btn-primary" onClick={regenerate} disabled={regenerating}>
          {regenerating ? <span className="spinner" /> : <SparkIcon />}{product.status === "failed" ? "Try again" : "Generate listing"}
        </button>
        <span className="small muted">Uses 1 credit</span>
      </div>
    );
  }

  const platformsForLang = Array.from(new Set(forLang.map((l) => l.platform ?? "generic")));

  return (
    <>
      <div className="card-head" style={{ flexWrap: "wrap" }}>
        <div className="tabs" role="tablist" aria-label="Listing language">
          {languages.map((l) => (
            <button key={l} type="button" role="tab" aria-selected={lang === l} onClick={() => setLang(l)}
              className={l === "ur" ? "urdu" : undefined} style={l === "ur" ? { lineHeight: 1.5 } : undefined}>
              {l === "en" ? "English" : "اردو"}
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          {platformsForLang.length > 1 && (
            <label className="small" style={{ display: "flex", gap: 8, alignItems: "center", fontWeight: 600 }}>Format
              <select className="select" style={{ height: 36, width: "auto" }} value={listing.platform ?? "generic"}
                onChange={(e) => setPlatform(e.target.value)}>
                {platformsForLang.map((p) => <option key={p} value={p}>{p === "generic" ? "General" : PLATFORM_NAMES[p]}</option>)}
              </select>
            </label>
          )}
          <button type="button" className="btn btn-ghost btn-sm" onClick={regenerate} disabled={regenerating}>
            {regenerating ? <span className="spinner" /> : <SparkIcon size={14} />}Regenerate
          </button>
        </div>
      </div>
      <ListingForm key={`${listing.id}-${listing.version}`} product={product} listing={listing} onSaved={onSaved} onNotice={onNotice} />
    </>
  );
}

function ListingForm({ product, listing, onSaved, onNotice }: {
  product: Product; listing: Listing; onSaved: (p: Product) => void; onNotice: Notice;
}) {
  const original = useMemo(() => toDraft(listing), [listing]);
  const [draft, setDraft] = useState<Draft>(original);
  const [tagInput, setTagInput] = useState("");
  const [saving, setSaving] = useState(false);
  const rtl = listing.language === "ur";
  const dirty = !sameDraft(draft, original);

  function addTag() {
    const t = tagInput.trim().replace(/,$/, "");
    if (t && !draft.tags.includes(t)) setDraft({ ...draft, tags: [...draft.tags, t] });
    setTagInput("");
  }

  async function save() {
    if (!draft.title.trim() || !draft.description.trim()) {
      onNotice({ kind: "error", text: "Title and description can’t be empty." });
      return;
    }
    setSaving(true);
    try {
      onSaved(await updateListing(product.id, listing.id, {
        title: draft.title.trim(),
        description: draft.description.trim(),
        highlights: draft.highlights.map((h) => h.trim()).filter(Boolean),
        tags: draft.tags,
        category_suggestion: draft.category.trim() || null,
      }));
      onNotice({ kind: "ok", text: "Listing saved." });
    } catch (e) {
      onNotice({ kind: "error", text: errText(e, "Couldn't save the listing.") });
    } finally {
      setSaving(false);
    }
  }

  const L = rtl
    ? { title: "عنوان", highlights: "اہم خصوصیات", description: "تفصیل", tags: "سرچ ٹیگز" }
    : { title: "Title", highlights: "Highlights", description: "Description", tags: "Search tags" };

  return (
    <div className="editor" dir={rtl ? "rtl" : "ltr"} lang={listing.language}>
      <div className="field" dir="ltr" lang="en">
        <div className="label-row"><label htmlFor="l-category">Category in your store</label></div>
        <input id="l-category" className="input" value={draft.category} placeholder="e.g. Personal Care"
          onChange={(e) => setDraft({ ...draft, category: e.target.value })} maxLength={60} />
        <span className="small muted">
          {product.detected?.category_is_new && draft.category === original.category && draft.category
            ? "New category — none of your store’s categories fit, so it will be created when you publish."
            : "If this category doesn’t exist in your store yet, it’s created when you publish."}
        </span>
      </div>
      <div className="field">
        <div className="label-row">
          <label htmlFor="l-title" className={rtl ? "urdu" : undefined}>{L.title}</label>
          <span className={`count${draft.title.length > TITLE_LIMIT ? " over" : ""}`}>{draft.title.length} / {TITLE_LIMIT}</span>
        </div>
        <input id="l-title" className="input" value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
      </div>

      <div className="field">
        <div className="label-row"><span className={rtl ? "urdu" : undefined}>{L.highlights}</span></div>
        <div className="hl-list">
          {draft.highlights.map((h, i) => (
            <div className="hl-item" key={i}>
              <input className="input" value={h} aria-label={`Highlight ${i + 1}`}
                onChange={(e) => setDraft({ ...draft, highlights: draft.highlights.map((x, j) => (j === i ? e.target.value : x)) })} />
              <button type="button" className="btn btn-ghost btn-sm" aria-label={`Remove highlight ${i + 1}`}
                onClick={() => setDraft({ ...draft, highlights: draft.highlights.filter((_, j) => j !== i) })}><CloseIcon size={14} /></button>
            </div>
          ))}
          {draft.highlights.length < 12 && (
            <button type="button" className="link-btn" style={{ alignSelf: "flex-start", fontSize: 14 }}
              onClick={() => setDraft({ ...draft, highlights: [...draft.highlights, ""] })}>+ Add highlight</button>
          )}
        </div>
      </div>

      <div className="field">
        <div className="label-row"><label htmlFor="l-desc" className={rtl ? "urdu" : undefined}>{L.description}</label></div>
        <textarea id="l-desc" className="textarea" rows={7} value={draft.description}
          onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
      </div>

      <div className="field">
        <div className="label-row"><span className={rtl ? "urdu" : undefined}>{L.tags}</span></div>
        <div className="tag-edit">
          {draft.tags.map((t) => (
            <span key={t} className="chip">{t}
              <button type="button" aria-label={`Remove tag ${t}`}
                onClick={() => setDraft({ ...draft, tags: draft.tags.filter((x) => x !== t) })}><CloseIcon size={12} /></button>
            </span>
          ))}
          <input value={tagInput} placeholder={rtl ? "ٹیگ شامل کریں" : "+ Add tag"} aria-label="Add a tag"
            onChange={(e) => setTagInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addTag(); } }}
            onBlur={addTag} />
        </div>
      </div>

      {dirty && (
        <div style={{ display: "flex", gap: 10 }} dir="ltr">
          <button type="button" className="btn btn-dark" onClick={save} disabled={saving}>
            {saving && <span className="spinner" />}Save changes
          </button>
          <button type="button" className="btn btn-ghost" onClick={() => setDraft(original)} disabled={saving}>Discard</button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- voice commands

function describe(r: CommandResult, p: Product, stores: Store[]): string | null {
  const parts: string[] = [];
  if (r.price !== null) parts.push(`price ${rs(r.price)}`);
  if (r.remove_discount) parts.push("no discount");
  else if (r.discount_pct !== null) {
    const base = r.price ?? p.price;
    parts.push(`${r.discount_pct}% off${base !== null ? ` (${rs(base * (1 - r.discount_pct / 100))})` : ""}`);
  }
  if (r.stock !== null) parts.push(`stock ${r.stock}`);
  if (r.sku) parts.push(`SKU ${r.sku}`);
  if (r.free_shipping !== null) parts.push(r.free_shipping ? "free shipping" : "no free shipping");
  const targets = publishTargets(r, stores);
  const pub = targets.length
    ? `publish to ${targets.map((s) => s.name).join(", ")} as ${(r.publish_mode ?? "draft") === "live" ? "live" : "a draft"}`
      + (r.publish_language === "ur" ? " (Urdu)" : r.publish_language === "en" ? " (English)" : "")
    : null;
  if (parts.length && pub) return `Set ${parts.join(", ")} and ${pub}?`;
  if (parts.length) return `Set ${parts.join(", ")}?`;
  if (pub) return `${pub[0].toUpperCase()}${pub.slice(1)}?`;
  return null;
}

/** The connected stores a publish command points at (only active ones). */
function publishTargets(r: CommandResult, stores: Store[]): Store[] {
  if (!r.publish) return [];
  return stores.filter((s) => s.status === "active" && r.publish_to.includes(s.id));
}

function VoiceCard({ product, stores, onSaved, onPublished, onNotice }: {
  product: Product; stores: Store[]; onSaved: (p: Product) => void; onPublished: () => void; onNotice: Notice;
}) {
  const [heard, setHeard] = useState<string | null>(null);
  const [result, setResult] = useState<CommandResult | null>(null);
  const [typed, setTyped] = useState("");
  const [working, setWorking] = useState(false);

  async function understand(text: string) {
    setHeard(text);
    setResult(null);
    setWorking(true);
    try {
      setResult(await parseCommand(text, stores.filter((s) => s.status === "active")));
    } catch (e) {
      onNotice({ kind: "error", text: errText(e, "Couldn't understand that.") });
      setHeard(null);
    } finally {
      setWorking(false);
    }
  }

  const recorder = useRecorder(async (audio) => {
    setWorking(true);
    try {
      const { text } = await transcribe(audio);
      await understand(text);
    } catch (e) {
      onNotice({ kind: "error", text: errText(e, "Couldn't hear that — try again or type it.") });
      setWorking(false);
    }
  });

  async function confirm() {
    if (!result) return;
    const patch: Parameters<typeof updateProduct>[1] = {};
    if (result.price !== null) patch.price = result.price;
    if (result.remove_discount) patch.discount_pct = null;
    else if (result.discount_pct !== null) patch.discount_pct = result.discount_pct;
    if (result.stock !== null) patch.stock = result.stock;
    if (result.sku) patch.sku = result.sku;
    if (result.free_shipping !== null) patch.free_shipping = result.free_shipping;
    const targets = publishTargets(result, stores);
    setWorking(true);
    try {
      let current = product;
      if (Object.keys(patch).length) {
        current = await updateProduct(product.id, patch);
        onSaved(current);
      }
      if (targets.length) {
        // Same rules as the Publish card.
        const langs = (["en", "ur"] as const).filter((l) => current.listings.some((x) => x.language === l));
        const lang = result.publish_language && langs.includes(result.publish_language) ? result.publish_language : langs[0];
        if (current.status !== "ready" || !lang) {
          onNotice({ kind: "error", text: "Generate the listing first, then publish." });
        } else if (current.price === null) {
          onNotice({ kind: "error", text: "Set a price first — e.g. say “price 2500 and publish”." });
        } else {
          const mode = result.publish_mode ?? "draft";
          await publishProduct(current.id, targets.map((s) => s.id), mode, lang);
          onNotice({ kind: "ok", text: `Sending to ${targets.map((s) => s.name).join(", ")} as ${mode === "draft" ? "a draft" : "live"}…` });
          onPublished();
        }
      } else {
        onNotice({ kind: "ok", text: "Details saved." });
      }
      setHeard(null);
      setResult(null);
    } catch (e) {
      onNotice({ kind: "error", text: errText(e, targets.length ? "Couldn't publish." : "Couldn't save those details.") });
    } finally {
      setWorking(false);
    }
  }

  const summary = result ? describe(result, product, stores) : null;
  const unknownStore = !!result && result.publish && !publishTargets(result, stores).length;

  return (
    <section className="card heard">
      <div className="kicker"><MicIcon size={16} style={{ color: "var(--flame)" }} />
        {heard ? "I heard" : "Say or type price, stock — or “publish”"}
      </div>
      {heard && <div className="said">“{heard}”</div>}
      {working && !recorder.recording && <span className="spinner" style={{ color: "var(--side-text)" }} />}
      {result && (
        <div className="confirm">{summary ?? (unknownStore
          ? "I couldn’t tell which store you meant — say its name, or “all stores”."
          : "I didn’t catch any values — try “price 2500, 10 percent off, stock 20” or “publish to Shopify”.")}</div>
      )}
      {result && summary && (
        <div style={{ display: "flex", gap: 10 }}>
          <button type="button" className="btn btn-primary btn-sm" onClick={confirm} disabled={working}>Confirm</button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setTyped(heard ?? ""); setHeard(null); setResult(null); }}>Fix it</button>
        </div>
      )}
      {!result && !working && (
        <form className="cmd-row" onSubmit={(e) => { e.preventDefault(); if (typed.trim()) { understand(typed.trim()); setTyped(""); } }}>
          <input className="input" style={{ background: "var(--side-item)", border: "1px solid #4A463E", color: "var(--bg)" }}
            placeholder="price 2500, stock 20, publish to Shopify" aria-label="Type a command"
            value={typed} onChange={(e) => setTyped(e.target.value)} />
          <button type="button" className={`icon-btn${recorder.recording ? " recording" : ""}`}
            style={{ background: recorder.recording ? undefined : "var(--flame)" }}
            aria-label={recorder.recording ? "Stop recording" : "Speak a command"}
            onClick={() => (recorder.recording ? recorder.stop() : recorder.start())}>
            {recorder.recording ? <StopIcon size={16} /> : <MicIcon />}
          </button>
        </form>
      )}
      {recorder.recording && <span className="small" style={{ color: "#FCA5A5" }}>Listening… {recorder.seconds}s — tap to stop</span>}
      {recorder.error && <span className="small" style={{ color: "#FCA5A5" }}>{recorder.error}</span>}
    </section>
  );
}

// ---------------------------------------------------------------- seller details

function DetailsCard({ product, onSaved, onNotice }: { product: Product; onSaved: (p: Product) => void; onNotice: Notice }) {
  const fromProduct = useCallback((p: Product) => ({
    price: p.price?.toString() ?? "", discount: p.discount_pct?.toString() ?? "",
    stock: p.stock?.toString() ?? "", sku: p.sku ?? "", free: p.free_shipping,
    weight: p.weight_kg?.toString() ?? "", size: p.length_cm && p.width_cm && p.height_cm ? `${p.length_cm} x ${p.width_cm} x ${p.height_cm}` : "",
  }), []);
  const [form, setForm] = useState(() => fromProduct(product));
  const [saving, setSaving] = useState(false);
  const key = `${product.price}|${product.discount_pct}|${product.stock}|${product.sku}|${product.free_shipping}|${product.weight_kg}|${product.length_cm}|${product.width_cm}|${product.height_cm}`;
  useEffect(() => setForm(fromProduct(product)), [key]); // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = JSON.stringify(form) !== JSON.stringify(fromProduct(product));

  async function save() {
    const price = form.price.trim() ? Number(form.price) : null;
    const discount = form.discount.trim() ? Number(form.discount) : null;
    const stock = form.stock.trim() ? Number(form.stock) : null;
    if (price !== null && (!Number.isFinite(price) || price <= 0)) return onNotice({ kind: "error", text: "Price must be more than 0." });
    if (discount !== null && (!Number.isInteger(discount) || discount < 1 || discount > 95)) return onNotice({ kind: "error", text: "Discount must be a whole number from 1 to 95." });
    if (stock !== null && (!Number.isInteger(stock) || stock < 0)) return onNotice({ kind: "error", text: "Stock must be a whole number." });
    const weight = form.weight.trim() ? Number(form.weight) : null;
    if (weight !== null && (!Number.isFinite(weight) || weight <= 0 || weight > 500)) return onNotice({ kind: "error", text: "Weight must be in kg, e.g. 0.8" });
    let dims: number[] | null = null;
    if (form.size.trim()) {
      dims = form.size.split(/[x×*,\s]+/i).filter(Boolean).map(Number);
      if (dims.length !== 3 || dims.some((d) => !Number.isFinite(d) || d <= 0 || d > 1000))
        return onNotice({ kind: "error", text: "Size must be length x width x height in cm, e.g. 30 x 20 x 10" });
    }
    setSaving(true);
    try {
      onSaved(await updateProduct(product.id, {
        price, discount_pct: discount, stock, sku: form.sku.trim() || null, free_shipping: form.free,
        weight_kg: weight, length_cm: dims ? dims[0] : null, width_cm: dims ? dims[1] : null, height_cm: dims ? dims[2] : null,
      }));
      onNotice({ kind: "ok", text: "Details saved." });
    } catch (e) {
      onNotice({ kind: "error", text: errText(e, "Couldn't save the details.") });
    } finally {
      setSaving(false);
    }
  }

  const final = finalPrice(product);
  return (
    <section className="card">
      <div className="card-head">
        <h2>Your details</h2>
        {final !== null && product.discount_pct ? <span className="small muted">Sells at <b style={{ color: "var(--ink)" }}>{rs(final)}</b></span> : null}
      </div>
      <div className="grid-2">
        <label className="field">Price (Rs.)<input className="input" inputMode="decimal" value={form.price} onChange={(e) => setForm({ ...form, price: e.target.value })} /></label>
        <label className="field">Discount %<input className="input" inputMode="numeric" value={form.discount} onChange={(e) => setForm({ ...form, discount: e.target.value })} /></label>
        <label className="field">Stock<input className="input" inputMode="numeric" value={form.stock} onChange={(e) => setForm({ ...form, stock: e.target.value })} /></label>
        <label className="field">SKU<input className="input" placeholder="Optional" value={form.sku} onChange={(e) => setForm({ ...form, sku: e.target.value })} /></label>
        <label className="field">Package weight (kg)<input className="input" inputMode="decimal" placeholder="e.g. 0.8" value={form.weight} onChange={(e) => setForm({ ...form, weight: e.target.value })} /></label>
        <label className="field">Package size (cm)<input className="input" placeholder="L x W x H" value={form.size} onChange={(e) => setForm({ ...form, size: e.target.value })} /></label>
      </div>
      <label className="check"><input type="checkbox" checked={form.free} onChange={(e) => setForm({ ...form, free: e.target.checked })} />Free shipping</label>
      {dirty && (
        <button type="button" className="btn btn-dark btn-sm" onClick={save} disabled={saving}>
          {saving && <span className="spinner" />}Save details
        </button>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- research

function ResearchCard({ product }: { product: Product }) {
  const r = product.research;
  if (!r || r.mode === "skipped") return null;
  return (
    <section className="card">
      <h2>Research</h2>
      {r.price_range && (
        <p className="small">Similar items sell for about <b>{rs(r.price_range[0])} – {rs(r.price_range[1])}</b> — a suggestion only.</p>
      )}
      {r.buyer_priorities && r.buyer_priorities.length > 0 && (
        <p className="small">Buyers look for {r.buyer_priorities.join(", ")}.</p>
      )}
      {r.facts && Object.keys(r.facts).length > 0 && (
        <div className="chips">{Object.entries(r.facts).map(([k, v]) => <span key={k} className="chip">{k}: {v}</span>)}</div>
      )}
      {r.sources && r.sources.length > 0 && (
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
          {r.sources.map((s) => <li key={s.url}><a href={s.url} target="_blank" rel="noreferrer">{s.title}</a></li>)}
        </ul>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- publish

function PublishCard({ product, stores, onPublished, onNotice }: {
  product: Product; stores: Store[]; onPublished: () => void; onNotice: Notice;
}) {
  const active = stores.filter((s) => s.status === "active");
  const [picked, setPicked] = useState<string[] | null>(null);
  const [mode, setMode] = useState<"draft" | "live">("draft");
  const [language, setLanguage] = useState<"en" | "ur">("en");
  const [busy, setBusy] = useState(false);
  const chosen = picked ?? active.map((s) => s.id);
  const langs = (["en", "ur"] as const).filter((l) => product.listings.some((x) => x.language === l));
  const lang = langs.includes(language) ? language : langs[0];
  const storeName = (id: string) => stores.find((s) => s.id === id)?.name ?? "Store";

  async function publish() {
    if (!lang) return;
    setBusy(true);
    try {
      await publishProduct(product.id, chosen, mode, lang);
      onNotice({ kind: "ok", text: `Sending to ${chosen.length} store${chosen.length === 1 ? "" : "s"} as ${mode === "draft" ? "a draft" : "live"}…` });
      onPublished();
    } catch (e) {
      onNotice({ kind: "error", text: errText(e, "Couldn't publish.") });
    } finally {
      setBusy(false);
    }
  }

  const blocker = product.status !== "ready" ? "Generate the listing first."
    : product.price === null ? "Set a price first (say it or type it above)."
    : !active.length ? null
    : !chosen.length ? "Pick at least one store." : null;

  return (
    <section className="card">
      <h2>Publish to</h2>
      {product.publications.length > 0 && (
        <div className="pub-list">
          {product.publications.map((x) => (
            <div key={x.store_connection_id} className="pub-item">
              <span>{storeName(x.store_connection_id)}</span>
              {x.status === "published" ? (
                x.external_url && x.mode === "live"
                  ? <a href={x.external_url} target="_blank" rel="noreferrer" className="badge badge-published">Live ✓ View</a>
                  : <span className="badge badge-published" title={x.mode === "draft" ? "Hidden in the store until you switch it on there" : undefined}>
                      {x.mode === "draft" ? "Draft" : "Live"} ✓</span>
              ) : x.status === "failed" ? <span className="badge badge-attention" title={x.error ?? undefined}>Failed</span>
                : <span className="badge badge-generating">Sending…</span>}
            </div>
          ))}
          {product.publications.filter((x) => x.status === "failed" && x.error).map((x) => (
            <span key={`e-${x.store_connection_id}`} className="small" style={{ color: "var(--bad)" }}>{storeName(x.store_connection_id)}: {x.error}</span>
          ))}
        </div>
      )}

      {!active.length ? (
        <p className="small muted">No stores connected yet. <Link href="/stores">Connect a store</Link> to publish.</p>
      ) : (
        <>
          {active.map((s) => (
            <label key={s.id} className="check">
              <input type="checkbox" checked={chosen.includes(s.id)}
                onChange={(e) => setPicked(e.target.checked ? [...chosen, s.id] : chosen.filter((x) => x !== s.id))} />
              {PLATFORM_NAMES[s.platform]} — {s.name}
            </label>
          ))}
          {langs.length > 1 && (
            <fieldset>
              <legend>Language</legend>
              <label className="check"><input type="radio" name="lang" checked={lang === "en"} onChange={() => setLanguage("en")} />English</label>
              <label className="check"><input type="radio" name="lang" checked={lang === "ur"} onChange={() => setLanguage("ur")} />Urdu</label>
            </fieldset>
          )}
          <fieldset>
            <legend>Arrive in the store as</legend>
            <label className="check"><input type="radio" name="mode" checked={mode === "draft"} onChange={() => setMode("draft")} />Draft</label>
            <label className="check"><input type="radio" name="mode" checked={mode === "live"} onChange={() => setMode("live")} />Live</label>
          </fieldset>
          <button type="button" className="btn btn-primary btn-block" onClick={publish} disabled={busy || !!blocker}>
            {busy && <span className="spinner" />}Publish to {chosen.length} store{chosen.length === 1 ? "" : "s"}
          </button>
          {blocker && <span className="small muted">{blocker}</span>}
        </>
      )}
    </section>
  );
}
